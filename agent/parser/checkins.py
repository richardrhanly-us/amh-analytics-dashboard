"""Canonical Checkins.txt line-parser. Moved unchanged from
agent/parse_checkins.py's _parse_checkins_lines/normalize_destination --
see agent/parser/__init__.py for why, and tests/test_parser_canonical.py
for the regression proof.
"""

from __future__ import annotations

import pandas as pd

from ..logger_config import get_logger

logger = get_logger("parser.checkins")

COLUMNS = [
    "title",
    "barcode",
    "collection_code",
    "call_number",
    "shelf_code",
    "destination_raw",
    "is_problem",
    "message",
    "bin",
    "flag_1",
    "flag_2",
    "flag_3",
    "date",
    "time",
]


def normalize_destination(value):
    if pd.isna(value):
        return ""

    text = str(value).strip()
    upper_text = text.upper()

    if not text:
        return ""

    if upper_text in {"1", "LOCAL", "MAIN"}:
        return "Main"

    if "WESTSIDE" in upper_text:
        return "Westside"

    if "LIBRARY EXPRESS" in upper_text:
        return "Library Express"

    if "NO AGENCY DESTINATION" in upper_text:
        return "No Agency Destination"

    return text


def empty_df():
    df = pd.DataFrame(columns=COLUMNS)

    df["destination"] = pd.Series(dtype="object")
    df["datetime"] = pd.Series(dtype="datetime64[ns]")
    df["date_only"] = pd.Series(dtype="object")
    df["hour"] = pd.Series(dtype="float")
    df["day_of_week"] = pd.Series(dtype="object")
    df["is_transit"] = pd.Series(dtype="bool")
    df["is_problem"] = pd.Series(dtype="bool")

    return df


def parse_lines(lines: list[str]) -> pd.DataFrame:
    df, _kept_offsets = parse_lines_with_offsets(list(enumerate(lines)))
    return df


def parse_lines_with_offsets(numbered_lines: list[tuple[int, str]]) -> tuple[pd.DataFrame, list[int]]:
    """Same parsing logic as parse_lines (parse_lines is a thin wrapper
    around this), but threads an opaque per-line offset through so a
    caller can recover exactly which offset produced each surviving
    output row -- needed because this function silently drops blank and
    short lines, so a naive positional zip(df.index, original_offsets)
    would desync the moment any line is skipped. `numbered_lines` pairs
    each line with whatever offset label the caller wants back for it
    (e.g. agent.tailer's per-line byte offsets); this module attaches no
    meaning to the value itself beyond returning it verbatim for rows
    that survive filtering. Added for Continuous Ingestion Phase E
    (deterministic source-event identity needs the START offset of the
    exact physical record that produced a row, not just the offset of
    the last line in a whole tailer read/batch).
    """
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
            "Parsed checkins | rows=0 skipped_short_rows=%s bad_datetime=0 transit_items=0 problem_items=0",
            skipped_short_rows,
        )
        return df, kept_offsets

    df = pd.DataFrame(rows, columns=COLUMNS)

    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()

    df["destination"] = df["destination_raw"].apply(normalize_destination)

    df["datetime"] = pd.to_datetime(
        df["date"] + " " + df["time"],
        format="%m/%d/%Y %I:%M:%S %p",
        errors="coerce"
    )

    df["date_only"] = df["datetime"].dt.date
    df["hour"] = df["datetime"].dt.hour
    df["day_of_week"] = df["datetime"].dt.day_name()
    df["is_transit"] = df["destination"].isin(["Westside", "Library Express"])
    df["is_problem"] = (
        df["is_problem"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
        == "TRUE"
    )

    logger.info(
        "Parsed checkins | rows=%s skipped_short_rows=%s bad_datetime=%s transit_items=%s problem_items=%s",
        len(df),
        skipped_short_rows,
        int(df["datetime"].isna().sum()),
        int(df["is_transit"].sum()),
        int(df["is_problem"].sum()),
    )

    logger.info(
        "Checkins destination breakdown: %s",
        df["destination"].value_counts(dropna=False).to_dict()
    )

    return df, kept_offsets
