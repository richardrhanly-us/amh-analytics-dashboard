"""Production parser adapter (parser-parity wiring phase).

    collector/run.py's ParseFn seam
        -> THIS MODULE (thin adapter, all mapping/dropping/NaN-safety)
        -> agent/parser/{checkins,rejects,acs}.py (UNCHANGED, the one
           source of truth for actual Tech Logic parsing behavior --
           column splitting, destination normalization, error message
           simplification, ACS control-character/tag extraction,
           timestamp format)
        -> backend row shape (main.py's CheckinRow/RejectRow/AcsRow)

This module contains NO Tech Logic parsing logic of its own -- no
delimiter splitting, no destination/error-message normalization, no ACS
tag extraction, no timestamp format string. All of that stays exactly
where it already lives, in agent/parser/*, imported and called here, not
copied. What lives here is purely: call the canonical parser, then map/
filter/serialize its DataFrame output into the exact dict shape
collector/uploader.py sends to POST /upload.

WHY A LOCAL JSON-SAFETY HELPER INSTEAD OF agent.uploader.make_json_safe:
collector/__init__.py's own architecture docstring is explicit that this
package imports ONLY agent.parser.* and reimplements everything else
(binary reading, atomic state, JSON-safety, HTTP) as small independent
functions, specifically so it has zero coupling to code outside that one
sanctioned exception. Importing agent.uploader would also trigger that
module's own import-time `load_config()` against agent/config.py's
entirely separate config system -- a real risk of crashing collector at
import time on a machine that only has collector's own config file.
_json_safe below is a generic NaN/None coercion helper, not a Tech Logic
parsing algorithm, so a small local copy does not violate "do not
duplicate parsing algorithms."

MALFORMED TIMESTAMP BEHAVIOR -- CORRECTED (backend-contract verification
phase). The legacy uploader's own behavior (agent/uploader.py's
build_checkins_payload/build_rejects_payload: send event_time=null
rather than skip) turned out to be unsafe against the CURRENT backend
schema, not merely a stylistic choice to preserve: checkins.event_time
and rejects.event_time are both `TIMESTAMP NOT NULL` in the database
(alembic/versions/26397a3947b1_baseline_current_schema.py) -- a Pydantic
CheckinRow/RejectRow happily accepts event_time=None (it's declared
`str | None`), but the resulting INSERT would raise a NOT NULL
violation, which main.py's /upload endpoint turns into a bare 500, which
collector/uploader.py classifies as RETRYABLE_INFRA, which -- combined
with collector/run.py's all-or-nothing per-run state commit -- means the
SAME unparseable row would be retried forever on every future scheduled
run, permanently stalling every source's state, not just the bad row's.
acs_events.event_time has no such constraint (nullable), which is
exactly why ACS was always safe sending nothing for these rows.

Current, verified-safe behavior, uniform across all three sources:
  - Checkins, Rejects, ACS: a row with an unparseable (NaT) timestamp is
    DROPPED ENTIRELY -- never appears in the returned list, so
    CheckinRow(event_time=None) / RejectRow(event_time=None) can never
    be constructed from this module's output. Checkins/Rejects now match
    ACS's already-established behavior instead of the legacy uploader's;
    this is a deliberate, verified correction to what the legacy
    pipeline did, not a preservation of it -- see the backend-contract
    verification report this followed.

Each dropped-row count is reported via the shared "sortview.collector"
logger (the same named logger collector/run.py's _build_logger already
attaches collector.log's handlers to -- see SKIPPED-ROW OBSERVABILITY
below) -- an aggregate count and source name only, never the row's own
content, per the "no patron-sensitive raw lines in logs" constraint.

SKIPPED-ROW OBSERVABILITY -- smallest existing mechanism, not a new one:
agent/parser/{checkins,rejects,acs}.py already report their own skip
categories (skipped_short_rows, bad_datetime) as one aggregate log line
per parse call, via a named logger, never raw line content. This module
reuses that exact same pattern -- a single WARNING-level log line per
call when any row was dropped for a missing event_time, an aggregate
count only -- through the SAME logger name collector/run.py's
_build_logger() already configures ("sortview.collector"), so these
lines land in the existing collector.log with zero additional wiring:
no new status-dict field, no new POST payload field, no new API/DB
contract. WARNING (not INFO, unlike agent/parser's own routine-skip
logging) because a dropped checkin/reject represents a real Tech Logic
event that parsed successfully in every other respect and is now simply
never uploaded -- worth an operator's attention, not routine noise.

source_event_id is DELIBERATELY NEVER SET by any of the three adapters
below. Every backend row model (CheckinRow/RejectRow/AcsRow) accepts it
as Optional with a default of None -- it is not required, and
collector/run.py's own module docstring already establishes that
Collector v1 follows the legacy pipeline's semantic-key-dedup model, not
the continuous agent's source_event_id model. Introducing it here would
be adopting continuous-agent semantics this phase was explicitly told
not to adopt.

source_file is a hardcoded literal per source ("Checkins.txt" /
"Rejects.txt" / "ACS Log.txt"), matching the legacy uploader's own
hardcoded values exactly, rather than derived from collector's
configurable (and possibly differently named) source path.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

import pandas as pd

from agent.parser import acs as acs_parser
from agent.parser import checkins as checkins_parser
from agent.parser import rejects as rejects_parser

# Same shape as collector/run.py's ParseFn -- defined independently here
# (not imported from .run) purely to avoid a circular import (run.py
# calls build_production_parse_fns below), not because the type itself
# differs. Not a parsing algorithm -- a type alias, safe to state once
# per module.
ParseFn = Callable[[list[str]], list[dict[str, Any]]]

# Same logger NAME collector/run.py's _build_logger() configures --
# deliberately not a different name, so these log lines land in the same
# collector.log via handlers main() already attached, with zero
# additional wiring in this module. See module docstring's SKIPPED-ROW
# OBSERVABILITY section.
_logger = logging.getLogger("sortview.collector")

_CHECKINS_SOURCE_FILE = "Checkins.txt"
_REJECTS_SOURCE_FILE = "Rejects.txt"
_ACS_SOURCE_FILE = "ACS Log.txt"


def _json_safe(value: Any) -> Any:
    """Same NaN/NaT/None -> None coercion as agent/uploader.py's
    make_json_safe, reimplemented locally -- see module docstring's WHY
    A LOCAL JSON-SAFETY HELPER section. A valid pandas Timestamp is
    intentionally serialized via plain str(value) (matching
    make_json_safe's own fallback for any non-primitive type), NOT
    .isoformat() -- replicating the exact string legacy has always sent,
    not a different-looking but 'equivalent' one."""
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(value, (str, int, float, bool)):
        return value

    return str(value)


def _parse_checkins(lines: list[str], *, customer_id: int, branch_id: int) -> list[dict[str, Any]]:
    df = checkins_parser.parse_lines(lines)
    records: list[dict[str, Any]] = []
    skipped_missing_event_time = 0

    for row in df.to_dict(orient="records"):
        event_time = _json_safe(row.get("datetime"))

        # See module docstring's MALFORMED TIMESTAMP BEHAVIOR -- a row
        # with no usable timestamp is dropped entirely, never sent as
        # event_time=null. checkins.event_time is NOT NULL in the
        # database; sending null here would 500 the whole batch and
        # permanently stall every source's state on retry.
        if event_time is None:
            skipped_missing_event_time += 1
            continue

        records.append({
            "customer_id": customer_id,
            "branch_id": branch_id,
            "event_time": event_time,
            "title": _json_safe(row.get("title")),
            "barcode": _json_safe(row.get("barcode")),
            "collection_code": _json_safe(row.get("collection_code")),
            "call_number": _json_safe(row.get("call_number")),
            "shelf_code": _json_safe(row.get("shelf_code")),
            "destination": _json_safe(row.get("destination")),
            "bin": _json_safe(row.get("bin")),
            "is_problem": _json_safe(row.get("is_problem")),
            "message": _json_safe(row.get("message")),
            "flag_1": _json_safe(row.get("flag_1")),
            "flag_2": _json_safe(row.get("flag_2")),
            "flag_3": _json_safe(row.get("flag_3")),
            "source_file": _CHECKINS_SOURCE_FILE,
        })

    if skipped_missing_event_time:
        # Aggregate count and source name only -- never the row's own
        # content (title/barcode/etc. can be patron-adjacent). See
        # module docstring's SKIPPED-ROW OBSERVABILITY section.
        _logger.warning(
            "checkins: dropped %s row(s) with no usable event_time (unparseable timestamp) -- not uploaded",
            skipped_missing_event_time,
        )

    return records


def _parse_rejects(lines: list[str], *, customer_id: int, branch_id: int) -> list[dict[str, Any]]:
    df = rejects_parser.parse_lines(lines)
    records: list[dict[str, Any]] = []
    skipped_missing_event_time = 0

    for row in df.to_dict(orient="records"):
        event_time = _json_safe(row.get("datetime"))

        # Same reasoning as _parse_checkins above -- rejects.event_time
        # is also NOT NULL.
        if event_time is None:
            skipped_missing_event_time += 1
            continue

        records.append({
            "customer_id": customer_id,
            "branch_id": branch_id,
            "event_time": event_time,
            "barcode": _json_safe(row.get("barcode")),
            "message": _json_safe(row.get("error_message")),
            "source_file": _REJECTS_SOURCE_FILE,
        })

    if skipped_missing_event_time:
        _logger.warning(
            "rejects: dropped %s row(s) with no usable event_time (unparseable timestamp) -- not uploaded",
            skipped_missing_event_time,
        )

    return records


def _parse_acs(lines: list[str], *, customer_id: int, branch_id: int) -> list[dict[str, Any]]:
    df = acs_parser.parse_lines(lines)
    records: list[dict[str, Any]] = []
    skipped_missing_event_time = 0

    for row in df.to_dict(orient="records"):
        event_time = _json_safe(row.get("datetime"))

        # ACS's already-established behavior, unchanged: rows with no
        # usable timestamp are dropped entirely. Only the observability
        # (the warning below) is new here -- the drop itself already
        # existed before this phase.
        if event_time is None:
            skipped_missing_event_time += 1
            continue

        records.append({
            "customer_id": customer_id,
            "branch_id": branch_id,
            "event_time": event_time,
            "message_code": _json_safe(row.get("message_code")),
            "barcode": _json_safe(row.get("barcode")),
            "title": _json_safe(row.get("title")),
            "patron_id": _json_safe(row.get("patron_id")),
            "destination": _json_safe(row.get("destination")),
            "raw_message": _json_safe(row.get("raw_message")),
            "source_file": _ACS_SOURCE_FILE,
        })

    if skipped_missing_event_time:
        _logger.warning(
            "acs: dropped %s row(s) with no usable event_time (unparseable timestamp) -- not uploaded",
            skipped_missing_event_time,
        )

    return records


def build_production_parse_fns(*, customer_id: int, branch_id: int) -> dict[str, ParseFn]:
    """Returns the {source_name: ParseFn} mapping collector/run.py's
    main() passes to run_once() -- one adapter per source, each already
    bound to this run's tenant scope via closure (ParseFn itself takes
    only `lines`, with no cfg parameter, so tenant scope has to be bound
    in here rather than threaded through run_once's existing signature).
    """
    return {
        "checkins": lambda lines: _parse_checkins(lines, customer_id=customer_id, branch_id=branch_id),
        "rejects": lambda lines: _parse_rejects(lines, customer_id=customer_id, branch_id=branch_id),
        "acs": lambda lines: _parse_acs(lines, customer_id=customer_id, branch_id=branch_id),
    }
