# Author: Richard Hanly
# app.py
# Streamlit dashboard for AMH analytics
# Displays item flow, routing, rejects, and transit diagnostics in a web interface

import streamlit as st
import pandas as pd
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh
import json

from views.live_today_view import render_live_today
from views.overview_view import render_overview
from views.reports_view import render_reports
from views.transits_view import render_transits
from services.settings_service import load_app_settings
from services.filters_service import resolve_date_filters

from data_loader import (
    load_checkins_df,
    load_checkins_history_df,
    load_rejects_df,
    load_rejects_history_df,
    load_pipeline_status,
    load_acs_df,
    load_acs_history_df,
)

from dashboard_context import build_dashboard_context

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

app_settings = load_app_settings(SETTINGS_FILE)

LIBRARY_SETTINGS = app_settings["LIBRARY_SETTINGS"]
TRANSIT_SETTINGS = app_settings["TRANSIT_SETTINGS"]
INTERNAL_ROUTING = app_settings["INTERNAL_ROUTING"]

LIBRARY_NAME = app_settings["LIBRARY_NAME"]
BRANCH_NAME = app_settings["BRANCH_NAME"]
SYSTEM_NAME = app_settings["SYSTEM_NAME"]

TRANSIT_HOME_LABEL = app_settings["TRANSIT_HOME_LABEL"]
TRANSIT_DESTINATIONS = app_settings["TRANSIT_DESTINATIONS"]
ENABLED_TRANSIT_DESTINATIONS = app_settings["ENABLED_TRANSIT_DESTINATIONS"]
TRANSIT_LABELS = app_settings["TRANSIT_LABELS"]

BRANCH_SERVICES_NAMES = app_settings["BRANCH_SERVICES_NAMES"]
COLLECTION_SERVICES_NAMES = app_settings["COLLECTION_SERVICES_NAMES"]
BRANCH_SERVICES_DA_PATTERNS = app_settings["BRANCH_SERVICES_DA_PATTERNS"]
COLLECTION_SERVICES_DA_PATTERNS = app_settings["COLLECTION_SERVICES_DA_PATTERNS"]

def is_operating_hours(now_ct: datetime) -> bool:
    return 6 <= now_ct.hour < 21

now_ct = datetime.now(APP_TZ)

refresh_count = 0
if is_operating_hours(now_ct):
    refresh_count = st_autorefresh(
        interval=10 * 60 * 1000,
        key="sortview_auto_refresh"
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

if "last_refresh_count" not in st.session_state:
    st.session_state["last_refresh_count"] = refresh_count

auto_refresh_triggered = refresh_count != st.session_state["last_refresh_count"]

if auto_refresh_triggered:
    st.cache_data.clear()
    st.session_state["last_refresh_count"] = refresh_count

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

selected_view = st.segmented_control(
    "Section",
    options=["Live Today", "Transits", "Reports", "Overview"],
    default="Live Today",
    label_visibility="collapsed"
)

local_today = datetime.now(APP_TZ).date()

start_date, end_date = resolve_date_filters(
    selected_view=selected_view,
    min_date=min_date,
    max_date=max_date,
    local_today=local_today,
)

theme_base = st.get_option("theme.base") or "light"
today = datetime.now(APP_TZ).date()
now_ct = datetime.now(APP_TZ)

context = build_dashboard_context(
    df_live_raw=df_live_raw,
    df_history_raw=df_history_raw,
    rejects_live_raw=rejects_live_raw,
    rejects_history_raw=rejects_history_raw,
    acs_live_raw=acs_live_raw,
    acs_history_raw=acs_history_raw,
    pipeline_status=pipeline_status,
    refresh_count=refresh_count,
    start_date=start_date,
    end_date=end_date,
    today=today,
    now_ct=now_ct,
    app_tz=APP_TZ,
    transit_labels=TRANSIT_LABELS,
    transit_home_label=TRANSIT_HOME_LABEL,
    branch_services_names=BRANCH_SERVICES_NAMES,
    collection_services_names=COLLECTION_SERVICES_NAMES,
    branch_services_da_patterns=BRANCH_SERVICES_DA_PATTERNS,
    collection_services_da_patterns=COLLECTION_SERVICES_DA_PATTERNS,
    library_name=LIBRARY_NAME,
    branch_name=BRANCH_NAME,
    system_name=SYSTEM_NAME,
    theme_base=theme_base,
)

if context["no_today_data"]:
    st.info("No checkins have been ingested yet for today. Live dashboard is showing the current day only.")

if selected_view == "Live Today":
    render_live_today(**context["live_today_args"])

if selected_view == "Overview":
    render_overview(**context["overview_args"])

if selected_view == "Reports":
    render_reports(**context["reports_args"])

if selected_view == "Transits":
    render_transits(**context["transits_args"])
