"""Continuous, polling-based watcher for the AMH log files (Phase 1:
capture only -- this module never makes a network call).

For each watched file, every poll:

  1. Stat the file. If (st_dev, st_ino) differs from what's stored in
     file_state, the file was replaced (deleted and recreated at the same
     path) -- treat it as a new file and start from byte 0. Verified
     empirically on Windows/NTFS: st_ino changes across a delete+recreate
     even though creation time can be silently preserved by NTFS
     "tunneling" -- see agent/README or the design note for the test that
     established this.
  2. Otherwise, if the current size is smaller than the stored offset,
     the file was truncated in place -- also start from byte 0.
  3. Otherwise, read from the stored offset. Only lines that end in a
     newline are treated as safe to consume; a trailing line with no
     newline yet (the AMH software mid-write) is left unconsumed and
     re-read, along with its completion, on a later poll. This matters
     specifically because Phase 1 polls every 1-2 seconds -- far more
     likely to catch a file mid-write than the old scheduled-batch
     pipeline ever was.
  4. The safe lines are handed to the existing parse_checkins /
     parse_rejects / parse_acs line-parsing functions unchanged, so
     Phase 1 doesn't reimplement any parsing behavior.
  5. Every resulting event, plus the new file offset, is written to
     SQLite in one transaction -- so a crash or error partway through a
     batch can't advance the stored offset past events that were never
     actually committed.

Watcher and (future) uploader are kept as separate components on purpose:
this module only ever reads log files and writes to the local outbox. It
has no knowledge of the network, the FastAPI backend, or upload retries,
so it can run -- and be tested -- with zero network access, and so a
future uploader can be added as a second component in the same process
(or a separate one) without touching this file.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from . import outbox
from .logger_config import get_logger
from .parse_acs import _parse_lines as _parse_acs_lines
from .parse_checkins import _parse_checkins_lines
from .parse_rejects import _parse_reject_lines

logger = get_logger("watcher")

DEFAULT_POLL_INTERVAL_SECONDS = 1.5


def _json_safe(value: Any) -> Any:
    if value is None:
        return None

    # pd.isna raises on some array-like inputs; every ordinary scalar
    # (str, int, etc.) falls through here, so this guards a duck-type
    # check rather than a real fault. Mirrors uploader.py's make_json_safe
    # deliberately (not imported from it -- see module docstring on
    # keeping the watcher free of any dependency the uploader owns).
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(value, (str, int, float, bool)):
        return value

    return str(value)


@dataclass(frozen=True)
class FileIdentity:
    dev: int
    ino: int
    size: int
    mtime: str


def _stat_file(path: str) -> FileIdentity | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None

    return FileIdentity(
        dev=st.st_dev,
        ino=st.st_ino,
        size=st.st_size,
        mtime=datetime.fromtimestamp(st.st_mtime, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
    )


def _read_safe_lines(path: str, start_offset: int) -> tuple[list[str], int]:
    """Read from start_offset to the last complete line in the file.

    A trailing chunk with no newline yet is left unconsumed: the returned
    offset points just past the last line that ended in "\\n", never past
    a line that might still be mid-write. Uses f.tell()/f.seek() tokens
    directly (never manual byte arithmetic on them), matching how the
    rest of this codebase already treats text-mode file offsets on
    Windows, where tell() is an opaque token, not a true byte count.
    """
    lines: list[str] = []
    safe_offset = start_offset

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start_offset)

        while True:
            line = f.readline()

            if not line:
                break  # real EOF

            if not line.endswith("\n"):
                # Partial trailing line -- the writer hasn't finished this
                # line yet. Stop here; don't consume it.
                break

            lines.append(line)
            safe_offset = f.tell()

    return lines, safe_offset


def _resolve_start_offset(
    conn: Any,
    *,
    file_path: str,
    identity: FileIdentity,
) -> tuple[int, bool, bool]:
    """Returns (start_offset, was_rotated, was_truncated)."""
    state = outbox.get_file_state(conn, file_path)

    if state is None:
        return 0, False, False

    if state["file_dev"] != identity.dev or state["file_ino"] != identity.ino:
        logger.warning(
            "File replaced, resetting offset | file=%s old_dev=%s old_ino=%s new_dev=%s new_ino=%s",
            file_path,
            state["file_dev"],
            state["file_ino"],
            identity.dev,
            identity.ino,
        )
        return 0, True, False

    stored_offset = int(state["last_byte_offset"])

    if identity.size < stored_offset:
        logger.warning(
            "File truncated in place, resetting offset | file=%s stored_offset=%s current_size=%s",
            file_path,
            stored_offset,
            identity.size,
        )
        return 0, False, True

    return stored_offset, False, False


@dataclass(frozen=True)
class PollFileResult:
    file_path: str
    existed: bool
    rotated: bool
    truncated: bool
    lines_read: int
    events_inserted: int
    events_deduped: int
    start_offset: int
    end_offset: int


def _poll_file(
    conn: Any,
    *,
    event_type: str,
    file_path: str,
    customer_id: int,
    branch_id: int,
    parse_lines_fn: Callable[[list[str]], pd.DataFrame],
    build_entry_fn: Callable[[dict[str, Any], int, int], tuple[str | None, dict[str, Any], dict[str, Any]]],
) -> PollFileResult:
    identity = _stat_file(file_path)

    if identity is None:
        return PollFileResult(
            file_path=file_path,
            existed=False,
            rotated=False,
            truncated=False,
            lines_read=0,
            events_inserted=0,
            events_deduped=0,
            start_offset=0,
            end_offset=0,
        )

    start_offset, rotated, truncated = _resolve_start_offset(
        conn, file_path=file_path, identity=identity
    )

    lines, end_offset = _read_safe_lines(file_path, start_offset)

    df = parse_lines_fn(lines)
    records = df.to_dict(orient="records") if len(df) else []

    inserted = 0
    deduped = 0

    with outbox.transaction(conn):
        for row in records:
            event_timestamp, dedup_fields, payload = build_entry_fn(
                row, customer_id, branch_id
            )
            dedup_key = outbox.compute_dedup_key(dedup_fields)

            was_inserted = outbox.insert_event(
                conn,
                event_type=event_type,
                customer_id=customer_id,
                branch_id=branch_id,
                event_timestamp=event_timestamp,
                dedup_key=dedup_key,
                payload=payload,
            )

            if was_inserted:
                inserted += 1
            else:
                deduped += 1

        outbox.save_file_state(
            conn,
            file_path=file_path,
            file_dev=identity.dev,
            file_ino=identity.ino,
            last_byte_offset=end_offset,
            last_modified=identity.mtime,
        )

    return PollFileResult(
        file_path=file_path,
        existed=True,
        rotated=rotated,
        truncated=truncated,
        lines_read=len(lines),
        events_inserted=inserted,
        events_deduped=deduped,
        start_offset=start_offset,
        end_offset=end_offset,
    )


# ---------------------------------------------------------------------
# per-event-type entry builders
#
# Field names mirror uploader.py's build_checkins_payload /
# build_rejects_payload / build_acs_payload exactly, so a future uploader
# can build its POST payload straight from payload_json with no
# reshaping. Deliberately not imported from uploader.py -- see the module
# docstring on why the watcher stays free of any dependency the (future)
# uploader owns.
# ---------------------------------------------------------------------


def _build_checkin_entry(
    row: dict[str, Any], customer_id: int, branch_id: int
) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    event_time = _json_safe(row.get("datetime"))

    payload = {
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
        "source_file": "Checkins.txt",
    }

    dedup_fields = {
        "title": row.get("title"),
        "barcode": row.get("barcode"),
        "collection_code": row.get("collection_code"),
        "call_number": row.get("call_number"),
        "shelf_code": row.get("shelf_code"),
        "destination_raw": row.get("destination_raw"),
        "is_problem": _json_safe(row.get("is_problem")),
        "message": row.get("message"),
        "bin": row.get("bin"),
        "flag_1": row.get("flag_1"),
        "flag_2": row.get("flag_2"),
        "flag_3": row.get("flag_3"),
        "date": row.get("date"),
        "time": row.get("time"),
    }

    return event_time, dedup_fields, payload


def _build_reject_entry(
    row: dict[str, Any], customer_id: int, branch_id: int
) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    event_time = _json_safe(row.get("datetime"))

    payload = {
        "customer_id": customer_id,
        "branch_id": branch_id,
        "event_time": event_time,
        "barcode": _json_safe(row.get("barcode")),
        "message": _json_safe(row.get("error_message")),
        "source_file": "Rejects.txt",
    }

    dedup_fields = {
        "barcode": row.get("barcode"),
        "error_message": row.get("error_message"),
        "date": row.get("date"),
        "time": row.get("time"),
    }

    return event_time, dedup_fields, payload


def _build_acs_entry(
    row: dict[str, Any], customer_id: int, branch_id: int
) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    event_time = _json_safe(row.get("datetime"))

    payload = {
        "customer_id": customer_id,
        "branch_id": branch_id,
        "event_time": event_time,
        "message_code": _json_safe(row.get("message_code")),
        "barcode": _json_safe(row.get("barcode")),
        "title": _json_safe(row.get("title")),
        "patron_id": _json_safe(row.get("patron_id")),
        "destination": _json_safe(row.get("destination")),
        "raw_message": _json_safe(row.get("raw_message")),
        "source_file": "ACS Log.txt",
    }

    # raw_message is the full, verbatim text of the source line's message
    # segment -- together with date/time (the other two pipe segments of
    # the raw ACS line) it's effectively the original line's content, no
    # need to also include the individually tag-extracted fields.
    dedup_fields = {
        "date": row.get("date"),
        "time": row.get("time"),
        "raw_message": row.get("raw_message"),
    }

    return event_time, dedup_fields, payload


@dataclass(frozen=True)
class PollSummary:
    checkins: PollFileResult
    rejects: PollFileResult
    acs: PollFileResult


def poll_once(
    conn: Any,
    *,
    checkins_path: str,
    rejects_path: str,
    acs_path: str,
    customer_id: int,
    branch_id: int,
) -> PollSummary:
    """Poll all three watched files exactly once. Never blocks on the
    network -- purely local file reads plus local SQLite writes."""
    checkins_result = _poll_file(
        conn,
        event_type="checkin",
        file_path=checkins_path,
        customer_id=customer_id,
        branch_id=branch_id,
        parse_lines_fn=_parse_checkins_lines,
        build_entry_fn=_build_checkin_entry,
    )

    rejects_result = _poll_file(
        conn,
        event_type="reject",
        file_path=rejects_path,
        customer_id=customer_id,
        branch_id=branch_id,
        parse_lines_fn=_parse_reject_lines,
        build_entry_fn=_build_reject_entry,
    )

    acs_result = _poll_file(
        conn,
        event_type="acs",
        file_path=acs_path,
        customer_id=customer_id,
        branch_id=branch_id,
        parse_lines_fn=_parse_acs_lines,
        build_entry_fn=_build_acs_entry,
    )

    return PollSummary(
        checkins=checkins_result,
        rejects=rejects_result,
        acs=acs_result,
    )


def run_forever(
    conn: Any,
    *,
    checkins_path: str,
    rejects_path: str,
    acs_path: str,
    customer_id: int,
    branch_id: int,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_iterations: int | None = None,
) -> None:
    """Poll in a loop until stopped.

    max_iterations exists for tests -- production use leaves it None and
    relies on the process being stopped externally. This function never
    imports the network/uploader side of the pipeline; nothing here can
    block on the backend being reachable.
    """
    import time

    iterations = 0

    while max_iterations is None or iterations < max_iterations:
        summary = poll_once(
            conn,
            checkins_path=checkins_path,
            rejects_path=rejects_path,
            acs_path=acs_path,
            customer_id=customer_id,
            branch_id=branch_id,
        )

        total_inserted = (
            summary.checkins.events_inserted
            + summary.rejects.events_inserted
            + summary.acs.events_inserted
        )

        if total_inserted:
            logger.info(
                "Poll complete | checkins=%s rejects=%s acs=%s",
                summary.checkins.events_inserted,
                summary.rejects.events_inserted,
                summary.acs.events_inserted,
            )

        iterations += 1

        if max_iterations is None or iterations < max_iterations:
            time.sleep(poll_interval_seconds)


if __name__ == "__main__":
    # Relative imports throughout this module mean it must be run as part
    # of the agent package -- `python -m agent.watcher` from the repo
    # root, not `python watcher.py` from inside agent/.
    from .config import load_config

    agent_config = load_config()
    db_conn = outbox.connect(Path("data") / "sortview_agent.db")

    logger.info("Watcher starting | poll_interval=%ss", DEFAULT_POLL_INTERVAL_SECONDS)

    run_forever(
        db_conn,
        checkins_path=agent_config["raw_checkins_file"],
        rejects_path=agent_config["raw_rejects_file"],
        acs_path=agent_config["raw_acs_file"],
        customer_id=agent_config["customer_id"],
        branch_id=agent_config["branch_id"],
    )
