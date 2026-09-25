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

import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import metrics
from dashboard_context import build_dashboard_context
from data_loader_diag import (
    load_pipeline_status,
    load_v2_ingest_status,
    validate_tenant_schema,
)
from services import auth_service, mixed_era_service
from services.access_service import (
    get_org_access_mode,
    get_org_branches,
    get_user_memberships,
    user_can_access_org,
)
from services.app_ui_service import apply_page_chrome, render_app_header
from services.dashboard_refresh_service import (
    is_operating_hours,
    resolve_live_data_cache_key,
    resolve_refresh_interval_seconds,
    resolve_run_every_seconds,
)
from services.email_service import send_password_reset_email
from services.entitlement_service import build_entitlement_context
from services.filters_service import resolve_date_filters
from services.permission_service import (
    can_export,
    can_manage_settings,
    can_view_advanced_reports,
    can_view_internal_workflow,
    can_view_transits,
)
from services.privacy_hardening import install_streamlit_log_scrubber
from services.readiness_service import get_branch_readiness
from services.settings_service import load_runtime_settings
from services.sidebar_service import render_main_sidebar
from views.live_today_view import render_live_today
from views.overview_view import render_overview
from views.reports_view import render_reports
from views.transits_view import render_transits

logger = logging.getLogger("sortview.app")

# Keep an uncaught page exception's text out of Streamlit's own server log (see services/privacy_hardening.py).
install_streamlit_log_scrubber()

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

reset_token = st.query_params.get("reset_token")

if reset_token:
    st.session_state["auth_user"] = None


#***************************************************************
# Guest Auto-Login
#
# Visiting the app with "?guest=1" (used by the public portfolio demo
# link) signs the visitor straight in as the guest account, skipping
# the login form entirely. This entire block is inert unless
# SORTVIEW_DEMO_MODE_ENABLED is explicitly set to "true" for this
# deployment -- a real customer deployment simply doesn't have this
# code path reachable unless someone deliberately turns it on. There
# is deliberately no default guest email/password: if demo mode is on
# but those aren't configured, guest login is treated as unavailable
# rather than falling back to a guessable credential. Falls through to
# the regular login form if guest authentication fails for any reason
# (e.g. the account is deactivated).
#***************************************************************

DEMO_MODE_ENABLED = os.getenv("SORTVIEW_DEMO_MODE_ENABLED", "false").strip().lower() == "true"
GUEST_EMAIL = os.getenv("SORTVIEW_GUEST_EMAIL")
GUEST_PASSWORD = os.getenv("SORTVIEW_GUEST_PASSWORD")

if (
    DEMO_MODE_ENABLED
    and st.session_state["auth_user"] is None
    and st.query_params.get("guest") == "1"
):
    if GUEST_EMAIL and GUEST_PASSWORD:
        guest_result = auth_service.authenticate_user(email=GUEST_EMAIL, password=GUEST_PASSWORD)
        if guest_result["ok"]:
            st.session_state["auth_user"] = guest_result["user"]
            st.rerun()
    else:
        logger.warning(
            "SORTVIEW_DEMO_MODE_ENABLED is true but SORTVIEW_GUEST_EMAIL/"
            "SORTVIEW_GUEST_PASSWORD are not set; guest login is unavailable."
        )


#***************************************************************
# Authentication Check
#
# Displays the login form when no authenticated user exists.
# If authentication succeeds, the authenticated user is saved in
# session state and the app is rerun.
#***************************************************************

reset_token = st.query_params.get("reset_token")

# A password-reset link must take precedence over an existing login session.
if reset_token:
    st.session_state["auth_user"] = None


