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

FAILURE ISOLATION -> FAIL-FAST (production-cutover correction, replacing
the policy below): every component's loop still runs inside
_run_guarded, which catches any exception escaping it and logs it -- but
now, instead of merely recording the failure and letting that ONE thread
quietly end while siblings keep going, _run_guarded calls
_handle_worker_crash, which:

  1. records the failure on RuntimeStatus (unchanged -- this is what
     heartbeat.compute_health_snapshot already folds into
     health_status="degraded", and what diagnostics.json already
     reflects via Housekeeping's normal cycle, if it gets one first);
  2. records WHICH component failed and its error message as this
     AgentRunner's _first_failure, exactly once -- a second/third
     component failing (e.g. during the shutdown this first failure
     itself triggers) is still individually recorded on RuntimeStatus,
     but never overwrites the original root cause;
  3. makes a best-effort, exception-swallowed attempt to write ONE
     diagnostics.json snapshot reflecting the failure immediately -- a
     local, synchronous file write, not a network call, so there is no
     shutdown-delay or backend-availability risk in attempting it (see
     agent/runtime/heartbeat.py's docstring and this module's own SHUTDOWN
     section below for why a HEARTBEAT is deliberately NOT given the same
     guarantee);
  4. sets self.stop_event.

Setting stop_event is the entire fail-fast trigger: every sibling loop
already checks it every iteration (see SHUTDOWN below) and returns
cleanly on its own -- _handle_worker_crash never calls AgentRunner.stop()
itself (that would try to join the very thread calling it, from inside
itself) and never calls os._exit/sys.exit directly (see agent/main.py:
the single call site that owns process exit is main(), not a worker
thread, so a crash on any worker propagates through the SAME
stop_event.wait() / AgentRunner.stop() path an operator-requested
shutdown already uses -- the two cases are only told apart afterward, by
whether first_failure() is None).

Deliberately does NOT auto-restart a crashed component itself.
Restart-with-backoff/flapping-detection for an INDIVIDUAL thread is real
complexity "keep it boring" argues against for this phase -- recovery is
now the WHOLE PROCESS restarting (see PRODUCTION SUPERVISION POLICY
below), which reuses machinery already proven in this phase's own tests
(test_restart_resumes_from_persisted_state_and_pending_spool) rather than
inventing a second, thread-level recovery mechanism alongside it.

PRODUCTION SUPERVISION POLICY -- IMPLEMENTED (previously recorded here as
a decision deferred until Windows Service/Scheduled Task packaging;
implemented now that that packaging exists -- see
docs/amh-production-cutover-runbook.md). Policy chosen: FAIL-FAST. An
unexpected critical worker death -> log it and record diagnostics (see
FAILURE ISOLATION above) -> stop_event set -> sibling workers wind down
via their own existing stop_event checks -> AgentRunner.stop() (called
from agent/main.py's main(), already implemented, already bounded by a
timeout) joins everyone -> main() observes runner.first_failure() is not
None -> the PROCESS exits with a non-zero code -> Windows Task
Scheduler's restart-on-failure (or, later, Windows Service Recovery)
restarts the ENTIRE agent process, which re-bootstraps every component
from durably persisted state/spool, exactly like any other clean restart.

A BOUNDED WORKER AUTO-RESTART policy (catch a component death and retry
just that one thread, with a capped budget) was considered and rejected
for the same reason recorded here previously: real added complexity
(flapping detection, a restart budget, whether restarting a collector
mid-generation is even safe without re-deriving Supervisor state) for a
failure mode expected to be rare, when a working bounded-restart policy
still needs a "give up and exit" fallback once its budget is exhausted --
which is just fail-fast with extra steps in between.

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
from .housekeeping import Housekeeping, write_diagnostics_snapshot
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

        # Fail-fast root cause: the FIRST component crash's (name, error)
        # pair, set at most once -- see _handle_worker_crash and this
        # module's docstring's FAILURE ISOLATION section. None means no
        # component has crashed (which is also true after a normal,
        # intentionally-requested shutdown -- a clean stop_event.wait()
        # return never touches this).
        self._first_failure: tuple[str, str] | None = None
        self._failure_lock = threading.Lock()

    # --- guarded loop wrapper -------------------------------------------

    def _run_guarded(self, name: str, loop_fn: Any) -> None:
        self.status.record_component_started(name)
        try:
            loop_fn()
        except Exception as exc:
            self._handle_worker_crash(name, exc)

    def _handle_worker_crash(self, name: str, exc: Exception) -> None:
        """Called synchronously from _run_guarded's except block (so
        sys.exc_info() -- and therefore self.logger.exception's traceback
        -- is still valid here) for exactly one worker's crash.

        Ordered by EXPLICIT priority, not just program convenience: (1)
        fail correctly / let siblings start stopping, (2) record local
        diagnostics, (3) [never: a forced final heartbeat -- see
        agent/runtime/heartbeat.py's docstring]. Recording status and
        setting stop_event happen FIRST and are never delayed by, or
        allowed to fail because of, the diagnostics write below -- that
        write is wrapped in its own try/except specifically so a local
        I/O failure there can never prevent (or even delay) the fail-fast
        trigger.
        """
        error_text = str(exc)
        self.logger.exception("Component %s crashed", name)
        self.status.record_component_failed(name, error_text)

        with self._failure_lock:
            if self._first_failure is None:
                self._first_failure = (name, error_text)

        # THE fail-fast trigger -- set BEFORE the diagnostics write below,
        # so siblings start winding down immediately rather than waiting
        # on a local file write first. Thread-safe from any thread
        # (that's the whole contract of threading.Event); never calls
        # self.stop() or sys.exit/os._exit directly from here -- see this
        # module's docstring for why (self-join risk; main() owns process
        # exit).
        self.stop_event.set()

        try:
            write_diagnostics_snapshot(
                self.cfg,
                self.status,
                agent_id=self.agent_id,
                agent_version=self.cfg.agent_version,
                started_at=self.started_at,
            )
        except Exception:
            # Best-effort only -- see this method's docstring. A failure
            # writing diagnostics must never prevent fail-fast shutdown,
            # which has already been triggered by this point regardless.
            self.logger.exception(
                "Failed to write diagnostics snapshot while handling %s crash", name
            )

    def first_failure(self) -> tuple[str, str] | None:
        """(component_name, error_message) for the first component that
        crashed this run, or None if none has (including after a normal,
        intentionally-requested shutdown). This is what agent/main.py's
        main() checks, AFTER AgentRunner.stop() has finished joining
        every thread, to decide the process's exit code -- see this
        module's docstring's PRODUCTION SUPERVISION POLICY section."""
        with self._failure_lock:
            return self._first_failure

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
