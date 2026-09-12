"""Canonical Rejects.txt line-parser. Moved unchanged from
agent/parse_rejects.py's _parse_reject_lines/simplify_error_message -- see
agent/parser/__init__.py for why, and tests/test_parser_canonical.py for
the regression proof.
"""

from __future__ import annotations

import pandas as pd

from ..logger_config import get_logger

logger = get_logger("parser.rejects")

COLUMNS = [
    "barcode",
    "error_message",
    "date",
    "time",
]


def simplify_error_message(msg):
    if pd.isna(msg):
        return "Unknown"

    msg = str(msg).lower()

    if "item not found" in msg:
        return "Item Not Found"

    if "acs" in msg:
        return "ILS / ACS Failure"

    if "multiple rfid" in msg or "multiple tags" in msg:
        return "RFID Collision"

    if "collection code" in msg:
        return "Call Number / Config Error"

    if "library not found" in msg:
        return "Routing Error"

    return "Other"


def empty_df():
    df = pd.DataFrame(columns=COLUMNS)

    df["datetime"] = pd.Series(dtype="datetime64[ns]")
    df["date_only"] = pd.Series(dtype="object")
    df["hour"] = pd.Series(dtype="float")
    df["day_of_week"] = pd.Series(dtype="object")
    df["error_simple"] = pd.Series(dtype="object")

    return df


def parse_lines(lines: list[str]) -> pd.DataFrame:
    df, _kept_offsets = parse_lines_with_offsets(list(enumerate(lines)))
    return df


def parse_lines_with_offsets(numbered_lines: list[tuple[int, str]]) -> tuple[pd.DataFrame, list[int]]:
    """See agent.parser.checkins.parse_lines_with_offsets's docstring for
    why this exists (Phase E: recovering the exact physical-record offset
    behind each surviving output row, since blank/short lines are
    silently dropped). parse_lines is a thin wrapper around this."""
    rows = []
    kept_offsets: list[int] = []
    skipped_short_rows = 0

    for offset, line in numbered_lines:
        line = line.strip()

        if not line:
            continue

        parts = line.split("|")

        if len(parts) < len(COLUMNS):
            skipped_short_rows += 1
            continue

        parts = parts[:len(COLUMNS)]
        rows.append(parts)
        kept_offsets.append(offset)

    if not rows:
        df = empty_df()
        logger.info(
            "Parsed rejects | rows=0 skipped_short_rows=%s bad_datetime=0",
            skipped_short_rows,
        )
        logger.info("Reject reason breakdown: {}")
        return df, kept_offsets

    df = pd.DataFrame(rows, columns=COLUMNS)

    df["barcode"] = df["barcode"].astype(str).str.strip()
    df["error_message"] = df["error_message"].astype(str).str.strip()
    df["date"] = df["date"].astype(str).str.strip()
    df["time"] = df["time"].astype(str).str.strip()

    df["datetime"] = pd.to_datetime(
        df["date"] + " " + df["time"],
        format="%m/%d/%Y %I:%M:%S %p",
        errors="coerce"
    )

    df["date_only"] = df["datetime"].dt.date
    df["hour"] = df["datetime"].dt.hour
    df["day_of_week"] = df["datetime"].dt.day_name()
    df["error_simple"] = df["error_message"].apply(simplify_error_message)

    logger.info(
        "Parsed rejects | rows=%s skipped_short_rows=%s bad_datetime=%s",
        len(df),
        skipped_short_rows,
        int(df["datetime"].isna().sum()),
    )
    logger.info("Reject reason breakdown: %s", df["error_simple"].value_counts(dropna=False).to_dict())

    return df, kept_offsets
