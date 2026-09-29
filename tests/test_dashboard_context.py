from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.dashboard_context import build_dashboard_context

APP_TZ = ZoneInfo("America/Chicago")
TODAY = pd.Timestamp("2026-03-30").date()  # freshness: allow FRESH004 -- handed to the code under test as an explicit argument
NOW_CT = datetime(2026, 3, 30, 12, 0, tzinfo=APP_TZ)  # freshness: allow FRESH004 -- handed to the code under test as an explicit argument


def build_checkins_df(dates):
    return pd.DataFrame({
        "datetime": pd.to_datetime(dates),
        "destination": ["Main"] * len(dates),
        "bin": ["1"] * len(dates),
    })


def build_rejects_df(dates):
    return pd.DataFrame({
        "datetime": pd.to_datetime(dates),
        "error_message": ["Item Not Found"] * len(dates),
    })


def empty_acs_item_summary():
    return {
        "holds_total": 0,
        "ill_total": 0,
        "programming_total": 0,
        "collection_services_total": 0,
        "ill_main": 0,
        "ill_by_branch": {},
        "items_df": pd.DataFrame(),
        "holds_df": pd.DataFrame(),
        "ill_df": pd.DataFrame(),
        "programming_df": pd.DataFrame(),
        "collection_services_df": pd.DataFrame(),
    }


def base_kwargs(selected_view, with_today_data=True):
    live_dates = ["2026-03-30 09:00"] if with_today_data else []
    history_dates = ["2026-03-29 09:00", "2026-03-30 09:00"]

    return {
        "df_live_raw": build_checkins_df(live_dates),
        "df_history_raw": build_checkins_df(history_dates),
        "rejects_live_raw": build_rejects_df(live_dates),
        "rejects_history_raw": build_rejects_df(history_dates),
        "acs_item_summary_live": empty_acs_item_summary(),
        "acs_item_summary_history": empty_acs_item_summary(),
        "v2_ingest_status": None,
        "pipeline_status": {
            "status": "completed",
            "updated_at": "2026-03-30T12:00:00",
            "last_run": "2026-03-30T12:00:00",
            "last_attempt": "2026-03-30T12:00:00",
        },
        "refresh_count": 0,
        "start_date": pd.Timestamp("2026-03-29").date(),
        "end_date": TODAY,
        "today": TODAY,
        "now_ct": NOW_CT,
        "app_tz": APP_TZ,
        "transit_labels": ["Westside"],
        "transit_home_label": "Main",
        "library_name": "Test Library",
        "branch_name": "Main Branch",
        "system_name": "SortView",
        "theme_base": "light",
        "selected_view": selected_view,
    }


@pytest.mark.parametrize("selected_view", ["Live Today", "Overview", "Reports", "Transits"])
def test_only_the_selected_views_args_are_populated(selected_view):
    context = build_dashboard_context(**base_kwargs(selected_view))

    all_arg_keys = {
        "Live Today": "live_today_args",
        "Overview": "overview_args",
        "Reports": "reports_args",
        "Transits": "transits_args",
    }

    for view_name, args_key in all_arg_keys.items():
        if view_name == selected_view:
            assert context[args_key] != {}, f"{args_key} should be populated for {selected_view}"
        else:
            assert context[args_key] == {}, f"{args_key} should stay empty when viewing {selected_view}"


def test_live_today_args_merge_pipeline_and_live_context():
    context = build_dashboard_context(**base_kwargs("Live Today"))

    live_today_args = context["live_today_args"]
    assert live_today_args["today_checkins"] == 1
    # pipeline_ctx keys
    assert "pipeline_status_label" in live_today_args
    # live_ctx keys
    assert "today_df" in live_today_args


def test_live_today_args_with_a_real_v2_status_bind_to_render_live_today():
    # Regression: a truthy v2_ingest_status once added a key render_live_today() does not accept, crashing
    # app.py's render_live_today(**live_today_args) in production. Bind against the real signature, with the
    # same keys app.py's _render_live_today injects before the call.
    import inspect

    from views.live_today_view import render_live_today

    kwargs = base_kwargs("Live Today")
    kwargs["v2_ingest_status"] = {
        "health_status": "healthy",
        "last_error_class": None,
        "pending_outbox_count": 0,
        "quarantined_count": 0,
        "last_heartbeat_at": "2026-03-30T16:59:00+00:00",
        "last_success_at": "2026-03-30T16:58:00+00:00",
    }
    live_today_args = build_dashboard_context(**kwargs)["live_today_args"]
    assert live_today_args["pipeline_result_text"] == "Contract v2 collector reporting healthy"

    live_today_args.update({
        "can_view_internal_workflow": True,
        "can_view_transits": True,
        "on_refresh_now": lambda: None,
        "live_today_paused": False,
        "on_toggle_live_updates": lambda: None,
        "refresh_interval_minutes": 3,
    })
    inspect.signature(render_live_today).bind(**live_today_args)


def test_no_today_data_true_when_no_live_checkins_regardless_of_tab():
    for selected_view in ["Live Today", "Overview", "Reports", "Transits"]:
        context = build_dashboard_context(**base_kwargs(selected_view, with_today_data=False))
        assert context["no_today_data"] is True


def test_no_today_data_false_when_live_checkins_exist():
    context = build_dashboard_context(**base_kwargs("Reports", with_today_data=True))
    assert context["no_today_data"] is False


def test_transits_args_use_todays_checkins_and_rejects():
    context = build_dashboard_context(**base_kwargs("Transits"))

    transits_args = context["transits_args"]
    assert len(transits_args["today_df"]) == 1
    assert "df" in transits_args
    assert "rejects_df" in transits_args


def test_reports_args_include_filtered_history_and_display_names():
    context = build_dashboard_context(**base_kwargs("Reports"))

    reports_args = context["reports_args"]
    assert reports_args["LIBRARY_NAME"] == "Test Library"
    assert reports_args["BRANCH_NAME"] == "Main Branch"
    assert reports_args["SYSTEM_NAME"] == "SortView"
    assert len(reports_args["df"]) == 2
