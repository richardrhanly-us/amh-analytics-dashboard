"""Continuous outbox uploader for the SortView agent (Phase 2: reliable
delivery from the local SQLite outbox to the existing FastAPI /upload
endpoint).

Watcher and uploader are kept as separate components on purpose (same as
Phase 1): this module only ever reads pending rows out of the outbox,
POSTs them, and writes back delivery/retry/quarantine state. It has no
knowledge of the AMH log files or how events got into the outbox.

Delivery/acknowledgement contract: /upload processes an entire request
inside one Postgres transaction (main.py's `with engine.begin() as conn:`
wraps every insert for the whole payload), so there is no partial-success
outcome for a single POST -- either the whole submitted batch committed
(each row newly inserted, or already present and skipped by the existing
`ON CONFLICT DO NOTHING` constraints) and the endpoint returns
`{"status": "success", ...}` with HTTP 200, or nothing in that batch
committed. A batch is marked delivered on, and only on, that 200/"success"
response -- inserted counts are read for logging only, never used to
decide delivery, since a legitimate duplicate resend correctly produces
`..._inserted: 0` while still being a fully successful delivery.

Failure classification and bad-row isolation
---------------------------------------------
A single POST's response is classified into exactly one of:

  SUCCESS               200, status == "success" -> mark delivered.
  RETRYABLE_INFRA        connection errors, timeouts, 429, 5xx -> the
                          payload hasn't been proven bad, so every row in
                          the attempted set stays pending untouched;
                          normal exponential backoff applies.
  AUTH_FAILURE            401/403 -> agent-wide/configuration problem,
                          not a row-level one. Every row stays pending
                          untouched, no quarantine. Bounded backoff, same
                          as RETRYABLE_INFRA, but tracked as a distinct
                          category (see DrainResult.last_category) so a
                          future Phase 3 monitoring/heartbeat integration
                          can tell "backend is down" apart from "the
                          token is wrong" without re-deriving it from log
                          text.
  REQUEST_FAILURE         400 -> deterministic: something about this
                          specific set of rows is invalid. Isolated (see
                          below), never blindly blamed on every row in
                          the batch.
  PAYLOAD_TOO_LARGE       413 -> deterministic, isolated the same way as
                          REQUEST_FAILURE. The client already keeps
                          batches under a conservative byte budget (see
                          _fit_batch_to_byte_budget), so this is expected
                          to be rare -- reaching it at all means either
                          that budget is misconfigured or the server's
                          actual limit is lower than assumed. It's still
                          handled explicitly rather than assumed away.

Isolation algorithm for REQUEST_FAILURE/PAYLOAD_TOO_LARGE, applied only to
the rows already selected for the current attempt (never re-queries the
database mid-isolation, so this is always bounded by the batch size that
was already chosen):

  - more than one row in the failing candidate -> split it in half and
    retry each half independently and recursively
  - a sub-batch that succeeds is marked delivered -- healthy siblings are
    never penalized just because they shared an original batch with a bad
    row
  - a sub-batch that still fails keeps splitting
  - only when exactly one row reproduces the deterministic failure on its
    own does it get quarantined -- durably marked (quarantined_at,
    quarantine_reason), excluded from all future automatic selection,
    never deleted

Attempt-count semantics: attempt_count/last_attempt_at are only written
for (a) a whole attempted set on RETRYABLE_INFRA/AUTH_FAILURE (the entire
set was genuinely retried together and the outcome is equally uninformative
about every row in it, so recording it on all of them is honest), and
(b) the single row at the moment it's quarantined (attempt_count bumped
by exactly 1, reflecting the one definitive singleton attempt that proved
it's bad). Rows that are merely part of a larger batch being split during
isolation get no attempt_count write at all -- a batch of N being split
doesn't mean any individual row in it failed; writing failure metadata at
that point would reintroduce exactly the "innocent rows penalized" problem
this design replaces.

SQLite concurrency: batches are selected with a plain read (no
transaction -- outbox.connect() uses autocommit, so a bare SELECT holds no
lock), every HTTP call happens with no transaction open at all, and only
the write-backs (marking delivered, recording a failed attempt,
quarantining) open a short outbox.transaction() each. The database is
never locked while waiting on the network, including during isolation's
recursive retries.

Isolation request budget: the recursive splitting above is bounded in
*depth* by construction (each split at least halves the remaining rows),
but an all-poison batch would still cost up to ~2N-1 logical POSTs in the
worst case -- too aggressive to let run unattended against a rate-limited
production endpoint. SORTVIEW_UPLOADER_MAX_ISOLATION_REQUESTS caps the
total number of logical _post_upload_batch calls _attempt_and_isolate may
make across the *entire* drain cycle (shared via one _IsolationBudget
instance passed through every recursive call, not reset per branch).
Once exhausted, no further requests are issued for that cycle; every row
that hasn't been resolved yet (not delivered, not independently
quarantined) is left pending untouched -- exhaustion is a safety stop,
never treated as evidence those rows are bad. DrainResult.isolation_budget_exhausted
reports this explicitly, and run_forever always applies backoff (never
the steady poll cadence) when it's set, so a chronically pathological
backlog can't re-exhaust its budget every few seconds in a tight loop.

Note on urllib3's own retry adapter (mounted on the shared `session`,
configured in agent/uploader.py): its status_forcelist is
(429, 500, 502, 503, 504) -- 400 and 413 are deliberately not in it,
confirmed empirically (a 400 response arrives as exactly one real HTTP
request, no inner retry, 0s of retry delay). So for the specific case
this budget exists to bound, one logical call against the isolation
budget really does correspond to exactly one real HTTP request. That
correspondence does NOT hold for a RETRYABLE_INFRA outcome encountered
mid-isolation (e.g. a transient 503 on one split branch): urllib3 may
silently issue up to 4 real HTTP requests (1 + 3 retries, by default)
for that single logical call before returning control here, same as
outside isolation entirely.

Not solved here (explicitly deferred to a cutover decision): a freshly
created outbox starts reading its watched files from byte 0, same as
Phase 1. Enabling this uploader on the live AMH machine without first
deciding how to seed/skip existing file offsets would re-capture and
re-upload a branch's entire historical log content. Out of scope here.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import requests

from . import outbox
from .logger_config import get_logger
from .uploader import (
    API_URL,
    CONNECT_TIMEOUT,
    UPLOAD_READ_TIMEOUT,
    auth_headers,
    session,
)

logger = get_logger("outbox_uploader")

# Row-count bound: the starting point for a batch. Configurable because
# what's "safe" depends on real row sizes in production, which weren't
# assumed here -- see _fit_batch_to_byte_budget below for the byte-size
# bound that actually guards against an oversized request regardless of
# this value.
DEFAULT_BATCH_SIZE = int(os.getenv("SORTVIEW_UPLOAD_BATCH_SIZE", "250"))

# Byte-size bound: comfortably under the backend's own
# SORTVIEW_MAX_REQUEST_BODY_BYTES (5 MB default) even if that's been
# lowered in a given deployment, and without assuming every row is the
# same size (some fields -- title, message, raw_message -- are
# unbounded-length text with no schema-level cap found in this repo).
DEFAULT_MAX_UPLOAD_BODY_BYTES = int(
    os.getenv("SORTVIEW_MAX_UPLOAD_BODY_BYTES", str(2 * 1024 * 1024))
)

# Caps total logical POST attempts bad-row isolation may make in one
# upload_pending_batch() call. 20 is conservative relative to the default
# /upload rate limit (SORTVIEW_UPLOAD_RATE_LIMIT, 30/minute per agent
# token as of Phase 2): a single drain cycle's isolation burst can use at
# most two-thirds of that per-minute budget, leaving headroom within the
# same rolling minute, and repeated exhaustion across cycles is throttled
# further by run_forever's backoff-on-exhaustion behavior below -- the
# two mechanisms are meant to reinforce each other, not just this one
# value alone.
DEFAULT_MAX_ISOLATION_REQUESTS = int(
    os.getenv("SORTVIEW_UPLOADER_MAX_ISOLATION_REQUESTS", "20")
)

DEFAULT_POLL_INTERVAL_SECONDS = float(os.getenv("SORTVIEW_UPLOADER_POLL_INTERVAL_SECONDS", "3"))
DEFAULT_BACKOFF_BASE_SECONDS = float(os.getenv("SORTVIEW_UPLOADER_BACKOFF_BASE_SECONDS", "2"))
DEFAULT_BACKOFF_MAX_SECONDS = float(os.getenv("SORTVIEW_UPLOADER_BACKOFF_MAX_SECONDS", "300"))
DEFAULT_BACKOFF_MULTIPLIER = float(os.getenv("SORTVIEW_UPLOADER_BACKOFF_MULTIPLIER", "2"))

_EVENT_TYPE_TO_PAYLOAD_KEY = {
    "checkin": "checkins",
    "reject": "rejects",
    "acs": "acs",
}

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_AUTH_STATUS_CODES = {401, 403}
_REQUEST_FAILURE_STATUS_CODE = 400
_PAYLOAD_TOO_LARGE_STATUS_CODE = 413


class FailureCategory(str, Enum):
    RETRYABLE_INFRA = "retryable_infra"
    AUTH_FAILURE = "auth_failure"
    REQUEST_FAILURE = "request_failure"
    PAYLOAD_TOO_LARGE = "payload_too_large"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class UploadAttemptResult:
    """Outcome of exactly one POST /upload call."""

    success: bool
    category: FailureCategory | None
    error: str | None
    inserted: dict[str, int] | None


@dataclass(frozen=True)
class DrainResult:
    """Outcome of one upload_pending_batch() call -- potentially the
    result of many individual POSTs, if isolation had to split a failing
    batch. delivered/quarantined/pending together account for every row
    id that was part of the originally selected candidate.
    """

    delivered_ids: list[int] = field(default_factory=list)
    quarantined_ids: list[int] = field(default_factory=list)
    pending_ids: list[int] = field(default_factory=list)
    last_category: FailureCategory | None = None
    isolation_budget_exhausted: bool = False

    @property
    def made_progress(self) -> bool:
        """True if anything was actually resolved (delivered or
        quarantined) this call -- used by run_forever to decide whether
        to reset backoff even if part of the batch is still pending."""
        return bool(self.delivered_ids or self.quarantined_ids)


class _IsolationBudget:
    """Shared, mutable request counter for one upload_pending_batch()
    call. Passed through every recursive _attempt_and_isolate call
    (including the initial one) so the whole recursive tree draws from
    one pool -- a split's two branches do not each get their own budget.
    """

    def __init__(self, max_requests: int) -> None:
        self.max_requests = max_requests
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_requests

    def consume(self) -> None:
        self.used += 1


class Backoff:
    """Exponential backoff with a bounded maximum, reset on success.

    Deliberately holds no reference to time.sleep -- the caller decides
    how to wait, so tests can inject a no-op/instant sleep instead of
    actually blocking.
    """

    def __init__(
        self,
        base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
    ) -> None:
        self.base_seconds = base_seconds
        self.max_seconds = max_seconds
        self.multiplier = multiplier
        self._current = base_seconds

    def reset(self) -> None:
        self._current = self.base_seconds

    def next_delay(self) -> float:
        delay = self._current
        self._current = min(self._current * self.multiplier, self.max_seconds)
        return delay


def _select_pending_batch(conn: Any, limit: int) -> list[Any]:
    """Plain read, no transaction -- outbox.connect() uses autocommit
    mode, so this never holds a write lock. Quarantined rows are excluded
    by outbox.count_pending_events's sibling filter, applied here too, so
    a quarantined row can never be re-selected for automatic delivery."""
    return conn.execute(
        "SELECT id, event_type, payload_json FROM local_events "
        "WHERE uploaded_at IS NULL AND quarantined_at IS NULL "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()


def _group_rows_for_upload(rows: list[Any]) -> tuple[dict[str, list[dict[str, Any]]], list[int]]:
    grouped: dict[str, list[dict[str, Any]]] = {"checkins": [], "rejects": [], "acs": []}
    row_ids: list[int] = []

    for row in rows:
        payload_key = _EVENT_TYPE_TO_PAYLOAD_KEY[row["event_type"]]
        grouped[payload_key].append(json.loads(row["payload_json"]))
        row_ids.append(row["id"])

    return grouped, row_ids


def _payload_byte_size(grouped: dict[str, list[dict[str, Any]]]) -> int:
    return len(json.dumps(grouped).encode("utf-8"))


def _fit_batch_to_byte_budget(
    rows: list[Any], max_bytes: int
) -> list[Any]:
    """Proactively shrinks the row selection (by half, repeatedly) until
    its serialized JSON fits under max_bytes, or exactly one row remains.

    Not every payload row is the same size -- title/message/raw_message
    are unbounded-length text fields with no schema-level cap found in
    this repo -- so a fixed row-count batch size alone isn't a provable
    safety guarantee. This is the proactive half of the bounded strategy
    that makes it one; _attempt_and_isolate's reactive handling of a real
    413 response is the other half, for the case this budget turns out to
    be wrong (e.g. the server's actual limit is lower than assumed).
    """
    candidate = rows

    while True:
        grouped, _row_ids = _group_rows_for_upload(candidate)
        size = _payload_byte_size(grouped)

        if size <= max_bytes or len(candidate) <= 1:
            return candidate

        candidate = candidate[: len(candidate) // 2]


def _safe_error_preview(response: requests.Response, max_chars: int = 300) -> str:
    """Response-body preview for logging/quarantine_reason. This previews
    the SERVER's response text, never the outgoing request (which is the
    only place the bearer token could appear) -- so this can never leak
    the token regardless of what the server sends back.
    """
    text = response.text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...<truncated>"


def _post_upload_batch(grouped: dict[str, list[dict[str, Any]]]) -> UploadAttemptResult:
    """Reuses the existing session (with its configured Retry adapter),
    auth_headers(), API_URL, and timeouts from agent/uploader.py rather
    than reimplementing HTTP/auth plumbing.

    Never logs the Authorization header or the raw token: auth_headers()
    is passed straight to requests, never formatted into a log message,
    and every log line / error string below only ever includes response
    status/body previews or exception text, not the request we sent.
    """
    url = f"{API_URL}/upload"

    try:
        response = session.post(
            url,
            json=grouped,
            headers=auth_headers(),
            timeout=(CONNECT_TIMEOUT, UPLOAD_READ_TIMEOUT),
        )
    except requests.Timeout as exc:
        return UploadAttemptResult(False, FailureCategory.RETRYABLE_INFRA, f"timeout: {exc}", None)
    except requests.ConnectionError as exc:
        return UploadAttemptResult(
            False, FailureCategory.RETRYABLE_INFRA, f"connection error: {exc}", None
        )
    except requests.RequestException as exc:
        return UploadAttemptResult(
            False, FailureCategory.RETRYABLE_INFRA, f"request failed: {exc}", None
        )

    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            return UploadAttemptResult(
                False, FailureCategory.RETRYABLE_INFRA, "200 response was not valid JSON", None
            )

        if body.get("status") != "success":
            return UploadAttemptResult(
                False,
                FailureCategory.RETRYABLE_INFRA,
                f"200 response had unexpected status field: {body.get('status')!r}",
                None,
            )

        inserted = {
            "checkins": int(body.get("checkins_inserted", 0)),
            "rejects": int(body.get("rejects_inserted", 0)),
            "acs": int(body.get("acs_inserted", 0)),
        }
        return UploadAttemptResult(True, None, None, inserted)

    if response.status_code in _RETRYABLE_STATUS_CODES:
        return UploadAttemptResult(
            False,
            FailureCategory.RETRYABLE_INFRA,
            f"retryable server error {response.status_code}: {_safe_error_preview(response)}",
            None,
        )

    if response.status_code in _AUTH_STATUS_CODES:
        return UploadAttemptResult(
            False,
            FailureCategory.AUTH_FAILURE,
            f"authentication/authorization failure {response.status_code}: "
            f"{_safe_error_preview(response)}",
            None,
        )

    if response.status_code == _REQUEST_FAILURE_STATUS_CODE:
        return UploadAttemptResult(
            False,
            FailureCategory.REQUEST_FAILURE,
            f"request rejected (400): {_safe_error_preview(response)}",
            None,
        )

    if response.status_code == _PAYLOAD_TOO_LARGE_STATUS_CODE:
        return UploadAttemptResult(
            False,
            FailureCategory.PAYLOAD_TOO_LARGE,
            f"payload too large (413): {_safe_error_preview(response)}",
            None,
        )

    # An unrecognized status -- treat conservatively as retryable infra
    # rather than isolating rows over a status code this module doesn't
    # specifically know is a deterministic payload problem.
    return UploadAttemptResult(
        False,
        FailureCategory.RETRYABLE_INFRA,
        f"unexpected status {response.status_code}: {_safe_error_preview(response)}",
        None,
    )


def _mark_rows_delivered(conn: Any, row_ids: list[int], timestamp: str) -> None:
    placeholders = ",".join("?" * len(row_ids))
    conn.execute(
        # nosec B608 -- placeholders are literal "?" marks (count only
        # depends on len(row_ids)); every actual value is bound below,
        # nothing here is string-interpolated from row data.
        f"UPDATE local_events SET uploaded_at = ? WHERE id IN ({placeholders})",  # nosec B608
        [timestamp, *row_ids],
    )


def _record_failed_attempt(conn: Any, row_ids: list[int], timestamp: str, error: str) -> None:
    """Used only for RETRYABLE_INFRA/AUTH_FAILURE, where the whole
    attempted set genuinely failed together and the outcome is equally
    uninformative about every row in it -- never called mid-isolation for
    a batch that's merely being split, since that would misattribute a
    collective, inconclusive outcome onto rows that may well be fine."""
    placeholders = ",".join("?" * len(row_ids))
    conn.execute(
        # nosec B608 -- see _mark_rows_delivered above; same pattern.
        f"UPDATE local_events SET attempt_count = attempt_count + 1, "  # nosec B608
        f"last_attempt_at = ?, last_error = ? WHERE id IN ({placeholders})",
        [timestamp, error, *row_ids],
    )


def _quarantine_rows(conn: Any, row_ids: list[int], reason: str, timestamp: str) -> None:
    """Only ever called with a single row id that independently
    reproduced a deterministic failure. attempt_count is bumped by
    exactly 1 here -- the one definitive singleton attempt that proved
    this row is bad -- so its metadata honestly reflects what happened,
    not an inflated count from being part of larger batches that were
    split around it."""
    placeholders = ",".join("?" * len(row_ids))
    conn.execute(
        # nosec B608 -- see _mark_rows_delivered above; same pattern.
        f"UPDATE local_events SET quarantined_at = ?, quarantine_reason = ?, "  # nosec B608
        f"attempt_count = attempt_count + 1, last_attempt_at = ? "
        f"WHERE id IN ({placeholders})",
        [timestamp, reason, timestamp, *row_ids],
    )


def _attempt_and_isolate(conn: Any, rows: list[Any], budget: _IsolationBudget) -> DrainResult:
    """Uploads `rows` (already-selected outbox rows, never re-queried).

    - success: marks them all delivered.
    - RETRYABLE_INFRA / AUTH_FAILURE: the whole set stays pending,
      untouched rows aside from a recorded attempt -- never split, since
      the payload hasn't been proven bad (infra) or the failure isn't
      row-level at all (auth).
    - REQUEST_FAILURE / PAYLOAD_TOO_LARGE with more than one row: split
      in half, recurse on each half independently -- as long as the
      isolation request budget isn't exhausted (see below).
    - REQUEST_FAILURE / PAYLOAD_TOO_LARGE with exactly one row: that row
      independently reproduced a deterministic failure -- quarantine it.

    Bounded by construction two ways: recursion only ever operates on the
    rows already passed in (never re-queries the database), and `budget`
    -- one shared _IsolationBudget instance threaded through every
    recursive call, not a fresh one per branch -- caps the total number
    of logical POSTs this call tree may make. Checked at the top of every
    call, including the very first: if the budget is already exhausted
    when this function is entered, no request is made at all and every
    row in `rows` is left pending untouched. Exhaustion is a safety stop,
    not a verdict -- these rows are not quarantined merely because they
    didn't get a chance to be attempted this cycle.
    """
    grouped, row_ids = _group_rows_for_upload(rows)

    if budget.exhausted:
        logger.warning(
            "Isolation budget exhausted (%s/%s requests used this drain cycle) -- "
            "leaving %s row(s) pending without further attempts, none quarantined",
            budget.used,
            budget.max_requests,
            len(row_ids),
        )
        return DrainResult(pending_ids=row_ids, isolation_budget_exhausted=True)

    budget.consume()
    outcome = _post_upload_batch(grouped)
    timestamp = _now_iso()

    if outcome.success:
        with outbox.transaction(conn):
            _mark_rows_delivered(conn, row_ids, timestamp)
        logger.info(
            "Batch delivered | rows=%s inserted=%s",
            len(row_ids),
            outcome.inserted,
        )
        return DrainResult(delivered_ids=row_ids)

    if outcome.category in (FailureCategory.RETRYABLE_INFRA, FailureCategory.AUTH_FAILURE):
        with outbox.transaction(conn):
            _record_failed_attempt(conn, row_ids, timestamp, outcome.error or "unknown error")
        logger.warning(
            "Batch upload failed | rows=%s category=%s error=%s",
            len(row_ids),
            outcome.category.value if outcome.category else None,
            outcome.error,
        )
        return DrainResult(pending_ids=row_ids, last_category=outcome.category)

    # REQUEST_FAILURE or PAYLOAD_TOO_LARGE: deterministic, isolate.
    if len(rows) == 1:
        with outbox.transaction(conn):
            _quarantine_rows(conn, row_ids, outcome.error or "unknown error", timestamp)
        logger.warning(
            "Row quarantined | row_id=%s category=%s reason=%s",
            row_ids[0],
            outcome.category.value if outcome.category else None,
            outcome.error,
        )
        return DrainResult(quarantined_ids=row_ids, last_category=outcome.category)

    logger.info(
        "Splitting failing batch for isolation | rows=%s category=%s",
        len(rows),
        outcome.category.value if outcome.category else None,
    )
    mid = len(rows) // 2
    left = _attempt_and_isolate(conn, rows[:mid], budget)
    right = _attempt_and_isolate(conn, rows[mid:], budget)
    return DrainResult(
        delivered_ids=left.delivered_ids + right.delivered_ids,
        quarantined_ids=left.quarantined_ids + right.quarantined_ids,
        pending_ids=left.pending_ids + right.pending_ids,
        last_category=right.last_category or left.last_category,
        isolation_budget_exhausted=left.isolation_budget_exhausted
        or right.isolation_budget_exhausted,
    )


def upload_pending_batch(
    conn: Any,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_body_bytes: int = DEFAULT_MAX_UPLOAD_BODY_BYTES,
    max_isolation_requests: int = DEFAULT_MAX_ISOLATION_REQUESTS,
) -> DrainResult | None:
    """Selects up to batch_size pending rows and drains them, isolating
    and quarantining any row(s) that deterministically fail on their own
    -- bounded by a fresh _IsolationBudget(max_isolation_requests) shared
    across the whole call tree for this cycle.

    Returns None -- and sends no HTTP request at all -- if the outbox has
    nothing pending.
    """
    rows = _select_pending_batch(conn, batch_size)

    if not rows:
        return None

    candidate = _fit_batch_to_byte_budget(rows, max_body_bytes)

    logger.info("Uploading batch | rows=%s", len(candidate))

    budget = _IsolationBudget(max_isolation_requests)
    return _attempt_and_isolate(conn, candidate, budget)


def run_forever(
    conn: Any,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_body_bytes: int = DEFAULT_MAX_UPLOAD_BODY_BYTES,
    max_isolation_requests: int = DEFAULT_MAX_ISOLATION_REQUESTS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    backoff: Backoff | None = None,
    sleep_fn: Any = time.sleep,
    max_iterations: int | None = None,
) -> None:
    """Drains the outbox in a loop until stopped.

    While there's pending data and progress is being made -- rows
    delivered or quarantined -- this polls at a steady
    poll_interval_seconds cadence (2-5s is the intended normal range) and
    never sends an empty request. Backoff grows whenever a cycle makes
    zero progress (a pure RETRYABLE_INFRA/AUTH_FAILURE outcome, or the
    batch untouched) -- and always applies when the isolation budget was
    exhausted, even if that same cycle also delivered or quarantined some
    rows, so a chronically pathological backlog can't re-exhaust its
    budget every poll_interval_seconds in a tight loop; each retry gets a
    fresh budget, spaced further apart as backoff grows. Backoff resets
    to its base as soon as a cycle both makes progress and doesn't hit
    the budget wall.

    backoff/sleep_fn/max_iterations exist for tests -- production use
    leaves backoff as a fresh Backoff() and max_iterations as None,
    relying on the process being stopped externally.
    """
    active_backoff = backoff if backoff is not None else Backoff()
    iterations = 0

    while max_iterations is None or iterations < max_iterations:
        result = upload_pending_batch(
            conn,
            batch_size=batch_size,
            max_body_bytes=max_body_bytes,
            max_isolation_requests=max_isolation_requests,
        )

        if result is None:
            # Nothing pending -- no request was sent. Wait at the normal
            # cadence and check again; this isn't a failure, so it
            # doesn't touch backoff.
            delay = poll_interval_seconds
        elif result.isolation_budget_exhausted:
            # A safety stop, not a failure per se -- but retrying
            # immediately with a fresh budget would just re-exhaust it on
            # the same pathological backlog. Always back off here,
            # regardless of any progress also made this cycle.
            delay = active_backoff.next_delay()
        elif result.made_progress:
            active_backoff.reset()
            delay = poll_interval_seconds
        else:
            delay = active_backoff.next_delay()

        iterations += 1

        if max_iterations is None or iterations < max_iterations:
            sleep_fn(delay)


if __name__ == "__main__":
    # Relative imports throughout this module mean it must be run as part
    # of the agent package -- `python -m agent.outbox_uploader` from the
    # repo/install root, not `python outbox_uploader.py` from inside
    # agent/.
    from pathlib import Path

    db_conn = outbox.connect(Path("data") / "sortview_agent.db")

    logger.info(
        "Uploader starting | poll_interval=%ss batch_size=%s",
        DEFAULT_POLL_INTERVAL_SECONDS,
        DEFAULT_BATCH_SIZE,
    )

    run_forever(db_conn)
