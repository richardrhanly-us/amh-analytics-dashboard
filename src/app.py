#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         app.py
#
#  Description: Main Streamlit entry point for the SortView AMH
#               analytics dashboard. This file controls page setup,
#               authentication, organization and branch selection,
#               permissions, runtime settings, data loading, dashboard
#               context creation, and view routing.
#
#***************************************************************

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from dashboard_context import build_dashboard_context
from data_loader import (
    load_acs_df,
    load_acs_history_df,
    load_checkins_df,
    load_checkins_history_df,
    load_pipeline_status,
    load_rejects_df,
    load_rejects_history_df,
    validate_tenant_schema,
)
from services import auth_service
from services.access_service import (
    get_org_branches,
    get_user_memberships,
    user_can_access_org,
)
from services.app_ui_service import apply_page_chrome, render_app_header
from services.entitlement_service import build_entitlement_context
from services.filters_service import resolve_date_filters
from services.permission_service import (
    can_export,
    can_manage_settings,
    can_view_advanced_reports,
    can_view_internal_workflow,
    can_view_transits,
)
from services.readiness_service import get_branch_readiness
from services.settings_service import load_runtime_settings
from services.sidebar_service import render_main_sidebar
from views.live_today_view import render_live_today
from views.overview_view import render_overview
from views.reports_view import render_reports
from views.transits_view import render_transits

#***************************************************************
# Page Configuration and Global Setup
#
# Sets the Streamlit page title, icon, layout, sidebar behavior,
# custom page styling, session state defaults, and application
# timezone.
#***************************************************************

st.set_page_config(
    page_title="SortView",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed"
)

apply_page_chrome()

if "auth_user" not in st.session_state:
    st.session_state["auth_user"] = None

APP_TZ = ZoneInfo("America/Chicago")


#***************************************************************
# Guest Auto-Login
#
# Visiting the app with a "guest" URL query parameter (used by the
# public demo link) signs the visitor straight in as the guest
# account, skipping the login form entirely. The plain URL with no
# query parameter is unaffected, so real accounts keep logging in
# normally. Falls through to the regular login form if guest
# authentication fails for any reason (e.g. the account is
# deactivated).
#***************************************************************

GUEST_EMAIL = os.getenv("SORTVIEW_GUEST_EMAIL", "guest@gmail.com")
GUEST_PASSWORD = os.getenv("SORTVIEW_GUEST_PASSWORD", "guest")

if st.session_state["auth_user"] is None and "guest" in st.query_params:
    guest_result = auth_service.authenticate_user(email=GUEST_EMAIL, password=GUEST_PASSWORD)
    if guest_result["ok"]:
        st.session_state["auth_user"] = guest_result["user"]
        st.rerun()


#***************************************************************
# Authentication Check
#
# Displays the login form when no authenticated user exists.
# If authentication succeeds, the authenticated user is saved in
# session state and the app is rerun.
#***************************************************************

if st.session_state["auth_user"] is None:
    st.title("SortView Login")

    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log In")

    if submitted:
        result = auth_service.authenticate_user(email=email, password=password)
        if result["ok"]:
            st.session_state["auth_user"] = result["user"]
            st.rerun()
        else:
            st.error(result["message"])

    st.stop()


#***************************************************************
# Authenticated User and Settings File Setup
#
# Defines the branch settings file path and loads the authenticated
# user from session state.
#***************************************************************

SETTINGS_FILE = Path(__file__).parent / "branch_settings.json"

auth_user = st.session_state["auth_user"]
user_memberships = get_user_memberships(auth_user["id"])


#***************************************************************
# Organization Membership Validation
#
# Stops the app if the authenticated account does not belong to
# any organizations.
#***************************************************************

if not user_memberships:
    st.error("Your account does not have access to any organizations.")
    with st.sidebar:
        st.caption(auth_user["email"])
        if st.button("Log out"):
            st.session_state["auth_user"] = None
            st.rerun()
    st.stop()


#***************************************************************
# Organization Selection
#
# Builds the list of organizations the user can access and ensures
# the selected organization stored in session state is valid.
#***************************************************************

allowed_org_slugs = [m["organization_slug"] for m in user_memberships]

if (
    "selected_org_slug" not in st.session_state
    or st.session_state["selected_org_slug"] not in allowed_org_slugs
):
    st.session_state["selected_org_slug"] = allowed_org_slugs[0]

