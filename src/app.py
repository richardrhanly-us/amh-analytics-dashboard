# app.py
# Streamlit dashboard for AMH analytics
# Displays item flow, routing, rejects, and transit diagnostics in a web interface

import streamlit as st
import pandas as pd
from pathlib import Path
from datetime import datetime
import altair as alt
from streamlit_autorefresh import st_autorefresh

st.set_page_config(layout="wide")
st_autorefresh(interval=60000, key="amh_auto_refresh")
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

from data_loader import load_checkins_df, load_rejects_df, load_pipeline_status
from metrics import get_date_filtered_df, get_today_metrics, get_overall_metrics, get_historical_reject_baseline
from reject_logic import simplify_error
from alerts import get_system_alerts

from transit_logic import *


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
        return datetime.fromtimestamp(file_path.stat().st_mtime)
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
        

def download_button(df, filename):
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="Download CSV",
        data=csv,
        file_name=filename,
        mime="text/csv"
    )
    
 


def render_chart(chart):
    chart = chart.interactive(False).configure_view(
        stroke=None
    )
    st.altair_chart(chart, use_container_width=True)
    
    


CHECKINS_FILE = "data/processed/checkins_history.csv"
REJECTS_FILE = "data/processed/rejects_history.csv"
STATUS_FILE = "data/processed/pipeline_status.json"

checkins_updated = get_file_updated_time(CHECKINS_FILE)
rejects_updated = get_file_updated_time(REJECTS_FILE)
status_updated = get_file_updated_time(STATUS_FILE)

checkins_mtime = checkins_updated.timestamp() if checkins_updated else 0
rejects_mtime = rejects_updated.timestamp() if rejects_updated else 0
status_mtime = status_updated.timestamp() if status_updated else 0

df_raw = load_checkins_df(mtime=checkins_mtime)
rejects_raw = load_rejects_df(mtime=rejects_mtime)
pipeline_status = load_pipeline_status(mtime=status_mtime)

rejects_raw["error_simple"] = rejects_raw["error_message"].apply(simplify_error)

min_date = df_raw["datetime"].min().date()
max_date = df_raw["datetime"].max().date()

st.caption("Hanly Analytics")
st.markdown('<div class="sortview-title">SortView</div>', unsafe_allow_html=True)
st.caption("Operational overview of AMH performance, failure patterns, and transit routing")

if pipeline_status:
    last_run = pipeline_status.get("last_run", "Unknown")
    checkins_rows = pipeline_status.get("checkins_rows", 0)
    rejects_rows = pipeline_status.get("rejects_rows", 0)
    transit_items = pipeline_status.get("transit_items", 0)

    st.info(
        f"Last pipeline refresh: {last_run} | "
        f"Checkins: {checkins_rows:,} | "
        f"Rejects: {rejects_rows:,} | "
        f"Transit items: {transit_items:,}"
    )

selected_view = st.segmented_control(
    "Section",
    options=["Live Today", "Overview", "Reports", "Transits"],
    default="Live Today",
    label_visibility="collapsed"
)

start_date = min_date
end_date = max_date




if selected_view in ["Overview", "Reports", "Transits"]:
    st.sidebar.header("Filters")

    date_selection = st.sidebar.date_input(
        "Select Date Range",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date
    )

    if isinstance(date_selection, (list, tuple)):
        if len(date_selection) == 2:
            start_date, end_date = date_selection
        elif len(date_selection) == 1:
            start_date = end_date = date_selection[0]
        else:
            start_date = min_date
            end_date = max_date
    else:
        start_date = end_date = date_selection

df = get_date_filtered_df(df_raw, start_date, end_date)
rejects_df = get_date_filtered_df(rejects_raw, start_date, end_date)

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

today = datetime.now().date()

today_metrics = get_today_metrics(df_raw, rejects_raw, today)

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


historical_checkins_df = df_raw[df_raw["datetime"].dt.date < today].copy()

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

historical_baseline = get_historical_reject_baseline(df_raw, rejects_raw, today)

historical_daily_avg_reject = historical_baseline.get("historical_daily_avg_reject")