if st.session_state["auth_user"] is None:

    if reset_token:
        st.title("Reset SortView Password")

        with st.form("reset_password_form"):
            st.caption("* Required")
            new_password = st.text_input(
                "New password *",
                type="password",
            )
            confirm_password = st.text_input(
                "Confirm new password *",
                type="password",
            )
            reset_submitted = st.form_submit_button("Reset Password")

        if reset_submitted:
            result = auth_service.reset_password_with_token(
                token=reset_token,
                new_password=new_password,
                confirm_password=confirm_password,
            )

            if result["ok"]:
                st.success(result["message"])

                st.query_params.clear()

                if st.button("Return to login"):
                    st.rerun()
            else:
                st.error(result["message"])

        st.stop()

    if "show_forgot_password" not in st.session_state:
        st.session_state["show_forgot_password"] = False

    if st.session_state["show_forgot_password"]:
        st.title("Reset SortView Password")

        st.write(
            "Enter your email address and we'll send you "
            "a password reset link."
        )

        with st.form("forgot_password_form"):
            st.caption("* Required")
            reset_email = st.text_input("Email *")
            reset_requested = st.form_submit_button(
                "Send Reset Link"
            )

        if reset_requested:
            result = auth_service.request_password_reset(
                reset_email
            )

            reset_token = result.get("reset_token")
            recipient_email = result.get("reset_email")

            if reset_token and recipient_email:
                try:
                    send_password_reset_email(
                        recipient_email=recipient_email,
                        reset_token=reset_token,
                    )
                except Exception:
                    logger.exception("Password reset email delivery failed")
                    st.error(
                        "We couldn't send the reset email. "
                        "Please try again later."
                    )
                    st.stop()

            st.success(result["message"])

        if st.button("Back to login"):
            st.session_state["show_forgot_password"] = False
            st.rerun()

        st.stop()

    st.title("SortView Login")

    with st.form("login_form"):
        st.caption("* Required")
        email = st.text_input("Email *")
        password = st.text_input("Password *", type="password")
        submitted = st.form_submit_button("Log In")

    if submitted:
        result = auth_service.authenticate_user(
            email=email,
            password=password,
        )

        if result["ok"]:
            st.session_state["auth_user"] = result["user"]
            st.rerun()
        else:
            st.error(result["message"])

    if st.button("Forgot password?"):
        st.session_state["show_forgot_password"] = True
        st.rerun()

    st.stop()


#***************************************************************
# Authenticated User and Settings File Setup
#
# Defines the branch settings file path and loads the authenticated
# user from session state.
#***************************************************************

SETTINGS_FILE = Path(__file__).parent / "branch_settings.json"

auth_user = st.session_state["auth_user"]
auth_service.enforce_active_session(auth_user)
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
# Organization Access Mode
#
# Determines whether the selected organization currently allows full
# customer access, read-only access (suspended), or no access at all
# (cancelled). Checked on every rerun (uncached) so an already-open
# session fails closed as soon as an organization is cancelled, rather
# than remaining valid until the org list's own cache expires. Placed
# before any organization-scoped query (branches, settings, data) runs,
# so a blocked organization's data is never queried at all.
#***************************************************************

org_access_mode = get_org_access_mode(selected_org_slug)

if org_access_mode == "blocked":
    st.error("This organization is no longer available. Please contact an administrator.")
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

show_admin_button = can_manage_settings(entitlement_context) and org_access_mode == "full"
reports_can_export = can_export(entitlement_context)
reports_can_advanced = can_view_advanced_reports(entitlement_context)
show_transits_tab = can_view_transits(entitlement_context)
show_internal_workflow = can_view_internal_workflow(entitlement_context)

show_header_admin_button = False

if org_access_mode == "read_only":
    st.info(
        "This organization's account is currently suspended. Historical dashboard "
        "data remains available, but settings and user management are unavailable "
        "until it is reactivated."
    )


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


# is_operating_hours moved to services.dashboard_refresh_service alongside
# resolve_refresh_interval_seconds (Phase 4) so both auto-refresh gating
# decisions are unit-testable without importing this Streamlit entry
# point. Behavior is unchanged -- see that module for the definition.


#***************************************************************
# Auto Refresh Interval
#
# Resolves how often Live Today's own live section refreshes itself.
#
# Dashboard performance pass: this used to drive st_autorefresh, which
# reruns the ENTIRE script (auth, entitlements, settings, schema
# validation, readiness, every data loader) on every tick, regardless of
# which section actually needed fresh data -- the direct cause of "the
# whole app feels like it's constantly reloading." It's replaced below
# (see the Live Today branch of view routing) with an st.fragment(
# run_every=...) scoped to only Live Today's live section: the rest of
# the page (sidebar, chrome, historical views) now reruns only on a
# genuine user interaction, never on a timer.
#
# Collector-cadence pass: the automatic poll interval defaults to 180s
# (3 minutes, enforced as a hard minimum -- see
# dashboard_refresh_service.MIN_REFRESH_SECONDS) because the production
# scheduled Collector itself only runs every 15 minutes. Every automatic
# poll still forces a fresh pipeline_status read (see the fragment below),
# but checkins/rejects/ACS are only reloaded when that read reveals a NEW
# successful Collector run (pipeline_status["last_run"] advanced) or the
# user clicks "Refresh now" -- never on every tick, and never keyed off
# pipeline_status["updated_at"], which continuous-agent heartbeat writes
# also bump roughly every 60s independent of any real data change.
#***************************************************************

now_ct = datetime.now(APP_TZ)