selected_org_slug = st.session_state["selected_org_slug"]

org_options = {
    m["organization_name"]: m["organization_slug"]
    for m in user_memberships
}


#***************************************************************
# Branch Selection
#
# Loads active branches for the selected organization and ensures
# the selected branch stored in session state is valid. If possible,
# the primary branch is selected by default.
#***************************************************************

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

if (
    "selected_branch_slug" not in st.session_state
    or st.session_state["selected_branch_slug"] not in allowed_branch_slugs
):
    primary_branch = next((b for b in branch_rows if b["is_primary"]), None)
    st.session_state["selected_branch_slug"] = (
        primary_branch["branch_slug"] if primary_branch else allowed_branch_slugs[0]
    )

selected_branch_slug = st.session_state["selected_branch_slug"]


#***************************************************************
# Tenant Mapping
#
# Finds the selected membership and branch records, then extracts
# the operational customer and branch IDs used by the data loaders.
#***************************************************************

selected_membership = next(
    (m for m in user_memberships if m["organization_slug"] == selected_org_slug),
    None,
)

selected_branch_row = next(
    (b for b in branch_rows if b["branch_slug"] == selected_branch_slug),
    None,
)

selected_customer_id = None
if selected_membership is not None:
    selected_customer_id = selected_membership.get("customer_id")

selected_branch_id = None
if selected_branch_row is not None:
    selected_branch_id = selected_branch_row.get("branch_id")


branch_options = {
    b["branch_name"]: b["branch_slug"]
    for b in branch_rows
}


#***************************************************************
# Organization Access Check
#
# Confirms the authenticated user still has access to the selected
# organization. If access is denied, the app stops and offers a
# logout option.
#***************************************************************

if not user_can_access_org(auth_user["id"], selected_org_slug):
    st.error("You do not have access to this organization.")
    with st.sidebar:
        st.caption(auth_user["email"])
        if st.button("Log out"):
            auth_service.log_auth_event(
                event_type="logout",
                is_success=True,
                user_id=auth_user["id"],
                email=auth_user["email"],
                message="User logged out.",
                metadata={
                    "selected_org_slug": st.session_state.get("selected_org_slug"),
                    "selected_branch_slug": st.session_state.get("selected_branch_slug"),
                },
            )
            st.session_state["auth_user"] = None
            st.session_state.pop("selected_org_slug", None)
            st.session_state.pop("selected_branch_slug", None)
            st.rerun()
    st.stop()


#***************************************************************
# Entitlement and Permission Setup
#
# Builds the user's entitlement context and converts it into feature
# flags used to control access to admin tools, exports, advanced
# reports, transits, and internal workflow details.
#***************************************************************

entitlement_context = build_entitlement_context(
    user_id=auth_user["id"],
    org_slug=selected_org_slug,
)

show_admin_button = can_manage_settings(entitlement_context)
reports_can_export = can_export(entitlement_context)
reports_can_advanced = can_view_advanced_reports(entitlement_context)
show_transits_tab = can_view_transits(entitlement_context)
show_internal_workflow = can_view_internal_workflow(entitlement_context)

show_header_admin_button = False


#***************************************************************
# Sidebar Rendering
#
# Displays the main sidebar controls for the authenticated user,
# including organization selection, branch selection, and admin
# access when permitted.
#***************************************************************

render_main_sidebar(
    auth_user=auth_user,
    entitlement_context=entitlement_context,
    org_options=org_options,
    selected_org_slug=selected_org_slug,
    branch_options=branch_options,
    selected_branch_slug=selected_branch_slug,
    show_admin_button=show_admin_button,
)


#***************************************************************
# Session State Refresh After Sidebar Rendering
#
# Reloads the currently selected organization and branch from
# session state in case the sidebar changed either value.
#***************************************************************

selected_org_slug = st.session_state["selected_org_slug"]
selected_branch_slug = st.session_state["selected_branch_slug"]


#***************************************************************
# Runtime Settings
#
# Loads organization and branch-specific settings used throughout
# the dashboard, including library labels, transit settings,
# routing rules, and display names.
#***************************************************************

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
TRANSIT_LABELS = list(dict.fromkeys(app_settings["TRANSIT_LABELS"]))