if historical_daily_avg_reject is None or historical_daily_avg_reject == 0:
    # fallback: compute manually from historical data
    historical_df = df_raw[df_raw["datetime"].dt.date < today]

    if len(historical_df) > 0:
        daily_checkins = historical_df["datetime"].dt.date.value_counts()
        daily_rejects = rejects_raw[rejects_raw["datetime"].dt.date < today]["datetime"].dt.date.value_counts()

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
    alert_has_critical = any(a["level"] == "critical" for a in alerts)
    alert_has_warning = any(a["level"] == "warning" for a in alerts)

    if alert_has_critical:
        alert_border = "#dc2626"
        alert_bg = "#fef2f2"
        alert_title_color = "#991b1b"
        alert_text_color = "#7f1d1d"
    elif alert_has_warning:
        alert_border = "#d97706"
        alert_bg = "#fffbeb"
        alert_title_color = "#92400e"
        alert_text_color = "#78350f"
    else:
        alert_border = "#2563eb"
        alert_bg = "#eff6ff"
        alert_title_color = "#1d4ed8"
        alert_text_color = "#1e3a8a"

    st.markdown(
        f"""
        <div style="
            border-left: 5px solid {alert_border};
            background-color: {alert_bg};
            padding: 14px 16px;
            border-radius: 8px;
            margin-bottom: 16px;
        ">
            <div style="font-weight: 600; color: {alert_title_color}; margin-bottom: 6px;">
                System Alerts
            </div>
            <ul style="margin: 0; padding-left: 18px; color: {alert_text_color};">
                {''.join(f"<li><b>{a['level'].upper()}</b>: {a['text']}</li>" for a in alerts)}
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

        st.markdown(
            f"""
            <div style="
                border-left: 4px solid #2563eb;
                background-color: #f9fafb;
                padding: 14px 16px;
                border-radius: 8px;
                margin-top: 0px;
                margin-bottom: 10px;
            ">
                <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                    Operational Readout
                </div>
                <div style="color: #4b5563; line-height: 1.4;">
                    {readout_text}
                </div>
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

    st.markdown("### Today at a Glance")

    live1, live2, live3, live4, live5, live6, live7, live8, live9 = st.columns(9)

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

    st.subheader("Worst Days (Top 5 by Reject Rate)")
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

        worst_table_display = worst_table.copy()
        worst_table_display["reject_rate"] = worst_table_display["reject_rate"].round(2)
        worst_table_display = worst_table_display[["day_of_week", "checkins", "rejects", "reject_rate"]]
        st.dataframe(worst_table_display, use_container_width=True)
    else:
        st.info("No worst-day data available for the selected date range.")


if selected_view == "Reports":
    st.header("Reports")
    st.caption("Reports are grouped by type so staff can browse insights more naturally.")
    st.markdown("---")

    # -----------------------------
    # Volume & Capacity
    # -----------------------------
    st.subheader("Volume & Capacity")
    st.caption("How much the AMH is processing, when demand peaks, and how current volume compares to normal patterns.")

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

            chart_df = daily_df.set_index("date")["count"]
            st.line_chart(chart_df)

            st.dataframe(daily_df, use_container_width=True)
            download_button(daily_df, "daily_volume.csv")
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

            chart_df = hourly_df.set_index("hour_label")["count"]
            st.bar_chart(chart_df)

            display_df = hourly_df[["hour_label", "count"]].rename(columns={"hour_label": "hour"})
            st.dataframe(display_df, use_container_width=True)
            download_button(display_df, "hourly_volume.csv")
        else:
            st.info("No hourly volume data available for the selected date range.")

    with st.expander("Throughput", expanded=False):
        st.caption("Shows how quickly the AMH processes items by hour, helping quantify peak handling capacity and operational demand.")

        hourly_volume = df["datetime"].dt.hour.value_counts().sort_index()
        hourly_df = hourly_volume.reset_index()
        hourly_df.columns = ["hour", "items_per_hour"]
        hourly_df["hour_label"] = hourly_df["hour"].apply(format_hour_plain)

        if len(hourly_df) > 0:
            peak_row = hourly_df.loc[hourly_df["items_per_hour"].idxmax()]
            avg_throughput = hourly_df["items_per_hour"].mean()

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
                        Peak throughput: {int(peak_row["items_per_hour"]):,} items/hour at {peak_row["hour_label"]}.
                        Average throughput across active hours: {avg_throughput:,.1f} items/hour.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            k1, k2, k3 = st.columns(3)

            with k1:
                render_kpi_card(
                    "Peak Throughput",
                    f"{int(peak_row['items_per_hour']):,}",
                    f"At {peak_row['hour_label']}",
                    "#6b7280",
                    value_font_size="1.6rem"
                )

            with k2:
                render_kpi_card(
                    "Avg Throughput",
                    f"{avg_throughput:,.1f}",
                    "Items per active hour",
                    "#6b7280",
                    value_font_size="1.6rem"
                )

            with k3:
                active_hours_count = len(hourly_df)
                render_kpi_card(
                    "Active Hours",
                    f"{active_hours_count}",
                    "Hours with recorded activity",
                    "#6b7280",
                    value_font_size="1.6rem"
                )

            chart_df = hourly_df.set_index("hour_label")["items_per_hour"]
            st.bar_chart(chart_df)

            display_df = hourly_df.rename(columns={
                "hour_label": "Hour",
                "items_per_hour": "Items Per Hour"
            })[["Hour", "Items Per Hour"]]

            st.dataframe(display_df, use_container_width=True)
            download_button(display_df, "throughput_report.csv")
        else:
            st.info("No throughput data available for the selected date range.")

    with st.expander("Today vs Typical Hourly Pattern", expanded=False):
        today = df_raw["datetime"].dt.date.max()

        today_df_report = df_raw[df_raw["datetime"].dt.date == today].copy()
        historical_df_report = df_raw[df_raw["datetime"].dt.date < today].copy()

        today_hourly = today_df_report["datetime"].dt.hour.value_counts().sort_index()

        if len(historical_df_report) > 0 and historical_df_report["datetime"].dt.date.nunique() > 0:
            typical_hourly = (
                historical_df_report.groupby(historical_df_report["datetime"].dt.hour).size()
                / historical_df_report["datetime"].dt.date.nunique()
            )
        else:
            typical_hourly = pd.Series(dtype=float)

        all_hours = sorted(set(today_hourly.index).union(set(typical_hourly.index)))

        compare_df = pd.DataFrame({
            "hour": all_hours
        })
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

            chart_df = compare_df.set_index("hour_label")[["today", "typical"]]
            st.line_chart(chart_df)

            display_df = compare_df[["hour_label", "today", "typical", "delta"]].rename(
                columns={"hour_label": "hour"}
            )
            st.dataframe(display_df, use_container_width=True)
            download_button(display_df, "today_vs_typical_hourly_pattern.csv")
        else:
            st.info("Not enough data available to compare today versus the typical hourly pattern.")

    st.markdown("---")

    # -----------------------------
    # Labor & Efficiency
    # -----------------------------
    st.subheader("Labor & Efficiency")
    st.caption("Translates machine activity into estimated staff effort replaced by automation.")

    with st.expander("Staff Time Equivalent", expanded=False):
        st.caption("This report shows how many staff hours the AMH is saving by handling check-ins automatically. It helps put the machine’s impact into simple terms—how much manual work it replaces each day.")

        MANUAL_RATE = 120  # items per hour

        daily_volume = df["datetime"].dt.date.value_counts().sort_index()
        staff_df = daily_volume.reset_index()
        staff_df.columns = ["date", "checkins"]
        staff_df["staff_hours_saved"] = (staff_df["checkins"] / MANUAL_RATE).round(2)
        staff_df["staff_shifts_saved"] = (staff_df["staff_hours_saved"] / 8).round(2)

        if len(staff_df) > 0:
            peak_hours_row = staff_df.loc[staff_df["staff_hours_saved"].idxmax()]
            avg_hours_saved = staff_df["staff_hours_saved"].mean()

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
                        Peak labor equivalent day: {pd.to_datetime(peak_hours_row["date"]).strftime("%a, %b %d")}
                        with {peak_hours_row["staff_hours_saved"]:.2f} staff hours saved.
                        Average daily labor equivalent saved: {avg_hours_saved:.2f} hours.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            k1, k2, k3 = st.columns(3)

            with k1:
                render_kpi_card(
                    "Avg Staff Hours Saved",
                    f"{avg_hours_saved:.2f}",
                    "Average daily manual labor equivalent",
                    "#6b7280",
                    value_font_size="1.6rem"
                )

            with k2:
                render_kpi_card(
                    "Peak Staff Hours Saved",
                    f"{peak_hours_row['staff_hours_saved']:.2f}",
                    pd.to_datetime(peak_hours_row["date"]).strftime("%b %d, %Y"),
                    "#6b7280",
                    value_font_size="1.6rem"
                )

            with k3:
                render_kpi_card(
                    "Peak Staff Shifts Saved",
                    f"{peak_hours_row['staff_shifts_saved']:.2f}",
                    "Based on 8-hour shifts",
                    "#6b7280",
                    value_font_size="1.6rem"
                )

            chart_df = staff_df.set_index("date")["staff_hours_saved"]
            st.line_chart(chart_df)

            display_df = staff_df.rename(columns={
                "date": "Date",
                "checkins": "Checkins",
                "staff_hours_saved": "Staff Hours Saved",
                "staff_shifts_saved": "Staff Shifts Saved (8 hr)"
            })

            st.dataframe(display_df, use_container_width=True)
            download_button(display_df, "staff_time_equivalent.csv")
        else:
            st.info("No data available for the selected date range.")

    st.markdown("---")

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

            chart_df = destination_counts.set_index("destination")["count"]
            st.bar_chart(chart_df)

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

            chart_df = reject_counts.set_index("reason")["count"]
            st.bar_chart(chart_df)

            st.dataframe(reject_counts, use_container_width=True)
            download_button(reject_counts, "reject_reasons.csv")
        else:
            st.info("No reject reason data available for the selected date range.")

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

            daily_exception = (
                exception_df["datetime"].dt.date.value_counts().sort_index()
            )
            daily_total = (
                bin_df["datetime"].dt.date.value_counts().sort_index()
            )

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

            hourly_exception = (
                exception_df["datetime"].dt.hour.value_counts().sort_index()
            )
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

            k1, k2, k3 = st.columns(3)
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

            if len(overflow_daily) > 0:
                st.subheader("Exception Bin Rate by Day")
                chart_df = overflow_daily["exception_rate_pct"]
                st.line_chart(chart_df)

                overflow_daily_display = overflow_daily.reset_index().rename(columns={"index": "date"})
                st.dataframe(overflow_daily_display, use_container_width=True)
                download_button(overflow_daily_display, "exception_bin_daily_report.csv")

            if len(hourly_exception_df) > 0:
                st.subheader("Exception Bin Volume by Hour")
                chart_df = hourly_exception_df.set_index("hour_label")["exception_items"]
                st.bar_chart(chart_df)

                hourly_exception_display = hourly_exception_df[["hour_label", "exception_items"]].rename(
                    columns={"hour_label": "hour"}
                )
                st.dataframe(hourly_exception_display, use_container_width=True)
                download_button(hourly_exception_display, "exception_bin_hourly_report.csv")
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

            chart_df = bin_summary.set_index("bin")["checkins"]
            st.bar_chart(chart_df)

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

    transit1, transit2, transit3, transit4 = st.columns(4)

    with transit1:
        render_kpi_card("Total Transit Items", f"{total_transit_items:,}", f"{total_transit_pct:.2f}% of all checkins", "#6b7280")

    with transit2:
        render_kpi_card("Transit Destinations", f"{transit_destination_count}", "Unique routed destinations in range", "#6b7280")

    with transit3:
        render_kpi_card("Top Destination", top_transit_destination, top_transit_subtitle, "#6b7280")

    with transit4:
        render_kpi_card("Peak Avg Transit Day", peak_transit_day_label, peak_transit_day_subtitle, "#6b7280")

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

    st.subheader("Average Transit Time")

    if len(transit_time_summary) > 0:
        display_df = transit_time_summary[["destination", "avg_minutes"]].rename(columns={
            "destination": "Destination",
            "avg_minutes": "Avg Transit Time (min)"
        })
        st.dataframe(display_df, use_container_width=True)
    else:
        st.info("Not enough data to calculate transit times.")

    st.subheader("Transit by Destination")
    if len(transit_summary) > 0:
        transit_display = transit_summary.copy()
        transit_display["transit_items"] = transit_display["transit_items"].astype(int)
        transit_display = transit_display.rename(columns={
            "destination": "Destination",
            "transit_items": "Transit Items",
            "pct_of_total_items": "% of Total Items"
        })
        st.dataframe(transit_display, use_container_width=True)
    else:
        st.info("No transit destination activity found for the selected date range.")

    st.subheader("Destination Transit Diagnostics")
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
    else:
        st.info("No destination-level transit reject data available for the selected date range.")

    st.subheader("Transit Destination Share")
    if len(transit_summary) > 0:
        chart_df = transit_summary.set_index("destination")["transit_items"]
        st.bar_chart(chart_df)
    else:
        st.info("No transit destination data available for charting.")

    st.subheader("Transit by Destination by Weekday")
    if len(destination_weekday_mix) > 0:
        weekday_chart = destination_weekday_mix.copy()
        st.line_chart(weekday_chart)
        weekday_display = weekday_chart.round(1)
        st.dataframe(weekday_display, use_container_width=True)
    else:
        st.info("No destination weekday mix data available for the selected date range.")

    st.subheader("Daily Transit Volume")
    if len(transit_df) > 0:
        daily_transit = transit_df["datetime"].dt.date.value_counts().sort_index()
        st.line_chart(daily_transit)
    else:
        st.info("No transit items found for the selected date range.")

    st.subheader("Transit Mix by Day")
    if len(transit_df) > 0:
        transit_mix = (
            transit_df.groupby([transit_df["datetime"].dt.date, "transit_destination"])
            .size()
            .unstack(fill_value=0)
            .sort_index()
        )
        st.line_chart(transit_mix)
    else:
        st.info("No transit mix data available for the selected date range.")

    st.subheader("Transit vs Reject Pattern by Weekday")
    if len(transit_weekday_comparison) > 0:
        comparison_display = transit_weekday_comparison.copy()
        comparison_display["Avg Transit Items / Day"] = comparison_display["Avg Transit Items / Day"].round(1)
        comparison_display["Avg Reject Rate %"] = comparison_display["Avg Reject Rate %"].round(2)
        st.dataframe(comparison_display, use_container_width=True)
    else:
        st.info("No weekday comparison data available for the selected date range.")
