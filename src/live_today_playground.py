#***************************************************************
#
#  File:         live_today_playground.py
#
#  Description: Local-only preview for the Live Today view, including the
#               surrounding page chrome (logo, header, nav row) so spacing
#               edits can be judged in context. Calls render_live_today(),
#               apply_page_chrome(), and render_app_header() directly with
#               hardcoded default values -- no database, no login, no
#               dashboard_context/live_context_service/metrics pipeline.
#               Touches nothing but views/live_today_view.py and
#               services/app_ui_service.py (both already imported by the
#               real app the same way).
#
#               Run with:  streamlit run src/live_today_playground.py
#
#               Not wired into the real app, not covered by the test
#               suite, never reads/writes the real database. Edit
#               views/live_today_view.py (or app_ui_service.py for the
#               header/logo) and save -- this reloads automatically.
#               The Transits/Reports/Overview nav buttons are cosmetic
#               only -- this playground always renders Live Today.
#
#***************************************************************

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from services.app_ui_service import apply_page_chrome, render_app_header
from views.live_today_view import render_live_today

st.set_page_config(page_title="Live Today Playground", page_icon="🧪", layout="wide", initial_sidebar_state="collapsed")
apply_page_chrome()
st.caption("🧪 Live Today Playground -- no database, no login. Not the real app.")

APP_TZ = ZoneInfo("America/Chicago")

header_left, header_right = st.columns([4, 2], vertical_alignment="center")

with header_left:
    render_app_header(
        library_name="Demo Library",
        branch_name="Main Branch",
        system_name="SortView",
        show_admin_button=False,
    )

with header_right:
    st.markdown(
        f"""
        <div style="
            font-size: 2rem;
            font-weight: 700;
            color: var(--text-color);
            text-align: left;
            white-space: nowrap;
        ">
            {datetime.now(APP_TZ).strftime('%A, %b %d')}
        </div>
        """,
        unsafe_allow_html=True,
    )

with st.container(key="sv_nav_row"):
    selected_view = st.segmented_control(
        "Section",
        options=["Live Today", "Transits", "Reports", "Overview"],
        default="Live Today",
        label_visibility="collapsed",
    )

if selected_view != "Live Today":
    st.info(f"'{selected_view}' is not wired up in this playground -- showing Live Today.")

render_live_today(
    today=date.today(),
    refresh_count=0,
    on_refresh_now=lambda: None,
    pipeline_status_label="Unknown",
    pipeline_status_color="#6b7280",
    pipeline_status_bg="#f9fafb",
    pipeline_expanded=False,
    app_refreshed_str="",
    latest_checkin_str="--",
    latest_checkin_ago="--",
    pipeline_status_written_str="--",
    pipeline_status_written_ago="--",
    pipeline_last_attempt_str="--",
    pipeline_last_attempt_ago="--",
    pipeline_last_run_str="--",
    pipeline_last_run_ago="--",
    pipeline_result_text="",
    status_code_text="",
    checkins_rows=0,
    rejects_rows=0,
    uploaded_checkins_rows=0,
    uploaded_rejects_rows=0,
    checkins_bad_datetime_rows=0,
    rejects_bad_datetime_rows=0,
    transit_items=0,
    problem_items=0,
    destination_breakdown_text="",
    today_metrics={"current_speed": 0},
    today_checkins=0,
    today_rejects=0,
    today_total_transit=0,
    today_transit_counts_map={},
    today_transit_pct_map={},
    today_peak_hour=None,
    today_peak_hour_count=0,
    today_peak_hour_pct=0,
    today_reject_rate=0,
    historical_daily_avg_reject=0,
    live_reject_deviation=0,
    live_reject_subtitle_color="#6b7280",
    live_reject_value_color="#6b7280",
    TRANSIT_LABELS=[],
    TRANSIT_HOME_LABEL="Main",
    today_holds=0,
    today_ill=0,
    today_ill_main=0,
    today_ill_by_branch={},
    today_programming=0,
    today_collection_services=0,
    today_public_holds_df=pd.DataFrame(),
    today_ill_items_df=pd.DataFrame(),
    today_programming_df=pd.DataFrame(),
    today_collection_services_df=pd.DataFrame(),
    info_alerts=[],
    show_live_alert=False,
    live_alert_title="",
    live_alert_text="",
    info_border="",
    info_bg="",
    info_title="",
    info_text="",
    danger_border="",
    danger_bg="",
    danger_title="",
    danger_text="",
    today_df=pd.DataFrame(),
    today_rejects_df=pd.DataFrame(),
    today_hourly_checkins=pd.Series(dtype="int64"),
    live_hour_range=range(24),
    live_today_paused=True,
    on_toggle_live_updates=lambda: None,
    refresh_interval_minutes=3,
    can_view_transits=True,
    can_view_internal_workflow=True,
)
