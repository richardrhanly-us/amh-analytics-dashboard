"""Canonical continuous agent entry point (Continuous Ingestion Phase F).

Run as: python -m agent.main --config PATH

This is the ONE canonical process for the new continuous runtime -- not
agent/run_pipeline.py, which remains in this repository only as a
LEGACY / VALIDATION-ONLY scheduled-style path (a local mirror of the
deployed baseline's logic, useful for side-by-side comparison during
later validation), never the actual production-authoritative agent --
that role belongs to the archived deployed snapshot under
"agent/SortViewAgent - What is currently sitting on the AMH computer/",
which is what actually runs on the real AMH machine via Task Scheduler
today, unchanged by any of this work. See agent/README.md for the full
picture of what runs where. The experimental SQLite continuous-ingestion
path this module supersedes (agent/outbox_uploader.py and friends) has
been removed from the repository entirely -- see agent/runtime/__init__.py.
See agent/runtime/supervisor.py for the AgentRunner this wires up.

Not the repository's root main.py -- that is the unrelated FastAPI
backend entry point. Two different processes, two different deployments,
sharing a name only by coincidence of both being "the main module" for
their own side.

Signal handling: SIGINT/SIGTERM only SET the shared stop event (kept
minimal and safe to run inside a signal handler); the actual shutdown
sequence (joining every component thread, bounded by a timeout) runs on
the main thread after it wakes from waiting on that event -- never inside
the handler itself.

EXIT CODE CONTRACT (fail-fast production-supervision correction):
  0  -- clean run: either a config/argument error was never reached
        (normal case), or the process ran and shut down because
        stop_event was set by an intentional SIGINT/SIGTERM.
  1  -- an unhandled exception escaped one of the four worker threads
        (collector/uploader/heartbeat/housekeeping) during this run --
        see AgentRunner._handle_worker_crash in
        agent/runtime/supervisor.py. The crashing worker's own
        stop_event.set() call is what wakes THIS wait() below; main()
        distinguishes crash from clean shutdown by checking
        AgentRunner.first_failure() (never set on a clean shutdown), not
        by stop_event's state, since both cases set the same Event.
  2  -- config error (--config missing/invalid, SORTVIEW_API_TOKEN
        unset, etc.) -- caught before AgentRunner is even constructed.

This is what lets an external process supervisor (Windows Task
Scheduler's "restart if the task fails" setting, or later Windows
Service Recovery) detect a worker crash and restart the whole process --
it depends on the PROCESS actually exiting non-zero, not merely on an
internal log line.
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Sequence

from .runtime.config import ConfigError, load_runtime_config
from .runtime.supervisor import AgentRunner


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SortView canonical continuous agent")
    parser.add_argument("--config", required=True, help="Path to the runtime config JSON file")
    parser.add_argument(
        "--shutdown-timeout", type=float, default=30.0, help="Seconds to wait for clean shutdown"
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_runtime_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    runner = AgentRunner(cfg)
    runner.start()

    def _handle_signal(signum: int, frame: object) -> None:
        runner.stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    runner.stop_event.wait()
    runner.stop(timeout=args.shutdown_timeout)

    # runner.stop_event is set either by the signal handler above (an
    # intentional, operator-requested shutdown) or by a worker crash (see
    # agent/runtime/supervisor.py's AgentRunner._handle_worker_crash) --
    # the two are indistinguishable from stop_event alone, which is why
    # AgentRunner.first_failure() (set ONLY on a real crash, never on a
    # clean stop_event.wait() return) is the exit-code decision, not
    # stop_event's own state.
    failure = runner.first_failure()
    if failure is not None:
        component, error = failure
        print(
            f"SortView agent exiting non-zero -- component {component!r} crashed: {error}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
