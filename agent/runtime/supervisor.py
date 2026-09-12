"""AgentRunner / Supervisor (Continuous Ingestion Phase F).

    AgentRunner
      -> collector    (one loop, all configured sources, round-robin)
      -> uploader
      -> heartbeat
      -> housekeeping

CONCURRENCY MODEL: plain OS threads, one per component, coordinated by
one shared threading.Event for shutdown. Chosen over asyncio because
nothing in this codebase's dependency stack is async (requests, not
httpx/aiohttp; tailer/state/spool all do blocking local file I/O) --
adopting an event loop would mean wrapping every blocking call in
run_in_executor anyway (no real benefit) or a much larger rewrite for no
behavioral gain. Chosen over a process-per-component design because
nothing here needs process isolation -- these are lightweight,
mostly-I/O-bound loops sharing simple, already-thread-safe state
(agent.runtime.status.RuntimeStatus's internal lock), not CPU-bound work
that would benefit from separate processes. Four threads with one shared
stop Event is the simplest model that satisfies "one component failure
does not silently kill unrelated work" without building a supervision
framework -- deliberately not a goal here.

FAILURE ISOLATION: every component's loop runs inside _run_guarded,
which catches any exception escaping it, logs it, and records it on
RuntimeStatus -- then lets that ONE thread end. It never calls
os._exit/sys.exit and never touches sibling threads. A crashed component
is visible (health(), and reflected in diagnostics.json via
Housekeeping) but does not stop already-running siblings.

Deliberately does NOT auto-restart a crashed component. Restart-with-
backoff/flapping-detection is real complexity "keep it boring" argues
against for this phase -- a crashed component is a visible, actionable
signal (checked EXPLICITLY by agent.runtime.heartbeat.compute_health_snapshot,
which folds any dead component into health_status="degraded" -- see that
module's docstring for why a dead collector in particular would otherwise
be invisible from spool/state content alone -- and always reflected in
diagnostics.json), not something silently papered over by respawning.

PRODUCTION SUPERVISION POLICY -- recorded now, NOT implemented in this
pass; this process (agent/main.py) still just waits on stop_event
indefinitely regardless of component deaths, exactly as before this
correction. The problem: today, if a critical worker thread (most
importantly the collector) dies, the three siblings keep running, the
process keeps running, and nothing here makes the OS-level process exit.
If this is ever wrapped in a Windows Service, Service Recovery can only
act on the PROCESS dying -- a process that stays alive forever with one
permanently-dead internal thread would never trigger a restart, even
though it has silently stopped doing its job (a dead collector, for
example, means new AMH events are never captured again, indefinitely,
with only the heartbeat's health_status="degraded" as the outward signal
-- and if it's the heartbeat thread itself that dies, not even that).

Two policies were evaluated for what should happen next, once this is
actually implemented ahead of Windows Service packaging:

  1. FAIL-FAST (RECOMMENDED): an unexpected critical worker death ->
     log it and record diagnostics (already happens today) -> stop the
     sibling workers cleanly (AgentRunner.stop(), already implemented)
     -> the process exits with a non-zero code -> Windows Service
     Recovery restarts the ENTIRE agent process, which re-bootstraps
     every component from durably persisted state/spool, exactly like
     any other clean restart already proven in this phase's tests
     (test_restart_resumes_from_persisted_state_and_pending_spool).
     Simple, well-understood, and reuses a recovery mechanism (process
     restart) that already exists and is already exercised -- Windows
     Service Recovery options (restart after N seconds, with a reset-
     failure-count window) are exactly designed for this shape of
     failure and need no new code here to work correctly.
  2. BOUNDED WORKER AUTO-RESTART: catch a component death and restart
     just that one thread, with a capped retry count and backoff.
     Rejected for this phase: it adds real complexity (flapping
     detection, a restart budget, deciding whether restarting a
     collector mid-generation is even safe without re-deriving
     Supervisor state) for a failure mode that should be RARE in
     practice (these loops are simple and already extensively tested) --
     and even a working bounded-restart policy still needs a fallback
     "give up and exit" path once the budget is exhausted, which is
     just policy 1 with extra steps in between.

RECOMMENDATION: fail-fast (policy 1) is the simplest reliable choice and
should be implemented as a small, explicit change (each component's
crash path calls a Supervisor method that stops siblings and calls
sys.exit(1) instead of merely recording the failure) BEFORE this runtime
is packaged as a Windows Service -- not before, since without a Service
Recovery policy configured, a bare process exit is strictly worse than
the current "stays up, visibly degraded" behavior for a human operator
watching it run interactively. Tracked here explicitly so this analysis
doesn't need to be redone at packaging time.

SHUTDOWN: stop() sets the shared Event. Every loop's sleep is
stop_event.wait(delay), which returns immediately once the event is set,
so a component blocked in a poll/backoff sleep wakes and exits within
one loop iteration rather than the full delay. The collector additionally
force-flushes any buffered-but-unspooled events before its thread exits
(SourceCollector.force_flush) so a clean shutdown never discards
already-read data -- it is either durably spooled by the flush, or (if
even that can't complete) safely left to be reread next startup, the
same crash-safety argument used everywhere else in this runtime.
stop(timeout=...) bounds how long shutdown waits for every thread to
actually exit.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from .. import identity
from .collector import CollectorCycleReport, SourceCollector
from .config import RuntimeConfig
from .heartbeat import Heartbeat
from .housekeeping import Housekeeping
from .http_client import build_session
from .logging_setup import configure_logging, get_component_logger
from .status import RuntimeStatus
from .uploader import Uploader


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class AgentRunner:
    def __init__(self, cfg: RuntimeConfig) -> None:
        self.cfg = cfg
        self.status = RuntimeStatus()
        self.stop_event = threading.Event()
        self.started_at = _now_iso()

        configure_logging(cfg.log_dir)
        self.logger = get_component_logger("supervisor")

        self.agent_id = identity.load_or_create_agent_id(cfg.agent_identity_path)
        self.session = build_session(
            transport_retry_total=cfg.http_transport_retry_total,
            transport_backoff_factor=cfg.http_transport_backoff_factor,
        )

        self.collectors: dict[str, SourceCollector] = {
            source_cfg.name: SourceCollector(
                source_cfg,
                agent_id=self.agent_id,
                customer_id=cfg.customer_id,
                branch_id=cfg.branch_id,
                state_path=cfg.state_path,
                spool_root=cfg.spool_root,
                batch_max_events=cfg.batch_max_events,
                batch_max_seconds=cfg.batch_max_seconds,
            )
            for source_cfg in cfg.sources
        }
        self.uploader = Uploader(cfg, self.session, get_component_logger("uploader"))
        self.heartbeat = Heartbeat(cfg, self.session, self.status, get_component_logger("heartbeat"))
        self.housekeeping = Housekeeping(
            cfg,
            self.status,
            get_component_logger("housekeeping"),
            agent_id=self.agent_id,
            agent_version=cfg.agent_version,
            started_at=self.started_at,
        )

        self._threads: list[threading.Thread] = []

    # --- guarded loop wrapper -------------------------------------------

    def _run_guarded(self, name: str, loop_fn: Any) -> None:
        self.status.record_component_started(name)
        try:
            loop_fn()
        except Exception as exc:
            self.logger.exception("Component %s crashed", name)
            self.status.record_component_failed(name, str(exc))

    # --- collector loop --------------------------------------------------

    def _run_one_collector_cycle(self, logger: Any) -> None:
        """Runs one full collector pass and, unconditionally as the very
        last thing this method does (whether the pass actually read
        anything, was skipped entirely due to disk pressure, or hit a
        per-source error), advances
        RuntimeStatus.record_collector_cycle_completed()'s counter.

        That counter -- not a sleep duration -- is what lets a test prove
        "at least one full collector cycle ran after point X" deterministically:
        wait for the count to exceed a baseline captured at or after X,
        rather than guessing how long a cycle might take. See
        tests/test_runtime_supervisor.py's disk-pressure test.
        """
        try:
            if self.status.is_disk_pressure_paused():
                logger.debug("Collection paused (disk pressure)")
                return

            for name, collector in self.collectors.items():
                try:
                    report: CollectorCycleReport = collector.poll_once()
                except Exception as exc:
                    logger.exception("Collector cycle failed | source=%s", name)
                    self.status.record_collector_error(f"{name}: {exc}")
                    continue

                self.status.set_source_missing(name, report.source_missing)
                if not report.source_missing:
                    self.status.record_collector_active()
                if report.events_flushed:
                    self.status.record_spool_write()
                self.status.set_source_position(
                    name, generation=collector.generation, offset=collector.current_offset
                )
        finally:
            self.status.record_collector_cycle_completed()

    def _collector_loop(self) -> None:
        logger = get_component_logger("collector")
        while not self.stop_event.is_set():
            self._run_one_collector_cycle(logger)
            if self.stop_event.wait(self.cfg.collector_poll_seconds):
                break

        for name, collector in self.collectors.items():
            try:
                collector.force_flush()
            except Exception:
                logger.exception("Force-flush during shutdown failed | source=%s", name)

    # --- upload-result -> status bridge -------------------------------------

    def _uploader_run_forever_with_status(self) -> None:
        """Thin wrapper around Uploader.run_forever that also updates
        RuntimeStatus after each cycle -- kept here (not inside
        agent.runtime.uploader.Uploader itself) so that module's own
        tests never need to know about RuntimeStatus at all."""
        from .backoff import Backoff

        if not self.cfg.upload_enabled:
            self.uploader.logger.info(
                "Uploader starting in SHADOW MODE (upload_enabled=false) -- "
                "spool will accumulate, no /upload requests will be sent"
            )

        backoff = Backoff(
            base_seconds=self.cfg.uploader_backoff_base_seconds,
            max_seconds=self.cfg.uploader_backoff_max_seconds,
            multiplier=self.cfg.uploader_backoff_multiplier,
        )

        while not self.stop_event.is_set():
            results = self.uploader.run_cycle()

            for result in results:
                if result.made_progress and result.category is None:
                    self.status.record_upload_success()
                if result.category is not None:
                    self.status.record_upload_failure(
                        result.category.value if hasattr(result.category, "value") else str(result.category),
                        result.last_error,
                    )

            if not results:
                delay = self.cfg.uploader_poll_seconds
            elif any(r.isolation_budget_exhausted for r in results):
                delay = backoff.next_delay()
            elif any(r.made_progress for r in results):
                backoff.reset()
                delay = self.cfg.uploader_poll_seconds
            else:
                delay = backoff.next_delay()

            if self.stop_event.wait(delay):
                return

    # --- start/stop --------------------------------------------------------

    def start(self) -> None:
        self.logger.info(
            "SortView agent starting | agent_id=%s version=%s customer_id=%s branch_id=%s sources=%s",
            self.agent_id,
            self.cfg.agent_version,
            self.cfg.customer_id,
            self.cfg.branch_id,
            [s.name for s in self.cfg.sources],
        )
        # Loud, impossible-to-miss mode banner -- a person scanning
        # startup logs must immediately know whether this process can
        # write to production. See RuntimeConfig.mode_description.
        self.logger.info("=" * 78)
        self.logger.info("MODE: %s", self.cfg.mode_description)
        self.logger.info("=" * 78)

        components: dict[str, Any] = {
            "collector": self._collector_loop,
            "uploader": self._uploader_run_forever_with_status,
            "heartbeat": lambda: self.heartbeat.run_forever(stop_event=self.stop_event),
            "housekeeping": lambda: self.housekeeping.run_forever(stop_event=self.stop_event),
        }

        for name, fn in components.items():
            thread = threading.Thread(target=self._run_guarded, args=(name, fn), name=f"sortview-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, *, timeout: float = 30.0) -> None:
        self.logger.info("SortView agent stopping")
        self.stop_event.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)

        still_running = [t.name for t in self._threads if t.is_alive()]
        if still_running:
            self.logger.warning("Shutdown timed out waiting for: %s", still_running)
        else:
            self.logger.info("SortView agent stopped cleanly")

    def health(self) -> dict[str, Any]:
        return self.status.component_health()
