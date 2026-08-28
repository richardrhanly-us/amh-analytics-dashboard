"""Tests for src/services/pipeline_context_service.py -- specifically the
Phase 3 compatibility rule: prefer health_status (the new continuous-agent
heartbeat vocabulary) when present, fall back to legacy status parsing
(the scheduled run_pipeline vocabulary) when it isn't. Both must keep
working during Continuous Ingestion Phase 0's parallel-validation
coexistence window.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from services.pipeline_context_service import build_pipeline_context

APP_TZ = ZoneInfo("America/Chicago")
NOW_CT = datetime(2026, 8, 28, 12, 0, tzinfo=APP_TZ)


def empty_df():
    return pd.DataFrame(columns=["datetime"])


def _ctx(pipeline_status):
    return build_pipeline_context(
        pipeline_status=pipeline_status,
        df_live_raw=empty_df(),
        now_ct=NOW_CT,
        local_tz=APP_TZ,
        theme_base="light",
    )


def test_health_status_healthy_maps_to_green():
    ctx = _ctx({"health_status": "healthy", "status": "completed"})
    assert ctx["pipeline_status_label"] == "Pipeline Healthy"
    assert ctx["pipeline_status_color"] == "#059669"


def test_health_status_degraded_maps_to_amber():
    ctx = _ctx({"health_status": "degraded"})
    assert ctx["pipeline_status_label"] == "Pipeline Degraded"
    assert ctx["pipeline_status_color"] == "#d97706"


def test_health_status_auth_failure_maps_to_red():
    ctx = _ctx({"health_status": "auth_failure"})
    assert ctx["pipeline_status_label"] == "Pipeline Auth Failure"
    assert ctx["pipeline_status_color"] == "#dc2626"


def test_health_status_present_takes_precedence_over_legacy_status():
    # A branch that has cut over to the heartbeat but still has a stale
    # legacy `status` column value from before -- health_status must win.
    ctx = _ctx({"health_status": "healthy", "status": "failed"})
    assert ctx["pipeline_status_label"] == "Pipeline Healthy"


def test_legacy_status_used_when_health_status_absent():
    # A branch still on the legacy scheduled pipeline only, never having
    # reported a heartbeat -- legacy parsing must still work exactly as
    # before Phase 3.
    ctx = _ctx({"status": "completed"})
    assert ctx["pipeline_status_label"] == "Pipeline Healthy"


def test_legacy_failed_status_used_when_health_status_absent():
    ctx = _ctx({"status": "failed_parse_error"})
    assert ctx["pipeline_status_label"] == "Pipeline Failed"


def test_legacy_unknown_status_when_health_status_absent():
    ctx = _ctx({"status": "some_future_value"})
    assert ctx["pipeline_status_label"] == "Pipeline Status Unknown"


def test_no_pipeline_status_at_all():
    ctx = _ctx(None)
    assert ctx["pipeline_status_label"] == "Pipeline Status Unknown"


def test_status_code_text_reflects_health_status_when_present():
    ctx = _ctx({"health_status": "degraded", "status": "completed"})
    assert ctx["status_code_text"] == "degraded"


def test_status_code_text_reflects_legacy_status_when_health_status_absent():
    ctx = _ctx({"status": "completed"})
    assert ctx["status_code_text"] == "completed"


def test_pipeline_expanded_true_when_degraded():
    ctx = _ctx({"health_status": "degraded"})
    assert ctx["pipeline_expanded"] is True


def test_pipeline_expanded_false_when_healthy_via_health_status():
    ctx = _ctx({"health_status": "healthy"})
    assert ctx["pipeline_expanded"] is False
