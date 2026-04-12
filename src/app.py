# app.py
# Streamlit dashboard for AMH analytics
# Displays item flow, routing, rejects, and transit diagnostics in a web interface

import streamlit as st
import pandas as pd
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import altair as alt

from streamlit_autorefresh import st_autorefresh
import json
from views.live_today_view import render_live_today
from views.overview_view import render_overview
from views.reports_view import render_reports
from views.transits_view import render_transits

st.set_page_config(
    page_title="SortView",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
<style>
    [data-testid="stSidebarNav"] {
        display: none;
    }
</style>
""", unsafe_allow_html=True)

APP_TZ = ZoneInfo("America/Chicago")

SETTINGS_FILE = Path(__file__).parent / "branch_settings.json"

def load_branch_settings():
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

branch_settings = load_branch_settings()

LIBRARY_SETTINGS = branch_settings.get("library", {})
TRANSIT_SETTINGS = branch_settings.get("transit", {})
INTERNAL_ROUTING = branch_settings.get("internal_routing", {})

LIBRARY_NAME = LIBRARY_SETTINGS.get("library_name", "New Braunfels Public Library")
BRANCH_NAME = LIBRARY_SETTINGS.get("branch_name", "Main Branch")
SYSTEM_NAME = LIBRARY_SETTINGS.get("system_name", "Tech Logic UltraSort")


TRANSIT_HOME_LABEL = TRANSIT_SETTINGS.get("home_branch_label", "Main")
TRANSIT_DESTINATIONS = TRANSIT_SETTINGS.get("destinations", [])

ENABLED_TRANSIT_DESTINATIONS = [
    d for d in TRANSIT_DESTINATIONS
    if bool(d.get("enabled", True)) and str(d.get("label", "")).strip()
]

TRANSIT_LABELS = [str(d.get("label", "")).strip() for d in ENABLED_TRANSIT_DESTINATIONS]

BRANCH_SERVICES_NAMES = {
    str(x).strip().upper()
    for x in INTERNAL_ROUTING.get("branch_services_names", [])
}

COLLECTION_SERVICES_NAMES = {
    str(x).strip().upper()
    for x in INTERNAL_ROUTING.get("collection_services_names", [])
}

BRANCH_SERVICES_DA_PATTERNS = [
    str(x).strip().upper()
    for x in INTERNAL_ROUTING.get("branch_services_da_patterns", [])
]

COLLECTION_SERVICES_DA_PATTERNS = [
    str(x).strip().upper()
    for x in INTERNAL_ROUTING.get("collection_services_da_patterns", [])
]
        

def is_operating_hours(now_ct: datetime) -> bool:
    # 6:00 AM through 8:59 PM
    return 6 <= now_ct.hour < 21

now_ct = datetime.now(APP_TZ)

refresh_count = 0
if is_operating_hours(now_ct):
    refresh_count = st_autorefresh(
        interval=10 * 60 * 1000,   # 10 minutes
        key="sortview_auto_refresh"
    )


from data_loader import (
    load_checkins_df,
    load_checkins_history_df,
    load_rejects_df,
    load_rejects_history_df,
    load_pipeline_status,
    load_acs_df,
    load_acs_history_df,
)
from metrics import (
    get_date_filtered_df,
    get_today_metrics,
    get_overall_metrics,
    get_historical_reject_baseline,
    build_acs_item_summary,
    build_roi_payload,
)
from reject_logic import simplify_error
from alerts import get_system_alerts

from transit_logic import (
    normalize_transit_destination,
    get_transit_summary,
    get_transit_time_summary,
    get_peak_transit_day_summary,
    get_transit_weekday_comparison,
    get_destination_weekday_mix,
    get_destination_reject_summary,
    get_transit_reject_insight,
    get_destination_driver_summary,
    build_internal_routing_summary,
)

from ui_components import (
    render_kpi_card,
    format_hour,
    format_hour_plain,
    format_relative_time,
    download_button,
    format_ill_branch_subtitle,
    render_chart,
    get_hour_range_df,
    build_hourly_bar_chart,
    build_category_bar_chart,
    build_date_line_chart,
    build_weekday_line_chart,
    build_hourly_line_chart,
)

st.markdown("""
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;800&display=swap" rel="stylesheet">

<style>
.sortview-title {
    font-family: 'Orbitron', sans-serif;
    font-size: 52px;
    font-weight: 800;
    letter-spacing: 3px;

    background: linear-gradient(90deg, #60a5fa, #a78bfa, #34d399);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;

    text-shadow:
        0 0 6px rgba(96, 165, 250, 0.4),
        0 0 12px rgba(167, 139, 250, 0.25);

    margin-bottom: -4px;
}

div.stDownloadButton > button {
    background: linear-gradient(135deg, #2563eb, #1d4ed8);
    color: white;
    border-radius: 10px;
    padding: 0.7em 1.4em;
    font-weight: 600;
    border: none;
    box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3);
}

div.stDownloadButton > button:hover {
    background: linear-gradient(135deg, #1d4ed8, #1e3a8a);
    transform: translateY(-1px);
}

</style>
""", unsafe_allow_html=True)

# ----------------------------------------
# AUTO REFRESH CACHE BUSTER
# ----------------------------------------
if "last_refresh_count" not in st.session_state:
    st.session_state["last_refresh_count"] = refresh_count

auto_refresh_triggered = refresh_count != st.session_state["last_refresh_count"]

if auto_refresh_triggered:
    st.cache_data.clear()
    st.session_state["last_refresh_count"] = refresh_count

# Load pipeline status first so its updated_at can drive cache invalidation
pipeline_status = load_pipeline_status(refresh_count=refresh_count)

status_mtime = 0
if pipeline_status:
    status_updated_at = pipeline_status.get("updated_at")
    if status_updated_at:
        status_mtime = str(status_updated_at)

df_live_raw = load_checkins_df(mtime=status_mtime, refresh_count=refresh_count)
df_history_raw = load_checkins_history_df(mtime=status_mtime, refresh_count=refresh_count)

rejects_live_raw = load_rejects_df(mtime=status_mtime, refresh_count=refresh_count)
rejects_history_raw = load_rejects_history_df(mtime=status_mtime, refresh_count=refresh_count)

acs_live_raw = load_acs_df(mtime=status_mtime, refresh_count=refresh_count)
acs_history_raw = load_acs_history_df(mtime=status_mtime, refresh_count=refresh_count)

# DEBUG
#st.write("df_live_raw columns:", list(df_live_raw.columns))
#st.write("df_live_raw rows:", len(df_live_raw))
#st.write("rejects_live_raw columns:", list(rejects_live_raw.columns))
#st.write("rejects_live_raw rows:", len(rejects_live_raw))


checkins_updated = None
if len(df_live_raw) > 0 and "datetime" in df_live_raw.columns:
    latest_dt = df_live_raw["datetime"].max()
    if pd.notna(latest_dt):
        if latest_dt.tzinfo is None:
            checkins_updated = latest_dt.tz_localize("America/Chicago")
        else:
            checkins_updated = latest_dt.tz_convert("America/Chicago")

rejects_live_raw["error_simple"] = rejects_live_raw["error_message"].apply(simplify_error)
rejects_history_raw["error_simple"] = rejects_history_raw["error_message"].apply(simplify_error)

min_date = df_history_raw["datetime"].min().date()
max_date = df_history_raw["datetime"].max().date()



header_left, header_right = st.columns([12, 1])

with header_left:
    st.caption("Hanly Analytics")
    st.markdown('<div class="sortview-title">SORTVIEW</div>', unsafe_allow_html=True)
    st.markdown(
        f"<div style='color:#6b7280; font-size:0.95rem; margin-bottom:10px;'>"
        f"{LIBRARY_NAME} • {BRANCH_NAME} • {SYSTEM_NAME}"
        f"</div>",
        unsafe_allow_html=True
    )
with header_right:
    if st.button("⚙️", help="Admin Settings"):
        st.switch_page("pages/1_admin_settings.py")

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

    if status_updated_raw:
        try:
            status_updated_dt = datetime.fromisoformat(str(status_updated_raw))
            if status_updated_dt.tzinfo is None:
                status_updated_dt = status_updated_dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Chicago"))
            else:
                status_updated_dt = status_updated_dt.astimezone(ZoneInfo("America/Chicago"))
        except Exception:
            status_updated_dt = None

    if last_run_raw:
        try:
            last_run = datetime.fromisoformat(str(last_run_raw))
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=ZoneInfo("America/Chicago"))
            else:
                last_run = last_run.astimezone(ZoneInfo("America/Chicago"))
        except Exception:
            last_run = None

    if last_attempt_raw:
        try:
            last_attempt = datetime.fromisoformat(str(last_attempt_raw))
            if last_attempt.tzinfo is None:
                last_attempt = last_attempt.replace(tzinfo=ZoneInfo("America/Chicago"))
            else:
                last_attempt = last_attempt.astimezone(ZoneInfo("America/Chicago"))
        except Exception:
            last_attempt = None

now_ct = datetime.now(ZoneInfo("America/Chicago"))

app_refreshed_str = now_ct.strftime('%b %d, %Y %I:%M %p')

pipeline_status_written_str = (
    status_updated_dt.strftime('%b %d, %Y %I:%M %p')
    if status_updated_dt else "N/A"
)

pipeline_last_run_str = (
    last_run.strftime('%b %d, %Y %I:%M %p')
    if last_run else "N/A"
)

pipeline_last_attempt_str = (
    last_attempt.strftime('%b %d, %Y %I:%M %p')
    if last_attempt else "N/A"
)

pipeline_status_written_ago = format_relative_time(status_updated_dt, now_ct)
pipeline_last_run_ago = format_relative_time(last_run, now_ct)
pipeline_last_attempt_ago = format_relative_time(last_attempt, now_ct)

latest_checkin_str = (
    checkins_updated.strftime('%b %d, %Y %I:%M %p')
    if checkins_updated else "N/A"
)
latest_checkin_ago = format_relative_time(checkins_updated, now_ct)

pipeline_run_status = pipeline_status.get("status", "unknown") if pipeline_status else "unknown"
status_code_text = str(pipeline_run_status)

theme_base = st.get_option("theme.base") or "light"

if theme_base == "dark":
    info_bg = "rgba(37, 99, 235, 0.14)"
    info_border = "#3b82f6"
    info_title = "#93c5fd"
    info_text = "#dbeafe"

    success_bg = "rgba(5, 150, 105, 0.14)"
    success_border = "#10b981"
    success_title = "#6ee7b7"
    success_text = "#d1fae5"

    warning_bg = "rgba(217, 119, 6, 0.14)"
    warning_border = "#f59e0b"
    warning_title = "#fcd34d"
    warning_text = "#fef3c7"

    danger_bg = "rgba(220, 38, 38, 0.14)"
    danger_border = "#ef4444"
    danger_title = "#fca5a5"
    danger_text = "#fee2e2"

    neutral_bg = "rgba(148, 163, 184, 0.10)"
    neutral_border = "#64748b"
    neutral_title = "#e5e7eb"
    neutral_text = "#cbd5e1"
else:
    info_bg = "#eff6ff"
    info_border = "#2563eb"
    info_title = "#1d4ed8"
    info_text = "#1e3a8a"

    success_bg = "#ecfdf5"
    success_border = "#059669"
    success_title = "#047857"
    success_text = "#065f46"

    warning_bg = "#fffbeb"
    warning_border = "#d97706"
    warning_title = "#92400e"
    warning_text = "#78350f"

    danger_bg = "#fef2f2"
    danger_border = "#dc2626"
    danger_title = "#991b1b"
    danger_text = "#7f1d1d"

    neutral_bg = "#f9fafb"
    neutral_border = "#6b7280"
    neutral_title = "#1f2937"
    neutral_text = "#4b5563"

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


selected_view = st.segmented_control(
    "Section",
    options=["Live Today", "Transits", "Reports", "Overview"],
    default="Live Today",
    label_visibility="collapsed"
)

start_date = min_date
end_date = max_date




local_today = datetime.now(ZoneInfo("America/Chicago")).date()

start_date = min_date
end_date = min(max_date, local_today)

if selected_view in ["Overview", "Reports", "Transits"]:
    st.sidebar.header("Filters")

    max_allowed_date = min(max_date, local_today)

    range_mode = st.sidebar.radio(
        "Date Range",
        ["Single Day", "Last 7 Days", "Last 30 Days", "Month to Date", "Full Month", "All Time", "Custom"],
        index=5
    )

    if range_mode == "Single Day":
        selected_day = st.sidebar.date_input(
            "Choose Day",
            value=max_allowed_date,
            min_value=min_date,
            max_value=max_allowed_date
        )
        start_date = selected_day
        end_date = selected_day

    elif range_mode == "Last 7 Days":
        end_date = max_allowed_date
        start_date = max(min_date, end_date - pd.Timedelta(days=6))

    elif range_mode == "Last 30 Days":
        end_date = max_allowed_date
        start_date = max(min_date, end_date - pd.Timedelta(days=29))

    elif range_mode == "Month to Date":
        end_date = max_allowed_date
        start_date = max(min_date, end_date.replace(day=1))

    elif range_mode == "Full Month":
        first_day_current_month = local_today.replace(day=1)
        last_day_previous_month = first_day_current_month - pd.Timedelta(days=1)

        month_starts = pd.date_range(
            start=min_date.replace(day=1),
            end=last_day_previous_month.replace(day=1),
            freq="MS"
        )

        month_options = []
        month_map = {}

        for month_start in month_starts:
            month_start_date = month_start.date()
            next_month_start = (month_start + pd.offsets.MonthBegin(1)).date()
            month_end_date = next_month_start - pd.Timedelta(days=1)

            # only include months fully available in the dataset and fully completed
            if month_start_date >= min_date and month_end_date <= max_allowed_date and month_end_date < first_day_current_month:
                label = month_start.strftime("%B %Y")
                month_options.append(label)
                month_map[label] = (month_start_date, month_end_date)

        month_options = list(reversed(month_options))

        if month_options:
            selected_month_label = st.sidebar.selectbox(
                "Choose Full Month",
                month_options,
                index=0
            )
            start_date, end_date = month_map[selected_month_label]
        else:
            st.sidebar.warning("No completed full months are available in the current dataset.")
            start_date = min_date
            end_date = max_allowed_date

    elif range_mode == "All Time":
        start_date = min_date
        end_date = max_allowed_date

    elif range_mode == "Custom":
        custom_range = st.sidebar.date_input(
            "Custom Range",
            value=(max(min_date, max_allowed_date - pd.Timedelta(days=6)), max_allowed_date),
            min_value=min_date,
            max_value=max_allowed_date
        )

        if isinstance(custom_range, (list, tuple)):
            if len(custom_range) == 2:
                start_date, end_date = custom_range
            elif len(custom_range) == 1:
                start_date = custom_range[0]
                end_date = custom_range[0]
            else:
                start_date = max(min_date, max_allowed_date - pd.Timedelta(days=6))
                end_date = max_allowed_date
        else:
            start_date = custom_range
            end_date = custom_range

        if start_date > end_date:
            start_date, end_date = end_date, start_date

    st.sidebar.caption(f"Showing: {start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}")
    
df = get_date_filtered_df(df_history_raw, start_date, end_date)
rejects_df = get_date_filtered_df(rejects_history_raw, start_date, end_date)

weekday_order = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday"
]

df["date"] = df["datetime"].dt.date
df["day_of_week"] = df["datetime"].dt.day_name()

rejects_df["date"] = rejects_df["datetime"].dt.date
rejects_df["day_of_week"] = rejects_df["datetime"].dt.day_name()

df["destination_clean"] = df["destination"].astype(str).str.strip()
df["destination_upper"] = df["destination_clean"].str.upper()

df["transit_destination"] = None

for transit_label in TRANSIT_LABELS:
    label_upper = transit_label.upper()
    match_mask = df["destination_upper"] == label_upper
    df.loc[match_mask, "transit_destination"] = transit_label

df["destination_report"] = df["destination_clean"].copy()
df.loc[df["destination_report"] == "1", "destination_report"] = TRANSIT_HOME_LABEL

for transit_label in TRANSIT_LABELS:
    label_upper = transit_label.upper()
    match_mask = df["destination_upper"] == label_upper
    df.loc[match_mask, "destination_report"] = transit_label

df["destination_clean"] = df["destination_report"]

valid_transit_destinations = TRANSIT_LABELS.copy()

transit_df = df[
    df["transit_destination"].isin(valid_transit_destinations)
].copy()


transit_summary = get_transit_summary(df)

peak_transit_day = get_peak_transit_day_summary(transit_df, weekday_order)
peak_transit_day_label = peak_transit_day["peak_transit_day_label"]
peak_transit_day_subtitle = peak_transit_day["peak_transit_day_subtitle"]

transit_weekday_comparison = get_transit_weekday_comparison(df, rejects_df, weekday_order)
destination_weekday_mix = get_destination_weekday_mix(transit_df, weekday_order)

transit_insight = get_transit_reject_insight(transit_weekday_comparison)
transit_reject_insight_title = transit_insight["title"]
transit_reject_insight_text = transit_insight["text"]
transit_reject_insight_color = transit_insight["color"]

destination_reject_summary = pd.DataFrame()
destination_transit_summary_text = "No transit destination diagnostics available for the selected date range."
destination_transit_summary_color = "#6b7280"

destination_reject_summary = get_destination_reject_summary(
    df,
    rejects_df,
    transit_summary,
    valid_transit_destinations
)

destination_driver_summary = get_destination_driver_summary(destination_reject_summary)
destination_transit_summary_text = destination_driver_summary["text"]
destination_transit_summary_color = destination_driver_summary["color"]

overall_metrics = get_overall_metrics(df, rejects_df)

overview_transit_counts_map = {}
overview_transit_pct_map = {}

for transit_label in TRANSIT_LABELS:
    transit_count = int((df["transit_destination"] == transit_label).sum()) if len(df) > 0 else 0
    overview_transit_counts_map[transit_label] = transit_count
    overview_transit_pct_map[transit_label] = (
        (transit_count / len(df)) * 100 if len(df) > 0 else 0
    )

peak_hour = overall_metrics["peak_hour"]
peak_hour_count = overall_metrics["peak_hour_count"]
peak_hour_pct = overall_metrics["peak_hour_pct"]
reject_count = overall_metrics["reject_count"]
reject_pct = overall_metrics["reject_pct"]

date_range_text = f"{start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}"

worst_day_label = "N/A"
worst_rate = None

checkins_daily = df["datetime"].dt.date.value_counts().sort_index()
rejects_daily = rejects_df["datetime"].dt.date.value_counts().sort_index()

daily_combined = pd.DataFrame()

if len(df) > 0:
    daily_combined = pd.DataFrame({
        "checkins": checkins_daily,
        "rejects": rejects_daily
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
peak_failure_window_subtitle = ""

if len(rejects_df) > 0:
    peak_failure_hour_counts = rejects_df["datetime"].dt.hour.value_counts().sort_index()
    peak_failure_hour = peak_failure_hour_counts.idxmax()
    peak_failure_count = peak_failure_hour_counts.max()
    peak_failure_pct = (peak_failure_count / len(rejects_df)) * 100
    peak_failure_window_text = format_hour(peak_failure_hour)
    peak_failure_window_subtitle = f"{peak_failure_count:,} rejects ({peak_failure_pct:.1f}% of failures)"

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

if len(TRANSIT_LABELS) > 0:
    primary_transit_label = TRANSIT_LABELS[0]
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

live_now = datetime.now(ZoneInfo("America/Chicago"))
today = live_now.date()

start_hour = 7
end_hour = 20
current_hour = live_now.hour

if current_hour < start_hour:
    live_hour_range = [start_hour]
else:
    live_hour_range = list(range(start_hour, min(current_hour, end_hour) + 1))
    
today_metrics = get_today_metrics(df_live_raw, rejects_live_raw, today)

if len(today_metrics["today_df"]) == 0:
    st.info("No checkins have been ingested yet for today. Live dashboard is showing the current day only.")

# fix current throughput: use most recent active hour instead of only the exact wall-clock hour
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

# historical max hourly throughput baseline
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
today_westside = today_metrics["today_westside"]
today_library_express = today_metrics["today_library_express"]
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

    for transit_label in TRANSIT_LABELS:
        label_upper = transit_label.upper()
        match_mask = historical_checkins_df["destination_upper"] == label_upper
        historical_checkins_df.loc[match_mask, "transit_destination"] = transit_label

    for transit_label in TRANSIT_LABELS:
        historical_transit_pct_map[transit_label] = (
            (historical_checkins_df["transit_destination"] == transit_label).sum()
            / len(historical_checkins_df)
        ) * 100
else:
    historical_transit_pct_map = {}
    
    
today_transit_counts_map = {}

for transit_label in TRANSIT_LABELS:
    transit_count = int((today_df["destination"].astype(str).str.strip().str.upper() == transit_label.upper()).sum()) if len(today_df) > 0 else 0
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

    # real ACS item responses live in raw_message, not message_code
    today_acs_df = today_acs_df[
        today_acs_df["raw_message"].str.startswith("101")
    ].copy()

if "barcode" in today_acs_df.columns and "datetime" in today_acs_df.columns:
    # keep most recent event per barcode
    today_acs_df = today_acs_df.sort_values("datetime")
    today_acs_df = today_acs_df.drop_duplicates(subset=["barcode"], keep="last")


acs_summary_today = build_acs_item_summary(
    today_acs_df,
    transit_labels=TRANSIT_LABELS,
    branch_services_names=BRANCH_SERVICES_NAMES,
    collection_services_names=COLLECTION_SERVICES_NAMES,
    branch_services_da_patterns=BRANCH_SERVICES_DA_PATTERNS,
    collection_services_da_patterns=COLLECTION_SERVICES_DA_PATTERNS,
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
    # fallback: compute manually from historical data
    historical_df = df_history_raw[df_history_raw["datetime"].dt.date < today]

    if len(historical_df) > 0:
        daily_checkins = historical_df["datetime"].dt.date.value_counts()
        daily_rejects = rejects_history_raw[
            rejects_history_raw["datetime"].dt.date < today
        ]["datetime"].dt.date.value_counts()

        combined = pd.DataFrame({
            "checkins": daily_checkins,
            "rejects": daily_rejects
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

live_reject_card_border = "#e5e7eb"
live_reject_value_color = "#1f2937"
live_reject_subtitle_color = "#6b7280"

reject_count_card_border = "#e5e7eb"
reject_count_value_color = "#1f2937"
reject_count_subtitle_color = "#6b7280"

live_alert_title = ""
live_alert_text = ""
show_live_alert = False

if today_reject_rate >= 10:
    live_reject_card_border = "#d97706"
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
    live_reject_card_border = "#f59e0b"
    live_reject_value_color = "#b45309"
    live_reject_subtitle_color = "#b45309"
elif today_reject_rate >= 5:
    live_reject_card_border = "#fcd34d"
    live_reject_value_color = "#92400e"
    live_reject_subtitle_color = "#92400e"
else:
    live_reject_card_border = "#059669"
    live_reject_value_color = "#059669"
    live_reject_subtitle_color = "#059669"
        
        
default_alert_branch_1 = TRANSIT_LABELS[0] if len(TRANSIT_LABELS) > 0 else None
default_alert_branch_2 = TRANSIT_LABELS[1] if len(TRANSIT_LABELS) > 1 else None

alerts = get_system_alerts(
    pipeline_status=pipeline_status,
    show_live_alert=show_live_alert,
    westside_pct=today_transit_pct_map.get(default_alert_branch_1, 0),
    library_express_pct=today_transit_pct_map.get(default_alert_branch_2, 0),
    historical_westside_pct=historical_transit_pct_map.get(default_alert_branch_1),
    historical_library_express_pct=historical_transit_pct_map.get(default_alert_branch_2),
)

critical_alerts = []
warning_alerts = []
info_alerts = []

if alerts:
    critical_alerts = [a for a in alerts if a["level"].lower() == "critical"]
    warning_alerts = [a for a in alerts if a["level"].lower() == "warning"]
    info_alerts = [a for a in alerts if a["level"].lower() in ["info", "trend"]]      

    
if selected_view == "Live Today":
    render_live_today(
        today=today,
        refresh_count=refresh_count,
        pipeline_status_label=pipeline_status_label,
        pipeline_status_color=pipeline_status_color,
        pipeline_status_bg=pipeline_status_bg,
        pipeline_expanded=pipeline_expanded,
        app_refreshed_str=app_refreshed_str,
        latest_checkin_str=latest_checkin_str,
        latest_checkin_ago=latest_checkin_ago,
        pipeline_status_written_str=pipeline_status_written_str,
        pipeline_status_written_ago=pipeline_status_written_ago,
        pipeline_last_attempt_str=pipeline_last_attempt_str,
        pipeline_last_attempt_ago=pipeline_last_attempt_ago,
        pipeline_last_run_str=pipeline_last_run_str,
        pipeline_last_run_ago=pipeline_last_run_ago,
        pipeline_result_text=pipeline_result_text,
        status_code_text=status_code_text,
        checkins_rows=checkins_rows,
        rejects_rows=rejects_rows,
        uploaded_checkins_rows=uploaded_checkins_rows,
        uploaded_rejects_rows=uploaded_rejects_rows,
        checkins_bad_datetime_rows=checkins_bad_datetime_rows,
        rejects_bad_datetime_rows=rejects_bad_datetime_rows,
        transit_items=transit_items,
        problem_items=problem_items,
        destination_breakdown_text=destination_breakdown_text,
        today_metrics=today_metrics,
        today_checkins=today_checkins,
        today_rejects=today_rejects,
        today_total_transit=today_total_transit,
        today_transit_counts_map=today_transit_counts_map,
        today_transit_pct_map=today_transit_pct_map,
        today_peak_hour=today_peak_hour,
        today_peak_hour_count=today_peak_hour_count,
        today_peak_hour_pct=today_peak_hour_pct,
        today_reject_rate=today_reject_rate,
        historical_daily_avg_reject=historical_daily_avg_reject,
        live_reject_deviation=live_reject_deviation,
        live_reject_subtitle_color=live_reject_subtitle_color,
        live_reject_value_color=live_reject_value_color,
        TRANSIT_LABELS=TRANSIT_LABELS,
        TRANSIT_HOME_LABEL=TRANSIT_HOME_LABEL,
        today_holds=today_holds,
        today_ill=today_ill,
        today_ill_main=today_ill_main,
        today_ill_by_branch=today_ill_by_branch,
        today_programming=today_programming,
        today_collection_services=today_collection_services,
        today_public_holds_df=today_public_holds_df,
        today_ill_items_df=today_ill_items_df,
        today_programming_df=today_programming_df,
        today_collection_services_df=today_collection_services_df,
        info_alerts=info_alerts,
        show_live_alert=show_live_alert,
        live_alert_title=live_alert_title,
        live_alert_text=live_alert_text,
        info_border=info_border,
        info_bg=info_bg,
        info_title=info_title,
        info_text=info_text,
        danger_border=danger_border,
        danger_bg=danger_bg,
        danger_title=danger_title,
        danger_text=danger_text,
        today_df=today_df,
        today_rejects_df=today_rejects_df,
        today_hourly_checkins=today_hourly_checkins,
        live_hour_range=live_hour_range,
    )


if selected_view == "Overview":
    render_overview(
        df=df,
        rejects_df=rejects_df,
        acs_history_raw=acs_history_raw,
        start_date=start_date,
        end_date=end_date,
        date_range_text=date_range_text,
        TRANSIT_LABELS=TRANSIT_LABELS,
        TRANSIT_HOME_LABEL=TRANSIT_HOME_LABEL,
        BRANCH_SERVICES_NAMES=BRANCH_SERVICES_NAMES,
        COLLECTION_SERVICES_NAMES=COLLECTION_SERVICES_NAMES,
        BRANCH_SERVICES_DA_PATTERNS=BRANCH_SERVICES_DA_PATTERNS,
        COLLECTION_SERVICES_DA_PATTERNS=COLLECTION_SERVICES_DA_PATTERNS,
        attention_title=attention_title,
        attention_text=attention_text,
        attention_color=attention_color,
        overview_transit_counts_map=overview_transit_counts_map,
        overview_transit_pct_map=overview_transit_pct_map,
    )


if selected_view == "Reports":
    render_reports(
        df=df,
        df_history_raw=df_history_raw,
        df_live_raw=df_live_raw,
        rejects_df=rejects_df,
        start_date=start_date,
        end_date=end_date,
        today=today,
        overall_metrics=overall_metrics,
        top_issue=top_issue,
        attention_text=attention_text,
        LIBRARY_NAME=LIBRARY_NAME,
        BRANCH_NAME=BRANCH_NAME,
        SYSTEM_NAME=SYSTEM_NAME,
    )

if selected_view == "Transits":
    render_transits(
        df=df,
        rejects_df=rejects_df,
        today_df=today_df,
        today_rejects_df=today_rejects_df,
        df_history_raw=df_history_raw,
        today=today,
        start_date=start_date,
        end_date=end_date,
    )
