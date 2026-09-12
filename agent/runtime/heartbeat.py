"""Canonical heartbeat (Continuous Ingestion Phase F).

Migrates agent/heartbeat.py's Phase 3 concept onto agent.state/agent.spool
instead of the SQLite outbox -- same backend contract
(POST /upload-pipeline-status, the heartbeat subset of
main.PipelineStatusRequest), same health precedence, entirely read-only
against local state/spool (this module never writes to either).

FIELD MAPPING: uses ONLY the 8 heartbeat fields main.py's
PipelineStatusRequest already accepts today (health_status,
pending_outbox_count, quarantined_count, oldest_pending_event_at,
last_success_at, last_failure_category, last_error,
watcher_last_active_at) -- no backend schema change in this phase, per
Phase F's "use only fields the backend currently accepts... do not invent
a huge observability framework" instruction. Richer internal detail
(source generations, current offsets, active source paths, agent_id,
agent_version, uptime) is real and useful, but stays LOCAL-ONLY, written
to a diagnostics JSON file by agent/runtime/housekeeping.py instead of
being sent over the wire -- see that module's docstring for why keeping
it there is the right scope for this phase.

pending_outbox_count / quarantined_count are SUMMED across every
configured source's spool.get_spool_stats -- the backend column is a
single per-branch number, same granularity the legacy SQLite-backed
heartbeat already reported (it never broke this down per source-file
either).

last_success_at / last_failure_category / last_error come from the
shared RuntimeStatus board (agent/runtime/status.py), populated by the
Supervisor's own loop wrappers from each UploadCycleResult -- this module
only reads that snapshot, never spool/state directly for those three
fields, since "when did upload last succeed" isn't derivable from spool
content alone (an empty pending/ directory could mean "just delivered
everything" or "never had anything to deliver" -- indistinguishable
without an explicit success timestamp).

Health precedence, unchanged from the Phase 3 heartbeat:
  auth_failure > degraded > healthy.
  DEGRADED additionally triggers on quarantined_count > 0, OR any
  configured source currently reported missing (RuntimeStatus
  source_missing) -- a signal the legacy heartbeat never had, since the
  old watcher had no structured "missing" concept the same way -- OR any
  Supervisor-managed component (collector/uploader/heartbeat/
  housekeeping) being reported dead in RuntimeStatus.

WORKER-DEATH VISIBILITY (production-supervision correction): a dead
component thread is checked EXPLICITLY, not inferred from its side
effects. A dead collector, in particular, produces no new pending
batches and no quarantine activity at all -- from spool/state content
alone, a permanently-dead collector is indistinguishable from an
idle-but-perfectly-healthy one, which is exactly the bug this correction
fixes (this function previously never consulted
RuntimeStatus.component_health() at all, so health_status could report
"healthy" forever after a collector crash). There is no dedicated
backend vocabulary for "a worker died" -- main.py's health_status is a
closed Literal["healthy","degraded","auth_failure"] -- so a dead
component is folded into "degraded" (with last_error naming which
component and why) rather than adding a fourth backend value for this
phase; see agent/runtime/supervisor.py's module docstring for the
broader production worker-failure policy this is one part of.

CAVEAT: if the HEARTBEAT component itself dies, this function's own
output can never reach the backend at all -- there is no heartbeat left
to report "heartbeat is dead." That failure mode is only visible locally
(RuntimeStatus.component_health(), diagnostics.json) and, from the
backend's side, only inferable indirectly as heartbeat staleness
(pipeline_status.updated_at no longer advancing) -- not something this
phase adds a dedicated signal for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .. import spool
from .config import RuntimeConfig
from .http_client import post_json
from .status import RuntimeStatus

DEFAULT_DEGRADED_BACKLOG_AGE_MINUTES = 15.0


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
    cfg: RuntimeConfig,
    status: RuntimeStatus,
    *,
    degraded_backlog_age_minutes: float = DEFAULT_DEGRADED_BACKLOG_AGE_MINUTES,
    now: datetime | None = None,
) -> HealthSnapshot:
    now = now or datetime.now(UTC)
    snap = status.snapshot()

    pending_count = 0
    quarantined_count = 0
    oldest_candidates: list[str] = []
    any_source_missing = False

    for source_cfg in cfg.sources:
        stats = spool.get_spool_stats(cfg.spool_root, source_cfg.name)
        pending_count += stats.pending_batch_count
        quarantined_count += stats.quarantined_batch_count
        if stats.oldest_pending_created_at is not None:
            oldest_candidates.append(stats.oldest_pending_created_at)
        if snap["source_missing"].get(source_cfg.name):
            any_source_missing = True

    oldest_pending_at = min(oldest_candidates) if oldest_candidates else None

    last_failure_category = snap["last_upload_failure_category"]

    # A dead component is at least as significant as any other degraded
    # signal below -- checked explicitly, because nothing else in this
    # computation (pending counts, quarantine, backlog age) is guaranteed
    # to move just because a worker thread died. A dead collector, for
    # instance, produces NO new pending batches and NO quarantine
    # activity at all -- from spool/state content alone, a dead collector
    # looks identical to an idle-but-healthy one. See
    # agent.runtime.status.RuntimeStatus.record_component_failed, written
    # by the Supervisor's _run_guarded wrapper the moment a component
    # thread's loop function raises.
    dead_components = [name for name, health in snap["components"].items() if not health.alive]
    any_component_dead = bool(dead_components)

    last_error = snap["last_upload_error"] or snap["last_collector_error"]
    if any_component_dead and not last_error:
        dead = snap["components"][dead_components[0]]
        last_error = f"component {dead_components[0]} is not running: {dead.last_error}"

    backlog_age_minutes = None
    oldest_dt = _parse_iso(oldest_pending_at)
    if oldest_dt is not None:
        backlog_age_minutes = (now - oldest_dt).total_seconds() / 60.0

    if last_failure_category == "auth_failure":
        health_status = "auth_failure"
    elif (
        quarantined_count > 0
        or last_failure_category == "retryable_infra"
        or any_source_missing
        or any_component_dead
        or (backlog_age_minutes is not None and backlog_age_minutes > degraded_backlog_age_minutes)
    ):
        health_status = "degraded"
    else:
        health_status = "healthy"

    return HealthSnapshot(
        health_status=health_status,
        pending_outbox_count=pending_count,
        quarantined_count=quarantined_count,
        oldest_pending_event_at=oldest_pending_at,
        last_success_at=snap["last_success_at"],
        last_failure_category=last_failure_category,
        last_error=last_error,
        watcher_last_active_at=snap["last_collector_active_at"],
    )


def _build_payload(cfg: RuntimeConfig, snapshot: HealthSnapshot) -> dict[str, Any]:
    """Every heartbeat field is always included explicitly, even as None
    -- never omitted. main.py's partial-update endpoint only clears a
    stored column when the field is EXPLICITLY present as null, not
    merely absent -- omitting a field here would leave a stale value
    behind instead of clearing it."""
    return {
        "customer_id": cfg.customer_id,
        "branch_id": cfg.branch_id,
        "health_status": snapshot.health_status,
        "pending_outbox_count": snapshot.pending_outbox_count,
        "quarantined_count": snapshot.quarantined_count,
        "oldest_pending_event_at": snapshot.oldest_pending_event_at,
        "last_success_at": snapshot.last_success_at,
        "last_failure_category": snapshot.last_failure_category,
        "last_error": snapshot.last_error,
        "watcher_last_active_at": snapshot.watcher_last_active_at,
    }


class Heartbeat:
    def __init__(self, cfg: RuntimeConfig, session, status: RuntimeStatus, logger: Any) -> None:
        self.cfg = cfg
        self.session = session
        self.status = status
        self.logger = logger

    def send_once(self) -> bool:
        """SHADOW MODE (cfg.heartbeat_enabled=False): the health snapshot
        is still computed (useful in local logs/diagnostics even without
        a backend to send it to), but no HTTP request is ever made. This
        is a single early return, not a broken URL or invalid token, so
        it never generates a RETRYABLE_INFRA/AUTH_FAILURE-shaped warning
        or backoff growth -- returns True (treated the same as a genuine
        successful send) specifically so the loop below stays at its
        normal steady interval instead of backing off for a condition
        that isn't a failure.
        """
        snapshot = compute_health_snapshot(self.cfg, self.status)

        if not self.cfg.heartbeat_enabled:
            self.logger.info(
                "Heartbeat suppressed (shadow mode, heartbeat_enabled=false) | "
                "would-be health=%s pending=%s quarantined=%s",
                snapshot.health_status, snapshot.pending_outbox_count, snapshot.quarantined_count,
            )
            return True

        url = f"{self.cfg.api_url}/upload-pipeline-status"
        outcome = post_json(
            self.session,
            url,
            _build_payload(self.cfg, snapshot),
            headers={"Authorization": f"Bearer {self.cfg.api_token}", "Content-Type": "application/json"},
            timeout=(self.cfg.http_connect_timeout, self.cfg.http_status_read_timeout),
        )
        if outcome.success:
            self.logger.info(
                "Heartbeat sent | health=%s pending=%s quarantined=%s",
                snapshot.health_status, snapshot.pending_outbox_count, snapshot.quarantined_count,
            )
        else:
            self.logger.warning("Heartbeat failed | error=%s", outcome.error)
        return outcome.success

    def run_forever(self, *, stop_event, backoff=None, max_iterations: int | None = None) -> None:
        from .backoff import Backoff

        active_backoff = backoff or Backoff(
            base_seconds=self.cfg.heartbeat_backoff_base_seconds,
            max_seconds=self.cfg.heartbeat_backoff_max_seconds,
        )
        iterations = 0

        while max_iterations is None or iterations < max_iterations:
            if stop_event.is_set():
                return

            sent = self.send_once()
            iterations += 1

            if sent:
                active_backoff.reset()
                delay = self.cfg.heartbeat_interval_seconds
            else:
                delay = active_backoff.next_delay()

            if (max_iterations is None or iterations < max_iterations) and stop_event.wait(delay):
                return
