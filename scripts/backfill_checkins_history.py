import pandas as pd
from pathlib import Path

from scripts.parse_checkins import load_checkins

HISTORY_FILE = Path("data/processed/checkins_history.csv")
HISTORICAL_RAW_FILE = Path(r"C:\amh_analytics\data\raw\checkins_history_source.txt")
CURRENT_CLEAN_FILE = Path("data/processed/checkins_clean.csv")


def main():
    if not HISTORICAL_RAW_FILE.exists():
        print(f"Historical raw file not found: {HISTORICAL_RAW_FILE}")
        return

    print(f"Loading historical raw file: {HISTORICAL_RAW_FILE}")
    historical_df = load_checkins(str(HISTORICAL_RAW_FILE)).copy()

    frames = [historical_df]

    if CURRENT_CLEAN_FILE.exists():
        print(f"Loading current clean file: {CURRENT_CLEAN_FILE}")
        current_df = pd.read_csv(CURRENT_CLEAN_FILE, low_memory=False)

        if "datetime" in current_df.columns:
            current_df["datetime"] = pd.to_datetime(current_df["datetime"], errors="coerce")

        frames.append(current_df)

    combined_df = pd.concat(frames, ignore_index=True)

    dedupe_cols = ["barcode", "datetime", "title", "destination_raw", "bin"]
    dedupe_cols = [col for col in dedupe_cols if col in combined_df.columns]

    before = len(combined_df)
    combined_df = combined_df.drop_duplicates(subset=dedupe_cols).copy()
    after = len(combined_df)

    combined_df["datetime"] = pd.to_datetime(combined_df["datetime"], errors="coerce")
    combined_df = combined_df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    combined_df.to_csv(HISTORY_FILE, index=False)

    print()
    print(f"Saved rebuilt history file to: {HISTORY_FILE}")
    print(f"Rows before dedupe: {before}")
    print(f"Rows after dedupe: {after}")
    print(f"Min datetime: {combined_df['datetime'].min()}")
    print(f"Max datetime: {combined_df['datetime'].max()}")


if __name__ == "__main__":
    main()
