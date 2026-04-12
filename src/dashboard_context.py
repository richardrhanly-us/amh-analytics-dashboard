from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from metrics import (
    get_date_filtered_df,
    get_today_metrics,
    get_overall_metrics,
    get_historical_reject_baseline,
    build_acs_item_summary,
)
from reject_logic import simplify_error
from alerts import get_system_alerts
from ui_components import format_hour, format_relative_time


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


def _get_theme_palette(theme_base):
    if theme_base == "dark":
        return {
            "info_bg": "rgba(37, 99, 235, 0.14)",
            "info_border": "#3b82f6",
            "info_title": "#93c5fd",
            "info_text": "#dbeafe",
            "danger_bg": "rgba(220, 38, 38, 0.14)",
            "danger_border": "#ef4444",
            "danger_title": "#fca5a5",
            "danger_text": "#fee2e2",
        }

    return {
        "info_bg": "#eff6ff",
        "info_border": "#2563eb",
        "info_title": "#1d4ed8",
        "info_text": "#1e3a8a",
        "danger_bg": "#fef2f2",
        "danger_border": "#dc2626",
        "danger_title": "#991b1b",
        "danger_text": "#7f1d1d",
    }


def _build_pipeline_context(pipeline_status, df_live_raw, now_ct, local_tz, theme_base):
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

    pipeline_run_status = pipeline_status.get("status", "unknown") if pipeline_status else "unknown"
    status_code_text = str(pipeline_run_status)

    if pipeline_run_status == "completed":
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


