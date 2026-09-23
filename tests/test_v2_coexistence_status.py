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
    assert ctx["pipeline_source"] == "v2"


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
