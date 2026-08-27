from pathlib import Path

import pandas as pd
from config import load_config

from logger_config import get_logger

config = load_config()

RAW_CHECKINS_FILE = config["raw_checkins_file"]
PROCESSED_CHECKINS_FILE = config["processed_checkins_file"]

logger = get_logger("parse_checkins")

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


def _empty_checkins_df():
    df = pd.DataFrame(columns=COLUMNS)

    df["destination"] = pd.Series(dtype="object")
    df["datetime"] = pd.Series(dtype="datetime64[ns]")
    df["date_only"] = pd.Series(dtype="object")
    df["hour"] = pd.Series(dtype="float")
    df["day_of_week"] = pd.Series(dtype="object")
    df["is_transit"] = pd.Series(dtype="bool")
    df["is_problem"] = pd.Series(dtype="bool")

    return df


def _parse_checkins_lines(lines):
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
        df = _empty_checkins_df()
        logger.info(
            "Parsed checkins | rows=0 skipped_short_rows=%s bad_datetime=0 transit_items=0 problem_items=0",
            skipped_short_rows,
        )
        return df

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

    return df

def load_checkins(filepath=None):
    if filepath is None:
        filepath = RAW_CHECKINS_FILE

    logger.info("Loading checkins from %s", filepath)

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    return _parse_checkins_lines(lines)


def load_checkins_incremental(filepath=None, start_offset=0):
    if filepath is None:
        filepath = RAW_CHECKINS_FILE

    file_path = Path(filepath)

    if not file_path.exists():
        logger.warning("Checkins file not found: %s", filepath)
        return _empty_checkins_df(), 0

    file_size = file_path.stat().st_size

    if start_offset is None:
        start_offset = 0

    if start_offset < 0 or start_offset > file_size:
        logger.warning(
            "Checkins offset invalid or file rotated/truncated. Resetting offset from %s to 0",
            start_offset
        )
        start_offset = 0

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start_offset)
        lines = f.readlines()
        end_offset = f.tell()

    df = _parse_checkins_lines(lines)

    logger.info(
        "Loaded checkins incrementally | start_offset=%s end_offset=%s rows=%s",
        start_offset,
        end_offset,
        len(df)
    )

    return df, end_offset


def save_checkins_csv(df, output_path=None):
    if output_path is None:
        output_path = PROCESSED_CHECKINS_FILE

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        df.to_csv(output_file, index=False)
        logger.info("Saved checkins CSV to %s", output_path)
    except PermissionError:
        logger.exception("Could not save checkins CSV because the file is open: %s", output_path)
        print()
        print("Could not save checkins_clean.csv because the file is open in another program.")
        print("Close the CSV in Excel and run the script again.")
        raise


if __name__ == "__main__":
    df = load_checkins()
    save_checkins_csv(df)

    print("Saved cleaned checkins file to:", PROCESSED_CHECKINS_FILE)
    print("Row count:", len(df))
    print()
    print("Destination breakdown:")
    print(df["destination"].value_counts(dropna=False))
    print()
    print("Bad datetime rows:", df["datetime"].isna().sum())
    print()
    print("Problem items:", int(df["is_problem"].sum()))
    print("Transit items:", int(df["is_transit"].sum()))
