"""One-shot orchestration entry point (Phase 4a).

    read config -> for each source: read new bytes (collector/reader.py)
    -> parse (SEE PARSER SEAM below) -> upload (collector/uploader.py)
    -> persist state ONLY IF every batch succeeded (collector/state.py)
    -> write + best-effort POST status -> exit

PARSER SEAM -- now wired to the real Tech Logic parsers (parser-parity
wiring phase), via a thin adapter, not a copy. run_once() still accepts
`parse_fns` as an explicit parameter: {source_name: (lines) -> list[dict]}
-- that seam itself is unchanged. What changed is what main() passes for
it: collector/parsers.py's build_production_parse_fns(customer_id=...,
branch_id=...) returns one adapter per source, each of which calls the
UNCHANGED agent/parser/{checkins,rejects,acs}.py modules (the same
canonical parsers the continuous agent uses) and maps their output into
the exact dict shape collector/uploader.py sends to POST /upload -- see
collector/parsers.py's own module docstring for the full field mapping,
the malformed-timestamp behavior (verified against agent/uploader.py,
not invented), and why it does not import agent/uploader.py itself.
This module still imports nothing from agent/parser/* or agent/uploader.py
directly -- only collector/parsers.py does.

FAIL CLOSED, NOT OPEN, remains the structural default for run_once()
itself: it still requires every configured source to have an entry in
parse_fns, and still raises ParserNotConfiguredError immediately (before
reading any source file or making any network call) if one is missing --
this protects against a future misconfigured/misnamed source, not just
the now-resolved "no parser wired at all" case. _passthrough_parse
remains UNCHANGED and still available only for tests that inject it into
parse_fns on purpose (see tests/test_collector_run.py) -- it is never
reachable from main()'s own production path, which now calls
collector.parsers.build_production_parse_fns() instead.

MULTI-BATCH STATE SEMANTICS (Phase 3 item 7, tested explicitly below and
in tests/test_collector_run.py): state for ALL sources is persisted
together, ONCE, only after collector.uploader.upload_records reports
every batch succeeded. If any batch fails, state is not touched at all --
not even for sources whose own batches happened to succeed before a
later source's batch failed. The next run re-reads from the same
previously-committed offsets and re-sends everything, which the backend's
existing semantic-key dedup absorbs safely. This is a deliberate
simplification (matching the legacy pipeline's own proven behavior, not
a per-source independent commit scheme) -- see the Phase 3 gap audit for
why per-run-atomic was judged sufficient.

FATAL FAILURES EXIT NONZERO: main() is the single place that decides the
process exit code. A config error exits 2 (matches the continuous
agent's own convention, agent/main.py). Anything else that prevents a
normal "completed" or "failed-cleanly" run -- including a bug this
module didn't anticipate -- is caught at the top of main() and exits 1.
A cleanly-detected upload failure (run_once returning exit_code=1 on its
own, having already logged and written status) is not treated any
differently from an unexpected crash at this level -- both produce the
same nonzero signal.

RECOVERY MODEL (corrected, Phase 4d Section H -- live-tested on LIB-L26,
not assumed): a nonzero exit here does NOT get retried by Task
Scheduler's RestartOnFailure setting -- isolated live testing proved
that setting does not activate for a process that starts and exits
cleanly with a nonzero code (tested for both exit 1 and exit 2; no
restart fired within its configured 5-minute interval in either case).
The setting is left configured (harmless, matches legacy production's
own value) but is NOT relied on for resilience. The actual recovery
mechanism is simpler and already correct without it: state is only
persisted on a fully successful run (see MULTI-BATCH STATE SEMANTICS
above), so a nonzero exit leaves the prior committed offsets untouched,
and the NEXT NORMAL 15-MINUTE SCHEDULED RUN re-reads and re-sends the
same uncommitted work -- no custom retry wrapper, supervisor, or daemon
is needed or planned for this.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import parsers, reader, state, uploader
from .config import CollectorConfig, ConfigError, load_config

ParseFn = Callable[[list[str]], list[dict[str, Any]]]


class ParserNotConfiguredError(Exception):
    """Raised by run_once when one or more configured sources have no
    entry in parse_fns. See module docstring's FAIL CLOSED section --
    this is what keeps an ordinary production CLI invocation from ever
    silently processing/uploading unparsed raw lines, e.g. if a config's
    `sources` names something other than checkins/rejects/acs (the three
    collector.parsers.build_production_parse_fns() actually provides)."""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _cursor_from_source_state(source_state: state.SourceState | None) -> reader.SourceCursor | None:
    """collector/state.py stores identity as a plain (int, int) tuple
    (or None) -- collector/reader.py's SourceCursor wants a FileIdentity
    wrapper. This is the one place that conversion happens, so it can
    never be silently skipped/forgotten at a call site (an earlier
    version of this module did exactly that, which would have compared
    a FileIdentity to a raw tuple on every second run -- always unequal
    -- and falsely detected a rotation on every single run after the
    first; caught by this phase's own tests before it ever shipped)."""
    if source_state is None:
        return None
    identity = reader.FileIdentity(token=source_state.identity) if source_state.identity is not None else None
    return reader.SourceCursor(identity=identity, offset=source_state.offset)


def _source_state_from_cursor(cursor: reader.SourceCursor) -> state.SourceState:
    """The reverse conversion -- see _cursor_from_source_state."""
    identity = cursor.identity.token if cursor.identity is not None else None
    return state.SourceState(identity=identity, offset=cursor.offset)


def _passthrough_parse(lines: list[str]) -> list[dict[str, Any]]:
    """NOT a production parser, and never wired in as main()'s default --
    see module docstring's PARSER SEAM section. Exists ONLY to be
    explicitly injected by tests that want to exercise run_once()'s
    orchestration (batching, state semantics, exit codes) without
    depending on real Tech Logic field parsing. main()'s production path
    calls collector.parsers.build_production_parse_fns() instead, never
    this function."""
    return [{"raw_line": line} for line in lines]


@dataclass(frozen=True)
class RunOutcome:
    exit_code: int
    status: dict[str, Any]


def run_once(
    cfg: CollectorConfig,
    *,
    session,
    parse_fns: dict[str, ParseFn],
    logger: logging.Logger,
) -> RunOutcome:
    missing_parsers = [s.name for s in cfg.sources if s.name not in parse_fns]
    if missing_parsers:
        # Fail closed, checked FIRST -- before touching state, status, or
        # the network -- so a misconfigured/unwired production run is
        # rejected immediately and unambiguously. See module docstring's
        # FAIL CLOSED section.
        raise ParserNotConfiguredError(
            f"No parser configured for source(s): {', '.join(missing_parsers)}. "
            "The production Tech Logic parser adapter has not been wired in yet "
            "(see collector/run.py's PARSER SEAM) -- refusing to process or "
            "upload unparsed raw lines. This is expected until the parser-parity "
            "phase wires in a real parser; it is not a data or network problem."
        )

    start_time = _now_iso()
    prior_status = state.load_status(cfg.status_path)

    try:
        prior_state = state.load_state(cfg.state_path)
    except state.CorruptStateError as exc:
        # Never silently reinterpreted as a fresh/empty state -- that
        # could cause an undetected full replay. Fails loudly; recovery
        # (quarantine_corrupt_file, then re-run) is an explicit operator
        # action, not something this function decides on its own.
        logger.error("State file is corrupt, refusing to guess: %s", exc)
        failed_status = {
            **prior_status,
            "last_attempt": start_time,
            "status": "failed_corrupt_state",
            "last_error": str(exc),
        }
        state.write_status(cfg.status_path, failed_status)
        return RunOutcome(exit_code=1, status=failed_status)

    new_cursors: dict[str, reader.SourceCursor] = {}
    records: dict[str, list[dict[str, Any]]] = {}
    sources_missing: list[str] = []
    sources_rotated: list[str] = []
    sources_truncated: list[str] = []

    for source_cfg in cfg.sources:
        prior_source_state = state.get_source(prior_state, source_cfg.name)
        prior_cursor = _cursor_from_source_state(prior_source_state)

        result = reader.read_new_lines(source_cfg.path, prior_cursor)

        if not result.existed:
            # Missing/stale source: normal, non-fatal. Logged and
            # reflected in status; never crashes the run.
            logger.warning("Source %s not found or unreadable: %s", source_cfg.name, source_cfg.path)
            sources_missing.append(source_cfg.name)
            continue

        if result.rotated:
            logger.warning("Source %s: rotation detected (identity changed)", source_cfg.name)
            sources_rotated.append(source_cfg.name)
        if result.truncated:
            logger.warning("Source %s: truncation detected (size shrank, identity unchanged)", source_cfg.name)
            sources_truncated.append(source_cfg.name)

        new_cursors[source_cfg.name] = result.cursor

        # Safe to index directly (not .get with a fallback) -- the
        # upfront check above already guarantees every configured source
        # has an entry here, or run_once already raised before reaching
        # this loop at all.
        parse_fn = parse_fns[source_cfg.name]
        records[source_cfg.name] = parse_fn(result.lines) if result.lines else []

        logger.info(
            "Source %s: %s new line(s) -> %s record(s)",
            source_cfg.name, len(result.lines), len(records[source_cfg.name]),
        )

    checkins = records.get("checkins", [])
    rejects = records.get("rejects", [])
    acs = records.get("acs", [])

    upload_result = uploader.upload_records(session, cfg, checkins, rejects, acs)

    if not upload_result.success:
        # THE multi-batch state semantics guarantee: state is NOT
        # persisted for ANY source when any batch failed -- not even for
        # sources whose own reads succeeded and whose earlier batches in
        # THIS run already delivered. The next run re-reads from the
        # last-committed offsets and re-sends everything; backend
        # semantic dedup makes that resend safe.
        failure = upload_result.failure
        error_text = failure.error if failure is not None else "unknown upload failure"
        category = failure.category.value if failure is not None and failure.category else None

        logger.error(
            "Upload failed | category=%s batches_attempted=%s batches_delivered=%s error=%s",
            category, upload_result.batches_attempted, upload_result.batches_delivered, error_text,
        )

        finished_time = _now_iso()
        failed_status = {
            **prior_status,
            "last_attempt": start_time,
            "updated_at": finished_time,
            "status": "failed_upload",
            "last_failure_category": category,
            # The real backend message (e.g. a scope mismatch) is always
            # preserved here, never genericized -- see
            # collector/uploader.py's SCOPE MISMATCH section.
            "last_error": error_text,
            "sources_missing": sources_missing,
            "sources_rotated": sources_rotated,
            "sources_truncated": sources_truncated,
        }
        state.write_status(cfg.status_path, failed_status)

        status_outcome = uploader.post_status(session, cfg, failed_status)
        if not status_outcome.success:
            logger.warning("Best-effort status POST also failed: %s", status_outcome.error)

        return RunOutcome(exit_code=1, status=failed_status)

    # Every batch succeeded (or there was nothing to upload) -- now, and
    # only now, persist the new cursor for every source that was
    # successfully read this run.
    new_state = prior_state
    for name, cursor in new_cursors.items():
        new_state = state.with_source(new_state, name, _source_state_from_cursor(cursor))
    state.save_state(cfg.state_path, new_state)

    finished_time = _now_iso()
    any_uploaded = bool(checkins or rejects or acs)
    completed_status = {
        "last_attempt": start_time,
        "last_run": finished_time,
        "updated_at": finished_time,
        "status": "completed" if any_uploaded else "completed_no_new_rows",
        "checkins_rows": len(checkins),
        "rejects_rows": len(rejects),
        "acs_rows": len(acs),
        "uploaded_checkins_rows": upload_result.checkins_inserted,
        "uploaded_rejects_rows": upload_result.rejects_inserted,
        "uploaded_acs_rows": upload_result.acs_inserted,
        "sources_missing": sources_missing,
        "sources_rotated": sources_rotated,
        "sources_truncated": sources_truncated,
    }
    state.write_status(cfg.status_path, completed_status)

    status_outcome = uploader.post_status(session, cfg, completed_status)
    if not status_outcome.success:
        # Best-effort, per module docstring / legacy-proven behavior --
        # a failed STATUS post never fails an otherwise-successful run.
        logger.warning("Best-effort status POST failed (run itself succeeded): %s", status_outcome.error)

    return RunOutcome(exit_code=0, status=completed_status)


def _build_logger(log_path) -> logging.Logger:
    logger = logging.getLogger("sortview.collector")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        from logging.handlers import RotatingFileHandler

        log_path.parent.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

        file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    return logger


def _requested_contract_mode(config_path: str) -> str:
    """"v1" (the default when `contract_mode` is absent) or "v2", read WITHOUT importing any v2 module: a build that does not ship Contract v2
    must still run v1 exactly as before. Kept in step with collector/v2_config.py::read_contract_mode by a test."""
    path = Path(config_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        raw = None  # an unreadable config is reported by load_config, with its own message
    mode = raw.get("contract_mode", "v1") if isinstance(raw, dict) else "v1"
    if mode not in ("v1", "v2"):
        raise ConfigError("config 'contract_mode' must be \"v1\" or \"v2\"")
    return str(mode)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SortView Collector -- one-shot scheduled run")
    parser.add_argument("--config", required=True, help="Path to the collector config JSON file")
    parser.add_argument(
        "--v2-dry-run",
        action="store_true",
        help="Contract v2: transform a bounded tail of each source and print only aggregate counts. Needs no API token, no server "
        "registration and no persistent secret; makes no network call and writes no file",
    )
    args = parser.parse_args(argv)

    if args.v2_dry_run:
        # The dry run is independent of every credential, so it is dispatched BEFORE the v1 configuration (which requires SORTVIEW_API_TOKEN).
        try:
            from .v2_run import main_v2_dry_run
        except ModuleNotFoundError as exc:
            if not (exc.name or "").startswith("collector"):
                raise
            print("Configuration error: this build does not include Contract v2", file=sys.stderr)
            return 2
        return main_v2_dry_run(args.config)

    try:
        mode = _requested_contract_mode(args.config)
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logger = _build_logger(cfg.log_path)
    if mode == "v2":
        # Contract v2 (privacy-safe) is opt-in; a config without contract_mode keeps running the v1 path below, unchanged.
        try:
            from .v2_run import (
                main_v2,  # imported only when v2 is chosen: the v1 path never loads (or needs) the v2 modules
            )
        except ModuleNotFoundError as exc:
            if not (exc.name or "").startswith("collector"):
                raise
            print("Configuration error: this build does not include Contract v2", file=sys.stderr)
            return 2
        return main_v2(cfg, args.config, logger=logger)
    if cfg.installation_id is None:
        logger.warning(
            "Config has no installation_id (legacy config): heartbeats will not update "
            "the server-side collector installation record. Uploads are unaffected."
        )

    try:
        session = uploader.build_session()
        parse_fns = parsers.build_production_parse_fns(customer_id=cfg.customer_id, branch_id=cfg.branch_id)
        outcome = run_once(cfg, session=session, parse_fns=parse_fns, logger=logger)
        return outcome.exit_code
    except ParserNotConfiguredError as exc:
        # Distinct from the generic crash handler below: this is an
        # expected, actionable configuration/deployment-readiness
        # condition (exit code 2, same family as a bad --config or a
        # missing token), never a data or network failure. See module
        # docstring's FAIL CLOSED section.
        logger.error("Refusing to run: %s", exc)
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        # Belt-and-braces: anything that escapes run_once's own handling
        # (a genuine bug, not a classified upload/state failure) must
        # still exit nonzero -- never let an unanticipated exception
        # produce a silent/ambiguous exit code. Mirrors agent/main.py's
        # own fail-fast exit-code contract.
        logger.exception("Collector run crashed with an unhandled exception")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
