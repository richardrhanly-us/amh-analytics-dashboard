"""Tests for services.pipeline_context_service.build_v2_aware_pipeline_context (government-readiness audit, Part 5/7).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from services.pipeline_context_service import (
    build_pipeline_context,
    build_v2_aware_pipeline_context,
)

APP_TZ = ZoneInfo("America/Chicago")
NOW_CT = datetime(2026, 10, 5, 12, 0, tzinfo=APP_TZ)  # freshness: allow FRESH004 -- passed as now_ct= to every call under test


def empty_df():
    return pd.DataFrame(columns=["datetime"])


# --- item 15: a v1-only/legacy branch is completely unaffected -------------------------------------------------------

def test_no_v2_status_delegates_identically_to_build_pipeline_context():
    pipeline_status = {
        "status": "completed",
        "uploaded_checkins_rows": 5,
        "uploaded_rejects_rows": 1,
        "updated_at": "2026-10-05T15:00:00Z",
    }
    expected = build_pipeline_context(pipeline_status, empty_df(), NOW_CT, APP_TZ, "light")
    actual = build_v2_aware_pipeline_context(pipeline_status, None, empty_df(), NOW_CT, APP_TZ, "light")
    assert actual == expected


# --- item 14: a v2-only branch does not appear stale merely because v1 stopped writing pipeline_status --------------

def test_v2_healthy_status_wins_even_when_v1_pipeline_status_is_stale_or_failed():
    stale_v1_status = {"status": "failed", "updated_at": "2026-01-01T00:00:00Z"}
    v2_status = {
        "health_status": "healthy",
        "last_error_class": None,
        "pending_outbox_count": 0,
        "quarantined_count": 0,
        "last_heartbeat_at": "2026-10-05T17:59:00Z",
        "last_success_at": "2026-10-05T17:59:00Z",
    }
    ctx = build_v2_aware_pipeline_context(stale_v1_status, v2_status, empty_df(), NOW_CT, APP_TZ, "light")
    assert ctx["pipeline_status_label"] == "Pipeline Healthy"
    assert ctx["pipeline_expanded"] is False


def test_v2_status_returns_the_same_key_shape_as_build_pipeline_context():
    # dashboard_context splats this dict straight into render_live_today(**...), so an extra key is a TypeError in
    # production (the removed "pipeline_source" key did exactly that once a real v2 row reached the dashboard).
    stale_v1_status = {"status": "failed", "updated_at": "2026-01-01T00:00:00Z"}
    v2_status = {"health_status": "healthy", "last_heartbeat_at": "2026-10-05T17:59:00Z"}
    expected = build_pipeline_context(stale_v1_status, empty_df(), NOW_CT, APP_TZ, "light")
    actual = build_v2_aware_pipeline_context(stale_v1_status, v2_status, empty_df(), NOW_CT, APP_TZ, "light")
    assert set(actual) == set(expected)


# --- a v2-active branch never shows frozen v1 status values next to current v2 ones ----------------------------------

# The v1 row as it was left at cutover: last written Oct 04 5:23 PM CT (22:23 UTC, naive as pipeline_status stores it),
# i.e. "19 hrs ago" at NOW_CT, with its last run's counters.
FROZEN_V1_STATUS = {
    "status": "completed",
    "updated_at": "2026-10-04T22:23:00",
    "last_attempt": "2026-10-04T22:23:00",
    "last_run": "2026-10-04T22:23:00",
    "checkins_rows": 6, "rejects_rows": 0, "uploaded_checkins_rows": 6, "uploaded_rejects_rows": 0,
    "checkins_bad_datetime_rows": 2, "rejects_bad_datetime_rows": 1, "transit_items": 3, "problem_items": 1,
    "destination_breakdown": {"Main": 6},
}

# NOW_CT is 12:00 PM CT == 17:00 UTC; each v2 instant is a few minutes before that.
CURRENT_V2_STATUS = {
    "health_status": "healthy",
    "last_error_class": None,
    "pending_outbox_count": 0,
    "quarantined_count": 0,
    "last_heartbeat_at": "2026-10-05T16:55:00.182880+00:00",  # 11:55 AM CT, the loader's isoformat() shape
    "watcher_last_active_at": "2026-10-05T16:54:00+00:00",   # 11:54 AM CT
    "last_success_at": "2026-10-05T16:50:00+00:00",          # 11:50 AM CT
}


def _v2_ctx(v1_status=None, v2_status=None):
    return build_v2_aware_pipeline_context(
        FROZEN_V1_STATUS if v1_status is None else v1_status,
        v2_status or CURRENT_V2_STATUS, empty_df(), NOW_CT, APP_TZ, "light",
    )


def test_v2_heartbeat_is_shown_in_dashboard_local_time_and_aged_from_itself():
    ctx = _v2_ctx()
    assert ctx["pipeline_status_written_str"] == "Oct 05, 2026 11:55 AM"
    assert ctx["pipeline_status_written_ago"] == "4 min ago"


def test_v2_last_success_is_shown_in_dashboard_local_time_and_aged_from_itself():
    ctx = _v2_ctx()
    assert ctx["pipeline_last_run_str"] == "Oct 05, 2026 11:50 AM"
    assert ctx["pipeline_last_run_ago"] == "10 min ago"


def test_v2_last_attempt_comes_from_watcher_last_active_at_never_v1_last_attempt():
    ctx = _v2_ctx()
    assert ctx["pipeline_last_attempt_str"] == "Oct 05, 2026 11:54 AM"
    assert ctx["pipeline_last_attempt_ago"] == "6 min ago"


def test_v2_run_summary_and_destination_breakdown_never_show_frozen_v1_counters():
    ctx = _v2_ctx()
    for key in ("checkins_rows", "rejects_rows", "uploaded_checkins_rows", "uploaded_rejects_rows",
                "checkins_bad_datetime_rows", "rejects_bad_datetime_rows", "transit_items", "problem_items"):
        assert ctx[key] is None, key
    assert ctx["destination_breakdown_text"].startswith("N/A")


def test_no_v1_status_value_leaks_into_a_v2_context():
    ctx = _v2_ctx()
    v1_only = build_pipeline_context(FROZEN_V1_STATUS, empty_df(), NOW_CT, APP_TZ, "light")
    # app_refreshed_str / latest_checkin_* are not v1 status fields (now_ct and the live dataframe).
    shared = {"app_refreshed_str", "latest_checkin_str", "latest_checkin_ago"}
    leaked = {k for k in v1_only if k not in shared and v1_only[k] not in ("N/A", None) and ctx[k] == v1_only[k]}
    # The label/colors legitimately coincide ("Pipeline Healthy" for both a completed v1 run and a healthy v2).
    assert leaked <= {"pipeline_status_label", "pipeline_status_color", "pipeline_status_bg", "pipeline_expanded"}
    rendered = " ".join(str(v) for v in ctx.values())
    assert "Oct 04" not in rendered
    assert "hrs ago" not in rendered


def test_missing_v2_timestamps_show_na_never_the_v1_fallback():
    v2_status = {**CURRENT_V2_STATUS, "last_success_at": None, "watcher_last_active_at": None}
    ctx = _v2_ctx(v2_status=v2_status)
    assert (ctx["pipeline_last_run_str"], ctx["pipeline_last_run_ago"]) == ("N/A", "N/A")
    assert (ctx["pipeline_last_attempt_str"], ctx["pipeline_last_attempt_ago"]) == ("N/A", "N/A")


def test_v2_degraded_status_reports_pending_and_quarantined_counts():
    v2_status = {
        "health_status": "degraded",
        "last_error_class": None,
        "pending_outbox_count": 42,
        "quarantined_count": 3,
        "last_heartbeat_at": "2026-10-05T17:59:00Z",
        "last_success_at": None,
    }
    ctx = build_v2_aware_pipeline_context(None, v2_status, empty_df(), NOW_CT, APP_TZ, "light")
    assert ctx["pipeline_status_label"] == "Pipeline Degraded"
    assert "42" in ctx["pipeline_result_text"]
    assert "3" in ctx["pipeline_result_text"]


def test_v2_auth_failure_reports_a_distinct_label_and_message():
    v2_status = {
        "health_status": "error",
        "last_error_class": "auth_failure",
        "pending_outbox_count": None,
        "quarantined_count": None,
        "last_heartbeat_at": "2026-10-05T17:59:00Z",
        "last_success_at": None,
    }
    ctx = build_v2_aware_pipeline_context(None, v2_status, empty_df(), NOW_CT, APP_TZ, "light")
    assert ctx["pipeline_status_label"] == "Pipeline Auth Failure"
    assert "ingest key" in ctx["pipeline_result_text"]


def test_v2_status_remains_tenant_scoped_by_only_ever_reading_the_dict_it_was_given():
    # build_v2_aware_pipeline_context takes already-scoped inputs; it does not query anything itself, so it cannot
    # cross tenants regardless of what pipeline_status/v2_ingest_status happen to contain.
    tenant_a_status = {"health_status": "healthy", "last_error_class": None, "pending_outbox_count": 0,
                       "quarantined_count": 0, "last_heartbeat_at": "2026-10-05T17:59:00Z", "last_success_at": None}
    ctx = build_v2_aware_pipeline_context(None, tenant_a_status, empty_df(), NOW_CT, APP_TZ, "light")
    assert ctx["pipeline_status_label"] == "Pipeline Healthy"
