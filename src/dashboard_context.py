#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         dashboard_context.py
#
#  Description: Builds the shared dashboard context for the SortView
#               Streamlit application. This file prepares copies of
#               raw AMH data, applies reject error simplification,
#               builds pipeline, filtered, and live context objects,
#               and packages the arguments needed by each dashboard
#               view.
#
#***************************************************************

from metrics import get_today_metrics
from reject_logic import simplify_error
from services.filter_context_service import build_filtered_context
from services.live_context_service import build_live_context
from services.pipeline_context_service import build_pipeline_context
from services.theme_service import get_theme_palette

#***************************************************************
#
#  Function:     build_dashboard_context
#
#  Description: Creates the combined context object used by the main
#               SortView dashboard views. This function prepares safe
#               copies of the raw dataframes, simplifies reject error
#               messages, builds supporting context dictionaries, and
#               organizes the final argument sets for the Live Today,
#               Overview, Reports, and Transits pages.
#
#  Parameters:  df_live_raw - Live checkin dataframe.
#               df_history_raw - Historical checkin dataframe.
#               rejects_live_raw - Live rejects dataframe.
#               rejects_history_raw - Historical rejects dataframe.
#               acs_live_raw - Live ACS dataframe.
#               acs_history_raw - Historical ACS dataframe.
#               pipeline_status - Latest pipeline status details.
#               refresh_count - Streamlit auto-refresh counter.
#               start_date - Start date for the active report range.
#               end_date - End date for the active report range.
#               today - Current local date.
#               now_ct - Current datetime in Central Time.
#               app_tz - Application timezone.
#               transit_labels - List of transit routing labels.
#               transit_home_label - Label used for home branch routing.
#               branch_services_names - Branch services destination names.
#               collection_services_names - Collection services destination names.
#               branch_services_da_patterns - Branch services destination patterns.
#               collection_services_da_patterns - Collection services destination patterns.
#               library_name - Display name for the library.
#               branch_name - Display name for the selected branch.
#               system_name - Display name for the library system.
#               theme_base - Active Streamlit theme base.
#
#  Returns:     dict - Dashboard context containing no-today-data status
#                      and argument dictionaries for each dashboard view.
#
#***************************************************************