def build_dashboard_context(
    df_live_raw,
    df_history_raw,
    rejects_live_raw,
    rejects_history_raw,
    acs_live_raw,
    acs_history_raw,
    pipeline_status,
    refresh_count,
    start_date,
    end_date,
    today,
    now_ct,
    app_tz,
    transit_labels,
    transit_home_label,
    branch_services_names,
    collection_services_names,
    branch_services_da_patterns,
    collection_services_da_patterns,
    library_name,
    branch_name,
    system_name,
    theme_base,
):
    df_live_raw = df_live_raw.copy()
    df_history_raw = df_history_raw.copy()
    rejects_live_raw = rejects_live_raw.copy()
    rejects_history_raw = rejects_history_raw.copy()
    acs_live_raw = acs_live_raw.copy()
    acs_history_raw = acs_history_raw.copy()

    if "error_message" in rejects_live_raw.columns:
        rejects_live_raw["error_simple"] = rejects_live_raw["error_message"].apply(simplify_error)
    if "error_message" in rejects_history_raw.columns:
        rejects_history_raw["error_simple"] = rejects_history_raw["error_message"].apply(simplify_error)

    pipeline_ctx = _build_pipeline_context(
        pipeline_status=pipeline_status,
        df_live_raw=df_live_raw,
        now_ct=now_ct,
        local_tz=app_tz,
        theme_base=theme_base,
    )

    df = get_date_filtered_df(df_history_raw, start_date, end_date)
    rejects_df = get_date_filtered_df(rejects_history_raw, start_date, end_date)

    if len(df) > 0 and "datetime" in df.columns:
        df["date"] = df["datetime"].dt.date
        df["day_of_week"] = df["datetime"].dt.day_name()
    else:
        df["date"] = pd.Series(dtype="object")
        df["day_of_week"] = pd.Series(dtype="object")

    if len(rejects_df) > 0 and "datetime" in rejects_df.columns:
        rejects_df["date"] = rejects_df["datetime"].dt.date
        rejects_df["day_of_week"] = rejects_df["datetime"].dt.day_name()
    else:
        rejects_df["date"] = pd.Series(dtype="object")
        rejects_df["day_of_week"] = pd.Series(dtype="object")

    df["destination_clean"] = df["destination"].astype(str).str.strip() if "destination" in df.columns else ""
    df["destination_upper"] = df["destination_clean"].astype(str).str.upper()
    df["transit_destination"] = None

    for transit_label in transit_labels:
        label_upper = transit_label.upper()
        match_mask = df["destination_upper"] == label_upper
        df.loc[match_mask, "transit_destination"] = transit_label

    df["destination_report"] = df["destination_clean"].copy()
    df.loc[df["destination_report"] == "1", "destination_report"] = transit_home_label

    for transit_label in transit_labels:
        label_upper = transit_label.upper()
        match_mask = df["destination_upper"] == label_upper
        df.loc[match_mask, "destination_report"] = transit_label

    df["destination_clean"] = df["destination_report"]

    overall_metrics = get_overall_metrics(df, rejects_df)

    overview_transit_counts_map = {}
    overview_transit_pct_map = {}
    for transit_label in transit_labels:
        transit_count = int((df["transit_destination"] == transit_label).sum()) if len(df) > 0 else 0
        overview_transit_counts_map[transit_label] = transit_count
        overview_transit_pct_map[transit_label] = (transit_count / len(df) * 100) if len(df) > 0 else 0

    date_range_text = f"{start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}"

    worst_day_label = "N/A"
    worst_rate = None

    checkins_daily = df["datetime"].dt.date.value_counts().sort_index() if len(df) > 0 else pd.Series(dtype=float)
    rejects_daily = rejects_df["datetime"].dt.date.value_counts().sort_index() if len(rejects_df) > 0 else pd.Series(dtype=float)

    daily_combined = pd.DataFrame()
    if len(df) > 0:
        daily_combined = pd.DataFrame({
            "checkins": checkins_daily,
            "rejects": rejects_daily,
        }).fillna(0)

        daily_combined = daily_combined[daily_combined["checkins"] > 0]

        if len(daily_combined) > 0:
            daily_combined["reject_rate"] = (daily_combined["rejects"] / daily_combined["checkins"]) * 100
            worst_day = daily_combined["reject_rate"].idxmax()
            worst_rate = daily_combined["reject_rate"].max()
            worst_day_label = pd.to_datetime(worst_day).strftime("%a, %b %d")

    if len(rejects_df) > 0:
        top_issue = rejects_df["error_simple"].value_counts().idxmax()
    else:
        top_issue = "N/A"

    peak_failure_window_text = "N/A"
    if len(rejects_df) > 0:
        peak_failure_hour_counts = rejects_df["datetime"].dt.hour.value_counts().sort_index()
        peak_failure_hour = peak_failure_hour_counts.idxmax()
        peak_failure_count = peak_failure_hour_counts.max()
        peak_failure_pct = (peak_failure_count / len(rejects_df)) * 100
        peak_failure_window_text = format_hour(peak_failure_hour)

    attention_items = []
    overall_daily_avg_reject = daily_combined["reject_rate"].mean() if len(daily_combined) > 0 else 0

    if worst_rate is not None and overall_daily_avg_reject > 0:
        spike_ratio = worst_rate / overall_daily_avg_reject
        if spike_ratio >= 2:
            attention_items.append(
                f"Daily rejects spiked on {worst_day_label} to {worst_rate:.2f}%, about {spike_ratio:.1f}x normal."
            )
        elif worst_rate >= 5:
            attention_items.append(
                f"Daily rejects peaked on {worst_day_label} at {worst_rate:.2f}%. Review what changed that day."
            )

    if top_issue == "Item Not Found":
        attention_items.append("Item Not Found is leading failures. Check ILS connection and RFID tag condition.")
    elif top_issue == "ILS / ACS Failure":
        attention_items.append("ILS/ACS failures detected. Check system connectivity.")
    elif top_issue == "RFID Collision":
        attention_items.append("RFID collisions detected. Items may be stacked or scanned together.")
    elif top_issue == "Routing Error":
        attention_items.append("Routing errors present. Verify destination mappings.")
    elif top_issue == "Call Number / Config Error":
        attention_items.append("Call number/config issues detected. Review item setup.")

    if peak_failure_window_text != "N/A":
        attention_items.append(f"Failures peak at {peak_failure_window_text}. Check conditions during that hour.")

    if len(transit_labels) > 0:
        primary_transit_label = transit_labels[0]
        primary_transit_pct = overview_transit_pct_map.get(primary_transit_label, 0)
        if primary_transit_pct >= 10:
            attention_items.append(
                f"{primary_transit_label} transit share is high. Watch for routing or branch-related issues."
            )

    if not attention_items:
        attention_title = "Recommended Attention"
        attention_color = "#059669"
        attention_text = "No major issues stand out in the selected date range."
    else:
        attention_title = "Recommended Attention"
        attention_color = "#d97706"
        attention_text = " ".join(attention_items)

    today_metrics = get_today_metrics(df_live_raw, rejects_live_raw, today)
    no_today_data = len(today_metrics["today_df"]) == 0

    current_speed = 0
    current_speed_fill_pct = 0
    max_observed_hourly_throughput = 1

    if len(today_metrics["today_df"]) > 0 and "datetime" in today_metrics["today_df"].columns:
        today_df_for_speed = today_metrics["today_df"].copy()
        today_df_for_speed["datetime"] = pd.to_datetime(today_df_for_speed["datetime"], errors="coerce")
        today_df_for_speed = today_df_for_speed.dropna(subset=["datetime"])

        if len(today_df_for_speed) > 0:
            latest_activity_hour = today_df_for_speed["datetime"].max().hour
            current_speed = int((today_df_for_speed["datetime"].dt.hour == latest_activity_hour).sum())

    if len(df_history_raw) > 0 and "datetime" in df_history_raw.columns:
        hourly_baseline_df = df_history_raw.copy()
        hourly_baseline_df["datetime"] = pd.to_datetime(hourly_baseline_df["datetime"], errors="coerce")
        hourly_baseline_df = hourly_baseline_df.dropna(subset=["datetime"])

        if len(hourly_baseline_df) > 0:
            hourly_baseline_df["date"] = hourly_baseline_df["datetime"].dt.date
            hourly_baseline_df["hour"] = hourly_baseline_df["datetime"].dt.hour

            hourly_counts = (
                hourly_baseline_df.groupby(["date", "hour"])
                .size()
                .reset_index(name="checkins")
            )

            if len(hourly_counts) > 0:
                max_observed_hourly_throughput = int(hourly_counts["checkins"].max())

    max_observed_hourly_throughput = max(max_observed_hourly_throughput, 1)
    current_speed_fill_pct = current_speed / max_observed_hourly_throughput

    today_metrics["current_speed"] = current_speed
    today_metrics["current_speed_fill_pct"] = current_speed_fill_pct
    today_metrics["max_observed_hourly_throughput"] = max_observed_hourly_throughput

    today_df = today_metrics["today_df"]
    today_rejects_df = today_metrics["today_rejects_df"]
    today_checkins = today_metrics["today_checkins"]
    today_rejects = today_metrics["today_rejects"]
    today_total_transit = today_metrics["today_total_transit"]
    today_peak_hour = today_metrics["today_peak_hour"]
    today_peak_hour_count = today_metrics["today_peak_hour_count"]
    today_peak_hour_pct = today_metrics["today_peak_hour_pct"]
    today_reject_rate = today_metrics["today_reject_rate"]

    historical_checkins_df = df_history_raw[df_history_raw["datetime"].dt.date < today].copy()
    historical_transit_pct_map = {}

    if len(historical_checkins_df) > 0:
        historical_checkins_df["destination_clean"] = historical_checkins_df["destination"].astype(str).str.strip()
        historical_checkins_df["destination_upper"] = historical_checkins_df["destination_clean"].str.upper()
        historical_checkins_df["transit_destination"] = None

        for transit_label in transit_labels:
            label_upper = transit_label.upper()
            match_mask = historical_checkins_df["destination_upper"] == label_upper
            historical_checkins_df.loc[match_mask, "transit_destination"] = transit_label

        for transit_label in transit_labels:
            historical_transit_pct_map[transit_label] = (
                (historical_checkins_df["transit_destination"] == transit_label).sum()
                / len(historical_checkins_df)
            ) * 100

    today_transit_counts_map = {}
    for transit_label in transit_labels:
        transit_count = int(
            (today_df["destination"].astype(str).str.strip().str.upper() == transit_label.upper()).sum()
        ) if len(today_df) > 0 else 0
        today_transit_counts_map[transit_label] = transit_count

    today_transit_pct_map = {
        transit_label: ((count / today_checkins) * 100 if today_checkins > 0 else 0)
        for transit_label, count in today_transit_counts_map.items()
    }

    if "datetime" in today_df.columns:
        today_hourly_checkins = today_df["datetime"].dt.hour.value_counts().sort_index()
    else:
        today_hourly_checkins = pd.Series(dtype=int)

    if "datetime" in today_rejects_df.columns:
        today_hourly_rejects = today_rejects_df["datetime"].dt.hour.value_counts().sort_index()
    else:
        today_hourly_rejects = pd.Series(dtype=int)

    today_acs_df = acs_live_raw.copy()

    if len(today_acs_df) > 0 and "datetime" in today_acs_df.columns:
        today_acs_df["datetime"] = pd.to_datetime(today_acs_df["datetime"], errors="coerce")
        today_acs_df = today_acs_df.dropna(subset=["datetime"]).copy()

        today_acs_latest_date = today_acs_df["datetime"].max().date()
        today_acs_df = today_acs_df[today_acs_df["datetime"].dt.date == today_acs_latest_date].copy()

    if "raw_message" in today_acs_df.columns:
        today_acs_df["raw_message"] = today_acs_df["raw_message"].fillna("").astype(str).str.strip()
        today_acs_df = today_acs_df[today_acs_df["raw_message"].str.startswith("101")].copy()

    if "barcode" in today_acs_df.columns and "datetime" in today_acs_df.columns:
        today_acs_df = today_acs_df.sort_values("datetime")
        today_acs_df = today_acs_df.drop_duplicates(subset=["barcode"], keep="last")

    acs_summary_today = build_acs_item_summary(
        today_acs_df,
        transit_labels=transit_labels,
        branch_services_names=branch_services_names,
        collection_services_names=collection_services_names,
        branch_services_da_patterns=branch_services_da_patterns,
        collection_services_da_patterns=collection_services_da_patterns,
    )

    today_holds = acs_summary_today["holds_total"]
    today_ill = acs_summary_today["ill_total"]
    today_ill_main = acs_summary_today["ill_main"]
    today_ill_by_branch = acs_summary_today["ill_by_branch"]
    today_programming = acs_summary_today["programming_total"]
    today_collection_services = acs_summary_today["collection_services_total"]
    today_ill_items_df = acs_summary_today["ill_df"]
    today_programming_df = acs_summary_today["programming_df"]
    today_collection_services_df = acs_summary_today["collection_services_df"]
    today_public_holds_df = acs_summary_today["holds_df"]

    historical_baseline = get_historical_reject_baseline(df_history_raw, rejects_history_raw, today)
    historical_daily_avg_reject = historical_baseline.get("historical_daily_avg_reject")

    if historical_daily_avg_reject is None or historical_daily_avg_reject == 0:
        historical_df = df_history_raw[df_history_raw["datetime"].dt.date < today]

        if len(historical_df) > 0:
            daily_checkins = historical_df["datetime"].dt.date.value_counts()
            daily_rejects = rejects_history_raw[
                rejects_history_raw["datetime"].dt.date < today
            ]["datetime"].dt.date.value_counts()

            combined = pd.DataFrame({
                "checkins": daily_checkins,
                "rejects": daily_rejects,
            }).fillna(0)

            combined = combined[combined["checkins"] > 0]

            if len(combined) > 0:
                combined["reject_rate"] = (combined["rejects"] / combined["checkins"]) * 100
                historical_daily_avg_reject = combined["reject_rate"].mean()
            else:
                historical_daily_avg_reject = 0
        else:
            historical_daily_avg_reject = 0

    live_reject_deviation = today_reject_rate - historical_daily_avg_reject

    live_reject_subtitle_color = "#6b7280"
    live_reject_value_color = "#1f2937"
    live_alert_title = ""
    live_alert_text = ""
    show_live_alert = False

    if today_reject_rate >= 10:
        live_reject_value_color = "#d97706"
        live_reject_subtitle_color = "#d97706"
        show_live_alert = True
        live_alert_title = "Operational Alert"

        if historical_daily_avg_reject > 0:
            live_alert_text = (
                f"Today's reject rate is {today_reject_rate:.2f}%, which is {live_reject_deviation:+.2f}% "
                f"above the typical daily rate of {historical_daily_avg_reject:.2f}%. "
                f"Review today's top reject issues and check AMH conditions around the busiest hours."
            )
        else:
            live_alert_text = (
                f"Today's reject rate is {today_reject_rate:.2f}%, which is above the 10% alert threshold. "
                f"Review today's top reject issues and check AMH conditions around the busiest hours."
            )
    elif today_reject_rate >= 7:
        live_reject_value_color = "#b45309"
        live_reject_subtitle_color = "#b45309"
    elif today_reject_rate >= 5:
        live_reject_value_color = "#92400e"
        live_reject_subtitle_color = "#92400e"
    else:
        live_reject_value_color = "#059669"
        live_reject_subtitle_color = "#059669"

    default_alert_branch_1 = transit_labels[0] if len(transit_labels) > 0 else None
    default_alert_branch_2 = transit_labels[1] if len(transit_labels) > 1 else None

    alerts = get_system_alerts(
        pipeline_status=pipeline_status,
        show_live_alert=show_live_alert,
        westside_pct=today_transit_pct_map.get(default_alert_branch_1, 0),
        library_express_pct=today_transit_pct_map.get(default_alert_branch_2, 0),
        historical_westside_pct=historical_transit_pct_map.get(default_alert_branch_1),
        historical_library_express_pct=historical_transit_pct_map.get(default_alert_branch_2),
    )

    info_alerts = []
    if alerts:
        info_alerts = [a for a in alerts if a["level"].lower() in ["info", "trend"]]

    theme_palette = _get_theme_palette(theme_base)

    start_hour = 7
    end_hour = 20
    current_hour = now_ct.hour
    if current_hour < start_hour:
        live_hour_range = [start_hour]
    else:
        live_hour_range = list(range(start_hour, min(current_hour, end_hour) + 1))

    live_today_args = {
        "today": today,
        "refresh_count": refresh_count,
        "pipeline_status_label": pipeline_ctx["pipeline_status_label"],
        "pipeline_status_color": pipeline_ctx["pipeline_status_color"],
        "pipeline_status_bg": pipeline_ctx["pipeline_status_bg"],
        "pipeline_expanded": pipeline_ctx["pipeline_expanded"],
        "app_refreshed_str": pipeline_ctx["app_refreshed_str"],
        "latest_checkin_str": pipeline_ctx["latest_checkin_str"],
        "latest_checkin_ago": pipeline_ctx["latest_checkin_ago"],
        "pipeline_status_written_str": pipeline_ctx["pipeline_status_written_str"],
        "pipeline_status_written_ago": pipeline_ctx["pipeline_status_written_ago"],
        "pipeline_last_attempt_str": pipeline_ctx["pipeline_last_attempt_str"],
        "pipeline_last_attempt_ago": pipeline_ctx["pipeline_last_attempt_ago"],
        "pipeline_last_run_str": pipeline_ctx["pipeline_last_run_str"],
        "pipeline_last_run_ago": pipeline_ctx["pipeline_last_run_ago"],
        "pipeline_result_text": pipeline_ctx["pipeline_result_text"],
        "status_code_text": pipeline_ctx["status_code_text"],
        "checkins_rows": pipeline_ctx["checkins_rows"],
        "rejects_rows": pipeline_ctx["rejects_rows"],
        "uploaded_checkins_rows": pipeline_ctx["uploaded_checkins_rows"],
        "uploaded_rejects_rows": pipeline_ctx["uploaded_rejects_rows"],
        "checkins_bad_datetime_rows": pipeline_ctx["checkins_bad_datetime_rows"],
        "rejects_bad_datetime_rows": pipeline_ctx["rejects_bad_datetime_rows"],
        "transit_items": pipeline_ctx["transit_items"],
        "problem_items": pipeline_ctx["problem_items"],
        "destination_breakdown_text": pipeline_ctx["destination_breakdown_text"],
        "today_metrics": today_metrics,
        "today_checkins": today_checkins,
        "today_rejects": today_rejects,
        "today_total_transit": today_total_transit,
        "today_transit_counts_map": today_transit_counts_map,
        "today_transit_pct_map": today_transit_pct_map,
        "today_peak_hour": today_peak_hour,
        "today_peak_hour_count": today_peak_hour_count,
        "today_peak_hour_pct": today_peak_hour_pct,
        "today_reject_rate": today_reject_rate,
        "historical_daily_avg_reject": historical_daily_avg_reject,
        "live_reject_deviation": live_reject_deviation,
        "live_reject_subtitle_color": live_reject_subtitle_color,
        "live_reject_value_color": live_reject_value_color,
        "TRANSIT_LABELS": transit_labels,
        "TRANSIT_HOME_LABEL": transit_home_label,
        "today_holds": today_holds,
        "today_ill": today_ill,
        "today_ill_main": today_ill_main,
        "today_ill_by_branch": today_ill_by_branch,
        "today_programming": today_programming,
        "today_collection_services": today_collection_services,
        "today_public_holds_df": today_public_holds_df,
        "today_ill_items_df": today_ill_items_df,
        "today_programming_df": today_programming_df,
        "today_collection_services_df": today_collection_services_df,
        "info_alerts": info_alerts,
        "show_live_alert": show_live_alert,
        "live_alert_title": live_alert_title,
        "live_alert_text": live_alert_text,
        "info_border": theme_palette["info_border"],
        "info_bg": theme_palette["info_bg"],
        "info_title": theme_palette["info_title"],
        "info_text": theme_palette["info_text"],
        "danger_border": theme_palette["danger_border"],
        "danger_bg": theme_palette["danger_bg"],
        "danger_title": theme_palette["danger_title"],
        "danger_text": theme_palette["danger_text"],
        "today_df": today_df,
        "today_rejects_df": today_rejects_df,
        "today_hourly_checkins": today_hourly_checkins,
        "live_hour_range": live_hour_range,
    }

    overview_args = {
        "df": df,
        "rejects_df": rejects_df,
        "acs_history_raw": acs_history_raw,
        "start_date": start_date,
        "end_date": end_date,
        "date_range_text": date_range_text,
        "TRANSIT_LABELS": transit_labels,
        "TRANSIT_HOME_LABEL": transit_home_label,
        "BRANCH_SERVICES_NAMES": branch_services_names,
        "COLLECTION_SERVICES_NAMES": collection_services_names,
        "BRANCH_SERVICES_DA_PATTERNS": branch_services_da_patterns,
        "COLLECTION_SERVICES_DA_PATTERNS": collection_services_da_patterns,
        "attention_title": attention_title,
        "attention_text": attention_text,
        "attention_color": attention_color,
        "overview_transit_counts_map": overview_transit_counts_map,
        "overview_transit_pct_map": overview_transit_pct_map,
    }

    reports_args = {
        "df": df,
        "df_history_raw": df_history_raw,
        "df_live_raw": df_live_raw,
        "rejects_df": rejects_df,
        "start_date": start_date,
        "end_date": end_date,
        "today": today,
        "overall_metrics": overall_metrics,
        "top_issue": top_issue,
        "attention_text": attention_text,
        "LIBRARY_NAME": library_name,
        "BRANCH_NAME": branch_name,
        "SYSTEM_NAME": system_name,
    }

    transits_args = {
        "df": df,
        "rejects_df": rejects_df,
        "today_df": today_df,
        "today_rejects_df": today_rejects_df,
        "df_history_raw": df_history_raw,
        "today": today,
        "start_date": start_date,
        "end_date": end_date,
    }

    return {
        "no_today_data": no_today_data,
        "live_today_args": live_today_args,
        "overview_args": overview_args,
        "reports_args": reports_args,
        "transits_args": transits_args,
    }
