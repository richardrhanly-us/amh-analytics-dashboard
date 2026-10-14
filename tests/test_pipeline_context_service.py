"""Tests for src/services/pipeline_context_service.py.

The dashboard may receive both scheduled Collector run status and
continuous-agent heartbeat status in the same pipeline_status row.
Fresh heartbeat state should win only when its watcher timestamp is newer
than the latest Collector attempt; otherwise Collector run status wins.
"""


from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from services import ingest_v2_models
from services import pipeline_context_service as service
from services.pipeline_context_service import (
    build_pipeline_context,
    build_v2_aware_pipeline_context,
)
from services.pipeline_state import (
    state_for_current_health,
    state_for_legacy_health,
    state_for_run_status,
)

APP_TZ = ZoneInfo("America/Chicago")
NOW_CT = datetime(2026, 8, 28, 12, 0, tzinfo=APP_TZ)  # freshness: allow FRESH004 -- passed as now_ct= to every call under test


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
    ctx = _ctx(
        {
            "health_status": "healthy",
            "watcher_last_active_at": "2026-09-18T14:13:00Z",
            "status": "failed",
            "last_attempt": "2026-09-18T14:12:00Z",
        }
    )
    assert ctx["pipeline_status_label"] == "Pipeline Healthy"
    assert ctx["pipeline_status_color"] == "#047857"


def test_health_status_degraded_maps_to_amber():
    ctx = _ctx(
        {
            "health_status": "degraded",
            "watcher_last_active_at": "2026-09-18T14:13:00Z",
            "last_attempt": "2026-09-18T14:12:00Z",
        }
    )
    assert ctx["pipeline_status_label"] == "Pipeline Degraded"
    assert ctx["pipeline_status_color"] == "#92400e"


def test_health_status_auth_failure_maps_to_red():
    ctx = _ctx(
        {
            "health_status": "auth_failure",
            "watcher_last_active_at": "2026-09-18T14:13:00Z",
            "last_attempt": "2026-09-18T14:12:00Z",
        }
    )
    assert ctx["pipeline_status_label"] == "Pipeline Auth Failure"
    assert ctx["pipeline_status_color"] == "#b91c1c"


def test_stale_health_status_does_not_override_newer_collector_status():
    ctx = _ctx(
        {
            "health_status": "healthy",
            "watcher_last_active_at": "2026-09-18T14:11:00Z",
            "status": "failed",
            "last_attempt": "2026-09-18T14:12:00Z",
        }
    )
    assert ctx["pipeline_status_label"] == "Pipeline Failed"


def test_heartbeat_after_collector_start_but_before_completion_does_not_win():
    ctx = _ctx(
        {
            "health_status": "healthy",
            "watcher_last_active_at": "2026-09-18T14:11:00Z",
            "status": "failed",
            "last_attempt": "2026-09-18T14:10:00Z",
            "last_run": "2026-09-18T14:12:00Z",
        }
    )
    assert ctx["pipeline_status_label"] == "Pipeline Failed"
    assert ctx["status_code_text"] == "failed"

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


def test_status_code_text_reflects_fresh_health_status():
    ctx = _ctx(
        {
            "health_status": "degraded",
            "watcher_last_active_at": "2026-09-18T14:13:00Z",
            "status": "completed",
            "last_attempt": "2026-09-18T14:12:00Z",
        }
    )
    assert ctx["status_code_text"] == "degraded"


def test_status_code_text_reflects_legacy_status_when_health_status_absent():
    ctx = _ctx({"status": "completed"})
    assert ctx["status_code_text"] == "completed"


def test_pipeline_expanded_true_when_degraded():
    ctx = _ctx(
        {
            "health_status": "degraded",
            "watcher_last_active_at": "2026-09-18T14:13:00Z",
            "last_attempt": "2026-09-18T14:12:00Z",
        }
    )
    assert ctx["pipeline_expanded"] is True


def test_pipeline_expanded_false_when_healthy_via_fresh_health_status():
    ctx = _ctx(
        {
            "health_status": "healthy",
            "watcher_last_active_at": "2026-09-18T14:13:00Z",
            "last_attempt": "2026-09-18T14:12:00Z",
        }
    )
    assert ctx["pipeline_expanded"] is False


# =====================================================================================================================
# Parity with services.pipeline_state: the panel's colour family is the shared state, shown
# =====================================================================================================================
#
# The panel shows each pipeline state in one colour family. Whatever a stored value means is pipeline_state's rule (the
# customer API's); these prove the dashboard reaches the same state for every value either writer stores. The one
# exception is presentation only: a legacy run reported as "started" is `unknown` to pipeline_state (its outcome has not
# been reported) and the dashboard shows it as "Pipeline Running" in the amber family.