refresh_interval_seconds, refresh_interval_warning = resolve_refresh_interval_seconds(
    os.getenv("SORTVIEW_DASHBOARD_REFRESH_SECONDS")
)
if refresh_interval_warning:
    logger.warning(refresh_interval_warning)


#***************************************************************
# Historical Data Loading
#
# Loads historical checkin and reject activity for the selected
# tenant/branch. These are cheap to load on every rerun regardless of
# which section is active: each underlying loader has its own 900s TTL
# and does not depend on any auto-refresh cadence (see data_loader.py),
# so this never re-queries the database more often than once every 15
# minutes no matter how many times the script reruns in between.
#
# Government-readiness audit: these go through
# services.mixed_era_service instead of calling data_loader's v1 loaders
# directly. For a branch with no v2_cutovers record (the overwhelming
# majority today) the result is byte-for-byte the same v1 history these
# loaders always returned -- see mixed_era_service.py's module docstring.
# Only a branch an operator has explicitly cut over to Contract v2 ever
# sees a combined v1+v2 history here. ACS history is loaded (and
# classified) further below, only when the Overview tab actually needs
# it -- unchanged from the prior "only compute what the active tab needs"
# behavior.
#***************************************************************

df_history_raw = mixed_era_service.build_mixed_checkins_df(
    selected_customer_id,
    selected_branch_id,
)

