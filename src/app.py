# app.py
# Streamlit dashboard for AMH analytics
# Displays item flow, routing, rejects, and transit diagnostics in a web interface

import streamlit as st
import pandas as pd
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import altair as alt
from textwrap import dedent

from streamlit_autorefresh import st_autorefresh
import pytz
import re

st.set_page_config(
    page_title="SortView",
    page_icon="📚",
    layout="wide"
)

APP_TZ = ZoneInfo("America/Chicago")

def is_operating_hours(now_ct: datetime) -> bool:
    # 7:00 AM through 8:59 PM
    return 7 <= now_ct.hour < 21

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

def render_kpi_card(
    title,
    value,
    subtitle="",
    subtitle_color="var(--text-color)",
    value_font_size="2.35rem",
    border_color="rgba(148, 163, 184, 0.28)",
    value_color="var(--text-color)",
    value_wrap=False,
    fill_pct=None,
    fill_color=None
):
    theme_base = st.get_option("theme.base") or "light"

    if fill_color is None:
        if theme_base == "dark":
            fill_color = "rgba(96, 165, 250, 0.28)"
        else:
            fill_color = "rgba(59, 130, 246, 0.12)"

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
            f'font-size:0.98rem;'
            f'font-weight:500;'
            f'color:{subtitle_color};'
            f'margin-top:10px;'
            f'line-height:1.35;'
            f'overflow:visible;'
            f'position:relative;'
            f'z-index:2;'
            f'opacity:0.82;'
            f'width:100%;'
            f'">{subtitle}</div>'
        )

    card_html = (
        f'<div style="'
        f'position:relative;'
        f'overflow:hidden;'
        f'border:1px solid {border_color};'
        f'border-radius:12px;'
        f'padding:16px 18px;'
        f'background:var(--secondary-background-color);'
        f'min-height:185px;'
        f'height:185px;'
        f'display:flex;'
        f'flex-direction:column;'
        f'justify-content:center;'
        f'align-items:center;'
        f'text-align:center;'
        f'box-shadow:0 1px 2px rgba(0, 0, 0, 0.08);'
        f'">'
        f'{fill_html}'
        f'<div style="'
        f'font-size:1.08rem;'
        f'font-weight:600;'
        f'color:var(--text-color);'
        f'margin-bottom:10px;'
        f'position:relative;'
        f'z-index:2;'
        f'opacity:0.80;'
        f'">{title}</div>'
        f'<div style="'
        f'font-size:{value_font_size};'
        f'font-weight:700;'
        f'color:{value_color};'
        f'line-height:1.15;'
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



def normalize_internal_destination(destination, raw_message="", message_code=""):
    destination = "" if destination is None else str(destination).strip()
    raw_message = "" if raw_message is None else str(raw_message)
    message_code = "" if message_code is None else str(message_code).strip()

    combined = f"{destination} {raw_message}".upper()

    if not destination and not raw_message:
        return None

    # exclude external transit destinations
    if "WESTSIDE" in combined or "LIBRARY EXPRESS" in combined:
        return None

    # exclude problem-item style routing failures from internal workflow
    if "NO AGENCY DESTINATION" in combined or destination == "":
        return None
    
    
    if re.search(r"\bILL\b", combined) or "INTERLIBRARY" in combined:
        return "ILL"

    if "COLLECTION SERVICES" in combined or "COLLECTION" in combined or "CATALOG" in combined or "PROCESSING" in combined:
        return "Collection Services"


    if "REPAIR" in combined or "MENDING" in combined or "MEND" in combined:
        return "Repair / Mending"

    if "STAFF" in combined or "REVIEW" in combined:
        return "Staff Review"

    if message_code in {"09", "10", "11", "12", "13", "14", "15", "16", "17", "18"}:
        return None

    return "Other Internal"


def build_internal_routing_summary(acs_df):
    if acs_df is None or len(acs_df) == 0:
        return pd.DataFrame(columns=["internal_category", "count"])

    work_df = acs_df.copy()

    if "datetime" in work_df.columns:
        work_df["datetime"] = pd.to_datetime(work_df["datetime"], errors="coerce")

    work_df["internal_category"] = work_df.apply(
        lambda row: normalize_internal_destination(
            row.get("destination"),
            row.get("raw_message"),
            row.get("message_code"),
        ),
        axis=1
    )

    work_df = work_df[work_df["internal_category"].notna()].copy()

    if len(work_df) == 0:
        return pd.DataFrame(columns=["internal_category", "count"])

    summary = (
        work_df["internal_category"]
        .value_counts()
        .rename_axis("internal_category")
        .reset_index(name="count")
    )

    return summary


def get_internal_count(summary_df, category_name):
    if summary_df is None or len(summary_df) == 0:
        return 0

    match = summary_df.loc[summary_df["internal_category"] == category_name, "count"]
    if len(match) == 0:
        return 0
    return int(match.iloc[0])

def build_ill_patron_lookup(acs_df):
    if acs_df is None or len(acs_df) == 0:
        return pd.DataFrame(columns=["patron_id", "patron_name_64", "is_ill_patron"])

    work_df = acs_df.copy()

    if "datetime" in work_df.columns:
        work_df["datetime"] = pd.to_datetime(work_df["datetime"], errors="coerce")

    if "raw_message" in work_df.columns:
        work_df["raw_message"] = work_df["raw_message"].fillna("").astype(str)

    if "patron_id" not in work_df.columns:
        return pd.DataFrame(columns=["patron_id", "patron_name_64", "is_ill_patron"])

    patron_df = work_df[
        work_df["message_code"].astype(str).str.strip() == "64"
    ].copy()

    if len(patron_df) == 0:
        return pd.DataFrame(columns=["patron_id", "patron_name_64", "is_ill_patron"])

    patron_df = patron_df[patron_df["patron_id"].notna()].copy()

    patron_df["patron_name_64"] = patron_df["raw_message"].str.extract(r"\|AE([^|]*)", expand=False)
    patron_df["patron_type_64"] = patron_df["raw_message"].str.extract(r"\|PT([^|]*)", expand=False)

    patron_df["is_ill_patron"] = patron_df["patron_type_64"].fillna("").astype(str).str.upper().eq("ILL")

    if "datetime" in patron_df.columns:
        patron_df = patron_df.sort_values("datetime")

    patron_df = patron_df.drop_duplicates(subset=["patron_id"], keep="last")

    return patron_df[["patron_id", "patron_name_64", "is_ill_patron"]]

def build_acs_item_summary(acs_df):
    if acs_df is None or len(acs_df) == 0:
        return {
            "holds_total": 0,
            "ill_total": 0,
            "ill_main": 0,
            "ill_westside": 0,
            "ill_library_express": 0,
            "items_df": pd.DataFrame(),
        }

    df = acs_df.copy()

    # normalize
    df["raw_message"] = df["raw_message"].fillna("").astype(str)
    df["message_code"] = df["message_code"].astype(str).str.strip()

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    # -----------------------------
    # STEP 1: GET HOLD ITEM EVENTS
    # -----------------------------
    items = df[df["raw_message"].str.startswith("101", na=False)].copy()

    if len(items) == 0:
        return {
            "holds_total": 0,
            "ill_total": 0,
            "ill_main": 0,
            "ill_westside": 0,
            "ill_library_express": 0,
            "items_df": pd.DataFrame(),
        }

    # latest per barcode
    items = items.sort_values("datetime")
    items = items.drop_duplicates(subset=["barcode"], keep="last")

    # HOLD = 101YNY
    items["is_hold"] = items["raw_message"].str.startswith("101YNY", na=False)

    # -----------------------------
    # STEP 2: GET PTILL FROM 64 ROWS
    # -----------------------------
    patrons = df[df["message_code"] == "64"].copy()

    patrons["patron_type"] = patrons["raw_message"].str.extract(r"\|PT([^|]*)", expand=False)

    patrons["is_ill"] = patrons["patron_type"].fillna("").str.upper().eq("ILL")

    patrons = patrons.sort_values("datetime")
    patrons = patrons.drop_duplicates(subset=["patron_id"], keep="last")

    # -----------------------------
    # STEP 3: MERGE
    # -----------------------------
    items = items.merge(
        patrons[["patron_id", "is_ill"]],
        on="patron_id",
        how="left"
    )

    items["is_ill"] = items["is_ill"].fillna(False)

    # -----------------------------
    # FINAL COUNTS
    # -----------------------------
    holds_df = items[items["is_hold"]].copy()
    ill_df = holds_df[holds_df["is_ill"]].copy()

    dest = ill_df["destination"].fillna("").astype(str)

    return {
        "holds_total": int(len(holds_df)),
        "ill_total": int(len(ill_df)),
        "ill_main": int((~dest.str.contains("WESTSIDE|LIBRARY EXPRESS", case=False)).sum()),
        "ill_westside": int(dest.str.contains("WESTSIDE", case=False).sum()),
        "ill_library_express": int(dest.str.contains("LIBRARY EXPRESS", case=False).sum()),
        "items_df": items,
    }


def get_problem_items_count(source_df):
    if source_df is None or len(source_df) == 0:
        return 0

    if "destination" not in source_df.columns:
        return 0

    destination_series = source_df["destination"].fillna("").astype(str).str.strip().str.upper()

    problem_mask = (
        destination_series.eq("")
        | destination_series.eq("NO AGENCY DESTINATION")
        | destination_series.str.contains("NO AGENCY DESTINATION", na=False)
    )

    return int(problem_mask.sum())


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



def format_relative_time(dt_value, now_value):
    if dt_value is None:
        return "N/A"

    minutes = int((now_value - dt_value).total_seconds() // 60)

    if minutes < 1:
        return "just now"
    if minutes == 1:
        return "1 min ago"
    if minutes < 60:
        return f"{minutes} min ago"

    hours = minutes // 60
    if hours == 1:
        return "1 hr ago"
    if hours < 24:
        return f"{hours} hrs ago"

    days = hours // 24
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"

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
    theme_base = st.get_option("theme.base") or "light"



    if theme_base == "dark":
        axis_label_color = "#cbd5e1"
        axis_title_color = "#e5e7eb"
        grid_color = "rgba(148, 163, 184, 0.18)"
        domain_color = "rgba(148, 163, 184, 0.28)"
        tick_color = "rgba(148, 163, 184, 0.28)"
        legend_label_color = "#cbd5e1"
        legend_title_color = "#e5e7eb"
        title_color = "#f8fafc"
        chart_background = "transparent"
        view_fill = "transparent"
    else:
        axis_label_color = "#6b7280"
        axis_title_color = "#6b7280"
        grid_color = "#e5e7eb"
        domain_color = "#d1d5db"
        tick_color = "#d1d5db"
        legend_label_color = "#6b7280"
        legend_title_color = "#6b7280"
        title_color = "#1f2937"
        chart_background = "transparent"
        view_fill = "transparent"

    chart = (
        chart
        .configure_view(
            stroke=None,
            fill=view_fill
        )
        .configure_axis(
            labelColor=axis_label_color,
            titleColor=axis_title_color,
            gridColor=grid_color,
            domainColor=domain_color,
            tickColor=tick_color,
            labelFontSize=12,
            titleFontSize=13
        )
        .configure_legend(
            labelColor=legend_label_color,
            titleColor=legend_title_color,
            labelFontSize=12,
            titleFontSize=13
        )
        .configure_title(
            color=title_color,
            fontSize=16
        )
        .properties(
            background=chart_background
        )
    )

    st.altair_chart(chart, use_container_width=True)


def build_roi_payload(df, df_history_raw, start_date, end_date):
    if len(df) == 0 or len(df_history_raw) == 0:
        return None

    MANUAL_RATE = 45
    HOURLY_COST = st.session_state.get("roi_hourly_cost", 18.0)
    UPFRONT_COST = st.session_state.get("roi_upfront_cost", 200000.0)
    MONTHLY_COST = st.session_state.get("roi_monthly_cost", 0.0)
    YEARLY_COST = st.session_state.get("roi_yearly_cost", 8000.0)
    roi_mode = st.session_state.get("roi_mode", "Annualized Projection")
    INSTALL_DATE = st.session_state.get("roi_install_date", pd.to_datetime("2019-01-01").date())
    INCLUDE_UPFRONT_IN_SINCE_INSTALL = st.session_state.get("roi_include_upfront_since_install", True)

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

    total_saved = staff_df["hours_saved"].sum()
    labor_value_saved = total_saved * HOURLY_COST

    days_in_range = max((pd.to_datetime(end_date) - pd.to_datetime(start_date)).days + 1, 1)
    months_in_range = days_in_range / 30.44
    years_in_range = days_in_range / 365.25

    observed_prorated_monthly_cost = MONTHLY_COST * months_in_range
    observed_prorated_yearly_cost = YEARLY_COST * years_in_range
    observed_operating_cost = observed_prorated_monthly_cost + observed_prorated_yearly_cost
    observed_total_roi_cost = UPFRONT_COST + observed_prorated_monthly_cost + observed_prorated_yearly_cost
    observed_net_operating_value = labor_value_saved - observed_operating_cost

    annual_labor_value = labor_value_saved * (12 / months_in_range) if months_in_range > 0 else 0
    annual_operating_cost = (MONTHLY_COST * 12) + YEARLY_COST

    if roi_mode == "Annualized Projection":
        total_roi_cost = annual_operating_cost
        net_roi_value = annual_labor_value - total_roi_cost
    else:
        total_roi_cost = observed_total_roi_cost
        net_roi_value = labor_value_saved - total_roi_cost

    # Payback based on annual net value (consistent with ROI)
    if net_roi_value > 0:
        payback_months = (UPFRONT_COST / net_roi_value) * 12
    else:
        payback_months = None

    roi_pct = (net_roi_value / total_roi_cost) * 100 if total_roi_cost > 0 else None

    install_date_ts = pd.to_datetime(INSTALL_DATE)
    today_ts = pd.Timestamp.today().normalize()
    installed_days = max((today_ts - install_date_ts).days, 1)
    installed_years = installed_days / 365.25

    since_install_labor_value = annual_labor_value * installed_years
    since_install_operating_cost = annual_operating_cost * installed_years

    if INCLUDE_UPFRONT_IN_SINCE_INSTALL:
        since_install_total_cost = UPFRONT_COST + since_install_operating_cost
    else:
        since_install_total_cost = since_install_operating_cost

    since_install_net_value = since_install_labor_value - since_install_total_cost
    since_install_roi_pct = (
        (since_install_net_value / since_install_total_cost) * 100
        if since_install_total_cost > 0 else None
    )

    return {
        "roi_mode": roi_mode,
        "roi_pct": roi_pct,
        "net_roi_value": net_roi_value,
        "total_roi_cost": total_roi_cost,
        "payback_months": payback_months,
        "since_install_roi_pct": since_install_roi_pct,
        "since_install_net_value": since_install_net_value,
        "annual_labor_value": annual_labor_value,
        "annual_operating_cost": annual_operating_cost,
        "labor_value_saved": labor_value_saved,
        "observed_operating_cost": observed_operating_cost,
        "observed_net_operating_value": observed_net_operating_value,
        "hourly_cost": HOURLY_COST,
    }
    
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



st.caption("Hanly Analytics")
st.markdown('<div class="sortview-title">SORTVIEW</div>', unsafe_allow_html=True)
st.markdown(
    "<div style='color:#6b7280; font-size:0.95rem; margin-bottom:10px;'>"
    "New Braunfels Public Library • Main Branch • Tech Logic UltraSort"
    "</div>",
    unsafe_allow_html=True
)

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
        f"Uploaded {uploaded_checkins_rows:,} checkins and {uploaded_rejects_rows:,} rejects this run"
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
df["transit_destination"] = df["destination"].apply(normalize_transit_destination)

df["destination_report"] = df["destination_clean"].copy()
df.loc[df["destination_report"] == "1", "destination_report"] = "Main"
df.loc[df["transit_destination"] == "Westside", "destination_report"] = "Westside"
df.loc[df["transit_destination"] == "Library Express", "destination_report"] = "Library Express"

df["destination_clean"] = df["destination_report"]

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

if len(df_live_raw) > 0 and "datetime" in df_live_raw.columns:
    latest_live_dt = df_live_raw["datetime"].max()
    today = pd.to_datetime(latest_live_dt).date()
else:
    today = live_now.date()

start_hour = 7
end_hour = 20
current_hour = live_now.hour

if current_hour < start_hour:
    live_hour_range = [start_hour]
else:
    live_hour_range = list(range(start_hour, min(current_hour, end_hour) + 1))
    
today_metrics = get_today_metrics(df_live_raw, rejects_live_raw, today)

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

if len(historical_checkins_df) > 0:
    historical_checkins_df["destination_clean"] = historical_checkins_df["destination"].astype(str).str.strip()
    historical_checkins_df["transit_destination"] = historical_checkins_df["destination"].apply(normalize_transit_destination)

    historical_westside_pct = (
        (historical_checkins_df["transit_destination"] == "Westside").sum()
        / len(historical_checkins_df)
    ) * 100

    historical_library_express_pct = (
        (historical_checkins_df["transit_destination"] == "Library Express").sum()
        / len(historical_checkins_df)
    ) * 100
else:
    historical_westside_pct = None
    historical_library_express_pct = None
    
    
today_westside_pct = (today_westside / today_checkins * 100) if today_checkins > 0 else 0
today_library_express_pct = (today_library_express / today_checkins * 100) if today_checkins > 0 else 0

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


today_bin0_count = 0
if "bin" in today_df.columns:
    today_bin0_count = (
        today_df["bin"].astype(str).str.contains("0", na=False).sum()
    )


internal_summary_today = build_internal_routing_summary(today_acs_df)


with st.expander("ACS Internal Routing Debug", expanded=False):
    st.write("today_acs_df rows:", len(today_acs_df))

    debug_cols = ["datetime", "barcode", "destination", "message_code", "raw_message"]
    debug_cols = [c for c in debug_cols if c in today_acs_df.columns]

    st.write("Internal summary:")
    st.dataframe(internal_summary_today, use_container_width=True)

    st.write("Rows containing ILL / INTERLIBRARY:")
    ill_debug = today_acs_df[
        today_acs_df["raw_message"].fillna("").astype(str).str.contains(r"ILL|INTERLIBRARY", case=False, na=False)
        | today_acs_df["destination"].fillna("").astype(str).str.contains(r"ILL|INTERLIBRARY", case=False, na=False)
    ].copy()
    st.write("ILL row count:", len(ill_debug))
    if len(ill_debug) > 0:
        st.dataframe(ill_debug[debug_cols], use_container_width=True)

    st.write("Rows containing REPAIR / MENDING / MEND:")
    repair_debug = today_acs_df[
        today_acs_df["raw_message"].fillna("").astype(str).str.contains(r"REPAIR|MENDING|MEND", case=False, na=False)
        | today_acs_df["destination"].fillna("").astype(str).str.contains(r"REPAIR|MENDING|MEND", case=False, na=False)
    ].copy()
    st.write("Repair row count:", len(repair_debug))
    if len(repair_debug) > 0:
        st.dataframe(repair_debug[debug_cols], use_container_width=True)

    st.write("Rows containing STAFF / REVIEW:")
    staff_debug = today_acs_df[
        today_acs_df["raw_message"].fillna("").astype(str).str.contains(r"STAFF|REVIEW", case=False, na=False)
        | today_acs_df["destination"].fillna("").astype(str).str.contains(r"STAFF|REVIEW", case=False, na=False)
    ].copy()
    st.write("Staff Review row count:", len(staff_debug))
    if len(staff_debug) > 0:
        st.dataframe(staff_debug[debug_cols], use_container_width=True)

    st.write("Rows containing COLLECTION / CATALOG / PROCESSING:")
    collection_debug = today_acs_df[
        today_acs_df["raw_message"].fillna("").astype(str).str.contains(r"COLLECTION|CATALOG|PROCESSING", case=False, na=False)
        | today_acs_df["destination"].fillna("").astype(str).str.contains(r"COLLECTION|CATALOG|PROCESSING", case=False, na=False)
    ].copy()
    st.write("Collection Services row count:", len(collection_debug))
    if len(collection_debug) > 0:
        st.dataframe(collection_debug[debug_cols], use_container_width=True)

    st.write("Top destination values in today_acs_df:")
    
    if "destination" in today_acs_df.columns:
        dest_counts = (
            today_acs_df["destination"]
            .fillna("(blank)")
            .astype(str)
            .value_counts()
            .rename_axis("destination")
            .reset_index(name="count")
        )
    
        st.dataframe(dest_counts, use_container_width=True)

    st.write("Top raw_message snippets:")
    raw_preview = today_acs_df.copy()
    raw_preview["raw_preview"] = raw_preview["raw_message"].fillna("").astype(str).str[:160]
    preview_cols = [c for c in ["datetime", "barcode", "destination", "message_code", "raw_preview"] if c in raw_preview.columns]
    st.dataframe(raw_preview[preview_cols].head(50), use_container_width=True)


today_collection_services = get_internal_count(internal_summary_today, "Collection Services")
today_repair = get_internal_count(internal_summary_today, "Repair / Mending")
today_problem_items = get_problem_items_count(today_df)
today_staff_review = get_internal_count(internal_summary_today, "Staff Review")

acs_summary_today = build_acs_item_summary(acs_live_raw)

today_holds = acs_summary_today["holds_total"]

today_ill = acs_summary_today["ill_total"]
today_ill_main = acs_summary_today["ill_main"]
today_ill_westside = acs_summary_today["ill_westside"]
today_ill_library_express = acs_summary_today["ill_library_express"]

today_ill_items_df = acs_summary_today["items_df"]

# audit values only - do not use these in the live card yet
today_holds_from_internal_summary = get_internal_count(internal_summary_today, "Holds")
today_other_internal = get_internal_count(internal_summary_today, "Other Internal")


today_total_internal = (
    today_collection_services
    + today_ill
    + today_holds
    + today_repair
    + today_problem_items
    + today_staff_review
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
    col1, col2 = st.columns([4, 2])
    
    with col1:
        st.header(f"{today.strftime('%A, %b %d')}")
    

        if st.button("Refresh Live Data"):
            st.cache_data.clear()
            st.session_state["last_refresh_count"] = refresh_count
            st.rerun()
    
    with col2:
        expander_label = f"● {pipeline_status_label}"
    
        st.markdown(
            f"""
            <style>
            div[data-testid="stExpander"] details {{
                border: 1px solid rgba(148, 163, 184, 0.28);
                border-radius: 10px;
                overflow: hidden;
                background-color: var(--secondary-background-color);
            }}
        
            div[data-testid="stExpander"] summary {{
                font-weight: 700;
                color: {pipeline_status_color};
                background-color: {pipeline_status_bg};
                padding-top: 0.2rem;
                padding-bottom: 0.2rem;
            }}
        
            div[data-testid="stExpander"] details[open] > div {{
                background-color: var(--secondary-background-color);
                color: var(--text-color);
            }}
            </style>
            """,
            unsafe_allow_html=True
        )
    
        with st.expander(expander_label, expanded=pipeline_expanded):
            st.markdown("##### Pipeline Status"
                f"""
App Last Refreshed: {app_refreshed_str}  
Latest Checkin in DB: {latest_checkin_str} ({latest_checkin_ago})  
Latest Status Row Written: {pipeline_status_written_str} ({pipeline_status_written_ago})  
Last Pipeline Attempt: {pipeline_last_attempt_str} ({pipeline_last_attempt_ago})  
Last Successful Upload Run: {pipeline_last_run_str} ({pipeline_last_run_ago})  
Latest Result: {pipeline_result_text}  
Status Code: `{status_code_text}`
                """
            )
    
            st.markdown("##### Run Summary")
            s1, s2 = st.columns(2)
    
            with s1:
                st.markdown(
                    f"""
    Parsed Checkins: {checkins_rows:,}  
    Parsed Rejects: {rejects_rows:,}  
    Uploaded Checkins: {uploaded_checkins_rows:,}  
    Uploaded Rejects: {uploaded_rejects_rows:,}
                    """
                )
    
            with s2:
                st.markdown(
                    f"""
    Bad Checkin Datetimes: {checkins_bad_datetime_rows:,}  
    Bad Reject Datetimes: {rejects_bad_datetime_rows:,}  
    Transit Items: {transit_items:,}  
    Problem Items: {problem_items:,}
                    """
                )
    
            st.markdown("##### Destination Breakdown")
            st.caption(destination_breakdown_text)

    
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
                value_font_size="2.2rem",
                border_color="#93c5fd",
                fill_pct=checkins_fill_pct
            )
            
        with ops2:
            pct = today_metrics.get("current_speed_fill_pct", 0)
        
            if pct < 0.3:
                activity_label = "Slow"
            elif pct < 0.7:
                activity_label = "Moderate"
            else:
                activity_label = "Busy"
        
            render_kpi_card(
                "Current Throughput",
                f"{today_metrics['current_speed']}",
                f"""
                Items this hour
                <div style="margin-top:6px; width:100%; padding:0 2px;">
                    <div style="
                        position:relative;
                        height:5px;
                        border-radius:999px;
                        background:linear-gradient(to right, #60a5fa, #f59e0b, #ef4444);
                        width:100%;
                        margin:0 auto;
                    ">
                        <div style="
                            position:absolute;
                            left:calc({pct * 100:.1f}% - 1px);
                            top:-3px;
                            width:3px;
                            height:11px;
                            border-radius:2px;
                            background:#111827;
                        "></div>
                    </div>
                    <div style="
                        display:flex;
                        justify-content:space-between;
                        align-items:center;
                        font-size:0.72rem;
                        color:#6b7280;
                        margin-top:3px;
                        line-height:1.1;
                    ">
                        <span>Slow</span>
                        <span>Busy</span>
                    </div>
                </div>
                """,
                "#6b7280",
                value_font_size="2.2rem",
                border_color="#93c5fd"
            )

        with ops3:
            if today_peak_hour is not None:
                render_kpi_card(
                    "Busiest Hour",
                    format_hour(today_peak_hour),
                    f"{today_peak_hour_count:,} items ({today_peak_hour_pct:.1f}%)",
                    "#6b7280",
                    value_font_size="1.7rem",
                    border_color="#93c5fd"
                )
            else:
                render_kpi_card(
                    "Busiest Hour",
                    "N/A",
                    "No activity yet",
                    "#6b7280",
                    value_font_size="1.7rem",
                    border_color="#93c5fd"
                )

    # Routing (moved to middle, takes green)
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
                value_font_size="2.2rem",
                border_color="#34d399"
            )

        with route2:
            render_kpi_card(
                "Westside",
                f"{today_westside:,}",
                f"{today_westside_pct:.1f}% of today",
                "#6b7280",
                value_font_size="2.2rem",
                border_color="#34d399"
            )

        with route3:
            render_kpi_card(
                "Library Express",
                f"{today_library_express:,}",
                f"{today_library_express_pct:.1f}% of today",
                "#6b7280",
                value_font_size="1.6rem",
                border_color="#34d399"
            )

    # Rejects (moved to right, takes purple)
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
                    Rejects
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

        quality1, quality2 = st.columns(2)

        with quality1:
            render_kpi_card(
                "Rejects",
                f"{today_rejects:,}",
                "Failures today",
                "#6b7280",
                value_font_size="2.2rem",
                border_color="#c4b5fd"
            )

        with quality2:
            reject_rate_subtitle = "Of checkins today"
            if historical_daily_avg_reject > 0:
                reject_rate_subtitle = (
                    f"{live_reject_deviation:+.2f}% vs avg daily "
                    f"rate ({historical_daily_avg_reject:.2f}%)"
                )

            render_kpi_card(
                "Reject Rate",
                f"{today_reject_rate:.2f}%",
                reject_rate_subtitle,
                live_reject_subtitle_color,
                value_font_size="1.8rem",
                border_color="#c4b5fd",
                value_color=live_reject_value_color
            )


    st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)

    st.markdown(
        """
        <div style="
            border: 2px solid #14b8a6;
            border-radius: 14px;
            padding: 12px 14px;
            background: linear-gradient(90deg, #14b8a6, #0ea5e9);
            margin-bottom: 8px;
        ">
            <div style="
                font-size: 0.95rem;
                font-weight: 700;
                color: #ffffff;
                line-height: 1.2;
            ">
                Internal Workflow
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    internal1, internal2, internal3, internal4, internal5, internal6 = st.columns(6)
    
    internal_pct_base = today_checkins if today_checkins > 0 else 1
    
    with internal1:
        render_kpi_card(
            "Holds",
            f"{today_holds:,}",
            f"Estimated from hold/bin routing ({(today_holds / internal_pct_base) * 100:.1f}% of checkins)",
            "#6b7280",
            value_font_size="2.0rem",
            border_color="#34d399"
        )
    
    with internal2:
        render_kpi_card(
            "Collection Services",
            f"{today_collection_services:,}",
            f"{(today_collection_services / internal_pct_base) * 100:.1f}% of checkins today",
            "#6b7280",
            value_font_size="1.65rem",
            border_color="#34d399"
        )
    
    with internal3:
        render_kpi_card(
            "Repair / Mending",
            f"{today_repair:,}",
            f"{(today_repair / internal_pct_base) * 100:.1f}% of checkins today",
            "#6b7280",
            value_font_size="1.5rem",
            border_color="#34d399"
        )
    
    with internal4:
        render_kpi_card(
            "Problem Items",
            f"{today_problem_items:,}",
            f"{(today_problem_items / internal_pct_base) * 100:.1f}% missing destination routing",
            "#6b7280",
            value_font_size="1.85rem",
            border_color="#34d399"
        )
    
    with internal5:
        render_kpi_card(
            "Staff Review",
            f"{today_staff_review:,}",
            f"{(today_staff_review / internal_pct_base) * 100:.1f}% of checkins today",
            "#6b7280",
            value_font_size="1.55rem",
            border_color="#34d399"
        )
    
    with internal6:
        render_kpi_card(
            "ILL",
            f"{today_ill:,}",
            f"Main {today_ill_main:,} • WS {today_ill_westside:,} • LE {today_ill_library_express:,}",
            "#6b7280",
            value_font_size="1.85rem",
            border_color="#34d399"
        )
        
    with st.expander("Internal workflow audit", expanded=False):
        st.write("ACS holds (live card):", today_holds)
        st.write("ACS classified holds:", today_holds_from_internal_summary)
        st.write("Collection Services:", today_collection_services)
        st.write("Repair / Mending:", today_repair)
        st.write("Staff Review:", today_staff_review)
        st.write("ILL:", today_ill)
        st.write("Other Internal:", today_other_internal)
        st.write("Bin 0 count:", today_bin0_count)
        st.write("Rejects today:", today_rejects)
        st.write("Library Express today:", today_library_express)
        st.write("Internal summary:")
        st.dataframe(internal_summary_today, use_container_width=True)
        with st.expander("ILL audit", expanded=False):
            st.write("ILL total:", today_ill)
            st.write("ILL Main:", today_ill_main)
            st.write("ILL Westside:", today_ill_westside)
            st.write("ILL Library Express:", today_ill_library_express)
        
            if len(today_ill_items_df) > 0:
                ill_debug_cols = [
                    c for c in [
                        "datetime",
                        "barcode",
                        "patron_id",
                        "patron_name_64",
                        "destination",
                        "raw_message",
                    ]
                    if c in today_ill_items_df.columns
                ]
                st.dataframe(
                    today_ill_items_df[ill_debug_cols].sort_values("datetime", ascending=False),
                    use_container_width=True
                )
            else:
                st.info("No ILL items detected today.")
    if info_alerts:
        st.markdown(
            f"""
            <div style="
                border-left: 5px solid {info_border};
                background-color: {info_bg};
                padding: 14px 16px;
                border-radius: 8px;
                margin-top: 18px;
                margin-bottom: 12px;
            ">
                <div style="font-weight: 600; color: {info_title}; margin-bottom: 6px;">
                    Trends / Info
                </div>
                <ul style="margin: 0; padding-left: 18px; color: {info_text};">
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

    if show_live_alert:
        st.markdown(
            f"""
            <div style="
                border-left: 4px solid {danger_border};
                background-color: {danger_bg};
                padding: 14px 16px;
                border-radius: 8px;
                margin-top: 18px;
                margin-bottom: 8px;
            ">
                <div style="font-weight: 600; color: {danger_title}; margin-bottom: 6px;">
                    {live_alert_title}
                </div>
                <div style="color: {danger_text}; line-height: 1.4;">
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
        .properties(height=250)
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

    # ---------------------------------
    # Internal Workflow Summary
    # ---------------------------------
    overview_acs_df = acs_history_raw.copy()

    if len(overview_acs_df) > 0 and "datetime" in overview_acs_df.columns:
        overview_acs_df["datetime"] = pd.to_datetime(overview_acs_df["datetime"], errors="coerce")
        overview_acs_df = overview_acs_df.dropna(subset=["datetime"]).copy()
        overview_acs_df = overview_acs_df[
            (overview_acs_df["datetime"].dt.date >= start_date) &
            (overview_acs_df["datetime"].dt.date <= end_date)
        ].copy()

    overview_acs_summary = build_acs_item_summary(overview_acs_df)

    overview_holds = overview_acs_summary["holds_total"]
    overview_ill = overview_acs_summary["ill_total"]
    overview_ill_main = overview_acs_summary["ill_main"]
    overview_ill_westside = overview_acs_summary["ill_westside"]
    overview_ill_library_express = overview_acs_summary["ill_library_express"]

    st.markdown("### Internal Workflow")
    internal_overview_col1, internal_overview_col2 = st.columns(2)

    with internal_overview_col1:
        render_kpi_card(
            "Holds",
            f"{overview_holds:,}",
            f"{date_range_text}",
            "#6b7280",
            value_font_size="2.0rem",
            border_color="#34d399"
        )

    with internal_overview_col2:
        render_kpi_card(
            "ILL",
            f"{overview_ill:,}",
            f"Main {overview_ill_main:,} • WS {overview_ill_westside:,} • LE {overview_ill_library_express:,}",
            "#6b7280",
            value_font_size="2.0rem",
            border_color="#34d399"
        )

    with st.expander("ILL Debug (Overview)", expanded=False):

        ill_items_df = overview_acs_summary["items_df"]

        if len(ill_items_df) > 0:
            ill_debug_df = ill_items_df[
                (ill_items_df["is_hold"]) & (ill_items_df["is_ill"])
            ].copy()

            st.write("ILL item count:", len(ill_debug_df))

            debug_cols = [
                c for c in [
                    "datetime",
                    "barcode",
                    "patron_id",
                    "destination",
                    "raw_message",
                ]
                if c in ill_debug_df.columns
            ]

            st.dataframe(
                ill_debug_df
                .sort_values("datetime", ascending=False),
                use_container_width=True
            )
        else:
            st.info("No ACS items available for this range.")


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
    
        # --- AVERAGE MODE ---
        weekday_source = df.copy()
        weekday_source["date_only"] = weekday_source["datetime"].dt.date
        weekday_source["day_of_week"] = weekday_source["datetime"].dt.day_name()

        weekday_daily = (
            weekday_source.groupby(["date_only", "day_of_week"])
            .size()
            .reset_index(name="daily_checkins")
        )

        weekday_avg = (
            weekday_daily.groupby("day_of_week")["daily_checkins"]
            .mean()
            .reindex([
                "Monday", "Tuesday", "Wednesday",
                "Thursday", "Friday", "Saturday", "Sunday"
            ])
            .fillna(0)
        )

    if len(df) > 0 and len(weekday_avg) > 0:
        peak_day_avg_name = weekday_avg.idxmax()
        peak_day_avg_value = peak_day_avg_name
        peak_day_avg_subtitle = f"{weekday_avg.max():,.1f} avg checkins/day"


    overview_avg_hours_saved = 0.0
    overview_total_hours_saved = 0.0
    overview_labor_value_saved = 0.0
    
    MANUAL_RATE_OVERVIEW = 45
    overview_roi_payload = build_roi_payload(df, df_history_raw, start_date, end_date)
    HOURLY_COST_OVERVIEW = (
        overview_roi_payload["hourly_cost"]
        if overview_roi_payload and "hourly_cost" in overview_roi_payload
        else 18.0
    )
    
    if len(df) > 0:
        labor_df = df.copy()
        labor_df["date"] = labor_df["datetime"].dt.date
        labor_df["hour"] = labor_df["datetime"].dt.hour
    
        labor_daily_hourly = (
            labor_df.groupby(["date", "hour"])
            .size()
            .reset_index(name="checkins")
        )
    
        labor_avg_hourly = (
            labor_daily_hourly.groupby("hour")["checkins"]
            .mean()
            .reset_index(name="avg_items_per_hour")
        )
    
        if len(labor_avg_hourly) > 0:
            labor_peak_row = labor_avg_hourly.loc[labor_avg_hourly["avg_items_per_hour"].idxmax()]
            labor_threshold = labor_peak_row["avg_items_per_hour"] * 0.75
            labor_peak_hours = labor_avg_hourly[
                labor_avg_hourly["avg_items_per_hour"] >= labor_threshold
            ].copy()
    
            amh_rate_overview = (
                labor_peak_hours["avg_items_per_hour"].mean()
                if len(labor_peak_hours) > 0
                else labor_peak_row["avg_items_per_hour"]
            )
        else:
            amh_rate_overview = 130.0

    labor_daily_counts = df["datetime"].dt.date.value_counts().sort_index()
    labor_staff_df = labor_daily_counts.reset_index()
    labor_staff_df.columns = ["date", "checkins"]

    labor_staff_df["manual_hours"] = labor_staff_df["checkins"] / MANUAL_RATE_OVERVIEW
    labor_staff_df["amh_hours"] = labor_staff_df["checkins"] / amh_rate_overview
    labor_staff_df["hours_saved"] = (
        labor_staff_df["manual_hours"] - labor_staff_df["amh_hours"]
    ).clip(lower=0)

    overview_avg_hours_saved = labor_staff_df["hours_saved"].mean() if len(labor_staff_df) > 0 else 0.0
    overview_total_hours_saved = labor_staff_df["hours_saved"].sum() if len(labor_staff_df) > 0 else 0.0
    
    # NEW (split avg vs total labor value)
    overview_avg_labor_value = overview_avg_hours_saved * HOURLY_COST_OVERVIEW
    overview_total_labor_value = overview_total_hours_saved * HOURLY_COST_OVERVIEW

    
    row1_col1, row1_col2, row1_col3 = st.columns(3)
    row2_col1, row2_col2, row2_col3 = st.columns(3)
    row3_col1, row3_col2, row3_col3 = st.columns(3)
    row4_col1, row4_col2, row4_col3 = st.columns(3)

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

    with row4_col1:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Avg Hours Saved",
                f"{overview_avg_hours_saved:,.2f}",
                "Per day",
                "#6b7280"
            )
        else:
            render_kpi_card(
                "Total Hours Saved",
                f"{overview_total_hours_saved:,.2f}",
                "Across selected date range",
                "#6b7280"
            )
    
    with row4_col2:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Avg Labor Value",
                f"${overview_avg_labor_value:,.0f}",
                "Per day",
                "#6b7280"
            )
        else:
            render_kpi_card(
                "Total Labor Value",
                f"${overview_total_labor_value:,.0f}",
                "Across selected date range",
                "#6b7280"
            )
    
    with row4_col3:
        if st.session_state.get("roi_calculated", False):
            overview_roi_payload = build_roi_payload(df, df_history_raw, start_date, end_date)

            if overview_roi_payload:
                if overview_roi_payload["roi_mode"] == "Annualized Projection":
                    render_kpi_card(
                        "Yearly Savings After Cost",
                        f'${overview_roi_payload["net_roi_value"]:,.0f}',
                        "Projected yearly savings after recurring cost",
                        "#6b7280",
                        value_color="#059669" if overview_roi_payload["net_roi_value"] >= 0 else "#dc2626"
                    )
                else:
                    render_kpi_card(
                        "Observed Net Value",
                        f'${overview_roi_payload["observed_net_operating_value"]:,.0f}',
                        "Selected range value minus recurring cost",
                        "#6b7280",
                        value_color="#059669" if overview_roi_payload["observed_net_operating_value"] >= 0 else "#dc2626"
                    )
            else:
                render_kpi_card(
                    "ROI",
                    "N/A",
                    "No ROI data for current date range",
                    "#6b7280"
                )
        else:
            render_kpi_card(
                "ROI",
                "Not Calculated",
                "Go to Reports and click Calculate ROI",
                "#6b7280"
            )

if selected_view == "Reports":
    st.header("Reports")
    pdf_button_placeholder = st.empty()


    # =========================================================
    # LABOR & EFFICIENCY
    # =========================================================
    st.subheader("Labor & Efficiency")
    st.caption("Estimates staff time saved by Automated Materials Handler processing.")


    # =========================================================
    # ROI CALCULATOR
    # =========================================================
    with st.expander("ROI Calculator", expanded=False):
        st.caption("Estimate operating value, annual ROI, payback period, and since-install return.")

        roi_help_col1, roi_help_col2 = st.columns([3, 2])

        with roi_help_col1:
            st.info(
                "Use this section to set labor cost, capital cost, recurring cost, ROI mode, and install date. "
                "The Overview tab will use these same ROI settings."
            )

        with roi_help_col2:
            st.markdown(
                """
                <div style="
                    border-left: 4px solid #7c3aed;
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 2px;
                    margin-bottom: 12px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        What this controls
                    </div>
                    <div style="color: #4b5563; line-height: 1.45;">
                        Labor value, observed net value, annual ROI, payback period,
                        and since-install ROI.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

        labor_input_col, upfront_col, monthly_col, yearly_col = st.columns(4)

        with labor_input_col:
            HOURLY_COST = st.number_input(
                "Hourly labor rate ($/hour)",
                min_value=0.0,
                max_value=1000.0,
                value=st.session_state.get("roi_hourly_cost", 18.0),
                step=0.5,
                format="%.2f",
                key="roi_hourly_cost",
                help="Adjust the hourly labor cost used to estimate labor value."
            )

        with upfront_col:
            UPFRONT_COST = st.number_input(
                "Upfront cost ($)",
                min_value=0.0,
                max_value=10000000.0,
                value=st.session_state.get("roi_upfront_cost", 250000.0),
                step=100.0,
                format="%.2f",
                key="roi_upfront_cost",
                help="One-time purchase or implementation cost."
            )

        with monthly_col:
            MONTHLY_COST = st.number_input(
                "Monthly cost ($/month)",
                min_value=0.0,
                max_value=1000000.0,
                value=st.session_state.get("roi_monthly_cost", 0.0),
                step=10.0,
                format="%.2f",
                key="roi_monthly_cost",
                help="Recurring monthly maintenance, service, or lease cost."
            )

        with yearly_col:
            YEARLY_COST = st.number_input(
                "Yearly cost ($/year)",
                min_value=0.0,
                max_value=1000000.0,
                value=st.session_state.get("roi_yearly_cost", 8500.0),
                step=50.0,
                format="%.2f",
                key="roi_yearly_cost",
                help="Recurring annual support, licensing, or maintenance cost."
            )

        roi_mode_col1, roi_mode_col2 = st.columns([2, 2])

        with roi_mode_col1:
            roi_mode = st.radio(
                "Calculation Mode",
                ["Observed (Selected Range)", "Annualized Projection"],
                horizontal=True,
                key="roi_mode",
                help="Observed uses only the selected date range. Annualized Projection scales the observed labor value to a 12-month estimate."
            )

        with roi_mode_col2:
            INSTALL_DATE = st.date_input(
                "Installed on",
                value=st.session_state.get("roi_install_date", pd.to_datetime("2019-05-01").date()),
                key="roi_install_date",
                help="Used to estimate ROI since the AMH was put into service."
            )

        INCLUDE_UPFRONT_IN_SINCE_INSTALL = st.checkbox(
            "Include upfront cost in since-install ROI",
            value=st.session_state.get("roi_include_upfront_since_install", True),
            key="roi_include_upfront_since_install",
            help="Usually this should stay on, since purchase ROI should include the initial capital cost."
        )

        calc_col1, calc_col2 = st.columns([1, 5])

        with calc_col1:
            calculate_roi_clicked = st.button("Calculate ROI", type="primary")

        with calc_col2:
            if st.button("Clear ROI Results"):
                st.session_state["roi_calculated"] = False
                st.rerun()

        if calculate_roi_clicked:
            st.session_state["roi_calculated"] = True

        roi_payload = None

        if st.session_state.get("roi_calculated", False):
            roi_payload = build_roi_payload(df, df_history_raw, start_date, end_date)

        if roi_payload:
            roi_pct = roi_payload["roi_pct"]
            net_roi_value = roi_payload["net_roi_value"]
            total_roi_cost = roi_payload["total_roi_cost"]
            payback_months = roi_payload["payback_months"]
            since_install_roi_pct = roi_payload["since_install_roi_pct"]
            since_install_net_value = roi_payload["since_install_net_value"]
            annual_labor_value = roi_payload["annual_labor_value"]
            annual_operating_cost = roi_payload["annual_operating_cost"]
            labor_value_saved = roi_payload["labor_value_saved"]
            observed_operating_cost = roi_payload["observed_operating_cost"]
            observed_net_operating_value = roi_payload["observed_net_operating_value"]
            observed_hours_saved = labor_value_saved / HOURLY_COST if HOURLY_COST > 0 else 0

            months_in_range = max((pd.to_datetime(end_date) - pd.to_datetime(start_date)).days + 1, 1) / 30.44
            years_in_range = max((pd.to_datetime(end_date) - pd.to_datetime(start_date)).days + 1, 1) / 365.25
            days_in_range = max((pd.to_datetime(end_date) - pd.to_datetime(start_date)).days + 1, 1)

            install_date_ts = pd.to_datetime(INSTALL_DATE)
            today_ts = pd.Timestamp.today().normalize()
            installed_days = max((today_ts - install_date_ts).days, 1)
            installed_years = installed_days / 365.25

            since_install_labor_value = annual_labor_value * installed_years
            since_install_operating_cost = annual_operating_cost * installed_years

            if INCLUDE_UPFRONT_IN_SINCE_INSTALL:
                since_install_total_cost = UPFRONT_COST + since_install_operating_cost
            else:
                since_install_total_cost = since_install_operating_cost

            def render_explainer_card(title, body, border_color):
                st.markdown(
                    f"""
                    <div style="
                        border-left: 4px solid {border_color};
                        background-color: #f9fafb;
                        padding: 14px 16px;
                        border-radius: 8px;
                        margin-top: 8px;
                        margin-bottom: 12px;
                    ">
                        <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                            {title}
                        </div>
                        <div style="color: #4b5563; line-height: 1.45;">
                            {body}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

            # ===============================
            # ROW HEADER (MODE DEPENDENT)
            # ===============================
            if roi_mode == "Annualized Projection":
                st.markdown("### Annualized Metrics")
            else:
                st.markdown("### Selected Range Metrics")
            
            # ===============================
            # FIRST ROW (MODE DEPENDENT KPIs)
            # ===============================
            roi1, roi2, roi3, roi4 = st.columns(4)

            if roi_mode == "Annualized Projection":

                with roi1:
                    render_kpi_card(
                        "1. Yearly Cost (User provided)",
                        f"${total_roi_cost:,.0f}",
                        "Recurring annual cost only",
                        "#6b7280"
                    )

                with roi2:
                    render_kpi_card(
                        "2. Twelve-month projection of savings at current rate",
                        f"${net_roi_value:,.0f}",
                        f"Based on last {days_in_range:,} days of activity",
                        "#6b7280",
                        value_color="#059669" if net_roi_value >= 0 else "#dc2626"
                    )

                with roi3:
                    if payback_months is not None:
                        payback_years = payback_months / 12
                        years_after_payback = installed_years - payback_years

                        if years_after_payback >= 0:
                            break_even_value = "Paid Off"
                            break_even_subtitle = f"Recovered cost ~{years_after_payback:,.1f} years ago"
                            break_even_color = "#059669"
                        else:
                            break_even_value = f"{abs(years_after_payback):,.1f} yrs"
                            break_even_subtitle = "Estimated time remaining to recover upfront cost"
                            break_even_color = "#d97706"
                    else:
                        break_even_value = "Not Reached"
                        break_even_subtitle = "Current annual run rate does not recover upfront cost"
                        break_even_color = "#dc2626"

                    render_kpi_card(
                        "3. Break-even Status",
                        break_even_value,
                        break_even_subtitle,
                        "#6b7280",
                        value_color=break_even_color
                    )

                with roi4:
                    render_kpi_card(
                        "4. Lifetime Value Generated",
                        f"${since_install_labor_value:,.0f}",
                        f"Estimated value created over {installed_years:,.1f} years",
                        "#6b7280",
                        value_color="#059669" if since_install_labor_value >= 0 else "#dc2626"
                    )

            else:

                with roi1:
                    render_kpi_card(
                        "Range Length",
                        f"{days_in_range:,} days",
                        f"{months_in_range:,.2f} months",
                        "#6b7280",
                        value_font_size="1.8rem"
                    )

                with roi2:
                    render_kpi_card(
                        "Observed Labor Value",
                        f"${labor_value_saved:,.0f}",
                        "Staff time avoided value",
                        "#6b7280"
                    )

                with roi3:
                    render_kpi_card(
                        "Observed Operating Cost",
                        f"${observed_operating_cost:,.0f}",
                        "Prorated recurring cost only",
                        "#6b7280"
                    )

                with roi4:
                    render_kpi_card(
                        "Observed Net Value",
                        f"${observed_net_operating_value:,.0f}",
                        "Labor value minus operating cost",
                        "#6b7280",
                        value_color="#059669" if observed_net_operating_value >= 0 else "#dc2626"
                    )

            # ===============================
            # LIFETIME HEADER (STATIC)
            # ===============================

            
            st.markdown("### Lifetime Performance")
            install_roi1, install_roi2, install_roi3, install_roi4 = st.columns(4)

            with install_roi1:
                render_kpi_card(
                    "5. Years Since Install",
                    f"{installed_years:,.1f}",
                    pd.to_datetime(INSTALL_DATE).strftime("%b %d, %Y"),
                    "#6b7280"
                )

            with install_roi2:
                render_kpi_card(
                    "6. Since-Install Value",
                    f"${since_install_labor_value:,.0f}",
                    "Projected cumulative labor value",
                    "#6b7280"
                )

            with install_roi3:
                render_kpi_card(
                    "7. Since-Install Net",
                    f"${since_install_net_value:,.0f}",
                    "Value minus total cost",
                    "#6b7280",
                    value_color="#059669" if since_install_net_value >= 0 else "#dc2626"
                )

            with install_roi4:
                render_kpi_card(
                    "8. Since-Install ROI",
                    f"{since_install_roi_pct:,.1f}%" if since_install_roi_pct is not None else "N/A",
                    "Estimated ROI since install",
                    "#6b7280",
                    value_color="#059669" if since_install_roi_pct is not None and since_install_roi_pct >= 0 else "#dc2626"
                )

            with st.expander("ROI Breakdown", expanded=False):
                st.markdown("### ROI Breakdown")
            
                if roi_mode == "Annualized Projection":
                    payback_display = f"{payback_months:,.1f} months" if payback_months is not None else "Not available"
                    roi_display = f"{roi_pct:,.1f}%" if roi_pct is not None else "N/A"
                    since_install_roi_display = f"{since_install_roi_pct:,.1f}%" if since_install_roi_pct is not None else "N/A"
                    
                    st.markdown("### How to interpret Annualized metrics")
                    render_explainer_card(
                        f"1. Annual Cost — ${total_roi_cost:,.0f}",
                        (
                            "This is the total recurring cost to operate the AMH for one full year."
                            f"<br><br>Inputs used:"
                            f"<br>• Yearly cost setting: <b>${YEARLY_COST:,.0f}</b>"
                            f"<br>• Monthly cost setting: <b>${MONTHLY_COST:,.0f}</b>"
                            f"<br><br>Math:"
                            f"<br>• Annual cost = yearly cost + (monthly cost × 12)"
                            f"<br>• <b>${YEARLY_COST:,.0f} + (${MONTHLY_COST:,.0f} × 12) = ${total_roi_cost:,.0f}</b>"
                        ),
                        "#6b7280"
                    )
                    
                    render_explainer_card(
                        f"2. Current Annual Run Rate — ${net_roi_value:,.0f}",
                        (
                            "This estimates how much net value the AMH is generating per year at its current usage level."
                            f"<br><br>What this represents:"
                            f"<br>• Based only on the selected date range"
                            f"<br>• Scaled up to a full 12-month equivalent"
                            f"<br>• Then reduced by annual operating cost"
                            f"<br><br>Inputs used:"
                            f"<br>• Selected range: <b>{days_in_range:,} days</b>"
                            f"<br>• Annual labor value (scaled): <b>${annual_labor_value:,.0f}</b>"
                            f"<br>• Annual operating cost: <b>${total_roi_cost:,.0f}</b>"
                            f"<br><br>Math:"
                            f"<br>• Net run rate = annual labor value − annual operating cost"
                            f"<br>• <b>${annual_labor_value:,.0f} − ${total_roi_cost:,.0f} = ${net_roi_value:,.0f}</b>"
                            f"<br><br>This is a current performance estimate, not a guaranteed future outcome."
                        ),
                        "#10b981"
                    )
                    
                    render_explainer_card(
                        f"3. Break-even Status — {break_even_value}",
                        (
                            "This shows whether the AMH has already recovered its upfront purchase cost."
                            f"<br><br>Inputs used:"
                            f"<br>• Upfront cost: <b>${UPFRONT_COST:,.0f}</b>"
                            f"<br>• Current annual run rate: <b>${net_roi_value:,.0f}</b>"
                            f"<br>• Years since install: <b>{installed_years:,.1f}</b>"
                            f"<br><br>Math:"
                            f"<br>• Break-even years = upfront cost ÷ annual run rate"
                            f"<br>• Break-even point ≈ <b>{payback_months/12:,.1f} years</b>"
                            f"<br><br>Status:"
                            f"<br>• <b>{break_even_value}</b>"
                            f"<br>• {break_even_subtitle}"
                        ),
                        "#3b82f6"
                    )
                    
                    render_explainer_card(
                        f"4. Lifetime Value Generated — ${since_install_labor_value:,.0f}",
                        (
                            "This estimates the total labor value created by the AMH over its full lifetime."
                            f"<br><br>Inputs used:"
                            f"<br>• Annual labor value (current pace): <b>${annual_labor_value:,.0f}</b>"
                            f"<br>• Years since install: <b>{installed_years:,.1f}</b>"
                            f"<br><br>Math:"
                            f"<br>• Lifetime value = annual labor value × years since install"
                            f"<br>• <b>${annual_labor_value:,.0f} × {installed_years:,.1f} = ${since_install_labor_value:,.0f}</b>"
                            f"<br><br>This is an estimate using current performance applied across the system's lifespan."
                        ),
                        "#f59e0b"
                    )
                    st.markdown("### How to interpret Since Install metrics")
                    render_explainer_card(
                        f"5. Years Since Install — {installed_years:,.1f}",
                        (
                            "This is the amount of time between the install date and today."
                            f"<br><br>Inputs used:"
                            f"<br>• Install date: <b>{pd.to_datetime(INSTALL_DATE).strftime('%b %d, %Y')}</b>"
                            f"<br>• Years in service: <b>{installed_years:,.1f}</b>"
                        ),
                        "#6b7280"
                    )
            
                    render_explainer_card(
                        f"6. Since-Install Value — ${since_install_labor_value:,.0f}",
                        (
                            "This estimates total labor value created over the machine's time in service."
                            f"<br><br>Inputs used:"
                            f"<br>• Annual labor value: <b>${annual_labor_value:,.0f}</b>"
                            f"<br>• Years since install: <b>{installed_years:,.1f}</b>"
                            f"<br><br>Math:"
                            f"<br>• Since-install value = annual labor value × years since install"
                            f"<br>• <b>${since_install_labor_value:,.0f}</b>"
                        ),
                        "#3b82f6"
                    )
            
                    render_explainer_card(
                        f"7. Since-Install Net — ${since_install_net_value:,.0f}",
                        (
                            "This estimates total value since install after subtracting total cost since install."
                            f"<br><br>Inputs used:"
                            f"<br>• Since-install labor value: <b>${since_install_labor_value:,.0f}</b>"
                            f"<br>• Since-install total cost: <b>${since_install_total_cost:,.0f}</b>"
                            f"<br><br>Math:"
                            f"<br>• Since-install net = since-install labor value − since-install total cost"
                            f"<br>• <b>${since_install_labor_value:,.0f} − ${since_install_total_cost:,.0f} = ${since_install_net_value:,.0f}</b>"
                        ),
                        "#10b981"
                    )
            
                    render_explainer_card(
                        f"8. Since-Install ROI — {since_install_roi_display}",
                        (
                            "This compares since-install net value against since-install total cost."
                            f"<br><br>Inputs used:"
                            f"<br>• Since-install net value: <b>${since_install_net_value:,.0f}</b>"
                            f"<br>• Since-install total cost: <b>${since_install_total_cost:,.0f}</b>"
                            f"<br><br>Math:"
                            f"<br>• Since-install ROI = since-install net value ÷ since-install total cost × 100"
                            f"<br>• <b>{since_install_roi_display}</b>"
                        ),
                        "#7c3aed"
                    )
            
                else:
                    since_install_roi_display = f"{since_install_roi_pct:,.1f}%" if since_install_roi_pct is not None else "N/A"
            
                    render_explainer_card(
                        f"Range Length — {days_in_range:,} days",
                        (
                            "This is the exact selected date range used for the observed calculation."
                            f"<br><br>Inputs used:"
                            f"<br>• Start date: <b>{pd.to_datetime(start_date).strftime('%b %d, %Y')}</b>"
                            f"<br>• End date: <b>{pd.to_datetime(end_date).strftime('%b %d, %Y')}</b>"
                            f"<br>• Total days: <b>{days_in_range:,}</b>"
                            f"<br>• Equivalent months: <b>{months_in_range:,.2f}</b>"
                            f"<br>• Equivalent years: <b>{years_in_range:,.4f}</b>"
                            "<br><br>All observed values below use only this selected period."
                        ),
                        "#6b7280"
                    )
            
                    render_explainer_card(
                        f"Observed Labor Value — ${labor_value_saved:,.0f}",
                        (
                            "This is the estimated dollar value of staff time saved during the selected date range."
                            f"<br><br>Inputs used:"
                            f"<br>• Hourly labor cost setting (User provided): <b>${HOURLY_COST:,.2f}</b> per hour"
                            f"<br>• Calculated observed labor value: <b>${labor_value_saved:,.0f}</b>"
                            f"<br><br>Meaning:"
                            f"<br>• This is the labor value created by the AMH during just this selected period."
                        ),
                        "#3b82f6"
                    )
            
                    render_explainer_card(
                        f"Observed Operating Cost — ${observed_operating_cost:,.0f}",
                        (
                            "This is the prorated recurring operating cost for only the selected date range."
                            f"<br><br>Inputs used:"
                            f"<br>• Yearly recurring cost setting (User provided): <b>${YEARLY_COST:,.0f}</b>"
                            f"<br>• Monthly recurring cost setting (User provided): <b>${MONTHLY_COST:,.0f}</b>"
                            f"<br>• Selected days: <b>{days_in_range:,}</b>"
                            f"<br>• Equivalent months: <b>{months_in_range:,.2f}</b>"
                            f"<br>• Equivalent years: <b>{years_in_range:,.4f}</b>"
                            f"<br><br>Math:"
                            f"<br>• Monthly portion = ${MONTHLY_COST:,.0f} × {months_in_range:,.2f}"
                            f"<br>• Yearly portion = ${YEARLY_COST:,.0f} × {years_in_range:,.4f}"
                            f"<br><br>Final observed operating cost:"
                            f"<br>• <b>${observed_operating_cost:,.0f}</b>"
                            "<br><br>This does not include the original upfront purchase cost."
                        ),
                        "#f59e0b"
                    )
            
                    render_explainer_card(
                        f"Observed Net Value — ${observed_net_operating_value:,.0f}",
                        (
                            "This is the value left after subtracting observed operating cost from observed labor value."
                            f"<br><br>Inputs used:"
                            f"<br>• Observed labor value: <b>${labor_value_saved:,.0f}</b>"
                            f"<br>• Observed operating cost: <b>${observed_operating_cost:,.0f}</b>"
                            f"<br><br>Math:"
                            f"<br>• Observed net value = observed labor value − observed operating cost"
                            f"<br>• <b>${labor_value_saved:,.0f} − ${observed_operating_cost:,.0f} = ${observed_net_operating_value:,.0f}</b>"
                        ),
                        "#10b981"
                    )
            
                    render_explainer_card(
                        f"Years Since Install — {installed_years:,.1f}",
                        (
                            "This is the amount of time between the install date and today."
                            f"<br><br>Inputs used:"
                            f"<br>• Install date: <b>{pd.to_datetime(INSTALL_DATE).strftime('%b %d, %Y')}</b>"
                            f"<br>• Years in service: <b>{installed_years:,.1f}</b>"
                        ),
                        "#6b7280"
                    )
            
                    render_explainer_card(
                        f"Since-Install Value — ${since_install_labor_value:,.0f}",
                        (
                            "This estimates total labor value created over the machine's time in service."
                            f"<br><br>Inputs used:"
                            f"<br>• Annual labor value: <b>${annual_labor_value:,.0f}</b>"
                            f"<br>• Years since install: <b>{installed_years:,.1f}</b>"
                            f"<br><br>Final estimated since-install labor value:"
                            f"<br>• <b>${since_install_labor_value:,.0f}</b>"
                        ),
                        "#3b82f6"
                    )
            
                    render_explainer_card(
                        f"Since-Install Net — ${since_install_net_value:,.0f}",
                        (
                            "This estimates total value since install after subtracting total cost since install."
                            f"<br><br>Inputs used:"
                            f"<br>• Since-install labor value: <b>${since_install_labor_value:,.0f}</b>"
                            f"<br>• Since-install total cost: <b>${since_install_total_cost:,.0f}</b>"
                            f"<br><br>Math:"
                            f"<br>• Since-install net = since-install labor value − since-install total cost"
                            f"<br>• <b>${since_install_labor_value:,.0f} − ${since_install_total_cost:,.0f} = ${since_install_net_value:,.0f}</b>"
                        ),
                        "#10b981"
                    )
            
                    render_explainer_card(
                        f"Since-Install ROI — {since_install_roi_display}",
                        (
                            "This compares since-install net value against since-install total cost."
                            f"<br><br>Inputs used:"
                            f"<br>• Since-install net value: <b>${since_install_net_value:,.0f}</b>"
                            f"<br>• Since-install total cost: <b>${since_install_total_cost:,.0f}</b>"
                            f"<br><br>Math:"
                            f"<br>• Since-install ROI = since-install net value ÷ since-install total cost × 100"
                            f"<br>• <b>{since_install_roi_display}</b>"
                        ),
                        "#7c3aed"
                    )
    
                    st.markdown(f"""
    The following formulas are used to calculate the ROI metrics shown above:
    
    Observed labor value = Observed hours saved × Hourly labor cost  
    Observed operating cost = Observed monthly cost + Observed yearly cost  
    Observed net value = Observed labor value − Observed operating cost  
    
    Annual labor value = Observed labor value × (12 ÷ Equivalent months)  
    Annual cost = Yearly cost + (Monthly cost × 12)  
    Current annual run rate = Annual labor value − Annual cost  
    Break-even years = Upfront cost ÷ Current annual run rate  
    
    Since-install value = Annual labor value × Years since install  
    Since-install operating cost = Annual operating cost × Years since install  
    Since-install total cost = Upfront cost + Since-install operating cost  
    Since-install net = Since-install value − Since-install total cost  
    Since-install ROI = Since-install net ÷ Since-install total cost × 100  
    
    The formulas above are built on a sequence of dependent calculations. Each value is derived from the values before it:
    
    Observed Labor Value + Observed Operating Cost  
    → Observed Net Value  
    
    Observed Labor Value  
    → Annual Labor Value  
    
    Annual Labor Value + Annual Cost  
    → Current Annual Run Rate  
    → Break-even Status  
    
    Annual Labor Value + Years Since Install  
    → Since-Install Value  
    
    Since-Install Value + Since-Install Total Cost  
    → Since-Install Net  
    → Since-Install ROI  
    
    ---
    
    #### 1. Selected date range
    This ROI calculation uses data from the selected reporting window.
    
    Selected date range = **{start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}**
    
    Total days in range = **{days_in_range:,} days**
    
    Equivalent months = Total days in range ÷ 30.44 days/month  
    
    Equivalent months = {days_in_range:,} days ÷ 30.44 days/month  
    
    **Equivalent months = {months_in_range:,.2f} months**
    
    Equivalent years = Total days in range ÷ 365.25 days/year  
    
    Equivalent years = {days_in_range:,} days ÷ 365.25 days/year  
    
    **Equivalent years = {years_in_range:,.4f} years**
    
    ---
    
    #### 2. Observed labor value
    This is the observed dollar value of staff time avoided during the selected range.
    
    *Refer to How Staff Time Saved Is Calculated in Labor & Efficiency for the full calculation of observed hours saved.*
    
    Observed hours saved = **{observed_hours_saved:,.2f} hours**
    
    Hourly labor cost = **${HOURLY_COST:.2f}/hour**
    
    Observed labor value = Observed hours saved × Hourly labor cost  
    
    Observed labor value = {observed_hours_saved:,.2f} hours × ${HOURLY_COST:.2f}/hour  
    
    **Observed labor value = ${labor_value_saved:,.0f}**
    
    ---
    
    #### 3. Observed operating cost
    This is the recurring operating cost assigned only to the selected date range.
    
    Observed monthly cost = Monthly cost × Equivalent months  
    
    Observed monthly cost = ${MONTHLY_COST:,.2f}/month × {months_in_range:,.2f} months  
    
    **Observed monthly cost = ${MONTHLY_COST * months_in_range:,.0f}**
    
    Observed yearly cost = Yearly cost × Equivalent years  
    
    Observed yearly cost = ${YEARLY_COST:,.2f}/year × {years_in_range:,.4f} years  
    
    **Observed yearly cost = ${YEARLY_COST * years_in_range:,.0f}**
    
    Observed operating cost = Observed monthly cost + Observed yearly cost  
    
    Observed operating cost = ${MONTHLY_COST * months_in_range:,.0f} + ${YEARLY_COST * years_in_range:,.0f}  
    
    **Observed operating cost = ${observed_operating_cost:,.0f}**
    
    ---
    
    #### 4. Observed net value
    This is the remaining observed value after subtracting recurring operating cost for the selected range.
    
    Observed net value = Observed labor value − Observed operating cost  
    
    Observed net value = ${labor_value_saved:,.0f} − ${observed_operating_cost:,.0f}  
    
    **Observed net value = ${observed_net_operating_value:,.0f}**
    
    ---
    
    #### 5. Annual labor value
    This scales the observed labor value from the selected range to a 12-month equivalent.
    
    Annual labor value = Observed labor value × (12 ÷ Equivalent months)  
    
    Annual labor value = ${labor_value_saved:,.0f} × (12 ÷ {months_in_range:,.2f})  
    
    **Annual labor value = ${annual_labor_value:,.0f}/year**
    
    ---
    
    #### 6. Annual cost
    This is the full recurring operating cost for one year.
    
    Annual monthly cost = Monthly cost × 12 months  
    
    Annual monthly cost = ${MONTHLY_COST:,.2f}/month × 12 months  
    
    **Annual monthly cost = ${MONTHLY_COST * 12:,.0f}**
    
    Annual cost = Yearly cost + Annual monthly cost  
    
    Annual cost = ${YEARLY_COST:,.0f} + ${MONTHLY_COST * 12:,.0f}  
    
    **Annual cost = ${annual_operating_cost:,.0f}/year**
    
    ---
    
    #### 7. Current annual run rate
    This is the projected annual value left after subtracting annual recurring cost.
    
    Current annual run rate = Annual labor value − Annual cost  
    
    Current annual run rate = ${annual_labor_value:,.0f} − ${annual_operating_cost:,.0f}  
    
    **Current annual run rate = ${net_roi_value:,.0f}/year**
    
    ---
    
    #### 8. Break-even status
    This checks whether the current annual run rate is enough to recover the upfront cost, and whether that break-even point has already passed.
    
    Upfront cost = **${UPFRONT_COST:,.0f}**
    
    Break-even years = Upfront cost ÷ Current annual run rate  
    
    Break-even years = ${UPFRONT_COST:,.0f} ÷ ${net_roi_value:,.0f}
    """)
    
                    if payback_months is not None:
                        st.markdown(f"""
    **Break-even years = {payback_months / 12:,.1f} years**
    
    Years since install = **{installed_years:,.1f} years**
    
    Years past break-even = Years since install − Break-even years  
    
    Years past break-even = {installed_years:,.1f} − {payback_months / 12:,.1f}  
    
    **Break-even status = {break_even_value}**  
    {break_even_subtitle}
    """)
                    else:
                        st.markdown(f"""
    Break-even years cannot be calculated because the current annual run rate is not positive.
    
    Years since install = **{installed_years:,.1f} years**
    
    **Break-even status = Not Reached**
    """)
    
                    st.markdown(f"""
    ---
    
    #### 9. Years since install
    This is the elapsed time from the install date to today.
    
    Install date = **{pd.to_datetime(INSTALL_DATE).strftime('%b %d, %Y')}**
    
    Years since install = **{installed_years:,.1f} years**
    
    ---
    
    #### 10. Since-install value
    This is the total projected labor value across the machine's time in service.
    
    Since-install value = Annual labor value × Years since install  
    
    Since-install value = ${annual_labor_value:,.0f}/year × {installed_years:,.1f} years  
    
    **Since-install value = ${since_install_labor_value:,.0f}**
    
    ---
    
    #### 11. Since-install operating cost
    This is the recurring operating cost accumulated across the installed life.
    
    Since-install operating cost = Annual operating cost × Years since install  
    
    Since-install operating cost = ${annual_operating_cost:,.0f}/year × {installed_years:,.1f} years  
    
    **Since-install operating cost = ${since_install_operating_cost:,.0f}**
    """)
    
                    if INCLUDE_UPFRONT_IN_SINCE_INSTALL:
                        st.markdown(f"""
    ---
    
    #### 12. Since-install total cost
    This includes both recurring operating cost and the original upfront purchase cost.
    
    Since-install total cost = Upfront cost + Since-install operating cost  
    
    Since-install total cost = ${UPFRONT_COST:,.0f} + ${since_install_operating_cost:,.0f}  
    
    **Since-install total cost = ${since_install_total_cost:,.0f}**
    """)
                    else:
                        st.markdown(f"""
    ---
    
    #### 12. Since-install total cost
    This includes recurring operating cost only.
    
    Since-install total cost = Since-install operating cost  
    
    Since-install total cost = ${since_install_operating_cost:,.0f}  
    
    **Since-install total cost = ${since_install_total_cost:,.0f}**
    """)
    
                    st.markdown(f"""
    ---
    
    #### 13. Since-install net
    This is the remaining value after subtracting total cost since install.
    
    Since-install net = Since-install value − Since-install total cost  
    
    Since-install net = ${since_install_labor_value:,.0f} − ${since_install_total_cost:,.0f}  
    
    **Since-install net = ${since_install_net_value:,.0f}**
    
    ---
    
    #### 14. Since-install ROI
    This compares since-install net value against since-install total cost.
    
    Since-install ROI = Since-install net ÷ Since-install total cost × 100  
    
    Since-install ROI = ${since_install_net_value:,.0f} ÷ ${since_install_total_cost:,.0f} × 100  
    
    **Since-install ROI = {since_install_roi_pct:,.1f}%**
    """)

        

        else:
            if st.session_state.get("roi_calculated", False):
                st.info("No ROI data is available for the selected date range.")
            else:
                st.info("Enter your assumptions above, then click Calculate ROI.")

    with st.expander("Staff Time Equivalent", expanded=False):
        st.caption("Estimates staff time saved by comparing manual processing time against observed AMH processing time.")

        MANUAL_RATE = 45

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

            avg_daily_checkins = staff_df["checkins"].mean()
            avg_daily_manual_hours = staff_df["manual_hours"].mean()
            avg_daily_amh_hours = staff_df["amh_hours"].mean()
            avg_saved = staff_df["hours_saved"].mean()
            total_saved = staff_df["hours_saved"].sum()
            peak_day = staff_df.loc[staff_df["hours_saved"].idxmax()]
            labor_value_saved = total_saved * HOURLY_COST

            try:
                from report_export import build_director_report_pdf

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
                    peak_day_saved=float(peak_day["hours_saved"]),
                    peak_day_saved_date=pd.to_datetime(peak_day["date"]).strftime("%b %d, %Y"),
                    manual_rate=MANUAL_RATE,
                    amh_rate=AMH_RATE,
                    library_name="New Braunfels Public Library",
                    branch_name="Main Branch",
                    system_name="Tech Logic UltraSort",
                    report_title="AMH Director Report",
                    hourly_cost=HOURLY_COST,
                    roi_mode=roi_mode,
                    annual_cost=annual_operating_cost if roi_payload else None,
                    yearly_savings_after_cost=net_roi_value if roi_payload and roi_mode == "Annualized Projection" else None,
                    payback_months=payback_months if roi_payload and roi_mode == "Annualized Projection" else None,
                    since_install_net_value=since_install_net_value if roi_payload else None,
                    install_date=pd.to_datetime(INSTALL_DATE).strftime("%b %d, %Y") if roi_payload else None,
                )

                pdf_button_placeholder.download_button(
                    label="Download Director PDF",
                    data=director_pdf,
                    file_name=f"amh_director_report_{pd.to_datetime(start_date).strftime('%Y%m%d')}_{pd.to_datetime(end_date).strftime('%Y%m%d')}.pdf",
                    mime="application/pdf",
                    key="director_pdf_download"
                )
            except Exception as e:
                pdf_button_placeholder.warning(f"Director PDF export is temporarily unavailable: {e}")

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
                render_kpi_card(
                    "1. Avg Hours Saved",
                    f"{avg_saved:,.2f}",
                    "Per day Across selected date range",
                    "#6b7280"
                )
            
            with k2:
                render_kpi_card(
                    "2. Total Hours Saved",
                    f"{total_saved:,.2f}",
                    "Across selected date range",
                    "#6b7280"
                )
            
            with k3:
                render_kpi_card(
                    "3. Estimated Labor Value for selected date range",
                    f"${labor_value_saved:,.0f}",
                    "Staff time avoided value",
                    "#6b7280"
                )

    if len(df) > 0 and len(df_history_raw) > 0:
        with st.expander("How the Staff Time Equivalent KPIs are calculated", expanded=False):
            st.info(f"""
    ##### 1. Average Hours Saved
    
    Average Daily Check-ins
    = Total check-ins / Total days
    
    Average Daily Check-ins
    = {int(staff_df["checkins"].sum()):,} / {staff_df["date"].nunique():,}
    
    Average Daily Check-ins
    = {avg_daily_checkins:,.1f} items/day
    
    Average Daily Manual Time
    = Average Daily Check-ins / Manual processing rate
    
    Average Daily Manual Time
    = {avg_daily_checkins:,.1f} / {MANUAL_RATE:.1f}
    
    Average Daily Manual Time
    = {avg_daily_manual_hours:,.2f} staff hours/day
    
    Average Daily AMH Time
    = Average Daily Check-ins / AMH processing rate
    
    Average Daily AMH Time
    = {avg_daily_checkins:,.1f} / {AMH_RATE:,.1f}
    
    Average Daily AMH Time
    = {avg_daily_amh_hours:,.2f} machine hours/day
    
    Average Hours Saved per Day
    = Average Daily Manual Time - Average Daily AMH Time
    
    Average Hours Saved per Day
    = {avg_daily_manual_hours:,.2f} - {avg_daily_amh_hours:,.2f}
    
    Average Hours Saved per Day
    = {avg_saved:,.2f} staff hours/day
    
    ##### 2. Total Hours Saved
    
    Total Hours Saved
    = Average Hours Saved per Day * Total days
    
    Total Hours Saved
    = {avg_saved:,.2f} * {staff_df["date"].nunique():,}
    
    Total Hours Saved
    = {total_saved:,.2f} hours
    
    ##### 3. Estimated Labor Value
    
    Estimated Labor Value
    = Total Hours Saved * Hourly labor cost
    
    Estimated Labor Value
    = {total_saved:,.2f} * ${HOURLY_COST:.2f}
    
    Estimated Labor Value
    = ${labor_value_saved:,.0f}
    """)
    
        with st.expander("Processing rates and supporting methodology", expanded=False):
            st.info(f"""
    ### How Staff Time Saved Is Calculated
    
    The calculation flow is:
    
    Average Daily Check-ins
    -> Manual Processing Rate and AMH Processing Rate
    -> Manual Processing Time and AMH Processing Time
    -> Average Hours Saved per Day
    -> Total Hours Saved
    -> Estimated Labor Value
    
    #### Manual Processing Rate
    
    The manual processing rate is calculated using Westside circulation activity reports in TLC from four months:
    
    - March 2026
    - June 2025
    - August 2025
    - September 2025
    
    Each monthly sheet was processed using the same method.
    
    ##### Step 1: Group transactions into hourly activity
    
    For each sheet, every check-in transaction timestamp was converted into:
    
    - a calendar date
    - an hour of the day
    
    Transactions were then grouped by date and hour so that each day had an hourly check-in count.
    
    ##### Step 2: Find each day's peak operating threshold
    
    For each individual day in a monthly sheet:
    
    - the highest hourly check-in count for that day was identified
    - a peak threshold was calculated as 75% of that day's maximum hourly count
    
    Peak threshold = Daily maximum hourly check-ins * 0.75
    
    Only hours meeting or exceeding that threshold were counted as peak manual operating hours for that day.
    
    ##### Step 3: Sum peak-hour counts within each month
    
    The results by month were:
    
    March 2026
    - Peak manual check-ins = 2,343 items
    - Peak manual hours = 51 hours
    - Monthly manual rate = 2,343 / 51
    - Monthly manual rate = 45.94 items/hour
    
    June 2025
    - Peak manual check-ins = 2,000 items
    - Peak manual hours = 45 hours
    - Monthly manual rate = 2,000 / 45
    - Monthly manual rate = 44.44 items/hour
    
    August 2025
    - Peak manual check-ins = 3,058 items
    - Peak manual hours = 60 hours
    - Monthly manual rate = 3,058 / 60
    - Monthly manual rate = 50.97 items/hour
    
    September 2025
    - Peak manual check-ins = 2,627 items
    - Peak manual hours = 57 hours
    - Monthly manual rate = 2,627 / 57
    - Monthly manual rate = 46.09 items/hour
    
    ##### Step 4: Combine all monthly peak-hour data
    
    Combined peak manual check-ins
    = 2,343 + 2,000 + 3,058 + 2,627
    = 10,028 items
    
    Combined peak manual hours
    = 51 + 45 + 60 + 57
    = 213 hours
    
    Manual processing rate
    = Combined peak manual check-ins / Combined peak manual hours
    = 10,028 / 213
    = {MANUAL_RATE:.1f} items/hour
    
    #### AMH Processing Rate
    
    The AMH processing rate is calculated from AMH check-in history within the currently selected date range shown in the report.
    
    ##### Step 1: Group AMH activity into hourly throughput
    
    AMH check-ins are grouped by:
    
    - date
    - hour
    
    This creates an hourly item count for each day in the selected range.
    
    ##### Step 2: Build the AMH hourly average profile
    
    Those daily hourly counts are then averaged by hour of day to estimate the machine's typical throughput at each hour.
    
    ##### Step 3: Identify peak machine operating hours
    
    From that hourly AMH profile:
    
    - the highest observed hourly average is identified
    - a peak threshold is calculated at 75% of that maximum
    
    Highest observed AMH hourly average = {peak_row["avg_items_per_hour"]:,.1f} items/hour
    
    Peak AMH threshold
    = {peak_row["avg_items_per_hour"]:,.1f} * 0.75
    = {threshold:,.1f} items/hour
    
    Only AMH hours meeting or exceeding that threshold are used in the final AMH rate.
    
    ##### Step 4: Compute AMH processing rate
    
    AMH processing rate = {AMH_RATE:,.1f} items/hour
    
    #### Supporting Daily Inputs
    
    Average Daily Check-ins
    = Total check-ins / Total days
    = {int(staff_df["checkins"].sum()):,} / {staff_df["date"].nunique():,}
    = {avg_daily_checkins:,.1f} items/day
    
    Daily Manual Time
    = Average Daily Check-ins / Manual processing rate
    = {avg_daily_checkins:,.1f} / {MANUAL_RATE:.1f}
    = {avg_daily_manual_hours:,.2f} staff hours/day
    
    Daily AMH Time
    = Average Daily Check-ins / AMH processing rate
    = {avg_daily_checkins:,.1f} / {AMH_RATE:,.1f}
    = {avg_daily_amh_hours:,.2f} machine hours/day
    """)
    else:
        st.info("No labor data is available for the selected date range.")




    
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
            throughput_df["day_of_week"] = throughput_df["datetime"].dt.day_name()
    
            # ===== BUILD HOURLY =====
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
    
            avg_hourly_chart_df = avg_hourly[(avg_hourly["hour"] >= 7) & (avg_hourly["hour"] <= 20)].copy()
    
            if len(avg_hourly) > 0:
                busiest_hour_row = avg_hourly.loc[avg_hourly["avg_items_per_hour"].idxmax()]
                st.subheader("Average Checkins per Hour")
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
                            Hourly Summary
                        </div>
                        <div style="color: #4b5563; line-height: 1.4;">
                            Busiest average hour: {busiest_hour_row["hour_label"]} at
                            {busiest_hour_row["avg_items_per_hour"]:,.1f} checkins per hour.
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            
            throughput_chart = build_hourly_bar_chart(
                avg_hourly_chart_df,
                "avg_items_per_hour",
                "Avg Checkins Per Hour"
            )
            render_chart(throughput_chart)
            
            display_df = avg_hourly_chart_df.rename(columns={
                "hour_label": "Hour",
                "avg_items_per_hour": "Avg Checkins Per Hour"
            })[["Hour", "Avg Checkins Per Hour"]]
            
            display_df["Avg Checkins Per Hour"] = display_df["Avg Checkins Per Hour"].round(1)
            
            st.dataframe(display_df, use_container_width=True)
            download_button(display_df, "throughput_report.csv")
    
            # ===== WEEKDAY SECTION =====
            st.subheader("Average Checkins per Day by Weekday")
    
            weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    
            weekday_daily = (
                throughput_df.groupby(["date", "day_of_week"])
                .size()
                .reset_index(name="daily_checkins")
            )
    
            weekday_avg = (
                weekday_daily.groupby("day_of_week")["daily_checkins"]
                .mean()
                .reindex(weekday_order)
                .fillna(0)
                .reset_index(name="avg_checkins_per_day")
            )
    
            busiest_weekday_row = weekday_avg.loc[weekday_avg["avg_checkins_per_day"].idxmax()]
    
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
                        Weekday Summary
                    </div>
                    <div style="color: #4b5563; line-height: 1.4;">
                        Busiest average weekday: {busiest_weekday_row["day_of_week"]} at
                        {busiest_weekday_row["avg_checkins_per_day"]:,.1f} checkins per day.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
    
            weekday_chart = build_category_bar_chart(
                weekday_avg,
                "day_of_week",
                "avg_checkins_per_day",
                "Avg Checkins Per Day",
                "Day of Week"
            )
            render_chart(weekday_chart)
    
            weekday_display = weekday_avg.rename(columns={
                "day_of_week": "Day of Week",
                "avg_checkins_per_day": "Avg Checkins Per Day"
            })[["Day of Week", "Avg Checkins Per Day"]]
    
            weekday_display["Avg Checkins Per Day"] = weekday_display["Avg Checkins Per Day"].round(1)
    
            st.dataframe(weekday_display, use_container_width=True)
            download_button(weekday_display, "throughput_by_weekday_report.csv")
    
        else:
            st.info("No throughput data available for the selected date range.")

    with st.expander("Today vs Typical Hourly Pattern", expanded=False):
    
        if "datetime" in df_live_raw.columns:
            today_df_report = df_live_raw[df_live_raw["datetime"].dt.date == today].copy()
        else:
            today_df_report = pd.DataFrame()
    
        if "datetime" in df_history_raw.columns:
            historical_df_report = df_history_raw[df_history_raw["datetime"].dt.date < today].copy()
        else:
            historical_df_report = pd.DataFrame()
    
        if "datetime" in today_df_report.columns and len(today_df_report) > 0:
            today_hourly = today_df_report["datetime"].dt.hour.value_counts().sort_index()
        else:
            today_hourly = pd.Series(dtype=float)
    
        if "datetime" in historical_df_report.columns and len(historical_df_report) > 0 and historical_df_report["datetime"].dt.date.nunique() > 0:
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
        
        destination_counts = (
            df["destination_report"]
            .value_counts()
            .reset_index()
        )
        destination_counts.columns = ["destination", "count"]
        destination_counts = destination_counts.sort_values("count", ascending=False)
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

    with st.expander("Top Issues", expanded=False):
        

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
                (df["destination_clean"] == "Library Express").sum()
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

            annual_col1, annual_col2 = st.columns(2)
            
            annual_roi_payload = build_roi_payload(df, df_history_raw, start_date, end_date)
            
            with annual_col1:
                annual_labor_value_text = "N/A"
                if annual_roi_payload and annual_roi_payload.get("annual_labor_value") is not None:
                    annual_labor_value_text = f"${annual_roi_payload['annual_labor_value']:,.0f}"
            
                render_kpi_card(
                    "Annual Labor Value",
                    annual_labor_value_text,
                    "Projected yearly labor value",
                    "#6b7280"
                )
            
            with annual_col2:
                annual_operating_cost_text = "N/A"
                if annual_roi_payload and annual_roi_payload.get("annual_operating_cost") is not None:
                    annual_operating_cost_text = f"${annual_roi_payload['annual_operating_cost']:,.0f}"
            
                render_kpi_card(
                    "Annual Operating Cost",
                    annual_operating_cost_text,
                    "Monthly + yearly recurring cost",
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
        
        base_df["destination_clean"] = base_df["destination"].astype(str).str.strip()
        base_df["transit_destination"] = base_df["destination"].apply(normalize_transit_destination)
        
        base_df["destination_report"] = base_df["destination_clean"].copy()
        base_df.loc[base_df["destination_report"] == "1", "destination_report"] = "Main"
        base_df.loc[base_df["transit_destination"] == "Westside", "destination_report"] = "Westside"
        base_df.loc[base_df["transit_destination"] == "Library Express", "destination_report"] = "Library Express"
        
        base_df["destination_clean"] = base_df["destination_report"]

    st.caption(f"Showing: {date_label}")

    valid_transit_destinations = [
        "Westside",
        "Library Express",
    ]
    
    if len(base_df) > 0 and "datetime" in base_df.columns:
        base_df = base_df.copy()
        base_df["date"] = base_df["datetime"].dt.date
        base_df["day_of_week"] = base_df["datetime"].dt.day_name()
        base_df["destination_clean"] = base_df["destination"].astype(str).str.strip()
        base_df["transit_destination"] = base_df["destination"].apply(normalize_transit_destination)
    
        base_df["destination_report"] = base_df["destination_clean"].copy()
        base_df.loc[base_df["destination_report"] == "1", "destination_report"] = "Main"
        base_df.loc[base_df["transit_destination"] == "Westside", "destination_report"] = "Westside"
        base_df.loc[base_df["transit_destination"] == "Library Express", "destination_report"] = "Library Express"
    
        base_df["destination_clean"] = base_df["destination_report"]
    else:
        base_df = pd.DataFrame({
            "datetime": pd.Series(dtype="datetime64[ns]"),
            "date": pd.Series(dtype="object"),
            "day_of_week": pd.Series(dtype="object"),
            "destination_clean": pd.Series(dtype="object"),
            "transit_destination": pd.Series(dtype="object"),
            "destination_report": pd.Series(dtype="object"),
        })


    if len(base_rejects_df) > 0 and "datetime" in base_rejects_df.columns:
        base_rejects_df = base_rejects_df.copy()
        base_rejects_df["date"] = base_rejects_df["datetime"].dt.date
        base_rejects_df["day_of_week"] = base_rejects_df["datetime"].dt.day_name()
    else:
        base_rejects_df = pd.DataFrame({
            "datetime": pd.Series(dtype="datetime64[ns]"),
            "date": pd.Series(dtype="object"),
            "day_of_week": pd.Series(dtype="object"),
        })
        
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
        base_df["destination_clean"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False).sum()
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

    transit1, transit2, transit3, transit4 = st.columns(4)
    
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
                base_df["destination_clean"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
            ].groupby(
                base_df[
                    base_df["destination_clean"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
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

        st.subheader("Problem Items Deep Dive")
        st.caption("Looks at items with missing destination routing to highlight system or configuration issues.")
        no_agency_df = base_df[
            base_df["destination_clean"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
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
        
            historical_df["destination_clean"] = historical_df["destination"].astype(str).str.strip()
            historical_df["transit_destination"] = historical_df["destination_clean"].apply(normalize_transit_destination)
        
            historical_df["destination_report"] = historical_df["destination_clean"].copy()
            historical_df.loc[historical_df["destination_report"] == "1", "destination_report"] = "Main"
            historical_df.loc[historical_df["transit_destination"] == "Westside", "destination_report"] = "Westside"
            historical_df.loc[historical_df["transit_destination"] == "Library Express", "destination_report"] = "Library Express"
        
            historical_df["destination_clean"] = historical_df["destination_report"]
        
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