BRANCH_SERVICES_NAMES = app_settings["BRANCH_SERVICES_NAMES"]
COLLECTION_SERVICES_NAMES = app_settings["COLLECTION_SERVICES_NAMES"]
BRANCH_SERVICES_DA_PATTERNS = app_settings["BRANCH_SERVICES_DA_PATTERNS"]
COLLECTION_SERVICES_DA_PATTERNS = app_settings["COLLECTION_SERVICES_DA_PATTERNS"]


#***************************************************************
# Tenant Schema Validation
#
# Verifies that required tenant database tables and columns exist.
# If validation fails, the dashboard stops before attempting to
# load or display incomplete data.
#***************************************************************

schema_errors = validate_tenant_schema()

if schema_errors:
    render_app_header(
        library_name=LIBRARY_NAME,
        branch_name=BRANCH_NAME,
        system_name=SYSTEM_NAME,
        show_admin_button=show_header_admin_button,
    )
    st.error("Tenant schema validation failed.")

    for err in schema_errors:
        st.write(f"Table `{err['table']}` is missing: {', '.join(err['missing'])}")

    st.stop()


#***************************************************************
# Branch Readiness Check
#
# Confirms the selected branch is configured and ready for dashboard
# use. If setup is incomplete, the app displays the readiness message
# and stops before loading analytics data.
#***************************************************************

readiness = get_branch_readiness(
    org_slug=selected_org_slug,
    branch_slug=selected_branch_slug,
)

if not readiness["is_ready"]:
    render_app_header(
        library_name=LIBRARY_NAME,
        branch_name=BRANCH_NAME,
        system_name=SYSTEM_NAME,
        show_admin_button=show_header_admin_button,
    )
    st.info(readiness["message"])

    if show_admin_button:
        st.caption(f"Readiness code: {readiness['code']}")

    st.stop()


#***************************************************************
# Operational Tenant Mapping Validation
#
# Ensures the selected organization and branch can be mapped to the
# IDs required by the operational data layer.
#***************************************************************

if selected_customer_id is None or selected_branch_id is None:
    st.error("Operational tenant mapping is missing for the selected organization or branch.")
    st.stop()


#***************************************************************
#
#  Function:     is_operating_hours
#
#  Description: Determines whether the current Central Time value
#               falls within the dashboard's active operating window.
#               Auto-refresh is only enabled during these hours to
#               reduce unnecessary refreshes outside normal use.
#
#  Parameters:  now_ct - Current datetime in the application timezone.
#
#  Returns:     bool - True if the time is between 6:00 AM and
#               8:59 PM Central Time; otherwise False.
#
#***************************************************************

def is_operating_hours(now_ct: datetime) -> bool:
    return 6 <= now_ct.hour < 21


#***************************************************************
# Auto Refresh Handling
#
# Enables automatic dashboard refresh during operating hours. The
# refresh_count value is passed into the cached data loaders below as
# part of their cache key, so each auto-refresh cycle naturally picks
# up fresh data without needing to wipe the shared cache. A manual
# st.cache_data.clear() used to run here on every cycle, but that
# clears cached data for every tenant/session on the server, not just
# this one -- with more than one active session, that made the shared
# cache get wiped far more often than once per refresh interval. The
# loaders' own TTLs (data_loader.py) already bound staleness to how
# often the AMH pipeline actually updates.
#***************************************************************

now_ct = datetime.now(APP_TZ)

refresh_count = 0
if is_operating_hours(now_ct):
    refresh_count = st_autorefresh(
        interval=10 * 60 * 1000,
        key="sortview_auto_refresh"
    )


#***************************************************************
# Pipeline Status Loading
#
# Loads the most recent SortView agent pipeline status for the
# selected tenant and branch. The status timestamp is later used to
# help invalidate cached data when new uploads arrive.
#***************************************************************

pipeline_status = load_pipeline_status(
    org_slug=selected_customer_id,
    branch_slug=selected_branch_id,
    mtime=None,
    refresh_count=refresh_count,
)

status_mtime = "0"
if pipeline_status:
    status_updated_at = pipeline_status.get("updated_at")
    if status_updated_at:
        status_mtime = str(status_updated_at)


#***************************************************************
# Dashboard Data Loading
#
# Loads live and historical data for checkins, rejects, and ACS
# activity. These dataframes provide the raw input for the dashboard
# context and individual dashboard views.
#***************************************************************

df_live_raw = load_checkins_df(
    org_slug=selected_customer_id,
    branch_slug=selected_branch_id,
    mtime=status_mtime,
    refresh_count=refresh_count,
)

