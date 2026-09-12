"""Normalized event construction (Continuous Ingestion Phase F).

Maps one parsed-row dict (from agent.parser.*.parse_lines_with_offsets's
DataFrame, via df.to_dict(orient="records")) into the exact record shape
main.py's CheckinRow/RejectRow/AcsRow accept. Field mapping and
make_json_safe below are intentionally a small, self-contained copy of
agent/uploader.py's build_checkins_payload/build_rejects_payload/
build_acs_payload/make_json_safe, NOT an import from that module -- see
agent/runtime/config.py's docstring for why (agent.uploader executes
load_config() at import time, a hard dependency on the legacy
agent_config.json shape this package must not inherit).
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

_SOURCE_FILE_DEFAULTS = {
    "checkins": "Checkins.txt",
    "rejects": "Rejects.txt",
    "acs": "ACS Log.txt",
}


def make_json_safe(value: Any) -> Any:
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(value, float) and math.isnan(value):
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    return str(value)


def _build_checkin_event(row: dict[str, Any], *, customer_id: int, branch_id: int, source_file: str) -> dict:
    return {
        "customer_id": customer_id,
        "branch_id": branch_id,
        "event_time": make_json_safe(row.get("datetime")),
        "title": make_json_safe(row.get("title")),
        "barcode": make_json_safe(row.get("barcode")),
        "collection_code": make_json_safe(row.get("collection_code")),
        "call_number": make_json_safe(row.get("call_number")),
        "shelf_code": make_json_safe(row.get("shelf_code")),
        "destination": make_json_safe(row.get("destination")),
        "bin": make_json_safe(row.get("bin")),
        "is_problem": make_json_safe(row.get("is_problem")),
        "message": make_json_safe(row.get("message")),
        "flag_1": make_json_safe(row.get("flag_1")),
        "flag_2": make_json_safe(row.get("flag_2")),
        "flag_3": make_json_safe(row.get("flag_3")),
        "source_file": source_file,
    }


def _build_reject_event(row: dict[str, Any], *, customer_id: int, branch_id: int, source_file: str) -> dict:
    return {
        "customer_id": customer_id,
        "branch_id": branch_id,
        "event_time": make_json_safe(row.get("datetime")),
        "barcode": make_json_safe(row.get("barcode")),
        "message": make_json_safe(row.get("error_message")),
        "source_file": source_file,
    }


def _build_acs_event(row: dict[str, Any], *, customer_id: int, branch_id: int, source_file: str) -> dict:
    return {
        "customer_id": customer_id,
        "branch_id": branch_id,
        "event_time": make_json_safe(row.get("datetime")),
        "message_code": make_json_safe(row.get("message_code")),
        "barcode": make_json_safe(row.get("barcode")),
        "title": make_json_safe(row.get("title")),
        "patron_id": make_json_safe(row.get("patron_id")),
        "destination": make_json_safe(row.get("destination")),
        "raw_message": make_json_safe(row.get("raw_message")),
        "source_file": source_file,
    }


_BUILDERS = {
    "checkins": _build_checkin_event,
    "rejects": _build_reject_event,
    "acs": _build_acs_event,
}


def build_event(
    source_name: str, row: dict[str, Any], *, customer_id: int, branch_id: int, source_file: str | None = None
) -> dict[str, Any]:
    """Normalizes one parsed row into the payload shape main.py's /upload
    accepts for `source_name`. Does NOT attach source_event_id -- that's
    the collector's job, using the row's own tailer offset, which this
    function has no knowledge of."""
    builder = _BUILDERS.get(source_name)
    if builder is None:
        raise ValueError(f"unknown source {source_name!r}")
    return builder(
        row, customer_id=customer_id, branch_id=branch_id,
        source_file=source_file or _SOURCE_FILE_DEFAULTS[source_name],
    )