rejects_history_raw = mixed_era_service.build_mixed_rejects_df(
    selected_customer_id,
    selected_branch_id,
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

with st.container(key="sv_nav_row"):
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
# Live Today Auto-Refresh Pause Control
#
# WCAG 2.2.2 (Pause, Stop, Hide): Live Today's pipeline status checks
# itself automatically during operating hours (see run_every below) with
# no way to stop it, which can disorient a screen reader or low-vision/
# cognitive-disability user who happens to be reading that section when
# it re-renders. This gives the user an explicit, visible, real-text
# (never icon-only) control to turn that off for their own session.
#
# Defaults to NOT paused, i.e. the exact behavior that already existed
# before this control was added: auto-refresh is on whenever the
# dashboard is inside operating hours. A user's pause choice is stored in
# st.session_state, so it survives reruns for the rest of this session,
# and always wins over the operating-hours gate -- paused stays paused
# even if the clock crosses into or out of operating hours.
#***************************************************************

LIVE_TODAY_PAUSE_KEY = "_live_today_auto_refresh_paused"
if LIVE_TODAY_PAUSE_KEY not in st.session_state:
    st.session_state[LIVE_TODAY_PAUSE_KEY] = False

live_today_paused = st.session_state[LIVE_TODAY_PAUSE_KEY]

if selected_view == "Live Today":
    with st.container(key="sv_live_refresh_controls"):
        if live_today_paused:
            if st.button("Resume live updates"):
                st.session_state[LIVE_TODAY_PAUSE_KEY] = False
                st.rerun()
        else:
            if st.button("Pause live updates"):
                st.session_state[LIVE_TODAY_PAUSE_KEY] = True
                st.rerun()

        st.caption(f"Automatic status check every {refresh_interval_seconds // 60} minutes")

live_today_run_every = resolve_run_every_seconds(
    is_operating_hours_now=is_operating_hours(now_ct),
    is_paused=live_today_paused,
    interval_seconds=refresh_interval_seconds,
)


#***************************************************************
# Live Today Refresh State (tenant-scoped)
#
# Tracks, per (customer, branch), the manual Refresh now counter and
# never anything from another tenant. Keyed by the operational
# customer/branch IDs -- the same scope already used for every live
# loader call below -- so switching organization or branch can never
# reuse another tenant's manual-refresh counter (see
# dashboard_refresh_service.resolve_live_data_cache_key, which combines
# this with the freshly-loaded pipeline_status["last_run"] on every
# fragment run).
#***************************************************************

LIVE_TODAY_REFRESH_STATE_KEY = "_live_today_refresh_state"


#***************************************************************
# View Rendering
#
# Routes the user to the selected dashboard section. Live Today is the
# only section that checks itself automatically, so it is the only
# section wrapped in an auto-refreshing st.fragment -- see the comment on
# _render_live_today for exactly what that buys.
# Overview/Reports/Transits build their context once per genuine
# interaction (nav click, filter/date/branch change) and never rerun on
# a timer at all: an all-time-history report gains nothing from
# recomputing itself every 3 minutes while nobody is even looking at it,
# and every dashboard section shares the same underlying data anyway
# once a real rerun does happen.
#***************************************************************

@st.fragment(run_every=live_today_run_every)
def _render_live_today():
    # This is the ONLY part of the dashboard that reruns on a timer.
    # Everything above (auth, entitlements, settings, schema validation,
    # readiness, sidebar, historical data loading) runs once per genuine
    # interaction, not once per tick -- replacing the old
    # st_autorefresh-driven full-script rerun, which re-ran all of that
    # every ~10 seconds regardless of which section was even visible.
    #
    # poll_tick is this fragment's own local, monotonically increasing
    # counter used purely to force load_pipeline_status to actually query
    # on every fragment run (automatic poll or manual Refresh now) --
    # pipeline_status is cheap (a single-row lookup) and its freshness is
    # exactly what the poll exists to check, so it always gets a real
    # read here regardless of its own ttl=60.
    tick_key = "_live_today_fragment_tick"
    st.session_state[tick_key] = st.session_state.get(tick_key, 0) + 1
    poll_tick = st.session_state[tick_key]

    pipeline_status = load_pipeline_status(
        org_slug=selected_customer_id,
        branch_slug=selected_branch_id,
        mtime=None,
        refresh_count=poll_tick,
    )

    # Tenant-scoped manual-refresh counter -- see the module-level
    # LIVE_TODAY_REFRESH_STATE_KEY comment above.
    tenant_key = (selected_customer_id, selected_branch_id)
    refresh_state_by_tenant = st.session_state.setdefault(LIVE_TODAY_REFRESH_STATE_KEY, {})
    tenant_refresh_state = refresh_state_by_tenant.setdefault(tenant_key, {"manual_refresh_count": 0})

    # last_run only advances when the scheduled Collector completes a run
    # successfully (collector/run.py's run_once) -- never on a failed
    # attempt (last_attempt advances instead) and never on a
    # continuous-agent heartbeat write (those only touch health_status/
    # updated_at, never last_run -- see main.py's
    # _PIPELINE_STATUS_HEARTBEAT_FIELDS). Missing/empty pipeline_status
    # (e.g. a brand-new tenant, or a failed status read) is handled the
    # same as "no successful run yet" rather than raising.
    last_run = pipeline_status.get("last_run") if pipeline_status else None

    # Unchanged across fragment reruns (same last_run, same manual-refresh
    # count) -> identical key -> load_checkins_df/load_rejects_df/
    # load_acs_df all hit their own cache, no DB read. A new successful
    # Collector run OR a manual Refresh now click changes this key ->
    # a real read on all three. Deliberately NOT derived from
    # pipeline_status["updated_at"] -- see resolve_live_data_cache_key's
    # own docstring for why.
    live_data_key = resolve_live_data_cache_key(
        last_run=last_run,
        manual_refresh_count=tenant_refresh_state["manual_refresh_count"],
    )

    # Government-readiness audit: these three go through
    # services.mixed_era_service (a branch with no v2_cutovers record gets
    # byte-for-byte the same v1 live data as before), with live_data_key
    # threaded through exactly as it already was for the v1 loaders --
    # a manual "Refresh now" click or a new Collector run still forces a
    # real reload for a mixed-era branch, same as a v1-only one.
    df_live_raw = mixed_era_service.build_mixed_checkins_live_df(
        selected_customer_id,
        selected_branch_id,
        refresh_count=live_data_key,
    )
    rejects_live_raw = mixed_era_service.build_mixed_rejects_live_df(
        selected_customer_id,
        selected_branch_id,
        refresh_count=live_data_key,
    )
    acs_item_summary_live = mixed_era_service.build_mixed_acs_item_summary_live(
        selected_customer_id,
        selected_branch_id,
        TRANSIT_LABELS,
        BRANCH_SERVICES_NAMES,
        COLLECTION_SERVICES_NAMES,
        BRANCH_SERVICES_DA_PATTERNS,
        COLLECTION_SERVICES_DA_PATTERNS,
        refresh_count=live_data_key,
    )

    # The tenant's latest Contract v2 heartbeat, if it has ever reported
    # one -- feeds the coexistence-aware pipeline status surface so a
    # branch that has moved to v2 does not read as stale just because its
    # v1 Collector/agent stopped writing pipeline_status. None for a
    # branch that has never used v2, which is the overwhelming majority
    # today.
    v2_ingest_status = load_v2_ingest_status(
        org_slug=selected_customer_id,
        branch_slug=selected_branch_id,
    )

    def _handle_refresh_now():
        # Bumping this (tenant-scoped) counter changes live_data_key on
        # the NEXT fragment run -- exactly the same "flip session_state,
        # then st.rerun()" pattern the Pause/Resume control above already
        # uses. poll_tick above already forces a fresh pipeline_status
        # read on every fragment run regardless of why it reran, so a
        # manual Refresh now click gets both a fresh status read and a
        # forced live-data reload without any extra branching here.
        tenant_refresh_state["manual_refresh_count"] += 1

    live_view_context = build_dashboard_context(
        df_live_raw=df_live_raw,
        df_history_raw=df_history_raw,
        rejects_live_raw=rejects_live_raw,
        rejects_history_raw=rejects_history_raw,
        acs_item_summary_live=acs_item_summary_live,
        acs_item_summary_history=None,
        v2_ingest_status=v2_ingest_status,
        pipeline_status=pipeline_status,
        refresh_count=poll_tick,
        start_date=start_date,
        end_date=end_date,
        today=today,
        now_ct=now_ct,
        app_tz=APP_TZ,
        transit_labels=TRANSIT_LABELS,
        transit_home_label=TRANSIT_HOME_LABEL,
        library_name=LIBRARY_NAME,
        branch_name=BRANCH_NAME,
        system_name=SYSTEM_NAME,
        theme_base=theme_base,
        selected_view="Live Today",
    )

    live_view_context["live_today_args"]["can_view_internal_workflow"] = show_internal_workflow
    live_view_context["live_today_args"]["can_view_transits"] = show_transits_tab
    live_view_context["live_today_args"]["on_refresh_now"] = _handle_refresh_now

    if live_view_context["no_today_data"]:
        st.info("No checkins have been ingested yet for today. Live dashboard is showing the current day only.")

    render_live_today(**live_view_context["live_today_args"])


if selected_view == "Live Today":
    _render_live_today()
else:
    # Overview/Reports/Transits never need sub-minute-fresh live data --
    # a plain, undecorated call (no refresh_count/mtime) relies solely on
    # these loaders' own 900s TTL, same as the historical loaders above.
    # Government-readiness audit: mixed-era aware, same as above -- a
    # v1-only branch gets byte-for-byte the same v1 live data as before.
    df_live_raw = mixed_era_service.build_mixed_checkins_live_df(
        selected_customer_id,
        selected_branch_id,
    )
    rejects_live_raw = mixed_era_service.build_mixed_rejects_live_df(
        selected_customer_id,
        selected_branch_id,
    )

    # ACS history is classified only for Overview, and only when the
    # viewer can actually see the Internal Workflow cards it feeds --
    # unchanged "only compute what the active tab needs" behavior from
    # before this round (see build_acs_item_summary's prior call site,
    # now moved here from views/overview_view.py).
    if selected_view == "Overview" and show_internal_workflow:
        acs_item_summary_history = mixed_era_service.build_mixed_acs_item_summary(
            selected_customer_id,
            selected_branch_id,
            start_date,
            end_date,
            TRANSIT_LABELS,
            BRANCH_SERVICES_NAMES,
            COLLECTION_SERVICES_NAMES,
            BRANCH_SERVICES_DA_PATTERNS,
            COLLECTION_SERVICES_DA_PATTERNS,
        )
    else:
        acs_item_summary_history = metrics.build_acs_item_summary(
            pd.DataFrame(), TRANSIT_LABELS, [], [], [], []
        )

    context = build_dashboard_context(
        df_live_raw=df_live_raw,
        df_history_raw=df_history_raw,
        rejects_live_raw=rejects_live_raw,
        rejects_history_raw=rejects_history_raw,
        acs_item_summary_live=None,
        acs_item_summary_history=acs_item_summary_history,
        v2_ingest_status=None,
        pipeline_status={},
        refresh_count=0,
        start_date=start_date,
        end_date=end_date,
        today=today,
        now_ct=now_ct,
        app_tz=APP_TZ,
        transit_labels=TRANSIT_LABELS,
        transit_home_label=TRANSIT_HOME_LABEL,
        library_name=LIBRARY_NAME,
        branch_name=BRANCH_NAME,
        system_name=SYSTEM_NAME,
        theme_base=theme_base,
        selected_view=selected_view,
    )

    context["reports_args"]["can_export"] = reports_can_export
    context["reports_args"]["can_advanced_reports"] = reports_can_advanced
    context["overview_args"]["can_view_internal_workflow"] = show_internal_workflow
    context["transits_args"]["can_view_transits"] = show_transits_tab

    if context["no_today_data"]:
        st.info("No checkins have been ingested yet for today. Live dashboard is showing the current day only.")

    if selected_view == "Overview":
        render_overview(**context["overview_args"])

    if selected_view == "Reports":
        render_reports(**context["reports_args"])

    if selected_view == "Transits":
        render_transits(**context["transits_args"])
