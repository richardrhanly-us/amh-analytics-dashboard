"""Local SQLite outbox maintenance for the continuous-agent (Phase 5:
Sustain). Prunes old delivered rows, checkpoints the WAL file, and
reports space-usage diagnostics -- entirely local, entirely offline.

Runs as its own independent component, same pattern as
agent/heartbeat.py: its own interval (default 3600s, far coarser than
the uploader's 2-5s poll cadence), its own bounded backoff on failure,
its own standalone __main__ entrypoint. It never imports requests or
agent.uploader -- there is no network path here at all, and a test in
tests/test_maintenance.py asserts that directly.

Retention policy (see run_maintenance_cycle / outbox.prune_delivered_events):
  - delivered rows (uploaded_at set) older than the retention cutoff are
    eligible for deletion
  - quarantined rows are NEVER auto-pruned, regardless of age -- an
    operator has to look at them first (no such tool exists yet; that's
    explicitly out of scope here)
  - pending rows are NEVER auto-pruned, regardless of age -- deleting an
    undelivered row would be data loss
  - age is measured from uploaded_at (when it was actually delivered),
    not created_at (when the watcher first saw it) -- see the pruning
    predicate in outbox.py

auto_vacuum / space reclamation (see outbox.connect / outbox.get_auto_vacuum_mode):
  A brand-new database gets `auto_vacuum=INCREMENTAL` at creation, so
  `PRAGMA incremental_vacuum` on it can actually shrink the file over
  time. An existing database created before Phase 5 stays at whatever
  mode it was created with (in practice 'none', since nothing before
  Phase 5 ever set this) -- confirmed empirically that issuing the
  pragma against an existing, already-populated database is a silent
  no-op, and the only way to change it retroactively is a full VACUUM
  (a full file rebuild), which this module deliberately never runs.
  Existing databases still benefit from Phase 5: deleted rows' pages go
  onto SQLite's own internal freelist and are reused by future inserts
  regardless of auto_vacuum mode (confirmed empirically) -- the file just
  won't shrink back down to reflect that reuse. run_maintenance_cycle
  only ever attempts incremental_vacuum when
  outbox.get_auto_vacuum_mode(conn) == "incremental" and there's an
  actual freelist to reclaim; on a 'none'-mode database it correctly
  does nothing rather than pretending to.

WAL checkpointing: TRUNCATE mode, once per cycle, after pruning. Chosen
because it's the only mode that actually shrinks the -wal file's on-disk
size (PASSIVE/RESTART checkpoint the WAL's content into the main file but
leave the file's current size unchanged -- confirmed empirically). A
nonzero `busy` result (another connection still needs some of those WAL
frames) is treated as a normal, expected condition -- logged, not
escalated as an error -- and simply retried on the next cycle.

Important correction from the initial investigation: a checkpoint under
contention does NOT return instantly with busy=1 -- it genuinely blocks
the calling thread for up to the connection's own busy_timeout first
(confirmed empirically: a 30s-timeout connection blocked ~32s under
sustained contention). This module's connection is opened with a much
shorter busy_timeout (see DEFAULT_MAINTENANCE_BUSY_TIMEOUT_SECONDS,
default 5s) specifically so that worst case is bounded to a few seconds
rather than thirty -- see outbox.connect()'s busy_timeout_seconds
parameter. No component in this codebase normally holds a transaction
open long enough to trigger even that (watcher/uploader/heartbeat reads
are bare autocommit SELECTs; the watcher's own write transactions are
short), so in practice this is a rare-contention safety bound, not a
routine cost.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import outbox
from .logger_config import get_logger

logger = get_logger("maintenance")

# Literal fallbacks -- kept separate from the DEFAULT_* names below so
# the resolver functions have a fixed, non-circular value to fall back
# to (DEFAULT_* is the *result* of resolving against these, not the
# other way around).
_FALLBACK_RETENTION_DAYS = 7
_FALLBACK_PRUNE_BATCH_SIZE = 1000
_FALLBACK_MAX_BATCHES_PER_CYCLE = 10
_FALLBACK_MAINTENANCE_INTERVAL_SECONDS = 3600.0
_FALLBACK_MAX_INCREMENTAL_VACUUM_PAGES = 1000

_MAX_ERROR_LENGTH = 500


def _resolve_positive_int(
    raw_value: str | None, *, fallback: int, env_var_name: str, unit: str
) -> tuple[int, str | None]:
    """Shared validation shape for every Phase 5 config knob below: never
    raises, never accepts zero or negative (a 0-second interval or a
    0-row batch size is exactly the kind of "unusable tight loop"
    footgun Phase 4's resolve_refresh_interval_seconds already guards
    against for the dashboard -- same principle here).
    """
    if raw_value is None or raw_value.strip() == "":
        return fallback, None

    try:
        parsed = int(raw_value.strip())
    except ValueError:
        return (
            fallback,
            f"{env_var_name}={raw_value!r} is not a valid integer -- falling back to {fallback} {unit}.",
        )

    if parsed <= 0:
        return (
            fallback,
            f"{env_var_name}={raw_value!r} must be positive -- falling back to {fallback} {unit}.",
        )

    return parsed, None


def resolve_retention_days(raw_value: str | None) -> tuple[int, str | None]:
    """Zero/negative/non-integer falls back to 7 days -- 0 is
    deliberately never treated as "delete everything immediately"."""
    return _resolve_positive_int(
        raw_value,
        fallback=_FALLBACK_RETENTION_DAYS,
        env_var_name="SORTVIEW_OUTBOX_DELIVERED_RETENTION_DAYS",
        unit="day(s)",
    )


def resolve_prune_batch_size(raw_value: str | None) -> tuple[int, str | None]:
    return _resolve_positive_int(
        raw_value,
        fallback=_FALLBACK_PRUNE_BATCH_SIZE,
        env_var_name="SORTVIEW_OUTBOX_PRUNE_BATCH_SIZE",
        unit="row(s) per batch",
    )


def resolve_max_batches_per_cycle(raw_value: str | None) -> tuple[int, str | None]:
    return _resolve_positive_int(
        raw_value,
        fallback=_FALLBACK_MAX_BATCHES_PER_CYCLE,
        env_var_name="SORTVIEW_OUTBOX_MAINTENANCE_MAX_BATCHES_PER_CYCLE",
        unit="batch(es) per cycle",
    )


def resolve_maintenance_interval_seconds(raw_value: str | None) -> tuple[float, str | None]:
    seconds, warning = _resolve_positive_int(
        raw_value,
        fallback=int(_FALLBACK_MAINTENANCE_INTERVAL_SECONDS),
        env_var_name="SORTVIEW_OUTBOX_MAINTENANCE_INTERVAL_SECONDS",
        unit="second(s)",
    )
    return float(seconds), warning


def resolve_max_incremental_vacuum_pages(raw_value: str | None) -> tuple[int, str | None]:
    return _resolve_positive_int(
        raw_value,
        fallback=_FALLBACK_MAX_INCREMENTAL_VACUUM_PAGES,
        env_var_name="SORTVIEW_OUTBOX_MAX_INCREMENTAL_VACUUM_PAGES",
        unit="page(s)",
    )


def _resolved(resolver: Any, env_var_name: str) -> Any:
    value, warning = resolver(os.getenv(env_var_name))
    if warning:
        logger.warning(warning)
    return value


DEFAULT_RETENTION_DAYS = _resolved(resolve_retention_days, "SORTVIEW_OUTBOX_DELIVERED_RETENTION_DAYS")
DEFAULT_PRUNE_BATCH_SIZE = _resolved(resolve_prune_batch_size, "SORTVIEW_OUTBOX_PRUNE_BATCH_SIZE")
# Caps how many prune_delivered_events batches one maintenance cycle may
# run, so an extremely large eligible backlog (e.g. after a long outage
# followed by recovery, or a first Phase 5 deploy against months of
# unpruned history) can't turn one cycle into an unbounded amount of
# DELETE work. 10 batches x the 1000-row default batch size = up to
# 10,000 rows pruned per cycle -- at the default hourly cadence, a
# backlog far larger than that still drains within a few hours rather
# than in one long-running cycle that could hold locks against the
# watcher/uploader for an extended stretch.
DEFAULT_MAX_BATCHES_PER_CYCLE = _resolved(
    resolve_max_batches_per_cycle, "SORTVIEW_OUTBOX_MAINTENANCE_MAX_BATCHES_PER_CYCLE"
)
# Once per hour by default -- deliberately far coarser than the
# uploader's 2-5s poll cadence. Retention is measured in days, so
# sub-minute maintenance precision buys nothing; an hourly cadence keeps
# the WAL file bounded and the eligible backlog from growing unbounded
# between cycles without adding meaningful background load.
DEFAULT_MAINTENANCE_INTERVAL_SECONDS = _resolved(
    resolve_maintenance_interval_seconds, "SORTVIEW_OUTBOX_MAINTENANCE_INTERVAL_SECONDS"
)
# Bounds one incremental_vacuum call so reclaiming a large freelist can't
# turn into a single long-running rewrite -- same "bounded work per
# cycle" principle as the prune batching above. Only ever used when
# outbox.get_auto_vacuum_mode(conn) == "incremental" (new databases).
DEFAULT_MAX_INCREMENTAL_VACUUM_PAGES = _resolved(
    resolve_max_incremental_vacuum_pages, "SORTVIEW_OUTBOX_MAX_INCREMENTAL_VACUUM_PAGES"
)

DEFAULT_BACKOFF_BASE_SECONDS = float(os.getenv("SORTVIEW_OUTBOX_MAINTENANCE_BACKOFF_BASE_SECONDS", "60"))
DEFAULT_BACKOFF_MAX_SECONDS = float(os.getenv("SORTVIEW_OUTBOX_MAINTENANCE_BACKOFF_MAX_SECONDS", "3600"))
DEFAULT_BACKOFF_MULTIPLIER = float(os.getenv("SORTVIEW_OUTBOX_MAINTENANCE_BACKOFF_MULTIPLIER", "2"))

# Deliberately much shorter than outbox.connect()'s default 30s -- see
# the module docstring above and outbox.checkpoint_wal's docstring for
# why: a checkpoint under contention blocks the calling thread for up to
# the connection's own busy_timeout, not just up to some instant "busy"
# return. This connection is only ever used for maintenance work (prune
# deletes, incremental_vacuum, wal_checkpoint), none of which is
# time-critical -- failing fast and retrying next cycle is preferable to
# blocking for up to 30s.
DEFAULT_MAINTENANCE_BUSY_TIMEOUT_SECONDS = float(
    os.getenv("SORTVIEW_OUTBOX_MAINTENANCE_BUSY_TIMEOUT_SECONDS", "5")
)

# A simple non-reentrant guard: run_maintenance_cycle is only ever meant
# to be driven by one run_forever loop, but this makes "never run
# concurrently with itself" an enforced property rather than an
# assumption -- e.g. if something later also triggers a manual/one-off
# maintenance run alongside the periodic loop.
_maintenance_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(UTC)


def _retention_cutoff_iso(retention_days: int, now: datetime | None = None) -> str:
    now = now or _now()
    return (now - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class MaintenanceResult:
    success: bool
    rows_pruned: int = 0
    batches_processed: int = 0
    pending_count: int | None = None
    delivered_count: int | None = None
    quarantined_count: int | None = None
    eligible_for_prune_before: int | None = None
    eligible_for_prune_after: int | None = None
    auto_vacuum_mode: str | None = None
    page_count_before: int | None = None
    page_count_after: int | None = None
    freelist_count_before: int | None = None
    freelist_count_after: int | None = None
    checkpoint_busy: int | None = None
    checkpoint_log_frames: int | None = None
    checkpoint_checkpointed_frames: int | None = None
    incremental_vacuum_ran: bool = False
    skipped_reason: str | None = None
    error: str | None = None


def _sanitize_error(exc: Exception) -> str:
    """Bounded, type-prefixed error text -- never the raw exception
    object (which could in principle carry a DB path or other local
    detail), and never anything network/token-related since this module
    makes no HTTP calls at all."""
    text = f"{type(exc).__name__}: {exc}"
    if len(text) <= _MAX_ERROR_LENGTH:
        return text
    return text[:_MAX_ERROR_LENGTH] + "...<truncated>"


def run_maintenance_cycle(
    conn: Any,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    batch_size: int = DEFAULT_PRUNE_BATCH_SIZE,
    max_batches_per_cycle: int = DEFAULT_MAX_BATCHES_PER_CYCLE,
    max_incremental_vacuum_pages: int = DEFAULT_MAX_INCREMENTAL_VACUUM_PAGES,
    checkpoint_mode: str = "TRUNCATE",
) -> MaintenanceResult:
    """Runs one bounded maintenance pass: prune eligible delivered rows
    (batched, oldest-first, capped at max_batches_per_cycle batches),
    optionally run a bounded incremental_vacuum, then checkpoint the WAL.

    Never raises -- every failure path is caught and returned as a
    MaintenanceResult with success=False and a sanitized error, so a
    maintenance bug can never propagate up into run_forever's loop (or
    any other caller) and take down watcher/uploader operation with it.

    Not reentrant: if a cycle is already running (only relevant if
    something outside run_forever's own sequential loop calls this),
    returns immediately with success=False and skipped_reason set,
    touching the database not at all.
    """
    if not _maintenance_lock.acquire(blocking=False):
        return MaintenanceResult(success=False, skipped_reason="maintenance cycle already in progress")

    try:
        cutoff = _retention_cutoff_iso(retention_days)

        auto_vacuum_mode = outbox.get_auto_vacuum_mode(conn)
        page_count_before = outbox.get_page_count(conn)
        freelist_count_before = outbox.get_freelist_count(conn)
        eligible_before = outbox.count_eligible_for_prune(conn, cutoff)

        rows_pruned = 0
        batches_processed = 0
        for _ in range(max_batches_per_cycle):
            deleted = outbox.prune_delivered_events(conn, cutoff=cutoff, batch_size=batch_size)
            if deleted == 0:
                break
            rows_pruned += deleted
            batches_processed += 1
            if deleted < batch_size:
                # Fewer than a full batch means nothing more is eligible
                # right now -- no point spending another round trip to
                # find that out again.
                break

        incremental_vacuum_ran = False
        if auto_vacuum_mode == "incremental":
            freelist_after_prune = outbox.get_freelist_count(conn)
            if freelist_after_prune > 0:
                outbox.run_incremental_vacuum(conn, max_incremental_vacuum_pages)
                incremental_vacuum_ran = True

        checkpoint_busy, checkpoint_log_frames, checkpoint_checkpointed_frames = outbox.checkpoint_wal(
            conn, mode=checkpoint_mode
        )
        if checkpoint_busy:
            logger.info(
                "WAL checkpoint did not fully complete this cycle (another connection is "
                "still active) -- normal, will retry next cycle | log_frames=%s checkpointed=%s",
                checkpoint_log_frames,
                checkpoint_checkpointed_frames,
            )

        pending_count = outbox.count_pending_events(conn)
        delivered_count = outbox.count_delivered_events(conn)
        quarantined_count = outbox.count_quarantined_events(conn)
        eligible_after = outbox.count_eligible_for_prune(conn, cutoff)
        page_count_after = outbox.get_page_count(conn)
        freelist_count_after = outbox.get_freelist_count(conn)

        return MaintenanceResult(
            success=True,
            rows_pruned=rows_pruned,
            batches_processed=batches_processed,
            pending_count=pending_count,
            delivered_count=delivered_count,
            quarantined_count=quarantined_count,
            eligible_for_prune_before=eligible_before,
            eligible_for_prune_after=eligible_after,
            auto_vacuum_mode=auto_vacuum_mode,
            page_count_before=page_count_before,
            page_count_after=page_count_after,
            freelist_count_before=freelist_count_before,
            freelist_count_after=freelist_count_after,
            checkpoint_busy=checkpoint_busy,
            checkpoint_log_frames=checkpoint_log_frames,
            checkpoint_checkpointed_frames=checkpoint_checkpointed_frames,
            incremental_vacuum_ran=incremental_vacuum_ran,
        )
    except Exception as exc:  # see docstring: this must never propagate
        logger.exception("Maintenance cycle failed")
        return MaintenanceResult(success=False, error=_sanitize_error(exc))
    finally:
        _maintenance_lock.release()


class Backoff:
    """Same shape as outbox_uploader.Backoff / heartbeat.Backoff --
    duplicated rather than imported to keep this module's only
    cross-component dependency limited to outbox.py and the logger, not
    the network-facing uploader module."""

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


def run_forever(
    conn: Any,
    *,
    interval_seconds: float = DEFAULT_MAINTENANCE_INTERVAL_SECONDS,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    batch_size: int = DEFAULT_PRUNE_BATCH_SIZE,
    max_batches_per_cycle: int = DEFAULT_MAX_BATCHES_PER_CYCLE,
    max_incremental_vacuum_pages: int = DEFAULT_MAX_INCREMENTAL_VACUUM_PAGES,
    backoff: Backoff | None = None,
    sleep_fn: Any = time.sleep,
    max_iterations: int | None = None,
) -> None:
    """Runs the maintenance loop until stopped. Sequential by
    construction -- the next cycle is never started until the previous
    run_maintenance_cycle call has returned, so this loop alone can never
    overlap with itself; the lock in run_maintenance_cycle is defense in
    depth for a caller outside this loop.

    A failed cycle backs off (bounded, default base 60s / max 3600s)
    rather than retrying at the normal interval -- this only governs how
    often *this loop* retries after a failure; it never touches the
    outbox contents beyond what run_maintenance_cycle itself already did
    (or didn't) do, and it never affects watcher/uploader/heartbeat
    behavior in any way.

    backoff/sleep_fn/max_iterations exist for tests -- production use
    leaves backoff as a fresh Backoff() and max_iterations as None.
    """
    active_backoff = backoff if backoff is not None else Backoff()
    iterations = 0

    while max_iterations is None or iterations < max_iterations:
        try:
            result = run_maintenance_cycle(
                conn,
                retention_days=retention_days,
                batch_size=batch_size,
                max_batches_per_cycle=max_batches_per_cycle,
                max_incremental_vacuum_pages=max_incremental_vacuum_pages,
            )
        except Exception:
            # run_maintenance_cycle already catches everything it can and
            # returns a MaintenanceResult -- this is a last-resort guard
            # so a bug in this loop itself still can't take down
            # watcher/uploader/heartbeat operation.
            logger.exception("Unexpected error in maintenance loop -- continuing")
            result = None

        if result is not None and result.success:
            active_backoff.reset()
            delay = interval_seconds
            logger.info(
                "Maintenance cycle complete | rows_pruned=%s batches=%s auto_vacuum=%s "
                "checkpoint_busy=%s pending=%s delivered=%s quarantined=%s",
                result.rows_pruned,
                result.batches_processed,
                result.auto_vacuum_mode,
                result.checkpoint_busy,
                result.pending_count,
                result.delivered_count,
                result.quarantined_count,
            )
        elif result is not None and result.skipped_reason:
            logger.info("Maintenance cycle skipped | reason=%s", result.skipped_reason)
            delay = interval_seconds
        else:
            error_text = result.error if result is not None else "unexpected loop error"
            logger.warning("Maintenance cycle failed | error=%s", error_text)
            delay = active_backoff.next_delay()

        iterations += 1

        if max_iterations is None or iterations < max_iterations:
            sleep_fn(delay)


if __name__ == "__main__":
    # Relative imports throughout this module mean it must be run as
    # part of the agent package -- `python -m agent.maintenance` from
    # the repo/install root, not `python maintenance.py` from inside
    # agent/.
    from pathlib import Path

    db_conn = outbox.connect(
        Path("data") / "sortview_agent.db",
        busy_timeout_seconds=DEFAULT_MAINTENANCE_BUSY_TIMEOUT_SECONDS,
    )

    logger.info(
        "Maintenance starting | interval=%ss retention_days=%s batch_size=%s max_batches_per_cycle=%s",
        DEFAULT_MAINTENANCE_INTERVAL_SECONDS,
        DEFAULT_RETENTION_DAYS,
        DEFAULT_PRUNE_BATCH_SIZE,
        DEFAULT_MAX_BATCHES_PER_CYCLE,
    )

    run_forever(db_conn)
