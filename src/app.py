# app.py
# Streamlit dashboard for AMH analytics
# Displays item flow, routing, rejects, and transit diagnostics in a web interface

import streamlit as st
import pandas as pd
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import altair as alt
st.set_page_config(layout="wide")
from streamlit_autorefresh import st_autorefresh
from streamlit_autorefresh import st_autorefresh
st_autorefresh(interval=30000, key="amh_auto_refresh")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Luckiest+Guy&display=swap');

.sortview-title {
    font-family: 'Luckiest Guy', cursive;
    font-size: 48px;
    color: #111;
    letter-spacing: 1px;
}
</style>
""", unsafe_allow_html=True)


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
    font-size: 48px;
    font-weight: 800;
    letter-spacing: 2px;
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
    value_wrap=False
):
    value_white_space = "normal" if value_wrap else "nowrap"
    value_word_break = "break-word" if value_wrap else "normal"

    subtitle_html = f"""
        <div style="
            font-size: 0.9rem;
            color: {subtitle_color};
            margin-top: 8px;
            line-height: 1.3;
            overflow: hidden;
        ">
            {subtitle}
        </div>
    """ if subtitle else ""

    st.markdown(
        f"""
        <div style="
            border: 2px solid {border_color};
            border-radius: 12px;
            padding: 16px 18px;
            background-color: white;
            min-height: 185px;
            height: 185px;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            text-align: center;
        ">
            <div style="
                font-size: 0.9rem;
                color: #6b7280;
                margin-bottom: 8px;
            ">
                {title}
            </div>
            <div style="
                font-size: {value_font_size};
                font-weight: 600;
                color: {value_color};
                line-height: 1.2;
                margin-bottom: 4px;
                white-space: {value_white_space};
                word-break: {value_word_break};
            ">
                {value}
            </div>
            {subtitle_html}
        </div>
        """,
        unsafe_allow_html=True
    )



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
    chart = chart.interactive(False).configure_view(
        stroke=None
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



CHECKINS_FILE = "data/processed/checkins_clean.csv"
REJECTS_FILE = "data/processed/rejects_clean.csv"
STATUS_FILE = "data/processed/pipeline_status.json"
CHECKINS_HISTORY_FILE = "data/processed/checkins_history.csv"

checkins_updated = get_file_updated_time(CHECKINS_FILE)
rejects_updated = get_file_updated_time(REJECTS_FILE)
status_updated = get_file_updated_time(STATUS_FILE)
checkins_history_updated = get_file_updated_time(CHECKINS_HISTORY_FILE)
checkins_history_mtime = checkins_history_updated.timestamp() if checkins_history_updated else 0

checkins_mtime = checkins_updated.timestamp() if checkins_updated else 0
rejects_mtime = rejects_updated.timestamp() if rejects_updated else 0
status_mtime = status_updated.timestamp() if status_updated else 0

df_live_raw = load_checkins_df(mtime=checkins_mtime)
df_history_raw = load_checkins_history_df(mtime=checkins_history_mtime)

rejects_live_raw = load_rejects_df(mtime=rejects_mtime)
rejects_history_raw = load_rejects_history_df()

pipeline_status = load_pipeline_status(mtime=status_mtime)




rejects_live_raw["error_simple"] = rejects_live_raw["error_message"].apply(simplify_error)
rejects_history_raw["error_simple"] = rejects_history_raw["error_message"].apply(simplify_error)

min_date = df_history_raw["datetime"].min().date()
max_date = df_history_raw["datetime"].max().date()



st.caption("Hanly Analytics")
st.markdown('<div class="sortview-title">SORTVIEW</div>', unsafe_allow_html=True)
st.caption("Operational overview of AMH performance, failure patterns, and transit routing")

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
        ["Single Day", "Last 7 Days", "Last 30 Days", "Month to Date", "All Time", "Custom"],
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

    elif range_mode == "All Data":
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
    peak_failure_hour_counts = rejects_df["datetime"].dt.hour.value_counts().sort_index()
    peak_failure_hour = peak_failure_hour_counts.idxmax()
    peak_failure_count = peak_failure_hour_counts.max()
    peak_failure_pct = (peak_failure_count / len(rejects_df)) * 100
    peak_failure_window_text = format_hour(peak_failure_hour)
    peak_failure_window_subtitle = f"{peak_failure_count:,} rejects ({peak_failure_pct:.1f}% of failures)"

today = datetime.now(ZoneInfo("America/Chicago")).date()
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




if alerts:
    critical_alerts = [a for a in alerts if a["level"].lower() == "critical"]
    warning_alerts = [a for a in alerts if a["level"].lower() == "warning"]
    info_alerts = [a for a in alerts if a["level"].lower() in ["info", "trend"]]

    if critical_alerts:
        st.markdown(
            f"""
            <div style="
                border-left: 5px solid #dc2626;
                background-color: #fef2f2;
                padding: 14px 16px;
                border-radius: 8px;
                margin-bottom: 16px;
            ">
                <div style="font-weight: 600; color: #991b1b; margin-bottom: 6px;">
                    Critical Alerts
                </div>
                <ul style="margin: 0; padding-left: 18px; color: #7f1d1d;">
                    {''.join(f"<li><b>{a['level'].upper()}</b>: {a['text']}</li>" for a in critical_alerts)}
                </ul>
            </div>
            """,
            unsafe_allow_html=True
        )

    if warning_alerts:
        st.markdown(
            f"""
            <div style="
                border-left: 5px solid #d97706;
                background-color: #fffbeb;
                padding: 14px 16px;
                border-radius: 8px;
                margin-bottom: 16px;
            ">
                <div style="font-weight: 600; color: #92400e; margin-bottom: 6px;">
                    Warnings
                </div>
                <ul style="margin: 0; padding-left: 18px; color: #78350f;">
                    {''.join(f"<li><b>{a['level'].upper()}</b>: {a['text']}</li>" for a in warning_alerts)}
                </ul>
            </div>
            """,
            unsafe_allow_html=True
        )

    if info_alerts:
        st.markdown(
            f"""
            <div style="
                border-left: 5px solid #2563eb;
                background-color: #eff6ff;
                padding: 14px 16px;
                border-radius: 8px;
                margin-bottom: 16px;
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
        

    