df_history_raw = load_checkins_history_df(
    org_slug=selected_customer_id,
    branch_slug=selected_branch_id,
    mtime=status_mtime,
    refresh_count=refresh_count,
)

rejects_live_raw = load_rejects_df(
    org_slug=selected_customer_id,
    branch_slug=selected_branch_id,
    mtime=status_mtime,
    refresh_count=refresh_count,
)

rejects_history_raw = load_rejects_history_df(
    org_slug=selected_customer_id,
    branch_slug=selected_branch_id,
    mtime=status_mtime,
    refresh_count=refresh_count,
)

acs_live_raw = load_acs_df(
    org_slug=selected_customer_id,
    branch_slug=selected_branch_id,
    mtime=status_mtime,
    refresh_count=refresh_count,
)

acs_history_raw = load_acs_history_df(
    org_slug=selected_customer_id,
    branch_slug=selected_branch_id,
    mtime=status_mtime,
    refresh_count=refresh_count,
)


#***************************************************************
# Historical Data Validation
#
# Stops the dashboard if no usable historical checkin data exists.
# The dashboard needs historical checkin dates to build the available
# date range and report filters.
#***************************************************************

if len(df_history_raw) == 0 or "datetime" not in df_history_raw.columns:
    render_app_header(
        library_name=LIBRARY_NAME,
        branch_name=BRANCH_NAME,
        system_name=SYSTEM_NAME,
        show_admin_button=show_header_admin_button,
    )
    st.warning("No historical checkin data is available yet.")
    st.stop()

min_date = df_history_raw["datetime"].min().date()
max_date = df_history_raw["datetime"].max().date()


#***************************************************************
# Header and Navigation
#
# Displays the application header and builds the dashboard navigation
# options. The Transits section is only added when the user's
# permissions allow it.
#***************************************************************

render_app_header(
    library_name=LIBRARY_NAME,
    branch_name=BRANCH_NAME,
    system_name=SYSTEM_NAME,
    show_admin_button=show_header_admin_button,
)
nav_options = ["Live Today", "Reports", "Overview"]

if show_transits_tab:
    nav_options.insert(1, "Transits")

selected_view = st.segmented_control(
    "Section",
    options=nav_options,
    default="Live Today",
    label_visibility="collapsed"
)


#***************************************************************
# Date Filter Resolution
#
# Determines the active date range based on the selected dashboard
# section, available historical data, and the current local date.
#***************************************************************

local_today = datetime.now(APP_TZ).date()

start_date, end_date = resolve_date_filters(
    selected_view=selected_view,
    min_date=min_date,
    max_date=max_date,
    local_today=local_today,
)


#***************************************************************
# Display and Time Context
#
# Captures the Streamlit theme and current Central Time values used
# by the dashboard context and downstream view rendering.
#***************************************************************

theme_base = st.get_option("theme.base") or "light"
now_ct = datetime.now(APP_TZ)
today = now_ct.date()


#***************************************************************
# Dashboard Context Creation
#
# Combines raw data, pipeline status, date filters, runtime settings,
# display settings, and timezone information into the context object
# used by the dashboard views.
#***************************************************************

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
    selected_view=selected_view,
)


#***************************************************************
# View Permission Injection
#
# Adds user permission flags to the argument dictionaries that are
# passed into each dashboard view.
#***************************************************************

context["reports_args"]["can_export"] = reports_can_export
context["reports_args"]["can_advanced_reports"] = reports_can_advanced
context["live_today_args"]["can_view_internal_workflow"] = show_internal_workflow
context["live_today_args"]["can_view_transits"] = show_transits_tab
context["overview_args"]["can_view_internal_workflow"] = show_internal_workflow
context["transits_args"]["can_view_transits"] = show_transits_tab


#***************************************************************
# No Today Data Notice
#
# Informs the user when the live dashboard has no checkin data for
# the current day.
#***************************************************************

if context["no_today_data"]:
    st.info("No checkins have been ingested yet for today. Live dashboard is showing the current day only.")


#***************************************************************
# View Rendering
#
# Routes the user to the selected dashboard section by calling the
# matching view renderer with the prepared context arguments.
#***************************************************************

if selected_view == "Live Today":
    render_live_today(**context["live_today_args"])

if selected_view == "Overview":
    render_overview(**context["overview_args"])

if selected_view == "Reports":
    render_reports(**context["reports_args"])

if selected_view == "Transits":
    render_transits(**context["transits_args"])
