import pandas as pd

from metrics import get_date_filtered_df, get_overall_metrics
from ui_components import format_hour


def build_filtered_context(
    df_history_raw,
    rejects_history_raw,
    start_date,
    end_date,
    transit_labels,
    transit_home_label,
):
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
    else:
        peak_failure_count = 0
        peak_failure_pct = 0

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

    return {
        "df": df,
        "rejects_df": rejects_df,
        "overall_metrics": overall_metrics,
        "overview_transit_counts_map": overview_transit_counts_map,
        "overview_transit_pct_map": overview_transit_pct_map,
        "date_range_text": date_range_text,
        "top_issue": top_issue,
        "attention_title": attention_title,
        "attention_text": attention_text,
        "attention_color": attention_color,
        "daily_combined": daily_combined,
        "overall_daily_avg_reject": overall_daily_avg_reject,
    }