if selected_view == "Live Today":
    top_left, top_right = st.columns([1, 1.45])

    with top_left:
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
        else:
            st.markdown(
                """
                <div style="
                    margin-top: -10px;
                    margin-bottom: 12px;
                    color: #6b7280;
                    font-size: 0.95rem;
                ">
                    Last updated time unavailable.
                </div>
                """,
                unsafe_allow_html=True
            )

        if st.button("Refresh Live Data"):
            st.cache_data.clear()
            st.success("Live data cache cleared. Reloading latest available files...")
            st.rerun()

    with top_right:
        if today_checkins == 0:
            readout_text = "No AMH activity has been logged for today yet."
        else:
            current_throughput = today_metrics["current_speed"]
            staff_hours = today_metrics["staff_hours_saved"]
            total_transit_pct = (today_total_transit / today_checkins * 100) if today_checkins > 0 else 0

            readout_text = (
                f"The AMH has processed {today_checkins:,} items with a reject rate of "
                f"{today_reject_rate:.2f}%. Current throughput is {current_throughput:,} items this hour. "
                f"Busiest hour: {format_hour(today_peak_hour)} with {today_peak_hour_count:,} checkins. "
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

    st.markdown("### Today at a Glance")

    live1, live2, live3, live4, live5, live6, live7, live8, live9, live10 = st.columns(10)

    with live1:
        render_kpi_card("Checkins", f"{today_checkins:,}", "Processed today", "#6b7280")

    with live2:
        render_kpi_card("Rejects", f"{today_rejects:,}", "Failed today", "#6b7280")

    with live3:
        reject_subtitle = "Of checkins today"
        if historical_daily_avg_reject > 0:
            reject_subtitle = (
                f"{live_reject_deviation:+.2f}% vs avg daily rate "
                f"({historical_daily_avg_reject:.2f}%)"
            )

        render_kpi_card(
            "Reject Rate",
            f"{today_reject_rate:.2f}%",
            reject_subtitle,
            live_reject_subtitle_color,
            value_font_size="1.55rem",
            border_color=live_reject_card_border,
            value_color=live_reject_value_color
        )

    with live4:
        render_kpi_card(
            "Current Throughput",
            f"{today_metrics['current_speed']}",
            "Items this hour",
            "#6b7280"
        )

    with live5:
        if today_peak_hour is not None:
            render_kpi_card(
                "Busiest Hour",
                format_hour(today_peak_hour),
                f"{today_peak_hour_count:,} items ({today_peak_hour_pct:.1f}%)",
                "#6b7280",
                value_font_size="1.4rem"
            )
        else:
            render_kpi_card("Busiest Hour", "N/A", "No activity yet", "#6b7280")

    with live6:
        staff_hours = today_metrics["staff_hours_saved"]
        render_kpi_card(
            "Staff Hours Saved Today",
            f"{staff_hours:.1f}",
            "Equivalent manual labor hours",
            "#6b7280"
        )

    with live7:
        total_transit_pct = (today_total_transit / today_checkins * 100) if today_checkins > 0 else 0
        render_kpi_card(
            "Total Transit",
            f"{today_total_transit:,}",
            f"{total_transit_pct:.1f}% of today",
            "#6b7280"
        )

    with live8:
        render_kpi_card(
            "Westside Transit",
            f"{today_westside:,}",
            f"{today_westside_pct:.1f}% of today",
            "#6b7280"
        )

    with live9:
        render_kpi_card(
            "Library Express Transit",
            f"{today_library_express:,}",
            f"{today_library_express_pct:.1f}% of today",
            "#6b7280"
        )


    with live10:
        render_kpi_card(
            "Estimated Holds",
            f"{today_estimated_holds:,}",
            "",
            "#6b7280"
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

    if len(today_hourly_checkins) > 0:
        checkins_hour_df = today_hourly_checkins.reset_index()
        checkins_hour_df.columns = ["hour", "checkins"]
        checkins_hour_df["hour_label"] = checkins_hour_df["hour"].apply(
            lambda h: pd.to_datetime(f"{int(h):02d}:00").strftime("%I%p").lstrip("0")
        )

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
            .properties(height=350)
        )

        st.altair_chart(checkins_hour_chart, use_container_width=True)
    else:
        st.info("No checkins found for today.")

    



    
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

            st.altair_chart(today_bin_bar_chart, use_container_width=True)
        else:
            st.info("No binned checkins found for today.")
    else:
        st.info("No bin column found in today's checkins data.")

    st.subheader("Bin Volume by Hour")

    if "bin" in today_df.columns:
        today_bin_df = today_df.copy()
        today_bin_df = today_bin_df[today_bin_df["bin"].notna()].copy()
        today_bin_df["bin"] = today_bin_df["bin"].astype(str)

        hour_range = list(range(7, 21))

        today_hourly_bin = (
            today_bin_df.groupby([today_bin_df["datetime"].dt.hour, "bin"])
            .size()
            .unstack(fill_value=0)
        )

        today_hourly_bin = today_hourly_bin.reindex(hour_range, fill_value=0)
        today_hourly_bin = today_hourly_bin.loc[today_hourly_bin.sum(axis=1) > 0]

        if len(today_hourly_bin) > 0:
            today_hourly_bin_chart = today_hourly_bin.copy()
            today_hourly_bin_chart.columns = [f"Bin {col}" for col in today_hourly_bin_chart.columns]

            today_hourly_bin_display = today_hourly_bin_chart.copy().reset_index()
            today_hourly_bin_display.columns = ["hour"] + list(today_hourly_bin_display.columns[1:])

            today_hourly_bin_display["hour_label"] = today_hourly_bin_display["hour"].apply(
                lambda h: pd.to_datetime(f"{int(h):02d}:00").strftime("%I%p").lstrip("0")
            )

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

            st.altair_chart(live_bin_chart, use_container_width=True)
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
    st.caption("Get a hitorical overview of your machine by choosing a date range.")
    st.markdown("---")

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

    if len(df) > 0:
        active_hours = df["datetime"].dt.hour.nunique()
        active_hours_subtitle = "Hours with recorded activity"
    else:
        active_hours = 0
        active_hours_subtitle = "No activity in selected range"

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
        attention_items.append("<b>Item Not Found</b> is leading failures. Check ILS connection and RFID tag condition.")
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

    if westside_transit_pct >= 10:
        attention_items.append("Westside transit share is high. Watch for routing or branch-related issues.")

    if not attention_items:
        attention_title = "Recommended Attention"
        attention_color = "#059669"
        attention_text = "No major issues stand out in the selected date range."
    else:
        attention_title = "Recommended Attention"
        attention_color = "#d97706"
        attention_text = " ".join(attention_items)

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

    col1, col2, col3 = st.columns(3)
    col4, col5, col6 = st.columns(3)
    col7, col8, col9 = st.columns(3)

    with col1:
        render_kpi_card("Checkins", f"{len(df):,}", date_range_text, "#6b7280")

    with col2:
        render_kpi_card("Westside Transits", f"{westside_transit_count:,}", f"{westside_transit_pct:.2f}% of total items", "#6b7280")

    with col3:
        render_kpi_card("Library Express Transits", f"{library_express_transit_count:,}", f"{library_express_transit_pct:.2f}% of total items", "#6b7280")

    with col4:
        render_kpi_card("Reject %", f"{reject_pct:.2f}%", date_range_text, "#6b7280", value_font_size="1.55rem")

    with col5:
        render_kpi_card(
            "Top Issue",
            top_issue,
            top_issue_subtitle,
            "#059669",
            value_font_size="1.15rem",
            value_wrap=True
        )

    with col6:
        render_kpi_card("Reject Count", f"{reject_count:,}", "Total failed checkins", "#6b7280")

    with col7:
        if peak_hour is not None:
            render_kpi_card(
                "Peak Hour",
                format_hour(peak_hour),
                f"{peak_hour_count:,} checkins ({peak_hour_pct:.1f}% of total volume)",
                "#6b7280"
            )
        else:
            render_kpi_card("Peak Hour", "N/A", "No activity in selected range", "#6b7280")

    with col8:
        render_kpi_card("Fail Peak Hr", peak_failure_window_text, peak_failure_window_subtitle, "#6b7280")

    with col9:
        render_kpi_card("Active Hours", f"{active_hours}", active_hours_subtitle, "#6b7280")

    st.divider()



if selected_view == "Reports":
    st.header("Reports")
    st.caption("Reports are grouped by type so staff can browse insights more naturally.")
    st.markdown("---")

    # -----------------------------
    # Volume & Capacity
    # -----------------------------
    st.subheader("Volume & Capacity")
    st.caption("How much the AMH is processing, when demand peaks, and how current volume compares to normal patterns.")

    with st.expander("Weekday & Peak Analysis", expanded=False):
        st.caption("Shows volume trends by day of week and identifies peak operating times.")

        dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        dow_counts = (
            df.groupby("day_of_week")
              .size()
              .reindex(dow_order)
              .fillna(0)
              .reset_index(name="count")
        )

        if len(dow_counts) > 0:
            busiest_day = dow_counts.sort_values("count", ascending=False).iloc[0]

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
                        Busiest day: {busiest_day['day_of_week']} with {int(busiest_day['count']):,} items processed.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            dow_chart = build_category_bar_chart(
                dow_counts,
                "day_of_week",
                "count",
                "Checkins",
                "Day of Week"
            )
            render_chart(dow_chart)

            st.dataframe(dow_counts, use_container_width=True)
            download_button(dow_counts, "weekday_volume.csv")
        else:
            st.info("No weekday data available for selected range.")

        st.subheader("Peak Hour Analysis")

        hour_counts = (
            df.groupby("hour")
              .size()
              .reset_index(name="count")
        )

        if len(hour_counts) > 0:
            busiest_hour = hour_counts.sort_values("count", ascending=False).iloc[0]

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
                        Busiest hour: {format_hour_plain(busiest_hour['hour'])} with {int(busiest_hour['count']):,} items.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            hour_counts["hour_label"] = hour_counts["hour"].apply(format_hour_plain)
            hour_counts = hour_counts[(hour_counts["hour"] >= 7) & (hour_counts["hour"] <= 20)]

            hourly_chart = build_hourly_bar_chart(hour_counts, "count", "Checkins")
            render_chart(hourly_chart)

            display_df = hour_counts[["hour_label", "count"]].rename(columns={"hour_label": "hour"})
            st.dataframe(display_df, use_container_width=True)
            download_button(display_df, "peak_hour_analysis.csv")
        else:
            st.info("No hourly data available for selected range.")

    with st.expander("Daily Volume", expanded=False):
        daily_volume = df["datetime"].dt.date.value_counts().sort_index()
        daily_df = daily_volume.reset_index()
        daily_df.columns = ["date", "count"]

        if len(daily_df) > 0:
            peak_day = daily_df.loc[daily_df["count"].idxmax()]
            avg_daily = daily_df["count"].mean()

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
                        Peak day: {pd.to_datetime(peak_day["date"]).strftime("%a, %b %d")} with {int(peak_day["count"]):,} checkins.
                        Average daily volume: {avg_daily:,.0f} checkins.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            daily_df["date"] = pd.to_datetime(daily_df["date"])
            daily_df["date_label"] = daily_df["date"].dt.strftime("%b %d")

            daily_volume_chart = (
                alt.Chart(daily_df)
                .mark_line(point=True)
                .encode(
                    x=alt.X(
                        "date_label:N",
                        sort=daily_df["date_label"].tolist(),
                        title="Date",
                        axis=alt.Axis(labelAngle=0)
                    ),
                    y=alt.Y("count:Q", title="Checkins"),
                    tooltip=["date_label", "count"]
                )
                .properties(height=350)
            )

            render_chart(daily_volume_chart)

            st.dataframe(daily_df, use_container_width=True)
            download_button(daily_df, "daily_volume_report.csv")
        else:
            st.info("No daily volume data available for the selected date range.")

    with st.expander("Hourly Volume", expanded=False):
        hourly_volume = df["datetime"].dt.hour.value_counts().sort_index()
        hourly_df = hourly_volume.reset_index()
        hourly_df.columns = ["hour", "count"]
        hourly_df["hour_label"] = hourly_df["hour"].apply(format_hour_plain)

        if len(hourly_df) > 0:
            peak_hour_row = hourly_df.loc[hourly_df["count"].idxmax()]

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
                        Peak hour: {peak_hour_row["hour_label"]} with {int(peak_hour_row["count"]):,} checkins.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            hourly_df = hourly_df[(hourly_df["hour"] >= 7) & (hourly_df["hour"] <= 20)].copy()
            hourly_chart = build_hourly_bar_chart(hourly_df, "count", "Checkins")
            render_chart(hourly_chart)

            display_df = hourly_df[["hour_label", "count"]].rename(columns={"hour_label": "hour"})
            st.dataframe(display_df, use_container_width=True)
            download_button(display_df, "hourly_volume.csv")
        else:
            st.info("No hourly volume data available for the selected date range.")

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
        today = datetime.now(ZoneInfo("America/Chicago")).date()

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
    # Labor & Efficiency
    # -----------------------------
    st.subheader("Labor & Efficiency")
    st.caption("Translates machine activity into estimated staff effort replaced by automation.")

    with st.expander("Staff Time Equivalent", expanded=False):
        st.caption("Estimates staff time saved by comparing manual processing time against observed AMH processing time.")

        MANUAL_RATE = 50

        if len(df) > 0 and len(df_history_raw) > 0:
            all_time_df = df_history_raw.copy()
            all_time_df["date"] = all_time_df["datetime"].dt.date
            all_time_df["hour"] = all_time_df["datetime"].dt.hour

            all_time_daily_hourly = (
                all_time_df.groupby(["date", "hour"])
                .size()
                .reset_index(name="checkins")
            )

            all_time_avg_hourly = (
                all_time_daily_hourly.groupby("hour")["checkins"]
                .mean()
                .reset_index(name="avg_items_per_hour")
            )

            if len(all_time_avg_hourly) > 0:
                all_time_peak_row = all_time_avg_hourly.loc[
                    all_time_avg_hourly["avg_items_per_hour"].idxmax()
                ]
                all_time_threshold = all_time_peak_row["avg_items_per_hour"] * 0.75
                all_time_peak_hours = all_time_avg_hourly[
                    all_time_avg_hourly["avg_items_per_hour"] >= all_time_threshold
                ].copy()

                AMH_RATE = (
                    all_time_peak_hours["avg_items_per_hour"].mean()
                    if len(all_time_peak_hours) > 0
                    else all_time_peak_row["avg_items_per_hour"]
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

            st.info(
    f"""How Staff Time Saved Is Calculated

This estimate compares how long it would take staff to process items manually versus how long the AMH processes the same workload.

Step 1 — Manual Processing Time

Manual rate = {MANUAL_RATE:.0f} check-ins p/hr

This dashboard uses a manual processing baseline of {MANUAL_RATE:.0f} items per hour.
That baseline comes from observed staff check-in pace from Westside circulation check-in reporting, and is used here as a reasonable manual processing benchmark.

**Manual time = check-ins ÷ Manual rate**


Step 2 — AMH Processing Time

Instead of guessing machine speed, this dashboard uses the AMH’s observed all-time busiest-hour average.

Current AMH rate used = {AMH_RATE:.1f} items per hour (based on all check-ins from 1/31/26 - 4/2/26)

**AMH time = checkins ÷ {AMH_RATE:.1f}**

Step 3 — Time Saved

**Staff time saved = (checkins ÷ {MANUAL_RATE:.0f}) − (checkins ÷ {AMH_RATE:.1f})**

Example Using the Current Selected Range

Average daily checkins: {avg_daily_checkins:,.1f}

Average daily manual time: {avg_daily_manual_hours:,.2f} hours

Average daily AMH time: {avg_daily_amh_hours:,.2f} hours

Average daily staff time saved: {avg_saved:,.2f} hours

What This Means
- Uses an observed manual staff benchmark rather than an arbitrary guess
- Uses actual AMH performance from historical data
- Compares manual processing time against AMH processing time
- Produces a more realistic estimate than treating all checkins as fully saved labor"""
)

            staff_chart_df = staff_df.copy()
            staff_chart_df["date"] = pd.to_datetime(staff_chart_df["date"])
            staff_chart_df["date_label"] = staff_chart_df["date"].dt.strftime("%b %d")

            staff_time_chart = (
                alt.Chart(staff_chart_df)
                .mark_line(point=True)
                .encode(
                    x=alt.X("date_label:N", sort=staff_chart_df["date_label"].tolist(), title="Date"),
                    y=alt.Y("hours_saved:Q", title="Staff Hours Saved"),
                    tooltip=["date_label", "hours_saved"]
                )
                .properties(height=350)
            )

            render_chart(staff_time_chart)

            display_df = staff_df.copy()
            display_df["date"] = pd.to_datetime(display_df["date"]).dt.strftime("%Y-%m-%d")
            display_df = display_df.rename(columns={
                "date": "Date",
                "checkins": "Checkins",
                "manual_hours": "Manual Hours",
                "amh_hours": "AMH Hours",
                "hours_saved": "Staff Hours Saved",
                "shifts_saved": "Staff Shifts Saved (8 hr)"
            })

            st.dataframe(display_df, use_container_width=True)
            download_button(display_df, "staff_time_equivalent.csv")
        else:
            st.info("Not enough data is available to calculate staff time savings for the selected date range.")

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

    st.markdown("---")

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

    with st.expander("Exceptions / Overflow", expanded=False):
        if "bin" not in df.columns:
            st.warning("No bin column found in the current dataset. Add bin parsing to your cleaned checkins file first.")
        else:
            exception_bin = "6"

            bin_df = df.copy()
            bin_df = bin_df[bin_df["bin"].notna()].copy()
            bin_df["bin"] = bin_df["bin"].astype(str)

            exception_df = bin_df[bin_df["bin"] == exception_bin].copy()
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
                f"Exception bin {exception_bin} handled {exception_count:,} items "
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
                    f"Bin {exception_bin}",
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
                    f"{estimated_holds_pct:.1f}% of Bin 6",
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

            if len(hourly_exception_df) > 0:
                st.subheader("Exception Bin Volume by Hour")
                hourly_exception_df = hourly_exception_df[
                    (hourly_exception_df["hour"] >= 7) & (hourly_exception_df["hour"] <= 20)
                ].copy()

                exception_chart = build_hourly_bar_chart(hourly_exception_df, "exception_items", "Exception Items")
                render_chart(exception_chart)

                hourly_exception_display = hourly_exception_df[["hour_label", "exception_items"]].rename(
                    columns={"hour_label": "hour"}
                )
                st.dataframe(hourly_exception_display, use_container_width=True)
                download_button(
                    hourly_exception_display,
                    "exception_bin_volume_by_hour_report.csv",
                    key="exception_bin_volume_by_hour_report_download"
                )
            else:
                st.info("No exception-bin items found for the selected date range.")

    st.markdown("---")

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

            k1, k2, k3 = st.columns(3)
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

            bin_volume_display = bin_summary.rename(columns={
                "bin": "Bin",
                "checkins": "Checkins",
                "pct_of_total": "% of Total"
            })

            st.dataframe(bin_volume_display, use_container_width=True)
            download_button(bin_volume_display, "bin_volume_report.csv")

            hour_range = list(range(7, 21))

            hourly_bin = (
                bin_df.groupby([bin_df["datetime"].dt.hour, "bin"])
                .size()
                .unstack(fill_value=0)
            )

            hourly_bin = hourly_bin.reindex(hour_range, fill_value=0)
            hourly_bin = hourly_bin.loc[hourly_bin.sum(axis=1) > 0]

            if len(hourly_bin) > 0:
                st.subheader("Bin Volume by Hour")

                hourly_bin_chart = hourly_bin.copy()
                hourly_bin_chart.columns = [f"Bin {col}" for col in hourly_bin_chart.columns]

                hourly_bin_chart_display = hourly_bin_chart.copy().reset_index()
                hourly_bin_chart_display.columns = ["hour"] + list(hourly_bin_chart_display.columns[1:])

                hourly_bin_chart_display["hour_label"] = hourly_bin_chart_display["hour"].apply(
                    lambda h: pd.to_datetime(f"{int(h):02d}:00").strftime("%I%p").lstrip("0")
                )

                hourly_bin_long = hourly_bin_chart_display.melt(
                    id_vars=["hour", "hour_label"],
                    var_name="bin",
                    value_name="checkins"
                )

                bin_chart = (
                    alt.Chart(hourly_bin_long)
                    .mark_line(point=False)
                    .encode(
                        x=alt.X(
                            "hour_label:N",
                            sort=hourly_bin_chart_display["hour_label"].tolist(),
                            title="Hour",
                            axis=alt.Axis(labelAngle=0)
                        ),
                        y=alt.Y("checkins:Q", title="Checkins"),
                        color=alt.Color("bin:N", title="Bin"),
                        tooltip=["hour_label", "bin", "checkins"]
                    )
                    .properties(height=350)
                )

                bin_chart = bin_chart.interactive(False)

                st.altair_chart(bin_chart, use_container_width=True)

                hourly_bin_display = hourly_bin_chart.copy().reset_index()
                hourly_bin_display.columns = ["hour"] + [str(col) for col in hourly_bin_display.columns[1:]]
                hourly_bin_display["hour"] = hourly_bin_display["hour"].apply(format_hour_plain)

                st.dataframe(hourly_bin_display, use_container_width=True)
                download_button(hourly_bin_display, "bin_volume_by_hour_report.csv")


if selected_view == "Transits":
    st.header("Transit Routing")
    st.caption("Tracks items routed to transit destinations such as Westside and Library Express.")

    transit_time_summary = get_transit_time_summary(transit_df)

    total_transit_items = len(transit_df)
    total_transit_pct = (total_transit_items / len(df) * 100) if len(df) > 0 else 0
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

    today_no_agency_dest = int(
        df["destination"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False).sum()
    )
    
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
            "To No Agency Destination",
            f"{today_no_agency_dest:,}",
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

    st.markdown(
        f"""
        <div style="
            border-left: 4px solid {transit_reject_insight_color};
            background-color: #f9fafb;
            padding: 14px 16px;
            border-radius: 8px;
            margin-top: 18px;
            margin-bottom: 8px;
        ">
            <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                {transit_reject_insight_title}
            </div>
            <div style="color: #4b5563; line-height: 1.4;">
                {transit_reject_insight_text}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    st.markdown(
        f"""
        <div style="
            border-left: 4px solid {destination_transit_summary_color};
            background-color: #f9fafb;
            padding: 14px 16px;
            border-radius: 8px;
            margin-top: 8px;
            margin-bottom: 4px;
        ">
            <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                Destination Driver Summary
            </div>
            <div style="color: #4b5563; line-height: 1.4;">
                {destination_transit_summary_text}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    st.divider()

    
    st.markdown("---")
    st.subheader("Transit Distribution")
    st.caption("Breakdown of transit volume by destination.")
    
    if len(transit_summary) > 0:
        transit_distribution_df = transit_summary.copy()
        transit_distribution_df["transit_items"] = transit_distribution_df["transit_items"].astype(int)
    
        transit_distribution_chart_df = transit_distribution_df[["destination", "transit_items"]].copy()
    
        transit_distribution_chart = build_category_bar_chart(
            transit_distribution_chart_df,
            "destination",
            "transit_items",
            "Transit Items",
            "Destination"
        )
        render_chart(transit_distribution_chart)
    
        transit_distribution_display = transit_distribution_df.rename(columns={
            "destination": "Destination",
            "transit_items": "Transit Items",
            "pct_of_total_items": "% of Total Items"
        })
    
        st.dataframe(transit_distribution_display, use_container_width=True)
        download_button(transit_distribution_display, "transit_distribution_report.csv")
    else:
        st.info("No transit distribution data available for the selected date range.")
    
    
    st.subheader("Transit by Hour")
    st.caption("Hourly transit volume from 7 AM to 8 PM.")
    
    if len(transit_df) > 0:
        transit_hourly = transit_df["datetime"].dt.hour.value_counts().sort_index().reset_index()
        transit_hourly.columns = ["hour", "transit_items"]
        transit_hourly["hour_label"] = transit_hourly["hour"].apply(format_hour_plain)
    
        transit_hourly = transit_hourly[(transit_hourly["hour"] >= 7) & (transit_hourly["hour"] <= 20)].copy()
    
        transit_hourly_chart = build_hourly_bar_chart(
            transit_hourly,
            "transit_items",
            "Transit Items"
        )
        render_chart(transit_hourly_chart)
    
        transit_hourly_display = transit_hourly[["hour_label", "transit_items"]].rename(
            columns={"hour_label": "Hour", "transit_items": "Transit Items"}
        )
    
        st.dataframe(transit_hourly_display, use_container_width=True)
        download_button(
            transit_hourly_display,
            "transit_by_hour_report.csv",
            key="transit_reports_volume_activity_transit_by_hour_download"
        )
    else:
        st.info("No hourly transit data available for the selected date range.")
    
    st.markdown("---")
    st.subheader("Transit Reports")
    st.caption("Additional transit reports organized by data type.")
    
    with st.expander("Volume & Activity", expanded=False):
        st.subheader("Daily Transfer Summary")
        if len(df) > 0:
            daily_total = df.groupby(df["datetime"].dt.date).size()
            daily_ws = df[df["transit_destination"] == "Westside"].groupby(
                df[df["transit_destination"] == "Westside"]["datetime"].dt.date
            ).size()
            daily_le = df[df["transit_destination"] == "Library Express"].groupby(
                df[df["transit_destination"] == "Library Express"]["datetime"].dt.date
            ).size()
            daily_no_agency = df[
                df["destination"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
            ].groupby(
                df[
                    df["destination"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
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
            transit_hourly = transit_df["datetime"].dt.hour.value_counts().sort_index().reset_index()
            transit_hourly.columns = ["hour", "Transit Items"]
            transit_hourly["hour_label"] = transit_hourly["hour"].apply(format_hour_plain)
            transit_hourly = transit_hourly[(transit_hourly["hour"] >= 7) & (transit_hourly["hour"] <= 20)].copy()
    
            transit_hourly_chart = build_hourly_bar_chart(
                transit_hourly.rename(columns={"Transit Items": "transit_items"}),
                "transit_items",
                "Transit Items"
            )
            render_chart(transit_hourly_chart)
    
            transit_hourly_display = transit_hourly[["hour_label", "Transit Items"]].rename(
                columns={"hour_label": "Hour"}
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
        st.subheader("Transit Distribution")
        st.caption("Visualizes total item counts sent to each destination for quick comparison.")
        if len(transit_summary) > 0:
            transit_distribution_df = transit_summary.copy()
            transit_distribution_df["transit_items"] = transit_distribution_df["transit_items"].astype(int)
    
            transit_distribution_chart_df = transit_distribution_df[["destination", "transit_items"]].copy()
            transit_distribution_chart = build_category_bar_chart(
                transit_distribution_chart_df,
                "destination",
                "transit_items",
                "Transit Items",
                "Destination"
            )
            render_chart(transit_distribution_chart)
    
            transit_distribution_display = transit_distribution_df.rename(columns={
                "destination": "Destination",
                "transit_items": "Transit Items",
                "pct_of_total_items": "% of Total Items"
            })
    
            st.dataframe(transit_distribution_display, use_container_width=True)
            download_button(
                transit_distribution_display,
                "transit_distribution_report.csv",
                key="transit_reports_distribution_flow_transit_distribution_download"
            )
        else:
            st.info("No transit distribution data available for the selected date range.")
    
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
        if len(df) > 0 and len(transit_df) > 0:
            daily_total = df.groupby(df["datetime"].dt.date).size().rename("total_checkins")
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
        no_agency_df = df[
            df["destination"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
        ].copy()
    
        if len(no_agency_df) > 0:
            no_agency_total = len(no_agency_df)
            no_agency_daily = no_agency_df["datetime"].dt.date.value_counts().sort_index().reset_index()
            no_agency_daily.columns = ["date", "count"]
            no_agency_daily["date"] = pd.to_datetime(no_agency_daily["date"])
    
            no_agency_daily_chart = build_date_line_chart(
                no_agency_daily,
                "date",
                "count",
                "No Agency Destination Items"
            )
            render_chart(no_agency_daily_chart)
    
            no_agency_hourly = no_agency_df["datetime"].dt.hour.value_counts().sort_index().reset_index()
            no_agency_hourly.columns = ["hour", "count"]
            no_agency_hourly["hour_label"] = no_agency_hourly["hour"].apply(format_hour_plain)
            no_agency_hourly = no_agency_hourly[(no_agency_hourly["hour"] >= 7) & (no_agency_hourly["hour"] <= 20)].copy()
    
            if len(no_agency_hourly) > 0:
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
    
        if len(df) > 0 and len(historical_df) > 0:
            current_total_transit = len(transit_df)
            current_ws_pct = westside_pct
            current_le_pct = library_express_pct
            current_no_agency = int(
                df["destination"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False).sum()
            )
    
            historical_transit_df = historical_df[
                historical_df["destination"].apply(normalize_transit_destination).isin(valid_transit_destinations)
            ].copy()
    
            historical_total_transit_avg = (
                historical_transit_df.groupby(historical_transit_df["datetime"].dt.date).size().mean()
                if len(historical_transit_df) > 0 else 0
            )
    
            historical_no_agency_avg = (
                historical_df[
                    historical_df["destination"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
                ].groupby(
                    historical_df[
                        historical_df["destination"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
                    ]["datetime"].dt.date
                ).size().mean()
                if len(historical_df) > 0 else 0
            )
    
            baseline_df = pd.DataFrame([{
                "Metric": "Total Transit Items",
                "Current": round(current_total_transit, 2),
                "Historical Avg": round(historical_total_transit_avg, 2),
                "Delta": round(current_total_transit - historical_total_transit_avg, 2)
            }, {
                "Metric": "Westside Routing %",
                "Current": round(current_ws_pct, 2),
                "Historical Avg": round(historical_westside_pct or 0, 2),
                "Delta": round(current_ws_pct - (historical_westside_pct or 0), 2)
            }, {
                "Metric": "Library Express Routing %",
                "Current": round(current_le_pct, 2),
                "Historical Avg": round(historical_library_express_pct or 0, 2),
                "Delta": round(current_le_pct - (historical_library_express_pct or 0), 2)
            }, {
                "Metric": "No Agency Destination",
                "Current": round(current_no_agency, 2),
                "Historical Avg": round(historical_no_agency_avg if pd.notna(historical_no_agency_avg) else 0, 2),
                "Delta": round(current_no_agency - (historical_no_agency_avg if pd.notna(historical_no_agency_avg) else 0), 2)
            }])
    
            st.dataframe(baseline_df, use_container_width=True)
            download_button(
                baseline_df,
                "baseline_comparison_report.csv",
                key="transit_reports_diagnostics_insights_baseline_comparison_download"
            )
        else:
            st.info("Not enough current and historical data available for baseline comparison.")
