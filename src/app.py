# Author: Richard Hanly
# app.py
# Streamlit dashboard for AMH analytics
# Displays item flow, routing, rejects, and transit diagnostics in a web interface

import streamlit as st
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh

from views.live_today_view import render_live_today
from views.overview_view import render_overview
from views.reports_view import render_reports
from views.transits_view import render_transits
from services.settings_service import load_runtime_settings
from services.filters_service import resolve_date_filters
from services.app_ui_service import apply_page_chrome, render_app_header
from services.auth_service import authenticate_user
from services.access_service import (
    get_user_memberships,
    user_can_access_org,
    get_org_branches,
)
from services.entitlement_service import build_entitlement_context, feature_enabled
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

if "auth_user" not in st.session_state:
    st.session_state["auth_user"] = None

APP_TZ = ZoneInfo("America/Chicago")

if st.session_state["auth_user"] is None:
    st.title("SortView Login")

    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log In")

    if submitted:
        user = authenticate_user(email=email, password=password)
        if user:
            st.session_state["auth_user"] = user
            st.rerun()
        else:
            st.error("Invalid email or password.")

    st.stop()

SETTINGS_FILE = Path(__file__).parent / "branch_settings.json"

auth_user = st.session_state["auth_user"]
user_memberships = get_user_memberships(auth_user["id"])

if not user_memberships:
    st.error("Your account does not have access to any organizations.")
    with st.sidebar:
        st.caption(auth_user["email"])
        if st.button("Log out"):
            st.session_state["auth_user"] = None
            st.rerun()
    st.stop()

allowed_org_slugs = [m["organization_slug"] for m in user_memberships]

if "selected_org_slug" not in st.session_state or st.session_state["selected_org_slug"] not in allowed_org_slugs:
    st.session_state["selected_org_slug"] = allowed_org_slugs[0]

selected_org_slug = st.session_state["selected_org_slug"]

org_options = {
    m["organization_name"]: m["organization_slug"]
    for m in user_memberships
}

selected_org_name = next(
    name for name, slug in org_options.items()
    if slug == selected_org_slug
)

branch_rows = get_org_branches(selected_org_slug)

if not branch_rows:
    st.error("No active branches were found for this organization.")
    with st.sidebar:
        st.caption(auth_user["email"])
        if st.button("Log out"):
            st.session_state["auth_user"] = None
            st.rerun()
    st.stop()

allowed_branch_slugs = [b["branch_slug"] for b in branch_rows]

if "selected_branch_slug" not in st.session_state or st.session_state["selected_branch_slug"] not in allowed_branch_slugs:
    primary_branch = next((b for b in branch_rows if b["is_primary"]), None)
    st.session_state["selected_branch_slug"] = (
        primary_branch["branch_slug"] if primary_branch else allowed_branch_slugs[0]
    )

selected_branch_slug = st.session_state["selected_branch_slug"]

branch_options = {
    b["branch_name"]: b["branch_slug"]
    for b in branch_rows
}

selected_branch_name = next(
    name for name, slug in branch_options.items()
    if slug == selected_branch_slug
)

with st.sidebar:
    st.caption(auth_user["email"])

    if len(org_options) > 1:
        new_org_name = st.selectbox(
            "Organization",
            options=list(org_options.keys()),
            index=list(org_options.values()).index(selected_org_slug),
        )
        new_org_slug = org_options[new_org_name]

        if new_org_slug != st.session_state["selected_org_slug"]:
            st.session_state["selected_org_slug"] = new_org_slug
            st.session_state.pop("selected_branch_slug", None)
            st.rerun()

    if len(branch_options) > 1:
        new_branch_name = st.selectbox(
            "Branch",
            options=list(branch_options.keys()),
            index=list(branch_options.values()).index(selected_branch_slug),
        )
        new_branch_slug = branch_options[new_branch_name]

        if new_branch_slug != st.session_state["selected_branch_slug"]:
            st.session_state["selected_branch_slug"] = new_branch_slug
            st.rerun()

    if st.button("Log out"):
        st.session_state["auth_user"] = None
        st.session_state.pop("selected_org_slug", None)
        st.session_state.pop("selected_branch_slug", None)
        st.rerun()

selected_org_slug = st.session_state["selected_org_slug"]
selected_branch_slug = st.session_state["selected_branch_slug"]

entitlement_context = build_entitlement_context(
    user_id=auth_user["id"],
    org_slug=selected_org_slug,
)

if not user_can_access_org(auth_user["id"], selected_org_slug):
    st.error("You do not have access to this organization.")
    st.stop()

app_settings = load_runtime_settings(
    settings_file=SETTINGS_FILE,
    org_slug=selected_org_slug,
    branch_slug=selected_branch_slug,
    prefer_database=True,
)

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

apply_page_chrome()

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

if len(df_history_raw) == 0 or "datetime" not in df_history_raw.columns:
    render_app_header(
        library_name=LIBRARY_NAME,
        branch_name=BRANCH_NAME,
        system_name=SYSTEM_NAME,
    )
    st.warning("No historical checkin data is available yet.")
    st.stop()


min_date = df_history_raw["datetime"].min().date()
max_date = df_history_raw["datetime"].max().date()

render_app_header(
    library_name=LIBRARY_NAME,
    branch_name=BRANCH_NAME,
    system_name=SYSTEM_NAME,
)

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
now_ct = datetime.now(APP_TZ)
today = now_ct.date()

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