def build_dashboard_context(
    df_live_raw,
    df_history_raw,
    rejects_live_raw,
    rejects_history_raw,
    acs_live_raw,
    acs_history_raw,
    pipeline_status,
    refresh_count,
    start_date,
    end_date,
    today,
    now_ct,
    app_tz,
    transit_labels,
    transit_home_label,
    branch_services_names,
    collection_services_names,
    branch_services_da_patterns,
    collection_services_da_patterns,
    library_name,
    branch_name,
    system_name,
    theme_base,
    selected_view,
):
    # Create local dataframe copies so this function can safely modify
    # values without changing the original dataframes passed into it.
    df_live_raw = df_live_raw.copy()
    df_history_raw = df_history_raw.copy()
    rejects_live_raw = rejects_live_raw.copy()
    rejects_history_raw = rejects_history_raw.copy()
    acs_live_raw = acs_live_raw.copy()
    acs_history_raw = acs_history_raw.copy()

    # Add simplified reject error labels when reject error messages are available.
    # These simplified labels make the dashboard easier to read and summarize.
    if "error_message" in rejects_live_raw.columns:
        rejects_live_raw["error_simple"] = rejects_live_raw["error_message"].apply(simplify_error)
    if "error_message" in rejects_history_raw.columns:
        rejects_history_raw["error_simple"] = rejects_history_raw["error_message"].apply(simplify_error)

    # Load the theme palette so downstream views can use consistent colors.
    theme_palette = get_theme_palette(theme_base)

    # Only the sections needed for the currently selected tab are built.
    # build_dashboard_context used to build pipeline, filtered, and live
    # context unconditionally on every rerun regardless of which single
    # tab was actually being viewed -- including a full-history groupby
    # inside live context -- which meant every widget interaction anywhere
    # on the page paid for all four tabs' worth of computation.
    needs_filtered_ctx = selected_view in ("Overview", "Reports", "Transits")
    needs_live_ctx = selected_view == "Live Today"

    # Cheap, always-needed lookup for today's checkin/reject rows. df_live_raw
    # is already scoped to today by the database query, so this is just a
    # light filter/dedupe pass -- not the expensive part of live context
    # (ACS summaries, alerts, historical transit comparisons, hourly
    # baselines over the full history). Used for the "no data today" banner
    # shown above every tab, and for Transits' same-day comparison.
    today_metrics = get_today_metrics(df_live_raw, rejects_live_raw, today)
    no_today_data = len(today_metrics["today_df"]) == 0

    pipeline_ctx = {}
    live_ctx = None
    if needs_live_ctx:
        # Build context related to pipeline health, freshness, and status display.
        pipeline_ctx = build_pipeline_context(
            pipeline_status=pipeline_status,
            df_live_raw=df_live_raw,
            now_ct=now_ct,
            local_tz=app_tz,
            theme_base=theme_base,
        )

        # Build live dashboard context for today's activity, live rejects,
        # routing summaries, and operational workflow indicators.
        live_ctx = build_live_context(
            df_live_raw=df_live_raw,
            df_history_raw=df_history_raw,
            rejects_live_raw=rejects_live_raw,
            rejects_history_raw=rejects_history_raw,
            acs_live_raw=acs_live_raw,
            pipeline_status=pipeline_status,
            refresh_count=refresh_count,
            today=today,
            now_ct=now_ct,
            transit_labels=transit_labels,
            transit_home_label=transit_home_label,
            branch_services_names=branch_services_names,
            collection_services_names=collection_services_names,
            branch_services_da_patterns=branch_services_da_patterns,
            collection_services_da_patterns=collection_services_da_patterns,
            theme_palette=theme_palette,
        )
        no_today_data = live_ctx["no_today_data"]

    filtered_ctx = None
    if needs_filtered_ctx:
        # Build filtered historical context for date-based reporting views.
        filtered_ctx = build_filtered_context(
            df_history_raw=df_history_raw,
            rejects_history_raw=rejects_history_raw,
            start_date=start_date,
            end_date=end_date,
            transit_labels=transit_labels,
            transit_home_label=transit_home_label,
        )

    live_today_args = {}
    if selected_view == "Live Today":
        # Combine pipeline context and live context into the argument
        # dictionary needed by the Live Today dashboard view.
        live_today_args = {
            **pipeline_ctx,
            **live_ctx["live_today_args"],
        }

    overview_args = {}
    if selected_view == "Overview":
        # Build the argument dictionary used by the Overview page.
        # This page focuses on filtered historical activity and summary metrics.
        overview_args = {
            "df": filtered_ctx["df"],
            "rejects_df": filtered_ctx["rejects_df"],
            "acs_history_raw": acs_history_raw,
            "start_date": start_date,
            "end_date": end_date,
            "date_range_text": filtered_ctx["date_range_text"],
            "TRANSIT_LABELS": transit_labels,
            "TRANSIT_HOME_LABEL": transit_home_label,
            "BRANCH_SERVICES_NAMES": branch_services_names,
            "COLLECTION_SERVICES_NAMES": collection_services_names,
            "BRANCH_SERVICES_DA_PATTERNS": branch_services_da_patterns,
            "COLLECTION_SERVICES_DA_PATTERNS": collection_services_da_patterns,
            "attention_title": filtered_ctx["attention_title"],
            "attention_text": filtered_ctx["attention_text"],
            "attention_color": filtered_ctx["attention_color"],
            "overview_transit_counts_map": filtered_ctx["overview_transit_counts_map"],
            "overview_transit_pct_map": filtered_ctx["overview_transit_pct_map"],
        }

    reports_args = {}
    if selected_view == "Reports":
        # Build the argument dictionary used by the Reports page.
        # This includes filtered data, raw history, date range details,
        # summary metrics, and display names for report output.
        reports_args = {
            "df": filtered_ctx["df"],
            "df_history_raw": df_history_raw,
            "df_live_raw": df_live_raw,
            "rejects_df": filtered_ctx["rejects_df"],
            "start_date": start_date,
            "end_date": end_date,
            "today": today,
            "overall_metrics": filtered_ctx["overall_metrics"],
            "top_issue": filtered_ctx["top_issue"],
            "attention_text": filtered_ctx["attention_text"],
            "LIBRARY_NAME": library_name,
            "BRANCH_NAME": branch_name,
            "SYSTEM_NAME": system_name,
        }

    transits_args = {}
    if selected_view == "Transits":
        # Build the argument dictionary used by the Transits page.
        # This page compares transit activity across the selected date range
        # and the current day.
        transits_args = {
            "df": filtered_ctx["df"],
            "rejects_df": filtered_ctx["rejects_df"],
            "today_df": today_metrics["today_df"],
            "today_rejects_df": today_metrics["today_rejects_df"],
            "df_history_raw": df_history_raw,
            "today": today,
            "start_date": start_date,
            "end_date": end_date,
        }

    # Return the complete dashboard context consumed by app.py.
    return {
        "no_today_data": no_today_data,
        "live_today_args": live_today_args,
        "overview_args": overview_args,
        "reports_args": reports_args,
        "transits_args": transits_args,
    }
