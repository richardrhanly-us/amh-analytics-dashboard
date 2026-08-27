import re
from pathlib import Path

import pandas as pd
from config import load_config

from logger_config import get_logger

config = load_config()

RAW_ACS_FILE = config["raw_acs_file"]
PROCESSED_ACS_FILE = config["processed_acs_file"]

logger = get_logger("parse_acs")

TAG_PATTERN = re.compile(r"([A-Z]{2})([^|]*)")


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


def _parse_lines(lines):
    rows = []

    for line in lines:
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

    df = pd.DataFrame(rows)

    if df.empty:
        df = pd.DataFrame(columns=[
            "date",
            "time",
            "datetime",
            "message_code",
            "barcode",
            "title",
            "patron_id",
            "destination",
            "raw_message",
        ])

    logger.info("Parsed ACS rows=%s", len(df))
    return df


def load_acs(filepath=None):
    if filepath is None:
        filepath = RAW_ACS_FILE

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    return _parse_lines(lines)


def load_acs_incremental(filepath=None, start_offset=0):
    if filepath is None:
        filepath = RAW_ACS_FILE

    file_path = Path(filepath)

    if not file_path.exists():
        logger.warning("ACS file not found: %s", filepath)
        return pd.DataFrame(columns=[
            "date",
            "time",
            "datetime",
            "message_code",
            "barcode",
            "title",
            "patron_id",
            "destination",
            "raw_message",
        ]), 0

    file_size = file_path.stat().st_size

    if start_offset is None:
        start_offset = 0

    if start_offset < 0 or start_offset > file_size:
        logger.warning(
            "ACS offset invalid or file rotated/truncated. Resetting offset from %s to 0",
            start_offset
        )
        start_offset = 0

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start_offset)
        lines = f.readlines()
        end_offset = f.tell()

    df = _parse_lines(lines)

    logger.info(
        "Loaded ACS incrementally | start_offset=%s end_offset=%s rows=%s",
        start_offset,
        end_offset,
        len(df)
    )

    return df, end_offset


def save_acs_csv(df):
    Path(PROCESSED_ACS_FILE).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(PROCESSED_ACS_FILE, index=False)
    logger.info("Saved ACS CSV")


if __name__ == "__main__":
    df = load_acs()
    save_acs_csv(df)
    logger.info("Saved cleaned ACS file to: %s", PROCESSED_ACS_FILE)
    logger.info("Row count: %s", len(df))
    logger.info("Bad datetime rows: %s", df["datetime"].isna().sum())
