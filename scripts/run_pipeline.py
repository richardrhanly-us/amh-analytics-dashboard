import json
from pathlib import Path
from datetime import datetime

from scripts.parse_checkins import load_checkins, save_checkins_csv
from scripts.parse_rejects import load_rejects, save_rejects_csv
from src.logger_config import get_logger

STATUS_FILE = "data/processed/pipeline_status.json"

logger = get_logger("run_pipeline")


def write_status_file(status, output_path=STATUS_FILE):
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)


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

    status = {
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "checkins_rows": int(len(checkins_df)),
        "rejects_rows": int(len(rejects_df)),
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
        "Pipeline summary | checkins=%s rejects=%s transit_items=%s bad_checkins=%s bad_rejects=%s",
        status["checkins_rows"],
        status["rejects_rows"],
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
