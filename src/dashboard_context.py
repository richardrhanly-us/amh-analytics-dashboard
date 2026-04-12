from reject_logic import simplify_error

from services.pipeline_context_service import build_pipeline_context
from services.filter_context_service import build_filtered_context
from services.live_context_service import build_live_context
from services.theme_service import get_theme_palette


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
):
    df_live_raw = df_live_raw.copy()
    df_history_raw = df_history_raw.copy()
    rejects_live_raw = rejects_live_raw.copy()
    rejects_history_raw = rejects_history_raw.copy()
    acs_live_raw = acs_live_raw.copy()
    acs_history_raw = acs_history_raw.copy()

    if "error_message" in rejects_live_raw.columns:
        rejects_live_raw["error_simple"] = rejects_live_raw["error_message"].apply(simplify_error)
    if "error_message" in rejects_history_raw.columns:
        rejects_history_raw["error_simple"] = rejects_history_raw["error_message"].apply(simplify_error)

    theme_palette = get_theme_palette(theme_base)

    pipeline_ctx = build_pipeline_context(
        pipeline_status=pipeline_status,
        df_live_raw=df_live_raw,
        now_ct=now_ct,
        local_tz=app_tz,
        theme_base=theme_base,
    )

    filtered_ctx = build_filtered_context(
        df_history_raw=df_history_raw,
        rejects_history_raw=rejects_history_raw,
        start_date=start_date,
        end_date=end_date,
        transit_labels=transit_labels,
        transit_home_label=transit_home_label,
    )

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

    live_today_args = {
        **pipeline_ctx,
        **live_ctx["live_today_args"],
    }

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

    transits_args = {
        "df": filtered_ctx["df"],
        "rejects_df": filtered_ctx["rejects_df"],
        "today_df": live_ctx["today_df"],
        "today_rejects_df": live_ctx["today_rejects_df"],
        "df_history_raw": df_history_raw,
        "today": today,
        "start_date": start_date,
        "end_date": end_date,
    }

    return {
        "no_today_data": live_ctx["no_today_data"],
        "live_today_args": live_today_args,
        "overview_args": overview_args,
        "reports_args": reports_args,
        "transits_args": transits_args,
    }
