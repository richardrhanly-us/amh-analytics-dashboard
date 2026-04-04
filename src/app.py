# app.py
# Streamlit dashboard for AMH analytics
# Displays item flow, routing, rejects, and transit diagnostics in a web interface

import streamlit as st
import pandas as pd
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import altair as alt
from report_export import build_director_report_pdf

st.set_page_config(layout="wide")
from streamlit_autorefresh import st_autorefresh
st_autorefresh(interval=120000, key="amh_auto_refresh")


from data_loader import load_checkins_df, load_checkins_history_df, load_rejects_df, load_rejects_history_df, load_pipeline_status
from metrics import get_date_filtered_df, get_today_metrics, get_overall_metrics, get_historical_reject_baseline
from reject_logic import simplify_error
from alerts import get_system_alerts

from transit_logic import *


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
</style>
""", unsafe_allow_html=True)


def render_kpi_card(
    title,
    value,
    subtitle="",
    subtitle_color="#059669",
    value_font_size="1.9rem",
    border_color="#e5e7eb",
    value_color="#1f2937",
    value_wrap=False,
    fill_pct=None,
    fill_color="rgba(37, 99, 235, 0.12)"
):
    value_white_space = "normal" if value_wrap else "nowrap"
    value_word_break = "break-word" if value_wrap else "normal"

    safe_fill_pct = 0
    if fill_pct is not None:
        safe_fill_pct = max(0, min(fill_pct, 1)) * 100

    fill_html = ""
    if fill_pct is not None:
        fill_html = (
            f'<div style="'
            f'position:absolute;'
            f'left:0;'
            f'bottom:0;'
            f'width:100%;'
            f'height:{safe_fill_pct:.1f}%;'
            f'background:{fill_color};'
            f'z-index:1;'
            f'transition:height 0.6s ease;'
            f'"></div>'
        )

    subtitle_html = ""
    if subtitle:
        subtitle_html = (
            f'<div style="'
            f'font-size:0.9rem;'
            f'color:{subtitle_color};'
            f'margin-top:8px;'
            f'line-height:1.3;'
            f'overflow:hidden;'
            f'position:relative;'
            f'z-index:2;'
            f'">{subtitle}</div>'
        )

    card_html = (
        f'<div style="'
        f'position:relative;'
        f'overflow:hidden;'
        f'border:2px solid {border_color};'
        f'border-radius:12px;'
        f'padding:16px 18px;'
        f'background-color:white;'
        f'min-height:185px;'
        f'height:185px;'
        f'display:flex;'
        f'flex-direction:column;'
        f'justify-content:center;'
        f'align-items:center;'
        f'text-align:center;'
        f'">'
        f'{fill_html}'
        f'<div style="'
        f'font-size:0.9rem;'
        f'color:#6b7280;'
        f'margin-bottom:8px;'
        f'position:relative;'
        f'z-index:2;'
        f'">{title}</div>'
        f'<div style="'
        f'font-size:{value_font_size};'
        f'font-weight:600;'
        f'color:{value_color};'
        f'line-height:1.2;'
        f'margin-bottom:4px;'
        f'white-space:{value_white_space};'
        f'word-break:{value_word_break};'
        f'position:relative;'
        f'z-index:2;'
        f'">{value}</div>'
        f'{subtitle_html}'
        f'</div>'
    )

    st.markdown(card_html, unsafe_allow_html=True)
    
def get_file_updated_time(path):
    file_path = Path(path)
    if file_path.exists():
        return datetime.fromtimestamp(
            file_path.stat().st_mtime,
            tz=ZoneInfo("America/Chicago")
        )
    return None




def format_hour(hour):
    if hour is None:
        return "N/A"

    if hour == 0:
        return "12:00<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>AM</span>"
    elif hour < 12:
        return f"{hour}:00<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>AM</span>"
    elif hour == 12:
        return "12:00<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>PM</span>"
    else:
        return f"{hour-12}:00<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>PM</span>"


def format_hour_plain(hour_value):
    if hour_value is None or pd.isna(hour_value):
        return "N/A"

    return pd.to_datetime(f"{int(hour_value):02d}:00").strftime("%I:%M %p")
        
def download_button(df, filename, key=None):
    csv = df.to_csv(index=False).encode("utf-8")

    left, _ = st.columns([1, 10])

    with left:
        st.download_button(
            label="Download CSV",
            data=csv,
            file_name=filename,
            mime="text/csv",
            key=key or f"{filename}_download"
        )
    
 

def render_chart(chart):
    chart = (
        chart
        .configure_view(
            stroke=None
        )
        .configure_axis(
            labelColor="#6b7280",
            titleColor="#6b7280",
            gridColor="#e5e7eb",
            domainColor="#d1d5db",
            tickColor="#d1d5db",
            labelFontSize=12,
            titleFontSize=13
        )
        .configure_legend(
            labelColor="#6b7280",
            titleColor="#6b7280",
            labelFontSize=12,
            titleFontSize=13
        )
        .configure_title(
            color="#1f2937",
            fontSize=16
        )
        .properties(
            background="white"
        )
    )

    st.altair_chart(chart, use_container_width=True)
    
    
def get_hour_range_df(start_hour=7, end_hour=20):
    hour_df = pd.DataFrame({"hour": list(range(start_hour, end_hour + 1))})
    hour_df["hour_label"] = hour_df["hour"].apply(format_hour_plain)
    return hour_df


def build_hourly_bar_chart(df, value_col, title_y, start_hour=7, end_hour=20):
    hour_base = get_hour_range_df(start_hour, end_hour)
    merged = hour_base.merge(df, on=["hour", "hour_label"], how="left").fillna(0)

    chart = (
        alt.Chart(merged)
        .mark_bar()
        .encode(
            x=alt.X(
                "hour_label:N",
                sort=merged["hour_label"].tolist(),
                title="Hour",
                axis=alt.Axis(labelAngle=0)
            ),
            y=alt.Y(f"{value_col}:Q", title=title_y),
            tooltip=["hour_label", value_col]
        )
        .properties(height=350)
    )
    return chart


def build_category_bar_chart(df, category_col, value_col, y_title, x_title=""):
    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X(
                f"{category_col}:N",
                sort=df[category_col].tolist(),
                title=x_title,
                axis=alt.Axis(labelAngle=0)
            ),
            y=alt.Y(f"{value_col}:Q", title=y_title),
            tooltip=[category_col, value_col]
        )
        .properties(height=350)
    )
    return chart


def build_date_line_chart(df, date_col, value_col, y_title, series_col=None):
    if series_col:
        chart = (
            alt.Chart(df)
            .mark_line(point=True)
            .encode(
                x=alt.X(
                    f"{date_col}:T",
                    title="Date",
                    axis=alt.Axis(labelAngle=0, format="%b %d")
                ),
                y=alt.Y(f"{value_col}:Q", title=y_title),
                color=alt.Color(f"{series_col}:N"),
                tooltip=[date_col, series_col, value_col]
            )
            .properties(height=350)
        )
    else:
        chart = (
            alt.Chart(df)
            .mark_line(point=True)
            .encode(
                x=alt.X(
                    f"{date_col}:T",
                    title="Date",
                    axis=alt.Axis(labelAngle=0, format="%b %d")
                ),
                y=alt.Y(f"{value_col}:Q", title=y_title),
                tooltip=[date_col, value_col]
            )
            .properties(height=350)
        )
    return chart


def build_weekday_line_chart(df, weekday_col, value_col, series_col=None):
    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    if series_col:
        chart = (
            alt.Chart(df)
            .mark_line(point=True)
            .encode(
                x=alt.X(
                    f"{weekday_col}:N",
                    sort=weekday_order,
                    title="Weekday",
                    axis=alt.Axis(labelAngle=0)
                ),
                y=alt.Y(f"{value_col}:Q", title="Value"),
                color=alt.Color(f"{series_col}:N"),
                tooltip=[weekday_col, series_col, value_col]
            )
            .properties(height=350)
        )
    else:
        chart = (
            alt.Chart(df)
            .mark_line(point=True)
            .encode(
                x=alt.X(
                    f"{weekday_col}:N",
                    sort=weekday_order,
                    title="Weekday",
                    axis=alt.Axis(labelAngle=0)
                ),
                y=alt.Y(f"{value_col}:Q", title="Value"),
                tooltip=[weekday_col, value_col]
            )
            .properties(height=350)
        )
    return chart

def build_hourly_line_chart(df, value_col, title_y, series_col=None, start_hour=7, end_hour=20):
    hour_base = get_hour_range_df(start_hour, end_hour)

    if series_col:
        series_values = df[series_col].dropna().unique().tolist()
        expanded = pd.MultiIndex.from_product(
            [hour_base["hour"].tolist(), series_values],
            names=["hour", series_col]
        ).to_frame(index=False)

        expanded = expanded.merge(hour_base, on="hour", how="left")
        merged = expanded.merge(df, on=["hour", "hour_label", series_col], how="left").fillna(0)

        chart = (
            alt.Chart(merged)
            .mark_line(point=False)
            .encode(
                x=alt.X(
                    "hour_label:N",
                    sort=hour_base["hour_label"].tolist(),
                    title="Hour",
                    axis=alt.Axis(labelAngle=0)
                ),
                y=alt.Y(f"{value_col}:Q", title=title_y),
                color=alt.Color(f"{series_col}:N"),
                tooltip=["hour_label", series_col, value_col]
            )
            .properties(height=350)
        )
    else:
        merged = hour_base.merge(df, on=["hour", "hour_label"], how="left").fillna(0)

        chart = (
            alt.Chart(merged)
            .mark_line(point=True)
            .encode(
                x=alt.X(
                    "hour_label:N",
                    sort=merged["hour_label"].tolist(),
                    title="Hour",
                    axis=alt.Axis(labelAngle=0)
                ),
                y=alt.Y(f"{value_col}:Q", title=title_y),
                tooltip=["hour_label", value_col]
            )
            .properties(height=350)
        )

    return chart



STATUS_FILE = "data/processed/pipeline_status.json"

status_updated = get_file_updated_time(STATUS_FILE)
status_mtime = status_updated.timestamp() if status_updated else 0

df_live_raw = load_checkins_df()
df_history_raw = load_checkins_history_df()

rejects_live_raw = load_rejects_df()
rejects_history_raw = load_rejects_history_df()

pipeline_status = load_pipeline_status(mtime=status_mtime)

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



st.caption("Hanly Analytics")
st.markdown('<div class="sortview-title">SORTVIEW</div>', unsafe_allow_html=True)
st.markdown(
    "<div style='color:#6b7280; font-size:0.95rem; margin-bottom:10px;'>"
    "New Braunfels Public Library • Main Branch • Tech Logic UltraSort"
    "</div>",
    unsafe_allow_html=True
)

if pipeline_status:
    last_run = pipeline_status.get("last_run", "Unknown")
    checkins_rows = pipeline_status.get("checkins_rows", 0)
    rejects_rows = pipeline_status.get("rejects_rows", 0)
    transit_items = pipeline_status.get("transit_items", 0)



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
        index=1
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

df["transit_destination"] = df["destination"].apply(normalize_transit_destination)

valid_transit_destinations = [
    "Westside",
    "Library Express",
]
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

westside_count = overall_metrics["westside_count"]
westside_pct = overall_metrics["westside_pct"]
library_express_count = overall_metrics["library_express_count"]
library_express_pct = overall_metrics["library_express_pct"]
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

if westside_pct >= 10:
    attention_items.append("Westside transit share is high. Watch for routing or branch-related issues.")

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

if len(historical_checkins_df) > 0:
    historical_westside_pct = (
        historical_checkins_df["destination"].astype(str).str.upper().str.contains("WESTSIDE", na=False).sum()
        / len(historical_checkins_df)
    ) * 100

    historical_library_express_pct = (
        historical_checkins_df["destination"].astype(str).str.upper().str.contains("LIBRARY EXPRESS", na=False).sum()
        / len(historical_checkins_df)
    ) * 100
else:
    historical_westside_pct = None
    historical_library_express_pct = None
    
    
today_westside_pct = (today_westside / today_checkins * 100) if today_checkins > 0 else 0
today_library_express_pct = (today_library_express / today_checkins * 100) if today_checkins > 0 else 0

today_hourly_checkins = today_df["datetime"].dt.hour.value_counts().sort_index()
today_hourly_rejects = today_rejects_df["datetime"].dt.hour.value_counts().sort_index()

today_bin0_count = 0
if "bin" in today_df.columns:
    today_bin0_count = (
        today_df["bin"].astype(str).str.contains("0", na=False).sum()
    )

today_estimated_holds = max(
    today_bin0_count - today_rejects - today_library_express,
    0
)

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
live_alert_title = ""
live_alert_text = ""
show_live_alert = False

if historical_daily_avg_reject > 0:
    if today_reject_rate >= historical_daily_avg_reject + 2:
        live_reject_card_border = "#dc2626"
        live_reject_value_color = "#dc2626"
        live_reject_subtitle_color = "#dc2626"
        show_live_alert = True
        live_alert_title = "Operational Alert"
        live_alert_text = (
            f"Today's reject rate is {today_reject_rate:.2f}%, which is {live_reject_deviation:+.2f}% "
            f"above the typical daily rate of {historical_daily_avg_reject:.2f}%. "
            f"Review today's top reject issues and check AMH conditions around the busiest hours."
        )
    elif today_reject_rate >= historical_daily_avg_reject + 1:
        live_reject_card_border = "#d97706"
        live_reject_value_color = "#d97706"
        live_reject_subtitle_color = "#d97706"
    else:
        live_reject_card_border = "#059669"
        live_reject_value_color = "#059669"
        live_reject_subtitle_color = "#059669"
        
        
alerts = get_system_alerts(
    pipeline_status=pipeline_status,
    show_live_alert=show_live_alert,
    westside_pct=today_westside_pct,
    library_express_pct=today_library_express_pct,
    historical_westside_pct=historical_westside_pct,
    historical_library_express_pct=historical_library_express_pct,
)


critical_alerts = []
warning_alerts = []
info_alerts = []

if alerts:
    critical_alerts = [a for a in alerts if a["level"].lower() == "critical"]
    warning_alerts = [a for a in alerts if a["level"].lower() == "warning"]
    info_alerts = [a for a in alerts if a["level"].lower() in ["info", "trend"]]      

    

if selected_view == "Live Today":
    st.header(f"{today.strftime('%A, %b %d')}")

    if checkins_updated is not None:
        st.markdown(
            f"""
            <div style="
                margin-top: -10px;
                margin-bottom: 12px;
                color: #6b7280;
                font-size: 0.95rem;
            ">
                Last updated: {checkins_updated.strftime('%b %d, %Y %I:%M %p')}
            </div>
            """,
            unsafe_allow_html=True
        )

    if st.button("Refresh Live Data"):
        st.cache_data.clear()
        st.success("Live data cache cleared. Reloading latest available files...")
        st.rerun()

    

    # =============================
    # Live Today KPI Groups - One Row
    # =============================

    live_group1, live_group2, live_group3 = st.columns(3)

    # Operations
    with live_group1:
        st.markdown(
            """
            <div style="
                border: 2px solid #60a5fa;
                border-radius: 14px;
                padding: 12px 14px;
                background: #60a5fa;
                margin-bottom: 8px;
            ">
                <div style="
                    font-size: 0.95rem;
                    font-weight: 700;
                    color: #ffffff;
                    line-height: 1.2;
                ">
                    Operations
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )
        ops1, ops2, ops3 = st.columns(3)

        with ops1:
            typical_daily_checkins = checkins_daily.mean() if len(checkins_daily) > 0 else 1
            checkins_fill_pct = (today_checkins / typical_daily_checkins) if typical_daily_checkins > 0 else 0

            render_kpi_card(
                "Checkins",
                f"{today_checkins:,}",
                "Processed today",
                "#6b7280",
                value_font_size="1.15rem",
                border_color="#93c5fd",
                fill_pct=checkins_fill_pct,
                fill_color="rgba(59, 130, 246, 0.14)"
            )

        with ops2:
            render_kpi_card(
                "Current Throughput",
                f"{today_metrics['current_speed']}",
                "Items this hour",
                "#6b7280",
                value_font_size="1.15rem",
                border_color="#93c5fd"
            )

        with ops3:
            if today_peak_hour is not None:
                render_kpi_card(
                    "Busiest Hour",
                    format_hour(today_peak_hour),
                    f"{today_peak_hour_count:,} items ({today_peak_hour_pct:.1f}%)",
                    "#6b7280",
                    value_font_size="1.05rem",
                    border_color="#93c5fd"
                )
            else:
                render_kpi_card(
                    "Busiest Hour",
                    "N/A",
                    "No activity yet",
                    "#6b7280",
                    value_font_size="1.05rem",
                    border_color="#93c5fd"
                )

    # Quality & Impact
    with live_group2:
        st.markdown(
            """
            <div style="
                border: 2px solid #34d399;
                border-radius: 14px;
                padding: 12px 14px;
                background: #34d399;
                margin-bottom: 8px;
            ">
                <div style="
                    font-size: 0.95rem;
                    font-weight: 700;
                    color: #ffffff;
                    line-height: 1.2;
                ">
                    Quality & Impact
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

        quality1, quality2 = st.columns(2)

        with quality1:
            reject_subtitle = f"{today_rejects:,} failures today"
            if historical_daily_avg_reject > 0:
                reject_subtitle = (
                    f"{live_reject_deviation:+.2f}% vs avg daily "
                    f"rate ({historical_daily_avg_reject:.2f}%)"
                )
        
            render_kpi_card(
                "Rejects",
                f"{today_rejects:,}",
                reject_subtitle,
                live_reject_subtitle_color,
                value_font_size="1.15rem",
                border_color=live_reject_card_border,
                value_color=live_reject_value_color
            )

        with quality2:
            reject_subtitle = "Of checkins today"
            if historical_daily_avg_reject > 0:
                reject_subtitle = (
                    f"{live_reject_deviation:+.2f}% vs avg daily "
                    f"rate ({historical_daily_avg_reject:.2f}%)"
                )

            render_kpi_card(
                "Reject Rate",
                f"{today_reject_rate:.2f}%",
                reject_subtitle,
                live_reject_subtitle_color,
                value_font_size="1.05rem",
                border_color=live_reject_card_border,
                value_color=live_reject_value_color
            )



    # Routing
    with live_group3:
        st.markdown(
            """
            <div style="
                border: 2px solid #a78bfa;
                border-radius: 14px;
                padding: 12px 14px;
                background: #a78bfa;
                margin-bottom: 8px;
            ">
                <div style="
                    font-size: 0.95rem;
                    font-weight: 700;
                    color: #ffffff;
                    line-height: 1.2;
                ">
                    Routing
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

        route1, route2, route3 = st.columns(3)

        with route1:
            total_transit_pct = (today_total_transit / today_checkins * 100) if today_checkins > 0 else 0
            render_kpi_card(
                "Total Transit",
                f"{today_total_transit:,}",
                f"{total_transit_pct:.1f}% of today",
                "#6b7280",
                value_font_size="1.15rem",
                border_color="#c4b5fd"
            )

        with route2:
            render_kpi_card(
                "Westside",
                f"{today_westside:,}",
                f"{today_westside_pct:.1f}% of today",
                "#6b7280",
                value_font_size="1.15rem",
                border_color="#c4b5fd"
            )

        with route3:
            render_kpi_card(
                "Library Express",
                f"{today_library_express:,}",
                f"{today_library_express_pct:.1f}% of today",
                "#6b7280",
                value_font_size="1.05rem",
                border_color="#c4b5fd"
            )




    if info_alerts:
        st.markdown(
            f"""
            <div style="
                border-left: 5px solid #2563eb;
                background-color: #eff6ff;
                padding: 14px 16px;
                border-radius: 8px;
                margin-top: 18px;
                margin-bottom: 12px;
            ">
                <div style="font-weight: 600; color: #1d4ed8; margin-bottom: 6px;">
                    Trends / Info
                </div>
                <ul style="margin: 0; padding-left: 18px; color: #1e3a8a;">
                    {''.join(f"<li><b>{a['level'].upper()}</b>: {a['text']}</li>" for a in info_alerts)}
                </ul>
            </div>
            """,
            unsafe_allow_html=True
        )

    if len(today_hourly_checkins) > 0:
        peak_hours_df = today_hourly_checkins.sort_values(ascending=False).head(3).reset_index()
        peak_hours_df.columns = ["hour", "checkins"]
        peak_hours_df["hour_label"] = peak_hours_df["hour"].apply(format_hour_plain)

        peak_hours_text = "<br>".join(
            [f"{row['hour_label']} — {int(row['checkins']):,} items" for _, row in peak_hours_df.iterrows()]
        )

        st.markdown(
            f"""
            <div style="
                border-left: 4px solid #2563eb;
                background-color: #f9fafb;
                padding: 14px 16px;
                border-radius: 8px;
                margin-top: 0px;
                margin-bottom: 8px;
            ">
                <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                    Peak Hours Today
                </div>
                <div style="color: #4b5563; line-height: 1.6;">
                    {peak_hours_text}
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )
    

    if show_live_alert:
        st.markdown(
            f"""
            <div style="
                border-left: 4px solid #dc2626;
                background-color: #fef2f2;
                padding: 14px 16px;
                border-radius: 8px;
                margin-top: 18px;
                margin-bottom: 8px;
            ">
                <div style="font-weight: 600; color: #991b1b; margin-bottom: 6px;">
                    {live_alert_title}
                </div>
                <div style="color: #7f1d1d; line-height: 1.4;">
                    {live_alert_text}
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )


    st.divider()
      
    st.subheader("Checkins by Hour")

    checkins_hour_base = pd.DataFrame({"hour": live_hour_range})
    checkins_hour_base["hour_label"] = checkins_hour_base["hour"].apply(format_hour_plain)

    if len(today_hourly_checkins) > 0:
        checkins_hour_df = today_hourly_checkins.reset_index()
        checkins_hour_df.columns = ["hour", "checkins"]
    else:
        checkins_hour_df = pd.DataFrame(columns=["hour", "checkins"])

    checkins_hour_df = checkins_hour_base.merge(checkins_hour_df, on="hour", how="left").fillna(0)
    checkins_hour_df["checkins"] = checkins_hour_df["checkins"].astype(int)

    checkins_hour_chart = (
        alt.Chart(checkins_hour_df)
        .mark_line(point=True, strokeWidth=3)
        .encode(
            x=alt.X(
                "hour_label:N",
                sort=checkins_hour_df["hour_label"].tolist(),
                title="Hour",
                axis=alt.Axis(labelAngle=0)
            ),
            y=alt.Y("checkins:Q", title="Checkins"),
            tooltip=["hour_label", "checkins"]
        )
        .properties(height=320)
    )

    render_chart(checkins_hour_chart)
    
    st.subheader("Bin Volume")
    st.caption("Distribution of items across sort bins for today.")
    if "bin" in today_df.columns:
        today_bin_kpi_df = today_df.copy()
        today_bin_kpi_df = today_bin_kpi_df[today_bin_kpi_df["bin"].notna()].copy()
        today_bin_kpi_df["bin"] = today_bin_kpi_df["bin"].astype(str)

        if len(today_bin_kpi_df) > 0:
            today_bin_summary = (
                today_bin_kpi_df["bin"]
                .value_counts()
                .sort_index()
                .reset_index()
            )
            today_bin_summary.columns = ["bin", "checkins"]
            today_bin_summary["pct_of_total"] = (
                today_bin_summary["checkins"] / today_bin_summary["checkins"].sum() * 100
            ).round(2)

            today_top_bin_row = today_bin_summary.loc[today_bin_summary["checkins"].idxmax()]

            bin_bar_df = today_bin_summary.copy()
            bin_bar_df["bin_label"] = bin_bar_df["bin"].apply(lambda b: f"Bin {b}")

            today_bin_bar_chart = (
                alt.Chart(bin_bar_df)
                .mark_bar()
                .encode(
                    x=alt.X(
                        "bin_label:N",
                        title="Bin",
                        axis=alt.Axis(labelAngle=0)
                    ),
                    y=alt.Y("checkins:Q", title="Checkins"),
                    tooltip=["bin_label", "checkins"]
                )
                .properties(height=350)
            )

            render_chart(today_bin_bar_chart)
        else:
            st.info("No binned checkins found for today.")
    else:
        st.info("No bin column found in today's checkins data.")


    st.subheader("Bin Volume by Hour")

    if "bin" in today_df.columns:
        today_bin_df = today_df.copy()
        today_bin_df = today_bin_df[today_bin_df["bin"].notna()].copy()
        today_bin_df["bin"] = today_bin_df["bin"].astype(str)

        if len(today_bin_df) > 0:
            today_hourly_bin = (
                today_bin_df.groupby([today_bin_df["datetime"].dt.hour, "bin"])
                .size()
                .unstack(fill_value=0)
            )

            today_hourly_bin = today_hourly_bin.reindex(live_hour_range, fill_value=0)

            today_hourly_bin_chart = today_hourly_bin.copy()
            today_hourly_bin_chart.columns = [f"Bin {col}" for col in today_hourly_bin_chart.columns]

            today_hourly_bin_display = today_hourly_bin_chart.copy().reset_index()
            today_hourly_bin_display.columns = ["hour"] + list(today_hourly_bin_display.columns[1:])
            today_hourly_bin_display["hour_label"] = today_hourly_bin_display["hour"].apply(format_hour_plain)

            today_hourly_bin_long = today_hourly_bin_display.melt(
                id_vars=["hour", "hour_label"],
                var_name="bin",
                value_name="checkins"
            )

            live_bin_chart = (
                alt.Chart(today_hourly_bin_long)
                .mark_line(point=False)
                .encode(
                    x=alt.X(
                        "hour_label:N",
                        sort=today_hourly_bin_display["hour_label"].tolist(),
                        title="Hour",
                        axis=alt.Axis(labelAngle=0)
                    ),
                    y=alt.Y("checkins:Q", title="Checkins"),
                    color=alt.Color("bin:N", title="Bin"),
                    tooltip=["hour_label", "bin", "checkins"]
                )
                .properties(height=350)
            )

            render_chart(live_bin_chart)
        else:
            st.info("No binned checkins found for today.")
    else:
        st.info("No bin column found in today's checkins data.")

    st.subheader("Top Issues Today")

    if len(today_rejects_df) > 0:
        today_issue_counts = today_rejects_df["error_simple"].value_counts()
        top_issue = today_issue_counts.index[0]
        top_issue_count = int(today_issue_counts.iloc[0])

        if len(today_issue_counts) > 1:
            second_issue = today_issue_counts.index[1]
            second_issue_count = int(today_issue_counts.iloc[1])
            issue_text = (
                f"Top issue today: {top_issue} ({top_issue_count}). "
                f"Next: {second_issue} ({second_issue_count})."
            )
        else:
            issue_text = f"Top issue today: {top_issue} ({top_issue_count})."

        st.markdown(
            f"""
            <div style="
                border-left: 4px solid #d97706;
                background-color: #f9fafb;
                padding: 14px 16px;
                border-radius: 8px;
                margin-top: 8px;
                margin-bottom: 8px;
            ">
                <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                    Issue Snapshot
                </div>
                <div style="color: #4b5563; line-height: 1.4;">
                    {issue_text}
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )
    else:
        st.info("No reject issues found for today.")


if selected_view == "Overview":
    st.subheader("Summary")
    st.caption("Get a historical summary by choosing a date range.")

    westside_transit_count = westside_count
    westside_transit_pct = westside_pct

    library_express_transit_count = library_express_count
    library_express_transit_pct = library_express_pct

    if len(rejects_df) > 0:
        top_issue = rejects_df["error_simple"].value_counts().idxmax()
        top_issue_count = rejects_df["error_simple"].value_counts().max()
        top_issue_pct = (top_issue_count / len(rejects_df)) * 100

        issue_explanations = {
            "Item Not Found": "Barcode not recognized by ILS / missing item record",
            "ILS / ACS Failure": "Communication issue between AMH and ILS/ACS",
            "RFID Collision": "Multiple tags detected in bin",
            "Call Number / Config Error": "Item routing configuration mismatch",
            "Routing Error": "Destination not resolved correctly",
            "Other": "Uncategorized system failure"
        }

        issue_detail = issue_explanations.get(top_issue, "Operational issue requiring review")

        top_issue_subtitle = (
            f"<span style='color:#059669'>{top_issue_count:,} rejects ({top_issue_pct:.1f}% of failures)</span><br>"
            f"<span style='color:#6b7280'>{issue_detail}</span>"
        )
    else:
        top_issue = "N/A"
        top_issue_subtitle = "No rejects in selected range"

    st.markdown(
        f"""
        <div style="
            border-left: 4px solid {attention_color};
            background-color: #f9fafb;
            padding: 14px 16px;
            border-radius: 8px;
            margin-top: 8px;
            margin-bottom: 18px;
        ">
            <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                {attention_title}
            </div>
            <div style="color: #4b5563; line-height: 1.5;">
                {attention_text}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    daily_counts = df["datetime"].dt.date.value_counts().sort_index()
    avg_daily_checkins = daily_counts.mean() if len(daily_counts) > 0 else 0

    overview_volume_mode = st.radio(
        "Volume display",
        ["Average per Day", "Total"],
        horizontal=True,
        key="overview_volume_mode"
    )

    days_in_range = df["datetime"].dt.date.nunique() if len(df) > 0 else 0

    avg_daily_westside = (westside_count / days_in_range) if days_in_range > 0 else 0
    avg_daily_library_express = (library_express_count / days_in_range) if days_in_range > 0 else 0
    avg_daily_rejects = (reject_count / days_in_range) if days_in_range > 0 else 0
    
    peak_avg_hour_value = "N/A"
    peak_avg_hour_subtitle = "No activity in selected range"
    peak_total_hour_value = "N/A"
    peak_total_hour_subtitle = "No activity in selected range"
    
    if len(df) > 0:
        # --- AVERAGE MODE ---
        hourly_source = df.copy()
        hourly_source["date_only"] = hourly_source["datetime"].dt.date
        hourly_source["hour_only"] = hourly_source["datetime"].dt.hour
    
        hourly_daily = (
            hourly_source.groupby(["date_only", "hour_only"])
            .size()
            .reset_index(name="checkins")
        )
    
        hourly_avg = (
            hourly_daily.groupby("hour_only")["checkins"]
            .mean()
            .reset_index(name="avg_checkins")
        )
    
        if len(hourly_avg) > 0:
            peak_avg_row = hourly_avg.loc[hourly_avg["avg_checkins"].idxmax()]
            peak_avg_hour_value = format_hour(int(peak_avg_row["hour_only"]))
            peak_avg_hour_subtitle = f"{peak_avg_row['avg_checkins']:,.1f} avg checkins/day"
    
        # --- TOTAL MODE ---
        hourly_total = (
            df["datetime"].dt.hour.value_counts()
            .sort_index()
            .reset_index()
        )
        hourly_total.columns = ["hour_only", "total_checkins"]
    
        if len(hourly_total) > 0:
            peak_total_row = hourly_total.loc[hourly_total["total_checkins"].idxmax()]
            peak_total_hour_value = format_hour(int(peak_total_row["hour_only"]))
            peak_total_hour_subtitle = f"{int(peak_total_row['total_checkins']):,} total checkins"

    fail_peak_avg_value = "N/A"
    fail_peak_avg_subtitle = "No failures in selected range"
    fail_peak_total_value = "N/A"
    fail_peak_total_subtitle = "No failures in selected range"
    
    if len(rejects_df) > 0:
        # --- AVERAGE MODE ---
        reject_hour_source = rejects_df.copy()
        reject_hour_source["date_only"] = reject_hour_source["datetime"].dt.date
        reject_hour_source["hour_only"] = reject_hour_source["datetime"].dt.hour
    
        fail_hour_daily = (
            reject_hour_source.groupby(["date_only", "hour_only"])
            .size()
            .reset_index(name="failures")
        )
    
        fail_hour_avg = (
            fail_hour_daily.groupby("hour_only")["failures"]
            .mean()
            .reset_index(name="avg_failures")
        )
    
        if len(fail_hour_avg) > 0:
            peak_avg_fail_row = fail_hour_avg.loc[fail_hour_avg["avg_failures"].idxmax()]
            fail_peak_avg_value = format_hour(int(peak_avg_fail_row["hour_only"]))
            fail_peak_avg_subtitle = f"{peak_avg_fail_row['avg_failures']:,.1f} avg failures/day"
    
        # --- TOTAL MODE ---
        fail_hour_total = (
            rejects_df["datetime"].dt.hour.value_counts()
            .sort_index()
            .reset_index()
        )
        fail_hour_total.columns = ["hour_only", "total_failures"]
    
        if len(fail_hour_total) > 0:
            peak_total_fail_row = fail_hour_total.loc[fail_hour_total["total_failures"].idxmax()]
            fail_peak_total_value = format_hour(int(peak_total_fail_row["hour_only"]))
            fail_peak_total_subtitle = f"{int(peak_total_fail_row['total_failures']):,} total rejects"

    peak_day_avg_value = "N/A"
    peak_day_avg_subtitle = "No activity in selected range"
    peak_day_total_value = "N/A"
    peak_day_total_subtitle = "No activity in selected range"

    if len(df) > 0:
        # --- TOTAL MODE (existing logic) ---
        daily_volume = df["datetime"].dt.date.value_counts().sort_index()
    
        if len(daily_volume) > 0:
            peak_day_date = daily_volume.idxmax()
            peak_day_count = int(daily_volume.max())
            peak_day_name = pd.to_datetime(peak_day_date).strftime("%A")
    
            peak_day_total_value = peak_day_name
            peak_day_total_subtitle = (
                f"{peak_day_count:,} checkins on "
                f"{pd.to_datetime(peak_day_date).strftime('%b %d, %Y')}"
            )
    
        # --- AVERAGE MODE (NEW FIX) ---
        weekday_avg = (
            df.assign(day_of_week=df["datetime"].dt.day_name())
              .groupby("day_of_week")
              .size()
              .div(df["datetime"].dt.date.nunique())
              .reindex([
                  "Monday", "Tuesday", "Wednesday",
                  "Thursday", "Friday", "Saturday", "Sunday"
              ])
        )

    if len(df) > 0 and len(weekday_avg) > 0:
        peak_day_avg_name = weekday_avg.idxmax()
        peak_day_avg_value = peak_day_avg_name
        peak_day_avg_subtitle = f"{weekday_avg.max():,.1f} avg checkins/day"

    row1_col1, row1_col2, row1_col3 = st.columns(3)
    row2_col1, row2_col2, row2_col3 = st.columns(3)
    row3_col1, row3_col2, row3_col3 = st.columns(3)

    # Row 1
    with row1_col1:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Avg Daily Checkins",
                f"{avg_daily_checkins:,.1f}",
                f"{start_date.strftime('%b %d')} – {end_date.strftime('%b %d')}",
                "#6b7280"
            )
        else:
            render_kpi_card(
                "Total Checkins",
                f"{len(df):,}",
                f"{start_date.strftime('%b %d')} – {end_date.strftime('%b %d')}",
                "#6b7280"
            )

    with row1_col2:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Avg Westside Transits",
                f"{avg_daily_westside:,.1f}",
                "Per day",
                "#6b7280"
            )
        else:
            render_kpi_card(
                "Total Westside Transits",
                f"{westside_transit_count:,}",
                f"{westside_transit_pct:.2f}% of total items",
                "#6b7280"
            )

    with row1_col3:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Avg Library Express Transits",
                f"{avg_daily_library_express:,.1f}",
                "Per day",
                "#6b7280"
            )
        else:
            render_kpi_card(
                "Total Library Express Transits",
                f"{library_express_transit_count:,}",
                f"{library_express_transit_pct:.2f}% of total items",
                "#6b7280"
            )

    # Row 2
    with row2_col1:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Avg Daily Rejects",
                f"{avg_daily_rejects:,.1f}",
                "Per day",
                "#6b7280"
            )
        else:
            render_kpi_card(
                "Reject Count",
                f"{reject_count:,}",
                "Total failed checkins",
                "#6b7280"
            )

    with row2_col2:
        render_kpi_card(
            "Reject %",
            f"{reject_pct:.2f}%",
            date_range_text,
            "#6b7280",
            value_font_size="1.55rem"
        )

    with row2_col3:
        render_kpi_card(
            "Top Issue",
            top_issue,
            top_issue_subtitle,
            "#059669",
            value_font_size="1.15rem",
            value_wrap=True
        )

    # Row 3
    with row3_col1:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Peak Avg Hour",
                peak_avg_hour_value,
                peak_avg_hour_subtitle,
                "#6b7280"
            )
        else:
            render_kpi_card(
                "Peak Total Hour",
                peak_total_hour_value,
                peak_total_hour_subtitle,
                "#6b7280"
            )

    with row3_col2:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Fail Peak Hr",
                fail_peak_avg_value,
                fail_peak_avg_subtitle,
                "#6b7280"
            )
        else:
            render_kpi_card(
                "Peak Failure Hour",
                fail_peak_total_value,
                fail_peak_total_subtitle,
                "#6b7280"
            )

    with row3_col3:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Peak Day of Week",
                peak_day_avg_value,
                peak_day_avg_subtitle,
                "#6b7280",
                value_font_size="1.4rem",
                value_wrap=True
            )
        else:
            render_kpi_card(
                "Peak Day",
                peak_day_total_value,
                peak_day_total_subtitle,
                "#6b7280",
                value_font_size="1.4rem",
                value_wrap=True
            )

    st.divider()


