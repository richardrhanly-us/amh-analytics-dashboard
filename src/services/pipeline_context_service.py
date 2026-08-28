from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from ui_components import format_relative_time


def _parse_status_datetime(value, local_tz):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(local_tz)
        return dt.astimezone(local_tz)
    except Exception:
        return None


def _parse_local_datetime(value, local_tz):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=local_tz)
        return dt.astimezone(local_tz)
    except Exception:
        return None


def build_pipeline_context(pipeline_status, df_live_raw, now_ct, local_tz, theme_base):
    checkins_updated = None
    if len(df_live_raw) > 0 and "datetime" in df_live_raw.columns:
        latest_dt = df_live_raw["datetime"].max()
        if pd.notna(latest_dt):
            if getattr(latest_dt, "tzinfo", None) is None:
                checkins_updated = latest_dt.tz_localize(local_tz)
            else:
                checkins_updated = latest_dt.tz_convert(local_tz)

    pipeline_status_label = "Unknown"
    pipeline_status_color = "#6b7280"
    pipeline_status_bg = "#f9fafb"

    status_updated_dt = None
    last_run = None
    last_attempt = None

    checkins_rows = 0
    rejects_rows = 0
    transit_items = 0
    problem_items = 0
    uploaded_checkins_rows = 0
    uploaded_rejects_rows = 0
    checkins_bad_datetime_rows = 0
    rejects_bad_datetime_rows = 0
    destination_breakdown = {}

    if pipeline_status:
        status_updated_raw = pipeline_status.get("updated_at")
        last_run_raw = pipeline_status.get("last_run")
        last_attempt_raw = pipeline_status.get("last_attempt")

        checkins_rows = pipeline_status.get("checkins_rows", 0)
        rejects_rows = pipeline_status.get("rejects_rows", 0)
        transit_items = pipeline_status.get("transit_items", 0)
        problem_items = pipeline_status.get("problem_items", 0)
        uploaded_checkins_rows = pipeline_status.get("uploaded_checkins_rows", 0)
        uploaded_rejects_rows = pipeline_status.get("uploaded_rejects_rows", 0)
        checkins_bad_datetime_rows = pipeline_status.get("checkins_bad_datetime_rows", 0)
        rejects_bad_datetime_rows = pipeline_status.get("rejects_bad_datetime_rows", 0)
        destination_breakdown = pipeline_status.get("destination_breakdown", {}) or {}

        status_updated_dt = _parse_status_datetime(status_updated_raw, local_tz)
        last_run = _parse_local_datetime(last_run_raw, local_tz)
        last_attempt = _parse_local_datetime(last_attempt_raw, local_tz)

    app_refreshed_str = now_ct.strftime("%b %d, %Y %I:%M %p")

    pipeline_status_written_str = (
        status_updated_dt.strftime("%b %d, %Y %I:%M %p")
        if status_updated_dt else "N/A"
    )
    pipeline_last_run_str = (
        last_run.strftime("%b %d, %Y %I:%M %p")
        if last_run else "N/A"
    )
    pipeline_last_attempt_str = (
        last_attempt.strftime("%b %d, %Y %I:%M %p")
        if last_attempt else "N/A"
    )

    pipeline_status_written_ago = format_relative_time(status_updated_dt, now_ct)
    pipeline_last_run_ago = format_relative_time(last_run, now_ct)
    pipeline_last_attempt_ago = format_relative_time(last_attempt, now_ct)

    latest_checkin_str = (
        checkins_updated.strftime("%b %d, %Y %I:%M %p")
        if checkins_updated else "N/A"
    )
    latest_checkin_ago = format_relative_time(checkins_updated, now_ct)

    # During Continuous Ingestion Phase 0's parallel validation, a branch's
    # pipeline_status row may carry two independent vocabularies: the
    # legacy scheduled pipeline's `status` (started/completed/failed*) and
    # the new continuous-agent heartbeat's `health_status` (healthy/
    # degraded/auth_failure). A branch that has ever reported a heartbeat
    # is understood via health_status; legacy status parsing remains the
    # fallback for branches that haven't (or haven't yet) cut over --
    # both must keep working at the same time, not one replacing the
    # other.
    health_status = pipeline_status.get("health_status") if pipeline_status else None
    pipeline_run_status = pipeline_status.get("status", "unknown") if pipeline_status else "unknown"
    status_code_text = str(health_status) if health_status else str(pipeline_run_status)

    if health_status == "healthy":
        pipeline_status_label = "Pipeline Healthy"
        pipeline_status_color = "#059669"
        pipeline_status_bg = "rgba(5, 150, 105, 0.14)" if theme_base == "dark" else "#ecfdf5"
        pipeline_result_text = "Continuous agent reporting healthy"
    elif health_status == "degraded":
        pipeline_status_label = "Pipeline Degraded"
        pipeline_status_color = "#d97706"
        pipeline_status_bg = "rgba(217, 119, 6, 0.14)" if theme_base == "dark" else "#fffbeb"
        pipeline_result_text = "Continuous agent reporting degraded -- backlog, quarantine, or unresolved delivery failure"
    elif health_status == "auth_failure":
        pipeline_status_label = "Pipeline Auth Failure"
        pipeline_status_color = "#dc2626"
        pipeline_status_bg = "rgba(220, 38, 38, 0.14)" if theme_base == "dark" else "#fef2f2"
        pipeline_result_text = "Continuous agent cannot authenticate -- check the agent token"
    elif pipeline_run_status == "completed":
        pipeline_status_label = "Pipeline Healthy"
        pipeline_status_color = "#059669"
        pipeline_status_bg = "rgba(5, 150, 105, 0.14)" if theme_base == "dark" else "#ecfdf5"
        pipeline_result_text = (
            f"Uploaded {uploaded_checkins_rows:,} new checkins and {uploaded_rejects_rows:,} new rejects this run"
        )
    elif pipeline_run_status == "completed_no_new_rows":
        pipeline_status_label = "Pipeline Healthy"
        pipeline_status_color = "#059669"
        pipeline_status_bg = "rgba(5, 150, 105, 0.14)" if theme_base == "dark" else "#ecfdf5"
        pipeline_result_text = "Run completed, but no new rows were uploaded"
    elif pipeline_run_status == "skipped_no_source_changes":
        pipeline_status_label = "Pipeline Healthy"
        pipeline_status_color = "#059669"
        pipeline_status_bg = "rgba(5, 150, 105, 0.14)" if theme_base == "dark" else "#ecfdf5"
        pipeline_result_text = "No new source changes detected this run"
    elif str(pipeline_run_status).startswith("failed"):
        pipeline_status_label = "Pipeline Failed"
        pipeline_status_color = "#dc2626"
        pipeline_status_bg = "rgba(220, 38, 38, 0.14)" if theme_base == "dark" else "#fef2f2"
        pipeline_result_text = "Latest run failed"
    elif pipeline_run_status == "started":
        pipeline_status_label = "Pipeline Running"
        pipeline_status_color = "#d97706"
        pipeline_status_bg = "rgba(217, 119, 6, 0.14)" if theme_base == "dark" else "#fffbeb"
        pipeline_result_text = "Run in progress"
    else:
        pipeline_status_label = "Pipeline Status Unknown"
        pipeline_status_color = "#94a3b8" if theme_base == "dark" else "#6b7280"
        pipeline_status_bg = "rgba(148, 163, 184, 0.12)" if theme_base == "dark" else "#f9fafb"
        pipeline_result_text = "Unknown"

    if health_status is not None:
        pipeline_expanded = health_status != "healthy"
    else:
        pipeline_expanded = pipeline_run_status not in ["completed", "skipped_no_source_changes"]

    if isinstance(destination_breakdown, dict) and destination_breakdown:
        destination_breakdown_text = ", ".join(
            [f"{k}: {int(v):,}" for k, v in destination_breakdown.items()]
        )
    else:
        destination_breakdown_text = "N/A"

    return {
        "pipeline_status_label": pipeline_status_label,
        "pipeline_status_color": pipeline_status_color,
        "pipeline_status_bg": pipeline_status_bg,
        "pipeline_expanded": pipeline_expanded,
        "app_refreshed_str": app_refreshed_str,
        "latest_checkin_str": latest_checkin_str,
        "latest_checkin_ago": latest_checkin_ago,
        "pipeline_status_written_str": pipeline_status_written_str,
        "pipeline_status_written_ago": pipeline_status_written_ago,
        "pipeline_last_attempt_str": pipeline_last_attempt_str,
        "pipeline_last_attempt_ago": pipeline_last_attempt_ago,
        "pipeline_last_run_str": pipeline_last_run_str,
        "pipeline_last_run_ago": pipeline_last_run_ago,
        "pipeline_result_text": pipeline_result_text,
        "status_code_text": status_code_text,
        "checkins_rows": checkins_rows,
        "rejects_rows": rejects_rows,
        "uploaded_checkins_rows": uploaded_checkins_rows,
        "uploaded_rejects_rows": uploaded_rejects_rows,
        "checkins_bad_datetime_rows": checkins_bad_datetime_rows,
        "rejects_bad_datetime_rows": rejects_bad_datetime_rows,
        "transit_items": transit_items,
        "problem_items": problem_items,
        "destination_breakdown_text": destination_breakdown_text,
    }
