"""Canonical ACS Log.txt line-parser. Moved unchanged from
agent/parse_acs.py's _parse_lines/extract_fields -- see
agent/parser/__init__.py for why, and tests/test_parser_canonical.py for
the regression proof.
"""

from __future__ import annotations

import re

import pandas as pd

from ..logger_config import get_logger

logger = get_logger("parser.acs")

TAG_PATTERN = re.compile(r"([A-Z]{2})([^|]*)")

EMPTY_COLUMNS = [
    "date",
    "time",
    "datetime",
    "message_code",
    "barcode",
    "title",
    "patron_id",
    "destination",
    "raw_message",
]


def parse_timestamp(date_str, time_str):
    return pd.to_datetime(
        f"{date_str} {time_str}",
        format="%m/%d/%Y %I:%M:%S %p",
        errors="coerce"
    )


def extract_fields(message):
    fields = {}
    for tag, value in TAG_PATTERN.findall(message):
        fields[tag] = value.strip()
    return fields


def empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=EMPTY_COLUMNS)


def parse_lines(lines: list[str]) -> pd.DataFrame:
    df, _kept_offsets = parse_lines_with_offsets(list(enumerate(lines)))
    return df


def parse_lines_with_offsets(numbered_lines: list[tuple[int, str]]) -> tuple[pd.DataFrame, list[int]]:
    """See agent.parser.checkins.parse_lines_with_offsets's docstring for
    why this exists (Phase E: recovering the exact physical-record offset
    behind each surviving output row, since blank/malformed lines are
    silently dropped). parse_lines is a thin wrapper around this."""
    rows = []
    kept_offsets: list[int] = []

    for offset, line in numbered_lines:
        line = line.replace("\x01", "").strip()

        if not line:
            continue

        parts = line.split("\x02")

        if len(parts) < 3:
            continue

        date = parts[0].strip()
        time = parts[1].strip()
        message = parts[2].strip()

        fields = extract_fields(message)

        rows.append({
            "date": date,
            "time": time,
            "datetime": parse_timestamp(date, time),
            "message_code": message[:2],
            "barcode": fields.get("AB"),
            "title": fields.get("AJ"),
            "patron_id": fields.get("AA"),
            "destination": fields.get("CT"),
            "raw_message": message,
        })
        kept_offsets.append(offset)

    df = pd.DataFrame(rows)

    if df.empty:
        df = empty_df()

    logger.info("Parsed ACS rows=%s", len(df))
    return df, kept_offsets