if selected_view == "Reports":
    st.header("Reports")

    # example placement inside selected_view == "Reports"
    # put this after you have avg_saved, total_saved, peak_day, MANUAL_RATE, and AMH_RATE
    
    director_pdf = build_director_report_pdf(
        start_date=start_date,
        end_date=end_date,
        df=df,
        rejects_df=rejects_df,
        overall_metrics=overall_metrics,
        top_issue=top_issue,
        attention_text=attention_text,
        avg_hours_saved=avg_saved,
        total_hours_saved=total_saved,
        peak_day_saved=float(peak_day["hours_saved"]) if peak_day is not None else None,
        peak_day_saved_date=pd.to_datetime(peak_day["date"]).strftime("%b %d, %Y") if peak_day is not None else None,
        manual_rate=MANUAL_RATE,
        amh_rate=AMH_RATE,
        library_name="New Braunfels Public Library",
        branch_name="Main Branch",
        system_name="Tech Logic UltraSort",
        report_title="AMH Director Report",
    )
    
    st.download_button(
        label="Download Director PDF",
        data=director_pdf,
        file_name=f"amh_director_report_{pd.to_datetime(start_date).strftime('%Y%m%d')}_{pd.to_datetime(end_date).strftime('%Y%m%d')}.pdf",
        mime="application/pdf",
        key="director_pdf_download"
    )
    
    # -----------------------------
    # Labor & Efficiency
    # -----------------------------
    st.subheader("Labor & Efficiency")
    st.caption("Estimates staff time saved by Automated Materials Handler processing.")

    with st.expander("Staff Time Equivalent", expanded=False):
        st.caption("Estimates staff time saved by comparing manual processing time against observed AMH processing time.")

        MANUAL_RATE = 50

        if len(df) > 0 and len(df_history_raw) > 0:
            rate_df = df.copy()
            rate_df["date"] = rate_df["datetime"].dt.date
            rate_df["hour"] = rate_df["datetime"].dt.hour

            daily_hourly = (
                rate_df.groupby(["date", "hour"])
                .size()
                .reset_index(name="checkins")
            )

            avg_hourly = (
                daily_hourly.groupby("hour")["checkins"]
                .mean()
                .reset_index(name="avg_items_per_hour")
            )

            if len(avg_hourly) > 0:
                peak_row = avg_hourly.loc[avg_hourly["avg_items_per_hour"].idxmax()]
                threshold = peak_row["avg_items_per_hour"] * 0.75
                peak_hours = avg_hourly[avg_hourly["avg_items_per_hour"] >= threshold].copy()

                AMH_RATE = (
                    peak_hours["avg_items_per_hour"].mean()
                    if len(peak_hours) > 0
                    else peak_row["avg_items_per_hour"]
                )
            else:
                AMH_RATE = 130.0

            daily_counts = df["datetime"].dt.date.value_counts().sort_index()
            staff_df = daily_counts.reset_index()
            staff_df.columns = ["date", "checkins"]

            staff_df["manual_hours"] = staff_df["checkins"] / MANUAL_RATE
            staff_df["amh_hours"] = staff_df["checkins"] / AMH_RATE
            staff_df["hours_saved"] = (staff_df["manual_hours"] - staff_df["amh_hours"]).clip(lower=0)
            staff_df["shifts_saved"] = staff_df["hours_saved"] / 8

            avg_saved = staff_df["hours_saved"].mean()
            total_saved = staff_df["hours_saved"].sum()
            peak_day = staff_df.loc[staff_df["hours_saved"].idxmax()]

            avg_daily_checkins = staff_df["checkins"].mean()
            avg_daily_manual_hours = staff_df["manual_hours"].mean()
            avg_daily_amh_hours = staff_df["amh_hours"].mean()

            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #059669;
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 8px;
                    margin-bottom: 16px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        Report Summary
                    </div>
                    <div style="color: #4b5563; line-height: 1.4;">
                        Using a manual processing rate of {MANUAL_RATE:.0f} items/hour and an observed AMH rate of
                        {AMH_RATE:.1f} items/hour, the average daily staff time saved in the selected range was
                        {avg_saved:,.2f} hours.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            k1, k2, k3 = st.columns(3)

            with k1:
                render_kpi_card("Avg Hours Saved", f"{avg_saved:,.2f}", "Per day", "#6b7280")

            with k2:
                render_kpi_card(
                    "Peak Day Saved",
                    f"{peak_day['hours_saved']:,.2f}",
                    pd.to_datetime(peak_day["date"]).strftime("%b %d, %Y"),
                    "#6b7280"
                )

            with k3:
                render_kpi_card(
                    "Total Hours Saved",
                    f"{total_saved:,.2f}",
                    "Across selected date range",
                    "#6b7280"
                )

            st.markdown(
                f"""

**Avg Hours Saved — {avg_saved:,.2f} hours/day**  
On an average day in the selected range, the AMH reduces staff workload by about **{avg_saved:,.2f} hours** compared to manual check-in processing.  
This is equivalent to roughly **{(avg_saved/8):,.2f} full staff shifts per day**.

**Peak Day Saved — {peak_day['hours_saved']:,.2f} hours**  
On the busiest day (**{pd.to_datetime(peak_day["date"]).strftime('%b %d, %Y')}**), the AMH reduced workload by **{peak_day['hours_saved']:,.2f} hours**.  
This represents the maximum operational impact observed in the selected period.

**Total Hours Saved — {total_saved:,.2f} hours**  
Across the full selected date range, the AMH reduced total staff workload by **{total_saved:,.2f} hours**.  
This is equivalent to approximately **{(total_saved/8):,.0f} full staff shifts** of labor.


- These values represent **estimated staff time avoided**, not time eliminated.
- Staff are still required for:
  - exception handling
  - routing issues
  - holds and transit processing
- The AMH primarily reduces repetitive scanning and sorting workload
- Higher numbers typically correlate with higher volume days, not necessarily inefficiency
                """
            )

            st.info(
                f"""### How Staff Time Saved Is Calculated
#### Average daily check-ins  
This is the average number of items checked in per day over the selected date range.

For **{start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}**, the average daily check-in volume is **{avg_daily_checkins:,.1f} items/day**.

#### Manual Processing Time

Manual rate = {MANUAL_RATE:.0f} items/hour* 

Manual time = {avg_daily_checkins:,.1f} items/day ÷ {MANUAL_RATE:.0f} items/hour  

**Manual time = {avg_daily_manual_hours:,.2f} staff hours/day**

\\*The manual check-in rate of 50 is based on circulation report data observed at the Westside branch during peak hours, where staff are working at their fastest steady pace.*

#### AMH Processing Time

Current AMH rate = {AMH_RATE:.1f} items/hour  

AMH time = {avg_daily_checkins:,.1f} items/day ÷ {AMH_RATE:.1f} items/hour  

**AMH time = {avg_daily_amh_hours:,.2f} machine hours/day**

#### Time Saved

Time saved = Manual time − AMH time  

Time saved = {avg_daily_manual_hours:,.2f} hours/day − {avg_daily_amh_hours:,.2f} hours/day  

**Average Daily Staff time saved = {avg_saved:,.2f} staff hours/day**
"""
            )




 # example placement inside selected_view == "Reports"
    # put this after you have avg_saved, total_saved, peak_day, MANUAL_RATE, and AMH_RATE
    
    director_pdf = build_director_report_pdf(
        start_date=start_date,
        end_date=end_date,
        df=df,
        rejects_df=rejects_df,
        overall_metrics=overall_metrics,
        top_issue=top_issue,
        attention_text=attention_text,
        avg_hours_saved=avg_saved,
        total_hours_saved=total_saved,
        peak_day_saved=float(peak_day["hours_saved"]) if peak_day is not None else None,
        peak_day_saved_date=pd.to_datetime(peak_day["date"]).strftime("%b %d, %Y") if peak_day is not None else None,
        manual_rate=MANUAL_RATE,
        amh_rate=AMH_RATE,
        library_name="New Braunfels Public Library",
        branch_name="Main Branch",
        system_name="Tech Logic UltraSort",
        report_title="AMH Director Report",
    )
    
    st.download_button(
        label="Download Director PDF",
        data=director_pdf,
        file_name=f"amh_director_report_{pd.to_datetime(start_date).strftime('%Y%m%d')}_{pd.to_datetime(end_date).strftime('%Y%m%d')}.pdf",
        mime="application/pdf",
        key="director_pdf_download"
    )
    # -----------------------------
    # Volume & Capacity
    # -----------------------------
    st.subheader("Volume & Capacity")
    st.caption("How much the AMH is processing, when demand peaks, and how current volume compares to normal patterns.")

    with st.expander("Weekday & Peak Analysis", expanded=False):
        st.caption("Shows volume trends by day of week and identifies peak operating times.")

        dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        if len(df) > 0:
            weekday_df = df.copy()
            weekday_df["date"] = weekday_df["datetime"].dt.date
            weekday_df["day_of_week"] = weekday_df["datetime"].dt.day_name()

            days_in_range = weekday_df["date"].nunique()

            dow_totals = (
                weekday_df.groupby("day_of_week")
                .size()
                .reindex(dow_order)
                .fillna(0)
                .reset_index(name="count")
            )

            daily_weekday = (
                weekday_df.groupby(["date", "day_of_week"])
                .size()
                .reset_index(name="daily_checkins")
            )

            dow_avg = (
                daily_weekday.groupby("day_of_week")["daily_checkins"]
                .mean()
                .reindex(dow_order)
                .fillna(0)
                .reset_index(name="avg_checkins")
            )

            dow_summary = dow_totals.merge(dow_avg, on="day_of_week", how="left")

            if len(dow_summary) > 0:
                busiest_day = dow_summary.loc[dow_summary["avg_checkins"].idxmax()]

                st.markdown(
                    f"""
                    <div style="
                        border-left: 4px solid #2563eb;
                        background-color: #f9fafb;
                        padding: 14px 16px;
                        border-radius: 8px;
                        margin-top: 8px;
                        margin-bottom: 16px;
                    ">
                        <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                            Report Summary
                        </div>
                        <div style="color: #4b5563; line-height: 1.4;">
                            Busiest average day: {busiest_day['day_of_week']} with
                            {busiest_day['avg_checkins']:,.1f} average checkins per day
                            across {days_in_range} day(s). Total volume for that weekday in the selected range:
                            {int(busiest_day['count']):,} checkins.
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

                dow_chart = build_category_bar_chart(
                    dow_summary.rename(columns={"avg_checkins": "avg_items_per_day"}),
                    "day_of_week",
                    "avg_items_per_day",
                    "Avg Checkins Per Day",
                    "Day of Week"
                )
                render_chart(dow_chart)

                dow_display = dow_summary.rename(columns={
                    "day_of_week": "Day of Week",
                    "count": "Total Checkins",
                    "avg_checkins": "Avg Checkins Per Day"
                })[["Day of Week", "Total Checkins", "Avg Checkins Per Day"]]

                dow_display["Avg Checkins Per Day"] = dow_display["Avg Checkins Per Day"].round(1)

                st.dataframe(dow_display, use_container_width=True)
                download_button(dow_display, "weekday_volume.csv")
            else:
                st.info("No weekday data available for selected range.")
        else:
            st.info("No weekday data available for selected range.")

        st.subheader("Peak Hour Analysis")

        if len(df) > 0:
            peak_df = df.copy()
            peak_df["date"] = peak_df["datetime"].dt.date
            peak_df["hour"] = peak_df["datetime"].dt.hour

            days_in_range = peak_df["date"].nunique()

            hour_totals = (
                peak_df.groupby("hour")
                .size()
                .reset_index(name="count")
            )

            hour_daily = (
                peak_df.groupby(["date", "hour"])
                .size()
                .reset_index(name="hourly_checkins")
            )

            hour_avg = (
                hour_daily.groupby("hour")["hourly_checkins"]
                .mean()
                .reset_index(name="avg_checkins")
            )

            hour_summary = hour_totals.merge(hour_avg, on="hour", how="left")
            hour_summary["hour_label"] = hour_summary["hour"].apply(format_hour_plain)
            hour_summary = hour_summary[(hour_summary["hour"] >= 7) & (hour_summary["hour"] <= 20)].copy()

            if len(hour_summary) > 0:
                busiest_hour_row = hour_summary.loc[hour_summary["avg_checkins"].idxmax()]

                st.markdown(
                    f"""
                    <div style="
                        border-left: 4px solid #2563eb;
                        background-color: #f9fafb;
                        padding: 14px 16px;
                        border-radius: 8px;
                        margin-top: 8px;
                        margin-bottom: 16px;
                    ">
                        <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                            Report Summary
                        </div>
                        <div style="color: #4b5563; line-height: 1.4;">
                            Busiest average hour: {busiest_hour_row["hour_label"]} with
                            {busiest_hour_row["avg_checkins"]:,.1f} average checkins per day
                            across {days_in_range} day(s). Total volume during that hour in the selected range:
                            {int(busiest_hour_row["count"]):,} checkins.
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

                peak_hour_chart = build_hourly_bar_chart(
                    hour_summary.rename(columns={"avg_checkins": "avg_items_per_hour"}),
                    "avg_items_per_hour",
                    "Avg Checkins Per Hour"
                )
                render_chart(peak_hour_chart)

                peak_hour_display = hour_summary.rename(columns={
                    "hour_label": "Hour",
                    "count": "Total Checkins",
                    "avg_checkins": "Avg Checkins Per Day"
                })[["Hour", "Total Checkins", "Avg Checkins Per Day"]]

                peak_hour_display["Avg Checkins Per Day"] = peak_hour_display["Avg Checkins Per Day"].round(1)

                st.dataframe(peak_hour_display, use_container_width=True)
                download_button(
                    peak_hour_display,
                    "peak_hour_analysis.csv"
                )
            else:
                st.info("No hourly data available for selected range.")
        else:
            st.info("No hourly data available for selected range.")

    

    with st.expander("Throughput", expanded=False):
        st.caption("Shows average checkins per hour per day across the selected date range, so multi-day ranges do not overstate throughput.")

        if len(df) > 0:
            throughput_df = df.copy()
            throughput_df["date"] = throughput_df["datetime"].dt.date
            throughput_df["hour"] = throughput_df["datetime"].dt.hour

            daily_hourly = (
                throughput_df.groupby(["date", "hour"])
                .size()
                .reset_index(name="checkins")
            )

            avg_hourly = (
                daily_hourly.groupby("hour")["checkins"]
                .mean()
                .reset_index(name="avg_items_per_hour")
            )

            avg_hourly["hour_label"] = avg_hourly["hour"].apply(format_hour_plain)

            peak_row = avg_hourly.loc[avg_hourly["avg_items_per_hour"].idxmax()]
            avg_items_per_hour = avg_hourly["avg_items_per_hour"].mean()

            peak_threshold = peak_row["avg_items_per_hour"] * 0.75
            peak_hours_df = avg_hourly[avg_hourly["avg_items_per_hour"] >= peak_threshold].copy()
            peak_times_avg = peak_hours_df["avg_items_per_hour"].mean() if len(peak_hours_df) > 0 else 0

            days_in_range = throughput_df["date"].nunique()

            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #2563eb;
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 8px;
                    margin-bottom: 16px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        Report Summary
                    </div>
                    <div style="color: #4b5563; line-height: 1.4;">
                        Based on {days_in_range} day(s) in the selected range, the busiest average hour was {peak_row["hour_label"]}
                        at {peak_row["avg_items_per_hour"]:,.1f} checkins per day.
                        Average checkins per active hour: {avg_items_per_hour:,.1f}.
                        Average during busiest hours: {peak_times_avg:,.1f}.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            k1, k2, k3 = st.columns(3)

            with k1:
                render_kpi_card(
                    "Avg Checkins / Hour",
                    f"{avg_items_per_hour:,.1f}",
                    "Average per hour per day",
                    "#6b7280",
                    value_font_size="1.6rem"
                )

            with k2:
                render_kpi_card(
                    "Peak Hours Avg",
                    f"{peak_times_avg:,.1f}",
                    "Average during busiest hours",
                    "#6b7280",
                    value_font_size="1.6rem"
                )

            with k3:
                render_kpi_card(
                    "Days in Range",
                    f"{days_in_range}",
                    "Days used in this average",
                    "#6b7280",
                    value_font_size="1.6rem"
                )

            avg_hourly = avg_hourly[(avg_hourly["hour"] >= 7) & (avg_hourly["hour"] <= 20)].copy()
            throughput_chart = build_hourly_bar_chart(
                avg_hourly.rename(columns={"avg_items_per_hour": "items_per_hour"}),
                "items_per_hour",
                "Avg Checkins Per Hour"
            )
            render_chart(throughput_chart)

            display_df = avg_hourly.rename(columns={
                "hour_label": "Hour",
                "avg_items_per_hour": "Avg Checkins Per Hour"
            })[["Hour", "Avg Checkins Per Hour"]]

            st.dataframe(display_df, use_container_width=True)
            download_button(display_df, "throughput_report.csv")
        else:
            st.info("No throughput data available for the selected date range.")

    with st.expander("Today vs Typical Hourly Pattern", expanded=False):

        today_df_report = df_live_raw[df_live_raw["datetime"].dt.date == today].copy()
        historical_df_report = df_history_raw[df_history_raw["datetime"].dt.date < today].copy()

        today_hourly = today_df_report["datetime"].dt.hour.value_counts().sort_index()

        if len(historical_df_report) > 0 and historical_df_report["datetime"].dt.date.nunique() > 0:
            typical_hourly = (
                historical_df_report.groupby(historical_df_report["datetime"].dt.hour).size()
                / historical_df_report["datetime"].dt.date.nunique()
            )
        else:
            typical_hourly = pd.Series(dtype=float)

        all_hours = sorted(set(today_hourly.index).union(set(typical_hourly.index)))

        compare_df = pd.DataFrame({"hour": all_hours})
        compare_df["today"] = compare_df["hour"].map(today_hourly).fillna(0)
        compare_df["typical"] = compare_df["hour"].map(typical_hourly).fillna(0).round(1)
        compare_df["delta"] = compare_df["today"] - compare_df["typical"]
        compare_df["hour_label"] = compare_df["hour"].apply(format_hour_plain)

        if len(compare_df) > 0:
            max_delta_row = compare_df.loc[compare_df["delta"].idxmax()]

            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #059669;
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 8px;
                    margin-bottom: 16px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        Report Summary
                    </div>
                    <div style="color: #4b5563; line-height: 1.4;">
                        Biggest deviation at {max_delta_row["hour_label"]} —
                        {max_delta_row["delta"]:+.1f} items versus the typical hourly pattern.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            compare_df = compare_df[(compare_df["hour"] >= 7) & (compare_df["hour"] <= 20)].copy()

            compare_long = compare_df.melt(
                id_vars=["hour", "hour_label"],
                value_vars=["today", "typical"],
                var_name="series",
                value_name="items"
            )

            compare_chart = build_hourly_line_chart(compare_long, "items", "Items", series_col="series")
            render_chart(compare_chart)

            display_df = compare_df[["hour_label", "today", "typical", "delta"]].rename(
                columns={"hour_label": "hour"}
            )
            st.dataframe(display_df, use_container_width=True)
            download_button(display_df, "today_vs_typical_hourly_pattern.csv")
        else:
            st.info("Not enough data available to compare today versus the typical hourly pattern.")

    
    # -----------------------------
    # Routing & Destinations
    # -----------------------------
    st.subheader("Routing & Destinations")
    st.caption("Shows where items are being sent after check-in and highlights routing concentration.")

    with st.expander("Destination Breakdown", expanded=False):
        destination_counts = df["destination"].value_counts().reset_index()
        destination_counts.columns = ["destination", "count"]

        if len(destination_counts) > 0:
            top_destination_row = destination_counts.loc[destination_counts["count"].idxmax()]
            top_destination_pct = (top_destination_row["count"] / destination_counts["count"].sum()) * 100

            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #2563eb;
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 8px;
                    margin-bottom: 16px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        Report Summary
                    </div>
                    <div style="color: #4b5563; line-height: 1.4;">
                        Top destination: {top_destination_row["destination"]} with {int(top_destination_row["count"]):,} items
                        ({top_destination_pct:.1f}% of all checkins).
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            destination_chart = (
                alt.Chart(destination_counts)
                .mark_bar()
                .encode(
                    x=alt.X(
                        "destination:N",
                        sort=destination_counts["destination"].tolist(),
                        title="Destination",
                        axis=alt.Axis(labelAngle=0)
                    ),
                    y=alt.Y("count:Q", title="Items"),
                    tooltip=["destination", "count"]
                )
                .properties(height=350)
            )

            render_chart(destination_chart)

            st.dataframe(destination_counts, use_container_width=True)
            download_button(destination_counts, "destination_breakdown.csv")
        else:
            st.info("No destination data available for the selected date range.")


    # -----------------------------
    # Errors & Exceptions
    # -----------------------------
    st.subheader("Errors & Exceptions")
    st.caption("Tracks failure types, exception routing, and patterns that may indicate operational issues.")

    with st.expander("Reject Reasons", expanded=False):
        reject_counts = rejects_df["error_simple"].value_counts().reset_index()
        reject_counts.columns = ["reason", "count"]

        if len(reject_counts) > 0:
            top_reason_row = reject_counts.loc[reject_counts["count"].idxmax()]
            top_reason_pct = (top_reason_row["count"] / reject_counts["count"].sum()) * 100

            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #dc2626;
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 8px;
                    margin-bottom: 16px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        Report Summary
                    </div>
                    <div style="color: #4b5563; line-height: 1.4;">
                        Top reject reason: {top_reason_row["reason"]} with {int(top_reason_row["count"]):,} rejects
                        ({top_reason_pct:.1f}% of all failures).
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            reject_chart = (
                alt.Chart(reject_counts)
                .mark_bar()
                .encode(
                    x=alt.X(
                        "reason:N",
                        sort=reject_counts["reason"].tolist(),
                        title="Reject Reason",
                        axis=alt.Axis(labelAngle=0)
                    ),
                    y=alt.Y("count:Q", title="Count"),
                    tooltip=["reason", "count"]
                )
                .properties(height=350)
            )

            render_chart(reject_chart)

            st.dataframe(reject_counts, use_container_width=True)
            download_button(reject_counts, "reject_reasons.csv")
        else:
            st.info("No reject reason data available for the selected date range.")

    with st.expander("Worst Days (Top 5 by Reject Rate)", expanded=False):
        worst_table = pd.DataFrame({
            "checkins": checkins_daily,
            "rejects": rejects_daily
        }).fillna(0)

        worst_table = worst_table[worst_table["checkins"] > 0]

        if len(worst_table) > 0:
            worst_table["reject_rate"] = (worst_table["rejects"] / worst_table["checkins"]) * 100
            worst_table.index = pd.to_datetime(worst_table.index)
            worst_table["day_of_week"] = worst_table.index.day_name()
            worst_table = worst_table.sort_values("reject_rate", ascending=False).head(5)

            worst_day_row = worst_table.iloc[0]

            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #dc2626;
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 8px;
                    margin-bottom: 16px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        Report Summary
                    </div>
                    <div style="color: #4b5563; line-height: 1.4;">
                        Worst day in the selected range: {worst_day_row['day_of_week']} with a reject rate of
                        {worst_day_row['reject_rate']:.2f}%.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            worst_table_display = worst_table.copy()
            worst_table_display["reject_rate"] = worst_table_display["reject_rate"].round(2)
            worst_table_display = worst_table_display[["day_of_week", "checkins", "rejects", "reject_rate"]]

            st.dataframe(worst_table_display, use_container_width=True)
            download_button(
                worst_table_display,
                "worst_days_top_5_reject_rate.csv",
                key="worst_days_top_5_reject_rate_download"
            )
        else:
            st.info("No worst-day data available for the selected date range.")



        st.markdown("### Top Issues Report")
    
        if len(rejects_df) > 0:
            top_issues_df = (
                rejects_df["error_simple"]
                .value_counts()
                .reset_index()
            )
            top_issues_df.columns = ["Issue", "Reject Count"]
            top_issues_df["% of Failures"] = (
                top_issues_df["Reject Count"] / top_issues_df["Reject Count"].sum() * 100
            ).round(1)
    
            issue_explanations = {
                "Item Not Found": "Barcode not recognized by ILS / missing item record",
                "ILS / ACS Failure": "Communication issue between AMH and ILS/ACS",
                "RFID Collision": "Multiple tags detected in bin",
                "Call Number / Config Error": "Item routing configuration mismatch",
                "Routing Error": "Destination not resolved correctly",
                "Other": "Uncategorized system failure"
            }
    
            top_issues_df["Explanation"] = top_issues_df["Issue"].map(issue_explanations).fillna("Operational issue requiring review")
    
            st.dataframe(top_issues_df, use_container_width=True)
        else:
            st.info("No reject issues found for the selected date range.")

    with st.expander("Exceptions / Overflow", expanded=False):
        if "bin" not in df.columns:
            st.warning("No bin column found in the current dataset. Add bin parsing to your cleaned checkins file first.")
        else:
            EXCEPTION_BIN = "0"

            bin_df = df.copy()
            bin_df = bin_df[bin_df["bin"].notna()].copy()
            bin_df["bin"] = bin_df["bin"].astype(str)

            exception_df = bin_df[bin_df["bin"] == EXCEPTION_BIN].copy()
            total_binned = len(bin_df)
            exception_count = len(exception_df)
            exception_pct = (exception_count / total_binned * 100) if total_binned > 0 else 0

            library_express_count = int(
                df["destination"].astype(str).str.upper().str.contains("LIBRARY EXPRESS", na=False).sum()
            )

            estimated_holds = max(
                exception_count - len(rejects_df) - library_express_count,
                0
            )

            estimated_holds_pct = (estimated_holds / exception_count * 100) if exception_count > 0 else 0

            daily_exception = exception_df["datetime"].dt.date.value_counts().sort_index()
            daily_total = bin_df["datetime"].dt.date.value_counts().sort_index()

            overflow_daily = pd.DataFrame({
                "total_binned": daily_total,
                "exception_bin_items": daily_exception
            }).fillna(0)

            overflow_daily["exception_rate_pct"] = (
                overflow_daily["exception_bin_items"] / overflow_daily["total_binned"] * 100
            ).round(2)

            peak_exception_day_label = "N/A"
            peak_exception_rate = 0
            if len(overflow_daily) > 0:
                peak_exception_day = overflow_daily["exception_rate_pct"].idxmax()
                peak_exception_day_label = pd.to_datetime(peak_exception_day).strftime("%a, %b %d")
                peak_exception_rate = overflow_daily["exception_rate_pct"].max()

            hourly_exception = exception_df["datetime"].dt.hour.value_counts().sort_index()
            hourly_exception_df = hourly_exception.reset_index()
            hourly_exception_df.columns = ["hour", "exception_items"]
            if len(hourly_exception_df) > 0:
                hourly_exception_df["hour_label"] = hourly_exception_df["hour"].apply(format_hour_plain)
                peak_exception_hour_row = hourly_exception_df.loc[hourly_exception_df["exception_items"].idxmax()]
                peak_exception_hour_text = peak_exception_hour_row["hour_label"]
                peak_exception_hour_count = int(peak_exception_hour_row["exception_items"])
            else:
                peak_exception_hour_text = "N/A"
                peak_exception_hour_count = 0

            insight_text = (
                f"Exception bin {EXCEPTION_BIN} handled {exception_count:,} items "
                f"({exception_pct:.2f}% of all binned checkins). "
                f"Peak exception day: {peak_exception_day_label} at {peak_exception_rate:.2f}%. "
                f"Peak exception hour: {peak_exception_hour_text} with {peak_exception_hour_count:,} items."
            )

            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #d97706;
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 8px;
                    margin-bottom: 16px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        Report Summary
                    </div>
                    <div style="color: #4b5563; line-height: 1.4;">
                        {insight_text}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            k1, k2, k3, k4 = st.columns(4)
            with k1:
                render_kpi_card(
                    "Exception Bin",
                    f"Bin {EXCEPTION_BIN}",
                    "Assumed overflow / exception lane",
                    "#6b7280",
                    value_font_size="1.4rem"
                )
            with k2:
                render_kpi_card(
                    "Exception Items",
                    f"{exception_count:,}",
                    "Routed to exception bin",
                    "#6b7280"
                )
            with k3:
                render_kpi_card(
                    "Exception Share",
                    f"{exception_pct:.2f}%",
                    "Of all binned checkins",
                    "#6b7280",
                    value_font_size="1.4rem"
                )
            with k4:
                render_kpi_card(
                    "Estimated Holds",
                    f"{estimated_holds:,}",
                    f"{estimated_holds_pct:.1f}% of Bin 0",
                    "#6b7280"
                )

            if len(overflow_daily) > 0:
                st.subheader("Exception Bin Rate by Day")
                chart_df = overflow_daily["exception_rate_pct"]
                st.line_chart(chart_df)

                overflow_daily_display = overflow_daily.reset_index().rename(columns={"index": "date"})
                st.dataframe(overflow_daily_display, use_container_width=True)
                download_button(
                    overflow_daily_display,
                    "exception_bin_rate_by_day_report.csv",
                    key="exception_bin_rate_by_day_report_download"
                )

            if len(exception_df) > 0:
                st.subheader("Exception Bin Volume by Hour")

                exception_hourly_source = exception_df.copy()
                exception_hourly_source["date"] = exception_hourly_source["datetime"].dt.date
                exception_hourly_source["hour"] = exception_hourly_source["datetime"].dt.hour

                days_in_range = exception_hourly_source["date"].nunique()

                exception_hour_totals = (
                    exception_hourly_source.groupby("hour")
                    .size()
                    .reset_index(name="exception_items")
                )

                exception_hour_daily = (
                    exception_hourly_source.groupby(["date", "hour"])
                    .size()
                    .reset_index(name="daily_exception_items")
                )

                exception_hour_avg = (
                    exception_hour_daily.groupby("hour")["daily_exception_items"]
                    .mean()
                    .reset_index(name="avg_exception_items")
                )

                hourly_exception_summary = exception_hour_totals.merge(
                    exception_hour_avg,
                    on="hour",
                    how="left"
                )

                hourly_exception_summary["hour_label"] = hourly_exception_summary["hour"].apply(format_hour_plain)
                hourly_exception_summary = hourly_exception_summary[
                    (hourly_exception_summary["hour"] >= 7) & (hourly_exception_summary["hour"] <= 20)
                ].copy()

                if len(hourly_exception_summary) > 0:
                    peak_exception_hour_row = hourly_exception_summary.loc[
                        hourly_exception_summary["avg_exception_items"].idxmax()
                    ]

                    st.markdown(
                        f"""
                        <div style="
                            border-left: 4px solid #d97706;
                            background-color: #f9fafb;
                            padding: 14px 16px;
                            border-radius: 8px;
                            margin-top: 8px;
                            margin-bottom: 16px;
                        ">
                            <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                                Report Summary
                            </div>
                            <div style="color: #4b5563; line-height: 1.4;">
                                Highest average exception-bin hour: {peak_exception_hour_row["hour_label"]} with
                                {peak_exception_hour_row["avg_exception_items"]:,.1f} average items per day
                                across {days_in_range} day(s). Total exception-bin volume during that hour in the
                                selected range: {int(peak_exception_hour_row["exception_items"]):,} items.
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )

                    exception_chart = build_hourly_bar_chart(
                        hourly_exception_summary.rename(columns={"avg_exception_items": "avg_items_per_hour"}),
                        "avg_items_per_hour",
                        "Avg Exception Items Per Hour"
                    )
                    render_chart(exception_chart)

                    hourly_exception_display = hourly_exception_summary.rename(columns={
                        "hour_label": "Hour",
                        "exception_items": "Total Exception Items",
                        "avg_exception_items": "Avg Exception Items Per Day"
                    })[["Hour", "Total Exception Items", "Avg Exception Items Per Day"]]

                    hourly_exception_display["Avg Exception Items Per Day"] = (
                        hourly_exception_display["Avg Exception Items Per Day"].round(1)
                    )

                    st.dataframe(hourly_exception_display, use_container_width=True)
                    download_button(
                        hourly_exception_display,
                        "exception_bin_volume_by_hour_report.csv",
                        key="exception_bin_volume_by_hour_report_download"
                    )
                else:
                    st.info("No exception-bin items found for the selected date range.")
            else:
                st.info("No exception-bin items found for the selected date range.")


    # -----------------------------
    # Bin Activity
    # -----------------------------
    st.subheader("Bin Activity")
    st.caption("Shows how items are distributed across physical bins and which bins are handling the most traffic.")

    with st.expander("Bin Volume", expanded=False):
        if "bin" not in df.columns:
            st.warning("No bin column found in the current dataset. Add bin parsing to your cleaned checkins file first.")
        else:
            bin_df = df.copy()
            bin_df = bin_df[bin_df["bin"].notna()].copy()
            bin_df["bin"] = bin_df["bin"].astype(str)

            bin_summary = (
                bin_df["bin"]
                .value_counts()
                .sort_index()
                .reset_index()
            )
            bin_summary.columns = ["bin", "checkins"]
            bin_summary["pct_of_total"] = (bin_summary["checkins"] / bin_summary["checkins"].sum() * 100).round(2)

            top_bin_row = bin_summary.loc[bin_summary["checkins"].idxmax()]
            low_bin_row = bin_summary.loc[bin_summary["checkins"].idxmin()]
            bin_discrepancy = int(top_bin_row["checkins"] - low_bin_row["checkins"])

            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #2563eb;
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 8px;
                    margin-bottom: 16px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        Report Summary
                    </div>
                    <div style="color: #4b5563; line-height: 1.4;">
                        Most-used bin: {top_bin_row["bin"]} with {int(top_bin_row["checkins"]):,} items
                        ({top_bin_row["pct_of_total"]:.2f}% of all binned checkins).
                        Lowest-volume bin: {low_bin_row["bin"]} with {int(low_bin_row["checkins"]):,} items.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
            
            k1, k2, k3, k4 = st.columns(4)
            
            with k1:
                render_kpi_card(
                    "Binned Checkins",
                    f"{int(bin_summary['checkins'].sum()):,}",
                    "Items with a detected bin",
                    "#6b7280"
                )
            
            with k2:
                render_kpi_card(
                    "Top Bin",
                    f"Bin {top_bin_row['bin']}",
                    f"{int(top_bin_row['checkins']):,} items",
                    "#6b7280",
                    value_font_size="1.4rem"
                )
            
            with k3:
                render_kpi_card(
                    "Top Bin Share",
                    f"{top_bin_row['pct_of_total']:.2f}%",
                    "Of all binned checkins",
                    "#6b7280",
                    value_font_size="1.4rem"
                )
            
            with k4:
                render_kpi_card(
                    "Bin Discrepancy",
                    f"{top_bin_row['pct_of_total'] - low_bin_row['pct_of_total']:.2f}% gap",
                    f"Between highest and lowest bin share<br>"
                    f"Bin {top_bin_row['bin']} leads ({top_bin_row['pct_of_total']:.2f}%) •"
                    f"Bin {low_bin_row['bin']} lowest ({low_bin_row['pct_of_total']:.2f}%)",
                    "#6b7280",
                    value_font_size="1.4rem"
                )

            bin_volume_display = bin_summary.rename(columns={
                "bin": "Bin",
                "checkins": "Checkins",
                "pct_of_total": "% of Total"
            })

            st.dataframe(bin_volume_display, use_container_width=True)
            download_button(bin_volume_display, "bin_volume_report.csv")

            hour_range = list(range(7, 21))

            bin_hourly_source = bin_df.copy()
            bin_hourly_source["date"] = bin_hourly_source["datetime"].dt.date
            bin_hourly_source["hour"] = bin_hourly_source["datetime"].dt.hour

            daily_bin_hour = (
                bin_hourly_source.groupby(["date", "hour", "bin"])
                .size()
                .reset_index(name="daily_checkins")
            )

            avg_bin_hour = (
                daily_bin_hour.groupby(["hour", "bin"])["daily_checkins"]
                .mean()
                .reset_index(name="avg_checkins")
            )

            total_bin_hour = (
                bin_hourly_source.groupby(["hour", "bin"])
                .size()
                .reset_index(name="total_checkins")
            )

            hourly_bin_summary = total_bin_hour.merge(
                avg_bin_hour,
                on=["hour", "bin"],
                how="left"
            )

            if len(hourly_bin_summary) > 0:
                st.subheader("Bin Volume by Hour")

                hourly_bin_summary = hourly_bin_summary[
                    (hourly_bin_summary["hour"] >= 7) & (hourly_bin_summary["hour"] <= 20)
                ].copy()

                hourly_bin_summary["hour_label"] = hourly_bin_summary["hour"].apply(format_hour_plain)
                hourly_bin_summary["bin_label"] = hourly_bin_summary["bin"].apply(lambda b: f"Bin {b}")

                bin_chart = (
                    alt.Chart(hourly_bin_summary)
                    .mark_line(point=False)
                    .encode(
                        x=alt.X(
                            "hour_label:N",
                            sort=[format_hour_plain(h) for h in hour_range],
                            title="Hour",
                            axis=alt.Axis(labelAngle=0)
                        ),
                        y=alt.Y("avg_checkins:Q", title="Avg Checkins Per Hour"),
                        color=alt.Color("bin_label:N", title="Bin"),
                        tooltip=[
                            "hour_label",
                            "bin_label",
                            alt.Tooltip("avg_checkins:Q", title="Avg Checkins", format=".1f"),
                            alt.Tooltip("total_checkins:Q", title="Total Checkins")
                        ]
                    )
                    .properties(height=350)
                    .interactive(False)
                )

                st.altair_chart(bin_chart, use_container_width=True)

                hourly_bin_display = hourly_bin_summary.pivot_table(
                    index="hour_label",
                    columns="bin_label",
                    values=["total_checkins", "avg_checkins"],
                    fill_value=0
                )

                hourly_bin_display.columns = [
                    f"{bin_name} Total" if metric == "total_checkins" else f"{bin_name} Avg/Day"
                    for metric, bin_name in hourly_bin_display.columns
                ]

                hourly_bin_display = hourly_bin_display.reset_index().rename(columns={"hour_label": "Hour"})

                avg_cols = [col for col in hourly_bin_display.columns if col.endswith("Avg/Day")]
                for col in avg_cols:
                    hourly_bin_display[col] = hourly_bin_display[col].round(1)

                st.dataframe(hourly_bin_display, use_container_width=True)
                download_button(hourly_bin_display, "bin_volume_by_hour_report.csv")

if selected_view == "Transits":
    st.header("Transit Routing")
    st.caption("Tracks items routed to transit destinations such as Westside and Library Express.")

    transit_mode = st.radio(
        "Transit view",
        ["Selected Range", "Today"],
        horizontal=True,
        key="transit_view_mode"
    )

    if transit_mode == "Today":
        base_df = today_df.copy()
        base_rejects_df = today_rejects_df.copy()
        date_label = today.strftime("%b %d, %Y")
    else:
        base_df = df.copy()
        base_rejects_df = rejects_df.copy()
        date_label = f"{start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}"

    base_df["transit_destination"] = base_df["destination"].apply(normalize_transit_destination)

    st.caption(f"Showing: {date_label}")

    valid_transit_destinations = [
        "Westside",
        "Library Express",
    ]

    transit_df = base_df[
        base_df["transit_destination"].isin(valid_transit_destinations)
    ].copy()

    transit_summary = get_transit_summary(base_df)
    transit_time_summary = get_transit_time_summary(transit_df)

    total_transit_items = len(transit_df)
    total_transit_pct = (total_transit_items / len(base_df) * 100) if len(base_df) > 0 else 0
    transit_destination_count = transit_summary["destination"].nunique() if len(transit_summary) > 0 else 0

    top_transit_destination = "N/A"
    top_transit_subtitle = ""

    if len(transit_summary) > 0:
        top_row = transit_summary.iloc[0]
        top_transit_destination = top_row["destination"]
        top_transit_subtitle = (
            f"{int(top_row['transit_items']):,} items "
            f"({float(top_row['pct_of_total_items']):.2f}% of total)"
        )

    westside_count = len(
        base_df[base_df["transit_destination"] == "Westside"]
    )
    westside_pct = (westside_count / len(base_df) * 100) if len(base_df) > 0 else 0

    library_express_count = len(
        base_df[base_df["transit_destination"] == "Library Express"]
    )
    library_express_pct = (library_express_count / len(base_df) * 100) if len(base_df) > 0 else 0

    no_agency_dest_count = int(
        base_df["destination"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False).sum()
    )

    peak_transit_day = get_peak_transit_day_summary(transit_df, weekday_order)
    peak_transit_day_label = peak_transit_day["peak_transit_day_label"]
    peak_transit_day_subtitle = peak_transit_day["peak_transit_day_subtitle"]

    transit_weekday_comparison = get_transit_weekday_comparison(base_df, base_rejects_df, weekday_order)
    destination_weekday_mix = get_destination_weekday_mix(transit_df, weekday_order)

    transit_insight = get_transit_reject_insight(transit_weekday_comparison)
    transit_reject_insight_title = transit_insight["title"]
    transit_reject_insight_text = transit_insight["text"]
    transit_reject_insight_color = transit_insight["color"]

    destination_reject_summary = get_destination_reject_summary(
        base_df,
        base_rejects_df,
        transit_summary,
        valid_transit_destinations
    )

    destination_driver_summary = get_destination_driver_summary(destination_reject_summary)
    destination_transit_summary_text = destination_driver_summary["text"]
    destination_transit_summary_color = destination_driver_summary["color"]

    transit1, transit2, transit3, transit4, transit5 = st.columns(5)

    with transit1:
        render_kpi_card(
            "Total Transit Items",
            f"{total_transit_items:,}",
            f"{total_transit_pct:.2f}% of all checkins",
            "#6b7280"
        )

    with transit2:
        render_kpi_card(
            "Transit to Westside",
            f"{westside_count:,}",
            f"{westside_pct:.2f}% of all checkins",
            "#6b7280"
        )

    with transit3:
        render_kpi_card(
            "Transit to Library Express",
            f"{library_express_count:,}",
            f"{library_express_pct:.2f}% of all checkins",
            "#6b7280"
        )

    with transit4:
        render_kpi_card(
            "Routing Failures",
            f"{no_agency_dest_count:,}",
            "Missing destination routing",
            "#6b7280"
        )

    with transit5:
        render_kpi_card(
            "Peak Avg Transit Day",
            peak_transit_day_label,
            peak_transit_day_subtitle,
            "#6b7280"
        )

    transit_pattern_title = "Transit Volume vs Reject Pattern"

    if transit_reject_insight_title == "No Clear Relationship":
        transit_pattern_text = (
            "High-transit days do not line up with high-reject days in the selected range. "
            "This suggests rejects are not mainly being driven by transit volume alone."
        )
        transit_pattern_color = "#6b7280"
    elif transit_reject_insight_title == "Strong Correlation":
        transit_pattern_text = (
            "High-transit days and high-reject days line up in the selected range. "
            "This suggests transit load may be contributing to failures."
        )
        transit_pattern_color = "#d97706"
    else:
        transit_pattern_text = transit_reject_insight_text
        transit_pattern_color = transit_reject_insight_color

    st.markdown(
        f"""
        <div style="
            border-left: 4px solid {transit_pattern_color};
            background-color: #f9fafb;
            padding: 14px 16px;
            border-radius: 8px;
            margin-top: 18px;
            margin-bottom: 4px;
        ">
            <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                {transit_pattern_title}
            </div>
            <div style="color: #4b5563; line-height: 1.4;">
                {transit_pattern_text}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    st.divider()

    st.subheader("Transit Reports")
    st.caption("Additional transit reports organized by data type.")

    with st.expander("Volume & Activity", expanded=False):
        st.subheader("Daily Transfer Summary")
        if len(base_df) > 0:
            daily_total = base_df.groupby(base_df["datetime"].dt.date).size()
            daily_ws = base_df[base_df["transit_destination"] == "Westside"].groupby(
                base_df[base_df["transit_destination"] == "Westside"]["datetime"].dt.date
            ).size()
            daily_le = base_df[base_df["transit_destination"] == "Library Express"].groupby(
                base_df[base_df["transit_destination"] == "Library Express"]["datetime"].dt.date
            ).size()
            daily_no_agency = base_df[
                base_df["destination"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
            ].groupby(
                base_df[
                    base_df["destination"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
                ]["datetime"].dt.date
            ).size()

            daily_transfer_summary = pd.DataFrame({
                "Date": pd.to_datetime(daily_total.index),
                "Total Checkins": daily_total.values
            })

            daily_transfer_summary["Transit to Westside"] = (
                daily_transfer_summary["Date"].dt.date.map(daily_ws).fillna(0).astype(int)
            )
            daily_transfer_summary["Transit to Library Express"] = (
                daily_transfer_summary["Date"].dt.date.map(daily_le).fillna(0).astype(int)
            )
            daily_transfer_summary["To No Agency Destination"] = (
                daily_transfer_summary["Date"].dt.date.map(daily_no_agency).fillna(0).astype(int)
            )
            daily_transfer_summary["Total Transit Items"] = (
                daily_transfer_summary["Transit to Westside"] +
                daily_transfer_summary["Transit to Library Express"]
            )
            daily_transfer_summary["Transit % of Total"] = (
                daily_transfer_summary["Total Transit Items"] / daily_transfer_summary["Total Checkins"] * 100
            ).round(2)

            daily_transfer_summary["Date"] = daily_transfer_summary["Date"].dt.strftime("%Y-%m-%d")

            st.dataframe(daily_transfer_summary, use_container_width=True)
            download_button(
                daily_transfer_summary,
                "daily_transfer_summary_report.csv",
                key="transit_reports_volume_activity_daily_transfer_summary_download"
            )
        else:
            st.info("No daily transfer summary data available for the selected date range.")
            
        st.subheader("Transit By Hour")
        st.caption("Shows how item transfers are distributed throughout the day to reveal peak routing times.")

        if len(transit_df) > 0:
            if transit_mode == "Today":
                transit_hourly = transit_df["datetime"].dt.hour.value_counts().sort_index().reset_index()
                transit_hourly.columns = ["hour", "transit_items"]
                transit_hourly["hour_label"] = transit_hourly["hour"].apply(format_hour_plain)
                transit_hourly = transit_hourly[
                    (transit_hourly["hour"] >= 7) & (transit_hourly["hour"] <= 20)
                ].copy()

                transit_hourly_chart = build_hourly_bar_chart(
                    transit_hourly,
                    "transit_items",
                    "Transit Items"
                )
                render_chart(transit_hourly_chart)

                transit_hourly_display = transit_hourly.rename(columns={
                    "hour_label": "Hour",
                    "transit_items": "Transit Items"
                })[["Hour", "Transit Items"]]

            else:
                transit_hourly_source = transit_df.copy()
                transit_hourly_source["date"] = transit_hourly_source["datetime"].dt.date
                transit_hourly_source["hour"] = transit_hourly_source["datetime"].dt.hour

                transit_hour_totals = (
                    transit_hourly_source.groupby("hour")
                    .size()
                    .reset_index(name="Total Transit Items")
                )

                transit_hour_daily = (
                    transit_hourly_source.groupby(["date", "hour"])
                    .size()
                    .reset_index(name="daily_transit_items")
                )

                transit_hour_avg = (
                    transit_hour_daily.groupby("hour")["daily_transit_items"]
                    .mean()
                    .reset_index(name="Avg Transit Items Per Day")
                )

                transit_hourly = transit_hour_totals.merge(
                    transit_hour_avg,
                    on="hour",
                    how="left"
                )

                transit_hourly["hour_label"] = transit_hourly["hour"].apply(format_hour_plain)
                transit_hourly = transit_hourly[
                    (transit_hourly["hour"] >= 7) & (transit_hourly["hour"] <= 20)
                ].copy()

                transit_hourly_chart = build_hourly_bar_chart(
                    transit_hourly.rename(columns={"Avg Transit Items Per Day": "avg_transit_items"}),
                    "avg_transit_items",
                    "Avg Transit Items Per Hour"
                )
                render_chart(transit_hourly_chart)

                transit_hourly_display = transit_hourly.rename(columns={
                    "hour_label": "Hour"
                })[["Hour", "Total Transit Items", "Avg Transit Items Per Day"]]

                transit_hourly_display["Avg Transit Items Per Day"] = (
                    transit_hourly_display["Avg Transit Items Per Day"].round(1)
                )

            st.dataframe(transit_hourly_display, use_container_width=True)
            download_button(
                transit_hourly_display,
                "transit_by_hour_report.csv",
                key="transit_by_hour_report_download"
            )
        else:
            st.info("No hourly transit data available for the selected date range.")

        st.subheader("Transit Trends Over Time")
        st.caption("Tells how transits to different branches change over time. Helps identify patterns in routing.")
        if len(transit_df) > 0:
            transit_mix = (
                transit_df.groupby([transit_df["datetime"].dt.date, "transit_destination"])
                .size()
                .reset_index(name="transit_items")
            )
            transit_mix.columns = ["date", "transit_destination", "transit_items"]
            transit_mix["date"] = pd.to_datetime(transit_mix["date"])

            transit_mix_chart = build_date_line_chart(
                transit_mix,
                "date",
                "transit_items",
                "Transit Items",
                series_col="transit_destination"
            )
            render_chart(transit_mix_chart)

            transit_mix_display = transit_mix.copy()
            transit_mix_display["date"] = pd.to_datetime(transit_mix_display["date"]).dt.strftime("%Y-%m-%d")
            transit_mix_display = transit_mix_display.rename(columns={
                "date": "Date",
                "transit_destination": "Destination",
                "transit_items": "Transit Items"
            })

            st.dataframe(transit_mix_display, use_container_width=True)
            download_button(
                transit_mix_display,
                "transit_trends_over_time_report.csv",
                key="transit_reports_volume_activity_transit_trends_over_time_download"
            )
        else:
            st.info("No transit trends data available for the selected date range.")

    with st.expander("Distribution & Flow", expanded=False):
        st.subheader("Routing Distribution")
        st.caption("Displays how items are proportionally distributed across all destinations.")
        if len(transit_summary) > 0:
            routing_distribution = transit_summary.copy()
            routing_distribution["pct_of_total_items"] = routing_distribution["pct_of_total_items"].round(2)

            routing_distribution_chart = build_category_bar_chart(
                routing_distribution.rename(columns={"pct_of_total_items": "routing_pct"}),
                "destination",
                "routing_pct",
                "% of Total Items",
                "Destination"
            )
            render_chart(routing_distribution_chart)

            routing_distribution_display = routing_distribution.rename(columns={
                "destination": "Destination",
                "transit_items": "Transit Items",
                "pct_of_total_items": "% of Total Items"
            })

            st.dataframe(routing_distribution_display, use_container_width=True)
            download_button(
                routing_distribution_display,
                "routing_distribution_report.csv",
                key="transit_reports_distribution_flow_routing_distribution_download"
            )
        else:
            st.info("No routing distribution data available for the selected date range.")

        st.subheader("Percentage Routing Over Time")
        st.caption("Gives the percentage of total items sent to each location over time.")
        if len(base_df) > 0 and len(transit_df) > 0:
            daily_total = base_df.groupby(base_df["datetime"].dt.date).size().rename("total_checkins")
            daily_routing = (
                transit_df.groupby([transit_df["datetime"].dt.date, "transit_destination"])
                .size()
                .reset_index(name="transit_items")
            )
            daily_routing.columns = ["date", "destination", "transit_items"]
            daily_routing["date"] = pd.to_datetime(daily_routing["date"])

            daily_routing["total_checkins"] = daily_routing["date"].dt.date.map(daily_total)
            daily_routing["routing_pct"] = (
                daily_routing["transit_items"] / daily_routing["total_checkins"] * 100
            ).round(2)

            routing_pct_chart = build_date_line_chart(
                daily_routing,
                "date",
                "routing_pct",
                "% of Total Items",
                series_col="destination"
            )
            render_chart(routing_pct_chart)

            routing_pct_display = daily_routing.copy()
            routing_pct_display["date"] = routing_pct_display["date"].dt.strftime("%Y-%m-%d")
            routing_pct_display = routing_pct_display.rename(columns={
                "date": "Date",
                "destination": "Destination",
                "transit_items": "Transit Items",
                "total_checkins": "Total Checkins",
                "routing_pct": "Routing %"
            })

            st.dataframe(routing_pct_display, use_container_width=True)
            download_button(
                routing_pct_display,
                "percentage_routing_over_time_report.csv",
                key="transit_reports_distribution_flow_percentage_routing_over_time_download"
            )
        else:
            st.info("No percentage routing data available for the selected date range.")

    with st.expander("Exceptions & Failures", expanded=False):
        st.subheader("Exception Report")
        if len(destination_reject_summary) > 0:
            diagnostics_display = destination_reject_summary.copy()
            diagnostics_display = diagnostics_display.rename(columns={
                "destination": "Destination",
                "transit_items": "Transit Items",
                "pct_of_total_items": "% of Total Items",
                "reject_count": "Transit-Linked Rejects",
                "reject_rate_pct": "Reject Rate %",
                "top_reject_reason": "Top Reject Reason",
                "reason_count": "Top Reason Count",
                "top_reason_pct_of_destination_rejects": "Top Reason % of Destination Rejects"
            })
            st.dataframe(diagnostics_display, use_container_width=True)
            download_button(
                diagnostics_display,
                "exception_report.csv",
                key="transit_reports_exceptions_failures_exception_report_download"
            )
        else:
            st.info("No destination-level transit reject data available for the selected date range.")

        st.subheader('"No Agency Destination" Deep Dive')
        st.caption("Looks at items that failed routing to highlight system or configuration issues.")
        no_agency_df = base_df[
            base_df["destination"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
        ].copy()

        if len(no_agency_df) > 0:
            no_agency_total = len(no_agency_df)

            no_agency_daily = (
                no_agency_df["datetime"]
                .dt.date
                .value_counts()
                .sort_index()
                .reset_index()
            )
            no_agency_daily.columns = ["date", "count"]
            no_agency_daily["date"] = pd.to_datetime(no_agency_daily["date"])

            st.subheader("No Agency Destination Items by Day")

            no_agency_daily_chart = build_date_line_chart(
                no_agency_daily,
                "date",
                "count",
                "No Agency Destination Items"
            )
            render_chart(no_agency_daily_chart)

            no_agency_hourly = (
                no_agency_df["datetime"]
                .dt.hour
                .value_counts()
                .sort_index()
                .reset_index()
            )
            no_agency_hourly.columns = ["hour", "count"]
            no_agency_hourly["hour_label"] = no_agency_hourly["hour"].apply(format_hour_plain)
            no_agency_hourly = no_agency_hourly[
                (no_agency_hourly["hour"] >= 7) & (no_agency_hourly["hour"] <= 20)
            ].copy()

            if len(no_agency_hourly) > 0:
                st.subheader("No Agency Destination Items by Hour")

                no_agency_hourly_chart = build_hourly_bar_chart(
                    no_agency_hourly,
                    "count",
                    "No Agency Destination Items"
                )
                render_chart(no_agency_hourly_chart)

            no_agency_display = no_agency_df[["datetime", "title", "barcode", "destination"]].copy()
            no_agency_display["datetime"] = pd.to_datetime(no_agency_display["datetime"]).dt.strftime("%Y-%m-%d %I:%M %p")
            no_agency_display = no_agency_display.rename(columns={
                "datetime": "Datetime",
                "title": "Title",
                "barcode": "Barcode",
                "destination": "Destination"
            })

            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #d97706;
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 8px;
                    margin-bottom: 16px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        Report Summary
                    </div>
                    <div style="color: #4b5563; line-height: 1.4;">
                        {no_agency_total:,} items were routed to No Agency Destination in the selected date range.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            st.dataframe(no_agency_display, use_container_width=True)
            download_button(
                no_agency_display,
                "no_agency_destination_deep_dive_report.csv",
                key="transit_reports_exceptions_failures_no_agency_deep_dive_download"
            )
        else:
            st.info("No No Agency Destination items found for the selected date range.")

    with st.expander("Diagnostics & Insights", expanded=False):
        st.subheader("Baseline Comparison")
        st.caption("Compares recent data against historical data to detect anomalous activity.")

        historical_df = df_history_raw[df_history_raw["datetime"].dt.date < today].copy()

        if len(base_df) > 0 and len(historical_df) > 0:
            current_total_transit = len(transit_df)
            current_total_items = len(base_df)
            current_ws_pct = westside_pct
            current_le_pct = library_express_pct

            current_days = max(1, base_df["datetime"].dt.date.nunique())

            historical_df["transit_destination"] = historical_df["destination"].apply(normalize_transit_destination)
            historical_transit_df = historical_df[
                historical_df["transit_destination"].isin(valid_transit_destinations)
            ].copy()

            historical_total_transit = len(historical_transit_df)
            historical_total_items = len(historical_df)
            historical_days = max(1, historical_df["datetime"].dt.date.nunique())

            current_avg_daily_transit = current_total_transit / current_days
            historical_avg_daily_transit = historical_total_transit / historical_days

            historical_ws_pct = (
                len(historical_df[historical_df["transit_destination"] == "Westside"]) / historical_total_items * 100
            ) if historical_total_items > 0 else 0

            historical_le_pct = (
                len(historical_df[historical_df["transit_destination"] == "Library Express"]) / historical_total_items * 100
            ) if historical_total_items > 0 else 0

            baseline_df = pd.DataFrame({
                "Metric": [
                    "Avg Daily Transit Items",
                    "Westside %",
                    "Library Express %"
                ],
                "Current": [
                    round(current_avg_daily_transit, 1),
                    round(current_ws_pct, 2),
                    round(current_le_pct, 2)
                ],
                "Historical Baseline": [
                    round(historical_avg_daily_transit, 1),
                    round(historical_ws_pct, 2),
                    round(historical_le_pct, 2)
                ]
            })

            st.dataframe(baseline_df, use_container_width=True)
            download_button(
                baseline_df,
                "transit_baseline_comparison_report.csv",
                key="transit_reports_diagnostics_baseline_comparison_download"
            )
        else:
            st.info("Not enough data available for baseline comparison.")

        st.subheader("Destination Diagnostics")
        st.caption("Shows which transit destinations are driving the most volume, rejects, and failure patterns.")

        if len(destination_reject_summary) > 0:
            diagnostics_df = destination_reject_summary.copy()

            diagnostics_df["transit_items"] = diagnostics_df["transit_items"].fillna(0).astype(int)
            diagnostics_df["pct_of_total_items"] = diagnostics_df["pct_of_total_items"].fillna(0).round(2)
            diagnostics_df["reject_count"] = diagnostics_df["reject_count"].fillna(0).astype(int)
            diagnostics_df["reject_rate_pct"] = diagnostics_df["reject_rate_pct"].fillna(0).round(2)
            diagnostics_df["reason_count"] = diagnostics_df["reason_count"].fillna(0).astype(int)
            diagnostics_df["top_reason_pct_of_destination_rejects"] = (
                diagnostics_df["top_reason_pct_of_destination_rejects"].fillna(0).round(2)
            )

            diagnostics_display = diagnostics_df.rename(columns={
                "destination": "Destination",
                "transit_items": "Transit Items",
                "pct_of_total_items": "% of Total Items",
                "reject_count": "Transit-Linked Rejects",
                "reject_rate_pct": "Reject Rate %",
                "top_reject_reason": "Top Reject Reason",
                "reason_count": "Top Reason Count",
                "top_reason_pct_of_destination_rejects": "Top Reason % of Destination Rejects"
            })

            if len(diagnostics_df) > 0:
                top_problem_row = diagnostics_df.sort_values(
                    ["reject_count", "reject_rate_pct", "transit_items"],
                    ascending=False
                ).iloc[0]

                st.markdown(
                    f"""
                    <div style="
                        border-left: 4px solid #d97706;
                        background-color: #f9fafb;
                        padding: 14px 16px;
                        border-radius: 8px;
                        margin-top: 8px;
                        margin-bottom: 16px;
                    ">
                        <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                            Report Summary
                        </div>
                        <div style="color: #4b5563; line-height: 1.4;">
                            {top_problem_row['destination']} currently shows the strongest operational impact:
                            {int(top_problem_row['transit_items']):,} transit items,
                            {int(top_problem_row['reject_count']):,} transit-linked rejects,
                            and a {float(top_problem_row['reject_rate_pct']):.2f}% reject rate.
                            Top issue: {top_problem_row['top_reject_reason']}.
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

            st.dataframe(diagnostics_display, use_container_width=True)
            download_button(
                diagnostics_display,
                "destination_diagnostics_report.csv",
                key="transit_reports_diagnostics_destination_diagnostics_download"
            )
        else:
            st.info("No destination diagnostics available for the selected date range.")
