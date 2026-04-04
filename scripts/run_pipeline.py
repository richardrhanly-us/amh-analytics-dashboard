import json
from pathlib import Path
from datetime import datetime

import pandas as pd

from config import load_config
from parse_checkins import load_checkins, save_checkins_csv
from parse_rejects import load_rejects, save_rejects_csv
from logger_config import get_logger
from uploader import upload_checkins_and_rejects

config = load_config()

STATUS_FILE = config["status_file"]
CHECKINS_HISTORY_FILE = config["checkins_history_file"]
REJECTS_HISTORY_FILE = config["rejects_history_file"]

logger = get_logger("run_pipeline")


def write_status_file(status, output_path=STATUS_FILE):
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)


def append_to_history(new_df, history_path):
    history_file = Path(history_path)
    history_file.parent.mkdir(parents=True, exist_ok=True)

    if history_file.exists():
        history_df = pd.read_csv(history_file, low_memory=False)
    else:
        history_df = pd.DataFrame()

    new_df = new_df.copy()
    history_df = history_df.copy()

    if "datetime" in new_df.columns:
        new_df["datetime"] = pd.to_datetime(new_df["datetime"], errors="coerce")

    if "datetime" in history_df.columns:
        history_df["datetime"] = pd.to_datetime(history_df["datetime"], errors="coerce")

    combined_df = pd.concat([history_df, new_df], ignore_index=True)

    for col in combined_df.columns:
        if combined_df[col].dtype == "object":
            combined_df[col] = combined_df[col].astype(str).str.strip()

    preferred_keys = ["datetime", "barcode", "title", "destination"]
    dedupe_cols = [col for col in preferred_keys if col in combined_df.columns]

    if dedupe_cols:
        combined_df = combined_df.drop_duplicates(subset=dedupe_cols, keep="first")
    else:
        combined_df = combined_df.drop_duplicates()

    if "datetime" in combined_df.columns:
        combined_df = combined_df.sort_values("datetime", kind="stable")

    combined_df = combined_df.reset_index(drop=True)
    combined_df.to_csv(history_file, index=False)

    return combined_df


def main():
    start_time = datetime.now()
    logger.info("Pipeline run started")

    checkins_df = load_checkins()
    logger.info("Loaded checkins: %s rows", len(checkins_df))

    rejects_df = load_rejects()
    logger.info("Loaded rejects: %s rows", len(rejects_df))

    save_checkins_csv(checkins_df)
    logger.info("Saved cleaned checkins CSV")

    save_rejects_csv(rejects_df)
    logger.info("Saved cleaned rejects CSV")

    upload_result = upload_checkins_and_rejects(checkins_df, rejects_df)
    logger.info(
        "Uploaded to Neon | checkins=%s rejects=%s",
        upload_result["uploaded_checkins"],
        upload_result["uploaded_rejects"],
    )

    checkins_history_df = append_to_history(checkins_df, CHECKINS_HISTORY_FILE)
    logger.info("Updated checkins history CSV | rows=%s", len(checkins_history_df))

    rejects_history_df = append_to_history(rejects_df, REJECTS_HISTORY_FILE)
    logger.info("Updated rejects history CSV | rows=%s", len(rejects_history_df))

    status = {
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "checkins_rows": int(len(checkins_df)),
        "rejects_rows": int(len(rejects_df)),
        "uploaded_checkins_rows": int(upload_result["uploaded_checkins"]),
        "uploaded_rejects_rows": int(upload_result["uploaded_rejects"]),
        "checkins_history_rows": int(len(checkins_history_df)),
        "rejects_history_rows": int(len(rejects_history_df)),
        "checkins_bad_datetime_rows": int(checkins_df["datetime"].isna().sum()),
        "rejects_bad_datetime_rows": int(rejects_df["datetime"].isna().sum()),
        "transit_items": int(checkins_df["is_transit"].sum()),
        "problem_items": int(checkins_df["is_problem"].sum()),
        "destination_breakdown": {
            str(k): int(v)
            for k, v in checkins_df["destination"].value_counts(dropna=False).to_dict().items()
        },
    }

    write_status_file(status)
    logger.info("Wrote pipeline status JSON")

    logger.info(
        "Pipeline summary | checkins=%s rejects=%s uploaded_checkins=%s uploaded_rejects=%s transit_items=%s bad_checkins=%s bad_rejects=%s",
        status["checkins_rows"],
        status["rejects_rows"],
        status["uploaded_checkins_rows"],
        status["uploaded_rejects_rows"],
        status["transit_items"],
        status["checkins_bad_datetime_rows"],
        status["rejects_bad_datetime_rows"],
    )

    logger.info("Destination breakdown: %s", status["destination_breakdown"])
    logger.info("Pipeline run completed successfully")

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    logger.info("Pipeline runtime: %.2f seconds", duration)

    print("Pipeline complete")
    print()
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()