FAMILY_FOR_STATE = {"ok": "healthy", "degraded": "degraded", "failed": "failed", "unknown": "unknown"}
_FAMILY_FOR_COLORS = {pairs["light"]: family for family, pairs in service._STATUS_COLOR_PAIRS.items()}


def _family(ctx):
    return _FAMILY_FOR_COLORS[(ctx["pipeline_status_color"], ctx["pipeline_status_bg"])]


# Every value a writer stores for each signal, and values no writer stores. Each table is one case: what each value
# MEANS is pinned value by value in tests/test_pipeline_state.py; these prove the panel takes its state from there (a
# local re-implementation would part company on the odd values) and that every family is reached.
LEGACY_HEALTH = ["healthy", "degraded", "auth_failure", "error", "Healthy", "", "some_future_value"]
RUN_STATUSES = ["completed", "completed_no_new_rows", "skipped_no_source_changes", "failed", "failed_upload",
                "failed_upload_none", "failed_parse_error", "preflight_check", "some_future_value", "", "Completed",
                "FAILED", None, 5]
CURRENT_HEALTH = [*ingest_v2_models.HEALTH_STATUSES, None, "", "something_else", "Healthy"]

# The panel's own words for each finished run (presentation, not a state rule).
OK_RUN_RESULT_TEXT = {
    "completed": "Uploaded 1,234 new checkins and 5 new rejects this run",
    "completed_no_new_rows": "Run completed, but no new rows were uploaded",
    "skipped_no_source_changes": "No new source changes detected this run",
}


def _mismatches(values, state_for, context_for):
    """(value, what the panel showed, what the shared state says) for every value the panel shows differently."""
    found = []
    for value in values:
        ctx, state = context_for(value), state_for(value)
        shown = (_family(ctx), ctx["pipeline_expanded"])
        if shown != (FAMILY_FOR_STATE[state], state != "ok"):
            found.append((value, shown, state))
    return found


def test_a_current_legacy_heartbeat_is_shown_in_the_family_of_its_shared_state():
    def context_for(health_status):
        return _ctx({"health_status": health_status, "watcher_last_active_at": "2026-09-18T14:13:00Z",
                     "last_attempt": "2026-09-18T14:12:00Z"})

    assert _mismatches(LEGACY_HEALTH, state_for_legacy_health, context_for) == []
    assert {_family(context_for(value)) for value in LEGACY_HEALTH} == {"healthy", "degraded", "failed", "unknown"}
    # A value pipeline_state does not recognise is shown as unknown, in the panel's own words for that state.
    for value in [value for value in LEGACY_HEALTH if state_for_legacy_health(value) == "unknown"]:
        ctx = context_for(value)
        assert (ctx["pipeline_status_label"], ctx["pipeline_result_text"]) == ("Pipeline Status Unknown", "Unknown"), value


def test_a_legacy_run_status_is_shown_in_the_family_of_its_shared_state_and_a_finished_run_in_its_own_words():
    def context_for(status):
        return _ctx({"status": status, "uploaded_checkins_rows": 1234, "uploaded_rejects_rows": 5})

    assert _mismatches(RUN_STATUSES, state_for_run_status, context_for) == []
    assert {_family(context_for(value)) for value in RUN_STATUSES} == {"healthy", "failed", "unknown"}
    for status, text in OK_RUN_RESULT_TEXT.items():
        ctx = context_for(status)
        assert (ctx["pipeline_status_label"], ctx["pipeline_result_text"]) == ("Pipeline Healthy", text), status


def test_the_one_presentation_exception_a_started_run_is_shown_as_running():
    ctx = _ctx({"status": "started"})

    assert state_for_run_status("started") == "unknown"
    assert ctx["pipeline_status_label"] == "Pipeline Running" and _family(ctx) == "degraded"
    assert ctx["pipeline_result_text"] == "Run in progress" and ctx["pipeline_expanded"] is True


def test_a_contract_v2_heartbeat_is_shown_in_the_family_of_its_shared_state():
    # No schedule reported, so the health alone decides the headline.
    def context_for(health_status):
        heartbeat = {"health_status": health_status, "last_error_class": None, "collector_schedule_status": None}
        return build_v2_aware_pipeline_context(None, heartbeat, empty_df(), NOW_CT, APP_TZ, "light")

    assert _mismatches(CURRENT_HEALTH, state_for_current_health, context_for) == []
    assert {_family(context_for(value)) for value in CURRENT_HEALTH} == {"healthy", "degraded", "failed", "unknown"}
