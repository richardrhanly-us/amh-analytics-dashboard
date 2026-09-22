"""Contract v2: the heartbeat (docs/collector-v2.md).

The v1 heartbeat carried free text (`last_error`: an exception string or a server-response preview). The v2 heartbeat cannot: a
`StatusSnapshot` holds only the approved enum values, two counters and timestamps, and its constructor refuses anything else. The whole
contract is `POST /v2/status`:

    contract_version = 2, key_id, status (healthy | degraded | error), last_error_class (null | retryable_infra | auth_failure |
    permanent_rejection | source_unavailable | configuration_error | other), pending_outbox_count, quarantined_count,
    oldest_pending_event_at, last_success_at, watcher_last_active_at

This collector has NO outbox (the Tech Logic files are the durable queue and the cursor only moves after delivery), so
`pending_outbox_count` is always 0 and `oldest_pending_event_at` is never sent: an outbox is not created to give the number meaning.
`quarantined_count` is the number of safe quarantine entries CURRENTLY retained.

HOW A RUN BECOMES A STATUS (`decide`):
    completed cleanly                                              -> healthy
    completed, but events were newly quarantined                   -> degraded, permanent_rejection
    completed, but a configured source was missing                 -> degraded, source_unavailable
    failed: network/5xx/429 (retryable)                            -> degraded, retryable_infra   (error once it has failed N runs in a row)
    failed: 401/403                                                -> error, auth_failure
    failed: 404 / envelope-level 422 / secret, rules or config     -> error, configuration_error
    failed: 400/413 / a chunk that cannot be delivered             -> error, permanent_rejection
    failed: anything else (including an unexpected crash)          -> error, other
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .v2_events import HEALTH_STATUSES, LAST_ERROR_CLASSES, format_time

# Failure categories a delivery can end in (what the uploader and the run report, never a message).
AUTH, CONFIG, PERMANENT, RETRYABLE, OTHER = "auth", "config", "permanent", "retryable", "other"
FAILURE_CATEGORIES = (AUTH, CONFIG, PERMANENT, RETRYABLE, OTHER)

_CLASS_OF_FAILURE = {AUTH: "auth_failure", CONFIG: "configuration_error", PERMANENT: "permanent_rejection",
                     RETRYABLE: "retryable_infra", OTHER: "other"}
MAX_COUNTER = 10_000_000


class StatusError(ValueError):
    """A snapshot field is outside the approved set. The message names the field, never a value."""


@dataclass(frozen=True)
class StatusSnapshot:
    status: str
    last_error_class: str | None
    quarantined_count: int
    last_success_at: datetime | None
    watcher_last_active_at: datetime | None
    pending_outbox_count: int = 0  # always 0: this collector has no outbox

    def __post_init__(self) -> None:
        if self.status not in HEALTH_STATUSES:
            raise StatusError("unapproved field: status")
        if self.last_error_class is not None and self.last_error_class not in LAST_ERROR_CLASSES:
            raise StatusError("unapproved field: last_error_class")
        for name in ("quarantined_count", "pending_outbox_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNTER:
                raise StatusError(f"unapproved field: {name}")
        if self.pending_outbox_count != 0:
            raise StatusError("unapproved field: pending_outbox_count")
        for name in ("last_success_at", "watcher_last_active_at"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, datetime) or value.tzinfo is None):
                raise StatusError(f"unapproved field: {name}")

    def payload(self, key_id: str) -> dict[str, Any]:
        """The request body, field by field. Optional fields that are absent are omitted (the server stores them as NULL)."""
        body: dict[str, Any] = {"contract_version": 2, "key_id": key_id, "status": self.status,
                                "pending_outbox_count": self.pending_outbox_count, "quarantined_count": self.quarantined_count}
        if self.last_error_class is not None:
            body["last_error_class"] = self.last_error_class
        if self.last_success_at is not None:
            body["last_success_at"] = format_time(self.last_success_at)
        if self.watcher_last_active_at is not None:
            body["watcher_last_active_at"] = format_time(self.watcher_last_active_at)
        return body


def decide(*, failure: str | None, new_quarantined: int, sources_missing: int, consecutive_failures: int,
           error_after: int) -> tuple[str, str | None]:
    """(status, last_error_class) for one run. `failure` is one of FAILURE_CATEGORIES, or None for a run that completed."""
    if failure is None:
        if sources_missing:
            return "degraded", "source_unavailable"
        if new_quarantined:
            return "degraded", "permanent_rejection"
        return "healthy", None
    if failure not in FAILURE_CATEGORIES:
        failure = OTHER
    if failure == RETRYABLE:
        return ("error" if consecutive_failures >= error_after else "degraded"), "retryable_infra"
    return "error", _CLASS_OF_FAILURE[failure]


def local_status_document(*, now: datetime, ok: bool, health: str, last_error_class: str | None, consecutive_failures: int,
                          last_success_at: datetime | None, counters: dict[str, int], quarantined_count: int) -> dict[str, Any]:
    """What `status_v2.json` holds: enums, integers and timestamps. Never a message."""
    return {
        "last_attempt": format_time(now),
        "run_ok": ok,
        "health_status": health,
        "last_error_class": last_error_class,
        "consecutive_failures": consecutive_failures,
        "last_success_at": format_time(last_success_at) if last_success_at else None,
        "quarantined_count": quarantined_count,
        "pending_outbox_count": 0,
        "counters": {name: int(value) for name, value in counters.items()},
    }


def parse_prior(document: dict[str, Any]) -> tuple[int, datetime | None]:
    """(consecutive_failures, last_success_at) from a previous status file; anything unexpected reads as (0, None)."""
    failures = document.get("consecutive_failures", 0)
    failures = failures if isinstance(failures, int) and not isinstance(failures, bool) and failures >= 0 else 0
    last = document.get("last_success_at")
    try:
        parsed = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) if isinstance(last, str) else None
    except ValueError:
        parsed = None
    return failures, parsed
