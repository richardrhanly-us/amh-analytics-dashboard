"""Upload client (Phase 4a).

TWO RETRY LAYERS, deliberately not three (Phase 2 approved decision, not
an oversight):

  1. TRANSPORT layer (urllib3's Retry adapter, mounted on the shared
     requests.Session): a few bounded retries for a brief network-level
     blip WITHIN one logical attempt -- a connection reset, one 429/5xx
     that clears immediately.
  2. THE NEXT SCHEDULED RUN, 15 minutes later, or Task Scheduler's own
     confirmed-live RestartOnFailure policy (3 attempts, 5 minutes apart)
     for a failure that crashes the whole process. There is deliberately
     NO third, application-level exponential-backoff-across-cycles loop
     here (unlike the continuous agent's uploader) -- Task Scheduler
     already IS that mechanism for a one-shot batch script, confirmed
     configured in live production. Building a second one would be
     exactly the kind of unrequested persistent-agent-style complexity
     this phase is explicitly scoped to avoid.

BATCHING: records are split into batches of at most
cfg.max_records_per_batch, each batch's checkins/rejects/acs interleaved
into one POST /upload request -- the same interleaved-batch shape the
legacy pipeline has used in production for months, reimplemented fresh
(not imported -- see collector/__init__.py).

MULTI-BATCH STATE SEMANTICS: upload_records stops at the FIRST failed
batch. Any batches after it are simply never attempted this run. This
module has NO knowledge of collector/state.py's cursor -- it is the
caller's (collector/run.py's) responsibility to never persist state
unless every batch this call attempted succeeded. That "all batches
succeeded, or nothing is committed" rule is what makes a partial-failure
retry safe: the NEXT run re-reads and re-sends everything from the last
successfully COMMITTED offset, including whatever this module already
successfully delivered before the failure -- which the backend's existing
semantic-key `ON CONFLICT DO NOTHING` dedup absorbs as a safe no-op, not
a duplicate row.

SCOPE MISMATCH: main.py's authenticate_agent already distinguishes
"invalid token" (401), "inactive token" (403), and "token scope does not
match customer_id/branch_id" (403) with a specific message in each case.
This module classifies all of them as AUTH_FAILURE (the retry/backoff
behavior is identical either way -- retrying a scope mismatch is exactly
as futile as retrying an invalid token) but ALWAYS preserves the real
response body text in UploadOutcome.error, so a scope mismatch is
reported to the operator as exactly that, not swallowed into a generic
"auth failed" message that would send someone looking in the wrong place.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import requests
from urllib3.util.retry import Retry

from .config import CollectorConfig

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_AUTH_STATUS_CODES = {401, 403}
_PERMANENT_REJECTION_STATUS_CODES = {400, 413}
_MAX_ERROR_PREVIEW_CHARS = 500


class FailureCategory(str, Enum):
    RETRYABLE_INFRA = "retryable_infra"
    AUTH_FAILURE = "auth_failure"
    PERMANENT_REJECTION = "permanent_rejection"


@dataclass(frozen=True)
class UploadOutcome:
    success: bool
    category: FailureCategory | None
    status_code: int | None
    error: str | None
    body: dict[str, Any] | None


def build_session(*, transport_retry_total: int = 2, transport_backoff_factor: float = 0.5) -> requests.Session:
    """trust_env defaults to True (requests' own default) -- deliberately
    left alone so standard Windows/environment proxy configuration
    (HTTP_PROXY/HTTPS_PROXY env vars, or the WinINET per-user registry
    settings urllib picks up) is honored automatically. Whether that is
    SUFFICIENT for a SYSTEM-context Scheduled Task -- which does not
    share the interactive Operator account's per-user proxy config -- is
    explicitly NOT verified here; see the Phase 3 gap audit item covering
    preflight validation under the real Task Scheduler identity, which
    this module intentionally does not attempt to solve on its own."""
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
    adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _preview(text: str | None) -> str:
    text = text or ""
    if len(text) <= _MAX_ERROR_PREVIEW_CHARS:
        return text
    return text[:_MAX_ERROR_PREVIEW_CHARS] + "...<truncated>"


def _auth_headers(cfg: CollectorConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg.api_token}", "Content-Type": "application/json"}


def _post_json(
    session: requests.Session, url: str, payload: dict[str, Any], *, headers: dict[str, str], timeout: tuple[float, float]
) -> UploadOutcome:
    """Never logs `headers` or the request payload's content -- only
    status codes and response-body previews (the server's own text),
    so the bearer token can never leak through logs via this path."""
    try:
        response = session.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.Timeout as exc:
        return UploadOutcome(False, FailureCategory.RETRYABLE_INFRA, None, f"timeout: {exc}", None)
    except requests.ConnectionError as exc:
        return UploadOutcome(False, FailureCategory.RETRYABLE_INFRA, None, f"connection error: {exc}", None)
    except requests.RequestException as exc:
        return UploadOutcome(False, FailureCategory.RETRYABLE_INFRA, None, f"request failed: {exc}", None)

    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            return UploadOutcome(False, FailureCategory.RETRYABLE_INFRA, 200, "200 response was not valid JSON", None)
        if body.get("status") != "success":
            return UploadOutcome(
                False, FailureCategory.RETRYABLE_INFRA, 200,
                f"200 response had unexpected status field: {body.get('status')!r}", body,
            )
        return UploadOutcome(True, None, 200, None, body)

    if response.status_code in _AUTH_STATUS_CODES:
        # Preserves the real backend message (e.g. "Token scope does not
        # match customer_id / branch_id") -- never collapsed to a generic
        # "auth failed" string. See module docstring's SCOPE MISMATCH.
        return UploadOutcome(
            False, FailureCategory.AUTH_FAILURE, response.status_code,
            f"authentication/authorization failure {response.status_code}: {_preview(response.text)}", None,
        )

    if response.status_code in _PERMANENT_REJECTION_STATUS_CODES:
        return UploadOutcome(
            False, FailureCategory.PERMANENT_REJECTION, response.status_code,
            f"deterministic rejection {response.status_code}: {_preview(response.text)}", None,
        )

    if response.status_code in _RETRYABLE_STATUS_CODES:
        return UploadOutcome(
            False, FailureCategory.RETRYABLE_INFRA, response.status_code,
            f"retryable server error {response.status_code}: {_preview(response.text)}", None,
        )

    return UploadOutcome(
        False, FailureCategory.RETRYABLE_INFRA, response.status_code,
        f"unexpected status {response.status_code}: {_preview(response.text)}", None,
    )


# --- batching ---------------------------------------------------------


def build_batches(
    checkins: list[dict], rejects: list[dict], acs: list[dict], max_records_per_batch: int
) -> list[dict[str, list]]:
    """Splits the three record lists into interleaved batches, each
    containing at most max_records_per_batch records PER SOURCE -- same
    proven shape as the legacy pipeline's batching, reimplemented fresh.
    Returns [] if there is nothing to upload at all."""
    total = max(len(checkins), len(rejects), len(acs))
    if total == 0:
        return []

    batch_count = -(-total // max_records_per_batch)  # ceil division
    batches = []
    for i in range(batch_count):
        start = i * max_records_per_batch
        end = start + max_records_per_batch
        batch = {
            "checkins": checkins[start:end],
            "rejects": rejects[start:end],
            "acs": acs[start:end],
        }
        if batch["checkins"] or batch["rejects"] or batch["acs"]:
            batches.append(batch)
    return batches


@dataclass(frozen=True)
class UploadRunResult:
    success: bool
    batches_attempted: int
    batches_delivered: int
    failure: UploadOutcome | None
    last_response_body: dict[str, Any] | None = None


def upload_records(
    session: requests.Session, cfg: CollectorConfig, checkins: list[dict], rejects: list[dict], acs: list[dict]
) -> UploadRunResult:
    """Uploads everything in one or more batches. Stops at the first
    failed batch -- see module docstring's MULTI-BATCH STATE SEMANTICS.
    The caller (collector/run.py) decides what to do with the result;
    this function never touches state itself."""
    batches = build_batches(checkins, rejects, acs, cfg.max_records_per_batch)
    if not batches:
        return UploadRunResult(success=True, batches_attempted=0, batches_delivered=0, failure=None)

    url = f"{cfg.api_url}/upload"
    timeout = (cfg.http_connect_timeout, cfg.http_read_timeout)
    last_body: dict[str, Any] | None = None

    for i, batch in enumerate(batches):
        outcome = _post_json(session, url, batch, headers=_auth_headers(cfg), timeout=timeout)
        if not outcome.success:
            return UploadRunResult(
                success=False, batches_attempted=i + 1, batches_delivered=i, failure=outcome,
                last_response_body=last_body,
            )
        last_body = outcome.body

    return UploadRunResult(
        success=True, batches_attempted=len(batches), batches_delivered=len(batches), failure=None,
        last_response_body=last_body,
    )


def post_status(session: requests.Session, cfg: CollectorConfig, status: dict[str, Any]) -> UploadOutcome:
    """Best-effort: a failed status POST never fails the run (matches the
    legacy pipeline's proven behavior) -- the caller decides whether to
    log a warning, never whether to treat it as fatal."""
    url = f"{cfg.api_url}/upload-pipeline-status"
    timeout = (cfg.http_connect_timeout, cfg.http_read_timeout)
    payload = {**status, "customer_id": cfg.customer_id, "branch_id": cfg.branch_id}
    return _post_json(session, url, payload, headers=_auth_headers(cfg), timeout=timeout)
