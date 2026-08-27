from pathlib import Path

import pandas as pd

from .config import load_config
from .logger_config import get_logger

logger = get_logger("parse_rejects")

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


def _empty_rejects_df():
    df = pd.DataFrame(columns=COLUMNS)

    df["datetime"] = pd.Series(dtype="datetime64[ns]")
    df["date_only"] = pd.Series(dtype="object")
    df["hour"] = pd.Series(dtype="float")
    df["day_of_week"] = pd.Series(dtype="object")
    df["error_simple"] = pd.Series(dtype="object")

    return df


def _parse_reject_lines(lines):
    rows = []
    skipped_short_rows = 0

    for line in lines:
        line = line.strip()

        if not line:
            continue

        parts = line.split("|")

        if len(parts) < len(COLUMNS):
            skipped_short_rows += 1
            continue

        parts = parts[:len(COLUMNS)]
        rows.append(parts)

    if not rows:
        df = _empty_rejects_df()
        logger.info(
            "Parsed rejects | rows=0 skipped_short_rows=%s bad_datetime=0",
            skipped_short_rows,
        )
        logger.info("Reject reason breakdown: {}")
        return df

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

    return df


def load_rejects(filepath=None):
    if filepath is None:
        filepath = load_config()["raw_rejects_file"]

    logger.info("Loading rejects from %s", filepath)

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    return _parse_reject_lines(lines)


def load_rejects_incremental(filepath=None, start_offset=0):
    if filepath is None:
        filepath = load_config()["raw_rejects_file"]

    file_path = Path(filepath)

    if not file_path.exists():
        logger.warning("Rejects file not found: %s", filepath)
        return _empty_rejects_df(), 0

    file_size = file_path.stat().st_size

    if start_offset is None:
        start_offset = 0

    if start_offset < 0 or start_offset > file_size:
        logger.warning(
            "Rejects offset invalid or file rotated/truncated. Resetting offset from %s to 0",
            start_offset
        )
        start_offset = 0

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start_offset)
        lines = f.readlines()
        end_offset = f.tell()

    df = _parse_reject_lines(lines)

    logger.info(
        "Loaded rejects incrementally | start_offset=%s end_offset=%s rows=%s",
        start_offset,
        end_offset,
        len(df)
    )

    return df, end_offset


def save_rejects_csv(df, output_path=None):
    if output_path is None:
        output_path = load_config()["processed_rejects_file"]

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        df.to_csv(output_file, index=False)
        logger.info("Saved rejects CSV to %s", output_path)
    except PermissionError:
        logger.exception("Could not save rejects CSV because the file is open: %s", output_path)
        raise


if __name__ == "__main__":
    config = load_config()
    raw_rejects_file = config["raw_rejects_file"]
    processed_rejects_file = config["processed_rejects_file"]

    df = load_rejects(raw_rejects_file)
    save_rejects_csv(df, processed_rejects_file)

    logger.info("Saved cleaned rejects file to: %s", processed_rejects_file)
    logger.info("Row count: %s", len(df))
    logger.info("Bad datetime rows: %s", df["datetime"].isna().sum())
    logger.info("Top reject reasons:\n%s", df["error_simple"].value_counts())
