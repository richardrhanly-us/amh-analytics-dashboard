import pandas as pd
from pathlib import Path
from src.logger_config import get_logger

RAW_CHECKINS_FILE = r"C:\TLCFinalDlls\Checkins.txt"
PROCESSED_CHECKINS_FILE = "data/processed/checkins_clean.csv"

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


def load_checkins(filepath=RAW_CHECKINS_FILE):
    logger.info("Loading checkins from %s", filepath)

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

    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()

    df["destination"] = df["destination_raw"].apply(normalize_destination)

    df["datetime"] = pd.to_datetime(
        df["date"] + " " + df["time"],
        errors="coerce"
    )

    df["date_only"] = df["datetime"].dt.date
    df["hour"] = df["datetime"].dt.hour
    df["day_of_week"] = df["datetime"].dt.day_name()
    df["is_transit"] = df["destination"].isin(["Westside", "Library Express"])
    df["is_problem"] = df["is_problem"].str.upper() == "TRUE"

    logger.info(
        "Parsed checkins | rows=%s skipped_short_rows=%s bad_datetime=%s transit_items=%s problem_items=%s",
        len(df),
        skipped_short_rows,
        int(df["datetime"].isna().sum()),
        int(df["is_transit"].sum()),
        int(df["is_problem"].sum()),
    )

    logger.info("Checkins destination breakdown: %s", df["destination"].value_counts(dropna=False).to_dict())

    return df


def save_checkins_csv(df, output_path=PROCESSED_CHECKINS_FILE):
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
