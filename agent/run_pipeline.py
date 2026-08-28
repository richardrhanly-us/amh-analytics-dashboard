import json
from datetime import datetime
from pathlib import Path

from .config import load_config
from .logger_config import get_logger
from .parse_acs import load_acs_incremental, save_acs_csv
from .parse_checkins import load_checkins_incremental, save_checkins_csv
from .parse_rejects import load_rejects_incremental, save_rejects_csv
from .uploader import upload_checkins_and_rejects, upload_pipeline_status

config = load_config()

STATUS_FILE = config["status_file"]
STATE_FILE = "data\\processed\\pipeline_state.json"

logger = get_logger("run_pipeline")


def write_status_file(status, output_path=STATUS_FILE):
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)


def load_status_file(path=STATUS_FILE):
    status_path = Path(path)

    if not status_path.exists():
        return {}

    try:
        with open(status_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}




def get_file_state(path):
    file_path = Path(path)

    if not file_path.exists():
        return {
            "mtime": None,
            "size": None,
        }

    stat = file_path.stat()
    return {
        "mtime": stat.st_mtime,
        "size": stat.st_size,
    }


def load_pipeline_state(path=STATE_FILE):
    state_path = Path(path)

    if not state_path.exists():
        return {}

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_pipeline_state(
    checkins_path,
    rejects_path,
    acs_path,
    checkins_offset,
    rejects_offset,
    acs_offset,
    path=STATE_FILE
):
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    checkins_state = get_file_state(checkins_path)
    rejects_state = get_file_state(rejects_path)
    acs_state = get_file_state(acs_path)

    state = {
        "checkins_mtime": checkins_state["mtime"],
        "checkins_size": checkins_state["size"],
        "rejects_mtime": rejects_state["mtime"],
        "rejects_size": rejects_state["size"],
        "acs_mtime": acs_state["mtime"],
        "acs_size": acs_state["size"],
        "checkins_offset": checkins_offset,
        "rejects_offset": rejects_offset,
        "acs_offset": acs_offset,
    }

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def main():
    start_time = datetime.now().astimezone()
    logger.info("Pipeline run started")

    existing_status = load_status_file()

    attempt_status = existing_status.copy()
    attempt_status["last_attempt"] = start_time.isoformat(timespec="seconds")
    attempt_status["status"] = "started"
    write_status_file(attempt_status)
    logger.info("Wrote pipeline attempt status")
    upload_pipeline_status(attempt_status)

    previous_state = load_pipeline_state()

    current_checkins_state = get_file_state(config["raw_checkins_file"])
    current_rejects_state = get_file_state(config["raw_rejects_file"])
    current_acs_state = get_file_state(config["raw_acs_file"])

    checkins_unchanged = (
        previous_state.get("checkins_mtime") == current_checkins_state["mtime"]
        and previous_state.get("checkins_size") == current_checkins_state["size"]
    )

    rejects_unchanged = (
        previous_state.get("rejects_mtime") == current_rejects_state["mtime"]
        and previous_state.get("rejects_size") == current_rejects_state["size"]
    )

    acs_unchanged = (
        previous_state.get("acs_mtime") == current_acs_state["mtime"]
        and previous_state.get("acs_size") == current_acs_state["size"]
    )

    if checkins_unchanged and rejects_unchanged and acs_unchanged:
        skip_status = existing_status.copy()
        skip_status["last_attempt"] = start_time.isoformat(timespec="seconds")
        skip_status["status"] = "skipped_no_source_changes"

        skip_status["checkins_rows"] = 0
        skip_status["rejects_rows"] = 0
        skip_status["acs_rows"] = 0

        skip_status["uploaded_checkins_rows"] = 0
        skip_status["uploaded_rejects_rows"] = 0
        skip_status["uploaded_acs_rows"] = 0

        skip_status["checkins_bad_datetime_rows"] = 0
        skip_status["rejects_bad_datetime_rows"] = 0
        skip_status["acs_bad_datetime_rows"] = 0

        write_status_file(skip_status)
        upload_pipeline_status(skip_status)

        logger.info("No source file changes detected; skipping run")
        raise SystemExit(0)

    previous_checkins_offset = previous_state.get("checkins_offset", 0)
    checkins_df, new_checkins_offset = load_checkins_incremental(
        config["raw_checkins_file"],
        start_offset=previous_checkins_offset
    )
    logger.info(
        "Loaded checkins incrementally: %s rows | old_offset=%s new_offset=%s",
        len(checkins_df),
        previous_checkins_offset,
        new_checkins_offset
    )

    previous_rejects_offset = previous_state.get("rejects_offset", 0)
    rejects_df, new_rejects_offset = load_rejects_incremental(
        config["raw_rejects_file"],
        start_offset=previous_rejects_offset
    )
    logger.info(
        "Loaded rejects incrementally: %s rows | old_offset=%s new_offset=%s",
        len(rejects_df),
        previous_rejects_offset,
        new_rejects_offset
    )

    previous_acs_offset = previous_state.get("acs_offset", 0)
    acs_df, new_acs_offset = load_acs_incremental(
        config["raw_acs_file"],
        start_offset=previous_acs_offset
    )
    logger.info(
        "Loaded ACS incrementally: %s rows | old_offset=%s new_offset=%s",
        len(acs_df),
        previous_acs_offset,
        new_acs_offset
    )

    save_checkins_csv(checkins_df)
    logger.info("Saved cleaned checkins CSV")

    save_rejects_csv(rejects_df)
    logger.info("Saved cleaned rejects CSV")

    save_acs_csv(acs_df)
    logger.info("Saved cleaned ACS CSV")

    upload_result = upload_checkins_and_rejects(checkins_df, rejects_df, acs_df)

    if upload_result is None:
        fail_status = existing_status.copy()
        fail_status["last_attempt"] = start_time.isoformat(timespec="seconds")
        fail_status["status"] = "failed_upload_none"
        write_status_file(fail_status)
        upload_pipeline_status(fail_status)

        logger.error("Upload returned no result; not updating history/state")
        raise RuntimeError("Upload failed; refusing to update history or pipeline state")

    if not isinstance(upload_result, dict):
        fail_status = existing_status.copy()
        fail_status["last_attempt"] = start_time.isoformat(timespec="seconds")
        fail_status["status"] = "failed_upload_invalid_type"
        write_status_file(fail_status)
        upload_pipeline_status(fail_status)

        logger.error("Upload returned invalid result type: %s", type(upload_result))
        raise RuntimeError("Upload failed; refusing to update history or pipeline state")

    if (
        "uploaded_checkins" not in upload_result
        or "uploaded_rejects" not in upload_result
        or "uploaded_acs" not in upload_result
    ):
        fail_status = existing_status.copy()
        fail_status["last_attempt"] = start_time.isoformat(timespec="seconds")
        fail_status["status"] = "failed_upload_missing_keys"
        write_status_file(fail_status)
        upload_pipeline_status(fail_status)

        logger.error("Upload result missing expected keys: %s", upload_result)
        raise RuntimeError("Upload failed; refusing to update history or pipeline state")

    logger.info(
        "Uploaded to Neon | checkins=%s rejects=%s acs=%s",
        upload_result["uploaded_checkins"],
        upload_result["uploaded_rejects"],
        upload_result["uploaded_acs"],
    )



    save_pipeline_state(
        config["raw_checkins_file"],
        config["raw_rejects_file"],
        config["raw_acs_file"],
        new_checkins_offset,
        new_rejects_offset,
        new_acs_offset
    )
    logger.info("Updated pipeline state file")

    finished_time = datetime.now().astimezone()

    new_rows_uploaded = (
        int(upload_result.get("uploaded_checkins", 0)) > 0
        or int(upload_result.get("uploaded_rejects", 0)) > 0
        or int(upload_result.get("uploaded_acs", 0)) > 0
    )

    final_status_code = "completed"

    if not new_rows_uploaded:
        final_status_code = "completed_no_new_rows"

    status = {
        "last_attempt": start_time.isoformat(timespec="seconds"),
        "last_run": finished_time.isoformat(timespec="seconds"),
        "updated_at": finished_time.isoformat(timespec="seconds"),
        "status": final_status_code,

        "checkins_rows": len(checkins_df),
        "rejects_rows": len(rejects_df),
        "acs_rows": len(acs_df),

        "uploaded_checkins_rows": int(upload_result.get("uploaded_checkins", 0)),
        "uploaded_rejects_rows": int(upload_result.get("uploaded_rejects", 0)),
        "uploaded_acs_rows": int(upload_result.get("uploaded_acs", 0)),


        "checkins_bad_datetime_rows": int(checkins_df["datetime"].isna().sum()) if "datetime" in checkins_df.columns else 0,
        "rejects_bad_datetime_rows": int(rejects_df["datetime"].isna().sum()) if "datetime" in rejects_df.columns else 0,
        "acs_bad_datetime_rows": int(acs_df["datetime"].isna().sum()) if "datetime" in acs_df.columns else 0,

        "transit_items": int(checkins_df["is_transit"].sum()) if "is_transit" in checkins_df.columns else 0,
        "problem_items": int(checkins_df["is_problem"].sum()) if "is_problem" in checkins_df.columns else 0,

        "destination_breakdown": {
            str(k): int(v)
            for k, v in checkins_df["destination"].value_counts(dropna=False).to_dict().items()
        } if "destination" in checkins_df.columns else {},
    }

    write_status_file(status)
    upload_pipeline_status(status)
    logger.info("Wrote pipeline status JSON")

    logger.info(
        "Pipeline summary | checkins=%s rejects=%s acs_incremental=%s uploaded_checkins=%s uploaded_rejects=%s uploaded_acs=%s",
        status["checkins_rows"],
        status["rejects_rows"],
        len(acs_df),
        status["uploaded_checkins_rows"],
        status["uploaded_rejects_rows"],
        upload_result["uploaded_acs"],
    )

    logger.info("Destination breakdown: %s", status["destination_breakdown"])
    logger.info("Pipeline run completed successfully")

    end_time = datetime.now().astimezone()
    duration = (end_time - start_time).total_seconds()
    logger.info("Pipeline runtime: %.2f seconds", duration)


if __name__ == "__main__":
    main()
