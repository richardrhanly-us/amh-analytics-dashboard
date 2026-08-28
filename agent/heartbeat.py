"""Continuous-agent heartbeat/health component (Phase 3: Signal).

Runs independently of the 2-5s uploader cadence, on its own longer
interval (default 60s -- see DEFAULT_HEARTBEAT_INTERVAL_SECONDS). Each
cycle it computes a snapshot of local outbox state -- entirely read-only,
no local SQLite writes, no transaction ever held during the network call
-- and POSTs it to the existing /upload-pipeline-status endpoint using the
same authentication as the uploader.

Health vs. activity are deliberately distinct concepts. health_status
describes whether ingestion is currently working: an idle branch with zero
new AMH events for hours is still "healthy" as long as nothing is stuck,
nothing is quarantined, and there's no unresolved auth/retry failure --
source-file activity never enters this computation. Liveness (is the
agent process still running at all) is a separate concern, satisfied
simply by this heartbeat continuing to arrive at all -- see updated_at on
the receiving pipeline_status row, refreshed by every write regardless of
writer.

Failure-state self-clearing: last_failure_category/last_error are always
recomputed fresh every cycle from outbox.get_latest_unresolved_failure,
which only considers rows that are still pending (never delivered, never
quarantined). Once the rows behind a failure succeed on a later retry,
they drop out of that query, and this module naturally sends
last_failure_category=None / last_error=None on the very next heartbeat --
an *explicit* null in the request body (never an omitted field), which
main.py's partial-update endpoint logic treats as an instruction to clear
the previously-stored value. No separate "mark resolved" step exists or
is needed.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests

from . import outbox
from .logger_config import get_logger
from .outbox_uploader import Backoff
from .uploader import (
    API_URL,
    BRANCH_ID,
    CONNECT_TIMEOUT,
    CUSTOMER_ID,
    STATUS_READ_TIMEOUT,
    auth_headers,
    session,
)

logger = get_logger("heartbeat")

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("SORTVIEW_HEARTBEAT_INTERVAL_SECONDS", "60"))

# How long something can sit pending before it counts as a "meaningful"
# backlog for DEGRADED purposes, rather than the few seconds of normal
# queued work any continuous system has between poll cycles. 15 minutes is
# comfortably longer than any expected transient blip (the uploader's own
# backoff is capped at 300s/5 minutes -- see
# outbox_uploader.DEFAULT_BACKOFF_MAX_SECONDS) while still being short
# enough that a real, sustained delivery problem gets flagged well within
# the hour-scale granularity scheduled monitoring already runs at.
DEFAULT_DEGRADED_BACKLOG_AGE_MINUTES = float(
    os.getenv("SORTVIEW_HEARTBEAT_DEGRADED_BACKLOG_AGE_MINUTES", "15")
)

DEFAULT_BACKOFF_BASE_SECONDS = float(os.getenv("SORTVIEW_HEARTBEAT_BACKOFF_BASE_SECONDS", "5"))
DEFAULT_BACKOFF_MAX_SECONDS = float(os.getenv("SORTVIEW_HEARTBEAT_BACKOFF_MAX_SECONDS", "300"))
DEFAULT_BACKOFF_MULTIPLIER = float(os.getenv("SORTVIEW_HEARTBEAT_BACKOFF_MULTIPLIER", "2"))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return None


@dataclass(frozen=True)
class HealthSnapshot:
    health_status: str
    pending_outbox_count: int
    quarantined_count: int
    oldest_pending_event_at: str | None
    last_success_at: str | None
    last_failure_category: str | None
    last_error: str | None
    watcher_last_active_at: str | None


def compute_health_snapshot(
    conn: Any,
    *,
    degraded_backlog_age_minutes: float = DEFAULT_DEGRADED_BACKLOG_AGE_MINUTES,
    now: datetime | None = None,
) -> HealthSnapshot:
    """Entirely read-only against the local outbox -- no writes, no
    transaction. Safe to call at any time without affecting watcher or
    uploader state.

    Precedence: auth_failure > degraded > healthy.

    - AUTH_FAILURE: the most recent attempt among currently-pending rows
      failed with auth_failure and hasn't since succeeded.
    - DEGRADED: quarantined_count > 0, OR the most recent attempt among
      currently-pending rows failed with retryable_infra and hasn't since
      succeeded, OR the oldest pending row has been waiting longer than
      degraded_backlog_age_minutes. Raw pending_outbox_count > 0 is never
      by itself a degraded signal -- a few seconds of normal queued work
      between poll cycles is expected in a continuous system.
    - HEALTHY: none of the above. A branch idle for hours with zero new
      AMH events is healthy as long as pending_outbox_count is 0 (or just
      recent) and nothing is quarantined or stuck -- source-file activity
      never enters this computation at all.
    """
    now = now or datetime.now(UTC)

    pending_count = outbox.count_pending_events(conn)
    quarantined_count = outbox.count_quarantined_events(conn)
    oldest_pending_at = outbox.get_oldest_pending_created_at(conn)
    last_success_at = outbox.get_last_success_at(conn)
    watcher_last_active_at = outbox.get_watcher_last_active_at(conn)

    unresolved = outbox.get_latest_unresolved_failure(conn)
    last_failure_category = unresolved["last_error_category"] if unresolved else None
    last_error = unresolved["last_error"] if unresolved else None

    backlog_age_minutes = None
    oldest_dt = _parse_iso(oldest_pending_at)
    if oldest_dt is not None:
        backlog_age_minutes = (now - oldest_dt).total_seconds() / 60.0

    if last_failure_category == "auth_failure":
        health_status = "auth_failure"
    elif (
        quarantined_count > 0
        or last_failure_category == "retryable_infra"
        or (
            backlog_age_minutes is not None
            and backlog_age_minutes > degraded_backlog_age_minutes
        )
    ):
        health_status = "degraded"
    else:
        health_status = "healthy"

    return HealthSnapshot(
        health_status=health_status,
        pending_outbox_count=pending_count,
        quarantined_count=quarantined_count,
        oldest_pending_event_at=oldest_pending_at,
        last_success_at=last_success_at,
        last_failure_category=last_failure_category,
        last_error=last_error,
        watcher_last_active_at=watcher_last_active_at,
    )


def _build_payload(snapshot: HealthSnapshot) -> dict[str, Any]:
    """Every heartbeat field is always included explicitly -- even as
    None -- never omitted. This is what makes failure-state clearing
    work: main.py's partial-update endpoint only clears a column when the
    field is *explicitly* present as null, not merely absent. Only
    heartbeat fields are ever sent here, never a legacy per-run field, so
    the legacy scheduled uploader's own columns are always left
    untouched."""
    return {
        "customer_id": CUSTOMER_ID,
        "branch_id": BRANCH_ID,
        "health_status": snapshot.health_status,
        "pending_outbox_count": snapshot.pending_outbox_count,
        "quarantined_count": snapshot.quarantined_count,
        "oldest_pending_event_at": snapshot.oldest_pending_event_at,
        "last_success_at": snapshot.last_success_at,
        "last_failure_category": snapshot.last_failure_category,
        "last_error": snapshot.last_error,
        "watcher_last_active_at": snapshot.watcher_last_active_at,
    }


def send_heartbeat(snapshot: HealthSnapshot) -> bool:
    """POSTs one heartbeat. Returns True on a 200/"success" response,
    False on any other outcome -- never raises. Reuses the uploader's
    session (with its own Retry adapter), auth_headers(), and API_URL
    rather than reimplementing HTTP/auth plumbing, same as
    outbox_uploader.py does. Never logs the Authorization header or the
    raw token -- auth_headers() is passed straight to requests, never
    formatted into a log message."""
    url = f"{API_URL}/upload-pipeline-status"

    try:
        response = session.post(
            url,
            json=_build_payload(snapshot),
            headers=auth_headers(),
            timeout=(CONNECT_TIMEOUT, STATUS_READ_TIMEOUT),
        )
    except requests.RequestException as exc:
        logger.warning("Heartbeat request failed | error=%s", exc)
        return False

    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            logger.warning("Heartbeat got a 200 response with a non-JSON body")
            return False

        if body.get("status") == "success":
            return True

        logger.warning("Heartbeat got an unexpected 200 body | body=%s", body)
        return False

    logger.warning(
        "Heartbeat rejected | status_code=%s body=%s",
        response.status_code,
        (response.text or "")[:300],
    )
    return False


def run_forever(
    conn: Any,
    *,
    interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    degraded_backlog_age_minutes: float = DEFAULT_DEGRADED_BACKLOG_AGE_MINUTES,
    backoff: Backoff | None = None,
    sleep_fn: Any = time.sleep,
    max_iterations: int | None = None,
) -> None:
    """Runs the heartbeat loop until stopped. Never touches watcher or
    uploader state, never blocks watcher capture -- each cycle is one
    read-only SQLite query pass followed by one HTTP POST, with no
    transaction open across the network call.

    A failed heartbeat POST backs off (bounded, reusing
    outbox_uploader.Backoff) rather than retrying at the normal cadence --
    this only governs how often *this loop* retries; it never touches the
    outbox or affects watcher/uploader behavior in any way.

    backoff/sleep_fn/max_iterations exist for tests -- production use
    leaves backoff as a fresh Backoff() and max_iterations as None.
    """
    active_backoff = backoff if backoff is not None else Backoff(
        base_seconds=DEFAULT_BACKOFF_BASE_SECONDS,
        max_seconds=DEFAULT_BACKOFF_MAX_SECONDS,
        multiplier=DEFAULT_BACKOFF_MULTIPLIER,
    )
    iterations = 0

    while max_iterations is None or iterations < max_iterations:
        snapshot = compute_health_snapshot(
            conn, degraded_backlog_age_minutes=degraded_backlog_age_minutes
        )
        sent = send_heartbeat(snapshot)

        if sent:
            active_backoff.reset()
            delay = interval_seconds
            logger.info(
                "Heartbeat sent | health=%s pending=%s quarantined=%s",
                snapshot.health_status,
                snapshot.pending_outbox_count,
                snapshot.quarantined_count,
            )
        else:
            delay = active_backoff.next_delay()

        iterations += 1

        if max_iterations is None or iterations < max_iterations:
            sleep_fn(delay)


if __name__ == "__main__":
    # Relative imports throughout this module mean it must be run as part
    # of the agent package -- `python -m agent.heartbeat` from the
    # repo/install root, not `python heartbeat.py` from inside agent/.
    from pathlib import Path

    db_conn = outbox.connect(Path("data") / "sortview_agent.db")

    logger.info("Heartbeat starting | interval=%ss", DEFAULT_HEARTBEAT_INTERVAL_SECONDS)

    run_forever(db_conn)
