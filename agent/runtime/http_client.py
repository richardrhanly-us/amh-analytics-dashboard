"""Canonical HTTP client (Continuous Ingestion Phase F).

One reusable requests.Session, shared by the uploader and heartbeat, with
response classification into agent.spool.FailureCategory -- the same
three-value enum spool.py already uses for retry/quarantine bookkeeping,
reused here rather than inventing a parallel classification.

TWO RETRY LAYERS, DELIBERATELY DIFFERENT TIMESCALES (documented per the
Phase F requirement not to stack retries into runaway delays):

  1. TRANSPORT layer (urllib3's Retry adapter, mounted on `session`
     below): handles brief network-level blips WITHIN one logical
     attempt -- a connection reset, one 5xx/429 that clears on an
     immediate retry. Bounded small on purpose: http_transport_retry_total
     defaults to 2, backoff_factor 0.5, so the worst case here is on the
     order of a few seconds (0.5 + 1.0 = 1.5s of urllib3-internal sleep
     for 2 retries) before this module's own post_json() even sees a
     response.
  2. APPLICATION layer (the uploader's own Backoff, seconds-to-minutes,
     applied BETWEEN separate calls to post_json across poll cycles):
     governs how soon to try again after post_json ultimately reports
     failure -- whether that failure came back after 0 transport retries
     (a connection error) or after 2 (a persistently-failing 503).

The two never compound into an unbounded wait: transport retries only
ever add a few bounded seconds to a single post_json() call; everything
longer than that is the application layer's job, which callers control
explicitly (see agent/runtime/uploader.py's Backoff).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from urllib3.util.retry import Retry

from ..spool import FailureCategory

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_AUTH_STATUS_CODES = {401, 403}
_PERMANENT_REJECTION_STATUS_CODES = {400, 413}

_MAX_ERROR_PREVIEW_CHARS = 300


def build_session(
    *, transport_retry_total: int = 2, transport_backoff_factor: float = 0.5
) -> requests.Session:
    retry_strategy = Retry(
        total=transport_retry_total,
        connect=transport_retry_total,
        read=transport_retry_total,
        status=transport_retry_total,
        backoff_factor=transport_backoff_factor,
        status_forcelist=tuple(_RETRYABLE_STATUS_CODES),
        allowed_methods=frozenset(["POST"]),
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=5, pool_maxsize=5, max_retries=retry_strategy
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@dataclass(frozen=True)
class HttpOutcome:
    success: bool
    category: FailureCategory | None
    status_code: int | None
    error: str | None
    body: dict[str, Any] | None


def _preview(text: str | None) -> str:
    text = text or ""
    if len(text) <= _MAX_ERROR_PREVIEW_CHARS:
        return text
    return text[:_MAX_ERROR_PREVIEW_CHARS] + "...<truncated>"


def post_json(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout: tuple[float, float],
) -> HttpOutcome:
    """POSTs `payload`, classifying the outcome. Never logs `headers` or
    the request body's content -- only status codes and response-body
    previews (the server's own text, never what we sent, so the bearer
    token can never leak through this path)."""
    try:
        response = session.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.Timeout as exc:
        return HttpOutcome(False, FailureCategory.RETRYABLE_INFRA, None, f"timeout: {exc}", None)
    except requests.ConnectionError as exc:
        return HttpOutcome(False, FailureCategory.RETRYABLE_INFRA, None, f"connection error: {exc}", None)
    except requests.RequestException as exc:
        return HttpOutcome(False, FailureCategory.RETRYABLE_INFRA, None, f"request failed: {exc}", None)

    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            return HttpOutcome(
                False, FailureCategory.RETRYABLE_INFRA, 200, "200 response was not valid JSON", None
            )
        if body.get("status") != "success":
            return HttpOutcome(
                False, FailureCategory.RETRYABLE_INFRA, 200,
                f"200 response had unexpected status field: {body.get('status')!r}", body,
            )
        return HttpOutcome(True, None, 200, None, body)

    if response.status_code in _AUTH_STATUS_CODES:
        return HttpOutcome(
            False, FailureCategory.AUTH_FAILURE, response.status_code,
            f"authentication/authorization failure {response.status_code}: {_preview(response.text)}", None,
        )

    if response.status_code in _PERMANENT_REJECTION_STATUS_CODES:
        return HttpOutcome(
            False, FailureCategory.PERMANENT_REJECTION, response.status_code,
            f"deterministic rejection {response.status_code}: {_preview(response.text)}", None,
        )

    if response.status_code in _RETRYABLE_STATUS_CODES:
        return HttpOutcome(
            False, FailureCategory.RETRYABLE_INFRA, response.status_code,
            f"retryable server error {response.status_code}: {_preview(response.text)}", None,
        )

    # Unrecognized status -- treat conservatively as retryable infra
    # rather than ever isolating/quarantining data over a status this
    # module doesn't specifically know is a deterministic data problem.
    return HttpOutcome(
        False, FailureCategory.RETRYABLE_INFRA, response.status_code,
        f"unexpected status {response.status_code}: {_preview(response.text)}", None,
    )
