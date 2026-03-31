import pandas as pd
from pathlib import Path
from src.logger_config import get_logger

REJECTS_FILE = "data/raw/Rejects.txt"
PROCESSED_REJECTS_FILE = "data/processed/rejects_clean.csv"

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


def load_rejects(filepath=REJECTS_FILE):
    logger.info("Loading rejects from %s", filepath)

    rows = []
    skipped_short_rows = 0

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            parts = line.split("|")

            if len(parts) < len(COLUMNS):
                skipped_short_rows += 1
                continue

            parts = parts[:len(COLUMNS)]
            rows.append(parts)

    df = pd.DataFrame(rows, columns=COLUMNS)

    df["barcode"] = df["barcode"].astype(str).str.strip()
    df["error_message"] = df["error_message"].astype(str).str.strip()

    df["datetime"] = pd.to_datetime(
        df["date"] + " " + df["time"],
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


def save_rejects_csv(df, output_path=PROCESSED_REJECTS_FILE):
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        df.to_csv(output_file, index=False)
        logger.info("Saved rejects CSV to %s", output_path)
    except PermissionError:
        logger.exception("Could not save rejects CSV because the file is open: %s", output_path)
        raise


if __name__ == "__main__":
    df = load_rejects()
    save_rejects_csv(df)

    print("Saved cleaned rejects file to:", PROCESSED_REJECTS_FILE)
    print("Row count:", len(df))
    print()
    print("Bad datetime rows:", df["datetime"].isna().sum())
    print()
    print("Top reject reasons:")
    print(df["error_simple"].value_counts())