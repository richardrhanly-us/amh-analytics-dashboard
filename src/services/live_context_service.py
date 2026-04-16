from datetime import datetime

import pandas as pd

from alerts import get_system_alerts
from metrics import (
    get_today_metrics,
    get_historical_reject_baseline,
    build_acs_item_summary,
)


def build_live_context(
    df_live_raw,
    df_history_raw,
    rejects_live_raw,
    rejects_history_raw,
    acs_live_raw,
    pipeline_status,
    refresh_count,
    today,
    now_ct,
    transit_labels,
    transit_home_label,
    branch_services_names,
    collection_services_names,
    branch_services_da_patterns,
    collection_services_da_patterns,
    theme_palette,
):
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

    if (
        "barcode" in today_acs_df.columns
        and "datetime" in today_acs_df.columns
        and "message_code" in today_acs_df.columns
    ):
        item_rows = today_acs_df[today_acs_df["message_code"].astype(str).str.strip() == "10"].copy()
        non_item_rows = today_acs_df[today_acs_df["message_code"].astype(str).str.strip() != "10"].copy()

        if len(item_rows) > 0:
            item_rows = item_rows.sort_values("datetime")
            item_rows = item_rows.drop_duplicates(subset=["barcode"], keep="last")

        today_acs_df = pd.concat([item_rows, non_item_rows], ignore_index=True)

    print("DEBUG collection_services_names =", collection_services_names)
    print("DEBUG branch_services_names =", branch_services_names)
    print("DEBUG collection_services_da_patterns =", collection_services_da_patterns)
    print("DEBUG branch_services_da_patterns =", branch_services_da_patterns)
    
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

    return {
        "no_today_data": no_today_data,
        "live_today_args": live_today_args,
        "today_df": today_df,
        "today_rejects_df": today_rejects_df,
        "today_transit_pct_map": today_transit_pct_map,
        "historical_transit_pct_map": historical_transit_pct_map,
    }
