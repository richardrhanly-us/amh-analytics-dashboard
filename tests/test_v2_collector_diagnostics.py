"""Collector run / schedule diagnostics on the dashboard: the four Pipeline Status lines and what the schedule does to the
panel's headline (services.pipeline_context_service.build_collector_diagnostics / build_v2_aware_pipeline_context), the
loader that feeds them (data_loader.load_v2_ingest_status), and the real view rendering them.

NOW_CT is 3:00 PM Central on 2026-10-01 (20:00 UTC) and is passed as now_ct= to every call, so nothing here reads a
clock. A healthy, current heartbeat at that moment: last run 2:45 PM, next run 3:15 PM... the tests move those around.
"""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from test_pipeline_status_null_counters import _render_run_summary

import data_loader as dl
from collector import v2_events
from services import ingest_v2_models
from services import pipeline_context_service as service
from services.pipeline_context_service import (
    build_pipeline_context,
    build_v2_aware_pipeline_context,
)

APP_TZ = ZoneInfo("America/Chicago")
NOW_CT = datetime(2026, 10, 1, 15, 0, tzinfo=APP_TZ)  # freshness: allow FRESH004 -- passed as now_ct= to every call under test

FAILED_COLORS = service._status_colors("failed", "light")
DEGRADED_COLORS = service._status_colors("degraded", "light")
HEALTHY_COLORS = service._status_colors("healthy", "light")


def utc_iso(minutes_from_now: float) -> str:
    """An instant `minutes_from_now` relative to NOW_CT, in the isoformat() shape the loader hands the service."""
    return (NOW_CT + timedelta(minutes=minutes_from_now)).astimezone(ZoneInfo("UTC")).isoformat()


def heartbeat(**overrides):
    """A healthy, current Contract v2 heartbeat row as load_v2_ingest_status returns it."""
    row = {
        "health_status": "healthy", "last_error_class": None, "pending_outbox_count": 0, "quarantined_count": 0,
        "last_heartbeat_at": utc_iso(-14.9), "watcher_last_active_at": utc_iso(-15), "last_success_at": utc_iso(-15),
        "collector_last_run_at": utc_iso(-15), "collector_next_run_at": utc_iso(12),
        "collector_run_duration_ms": 3240, "collector_schedule_status": "healthy",
    }
    row.update(overrides)
    return row


OLD_COLLECTOR = {"collector_last_run_at": None, "collector_next_run_at": None, "collector_run_duration_ms": None,
                 "collector_schedule_status": None}


def context(v2_status, v1_status=None):
    return build_v2_aware_pipeline_context(v1_status, v2_status, pd.DataFrame(columns=["datetime"]), NOW_CT, APP_TZ, "light")


def lines(v2_status):
    return context(v2_status)["collector_diagnostics_lines"]


# ======================================================================================================================
# the four lines
# ======================================================================================================================

def test_a_healthy_current_heartbeat_renders_the_four_lines_as_specified():
    assert lines(heartbeat()) == [
        "Last Collector Run: Oct 01, 2026 02:45 PM (15 min ago)",
        "Next Scheduled Run: Oct 01, 2026 03:12 PM (in 12 min)",
        "Latest Run Duration: 3.24 seconds",
        "Collector Schedule: Healthy",
    ]


@pytest.mark.parametrize(("duration_ms", "text"), [
    (3240, "3.24 seconds"), (0, "0.00 seconds"), (5, "0.01 seconds"), (999, "1.00 seconds"), (1000, "1.00 seconds"),
    (61_005, "61.01 seconds"), (3_599_990, "3599.99 seconds"), (86_400_000, "86400.00 seconds"),
])
def test_the_duration_is_shown_in_seconds_with_two_decimals(duration_ms, text):
    assert lines(heartbeat(collector_run_duration_ms=duration_ms))[2] == f"Latest Run Duration: {text}"


@pytest.mark.parametrize("duration_ms", [None, -1, "3240", 3.24, True, float("nan")])
def test_a_missing_or_malformed_duration_is_na_never_a_crash(duration_ms):
    assert lines(heartbeat(collector_run_duration_ms=duration_ms))[2] == "Latest Run Duration: N/A"


@pytest.mark.parametrize(("minutes", "text"), [
    (12, "in 12 min"), (0.5, "in under a minute"), (1, "in 1 min"), (59, "in 59 min"), (60, "in 1 hr"), (125, "in 2 hrs"),
    (-3, "3 min ago"), (-0.5, "just now"),
])
def test_the_next_run_is_described_relative_to_now_in_either_direction(minutes, text):
    line = lines(heartbeat(collector_next_run_at=utc_iso(minutes)))[1]

    assert line.startswith("Next Scheduled Run: Oct 01, 2026 ") and line.endswith(f"({text})")


def test_times_are_shown_in_the_dashboards_local_time_zone():
    row = heartbeat(collector_last_run_at="2026-10-01T19:45:00+00:00", collector_next_run_at="2026-10-01T20:15:00+00:00")

    assert lines(row)[0] == "Last Collector Run: Oct 01, 2026 02:45 PM (15 min ago)"   # 19:45 UTC = 2:45 PM CDT
    assert lines(row)[1] == "Next Scheduled Run: Oct 01, 2026 03:15 PM (in 15 min)"


# ======================================================================================================================
# an older collector: all four fields absent
# ======================================================================================================================

@pytest.mark.parametrize("row", [
    heartbeat(**OLD_COLLECTOR),                                                         # the columns exist, all NULL
    {k: v for k, v in heartbeat().items() if not k.startswith("collector_")},           # the keys are not there at all
])
def test_an_old_collector_heartbeat_with_all_four_fields_absent_renders_safely_and_changes_nothing(row):
    ctx = context(row)

    assert ctx["collector_diagnostics_lines"] == [
        "Last Collector Run: N/A",
        "Next Scheduled Run: N/A",
        "Latest Run Duration: N/A",
        "Collector Schedule: Unknown (not reported by this collector version)",
    ]
    # ... and the headline is exactly what it was before the diagnostics existed.
    assert ctx["pipeline_status_label"] == "Pipeline Healthy" and ctx["pipeline_expanded"] is False
    assert (ctx["pipeline_status_color"], ctx["pipeline_status_bg"]) == HEALTHY_COLORS
    assert ctx["pipeline_result_text"] == "Contract v2 collector reporting healthy"


@pytest.mark.parametrize("health", ["degraded", "error"])
def test_an_old_collector_heartbeat_never_changes_a_non_healthy_headline_either(health):
    with_fields = context(heartbeat(health_status=health, **OLD_COLLECTOR))
    before = {k: v for k, v in with_fields.items() if k != "collector_diagnostics_lines"}

    assert before["pipeline_status_label"] == ("Pipeline Degraded" if health == "degraded" else "Pipeline Failed")
    assert "schedule" not in before["pipeline_result_text"].lower()


# ======================================================================================================================
# the overdue rule: two cadences (the grace itself is services.collector_schedule's, see tests/test_collector_schedule.py)
# ======================================================================================================================

@pytest.mark.parametrize("minutes_overdue", [0, 1, 14, 15, 16, 29, 29.9, 30])
def test_a_healthy_schedule_up_to_thirty_minutes_overdue_is_still_healthy(minutes_overdue):
    ctx = context(heartbeat(collector_next_run_at=utc_iso(-minutes_overdue)))

    assert ctx["collector_diagnostics_lines"][3] == "Collector Schedule: Healthy"
    assert ctx["pipeline_status_label"] == "Pipeline Healthy" and ctx["pipeline_expanded"] is False


@pytest.mark.parametrize("minutes_overdue", [30.1, 31, 45, 60, 24 * 60, 7 * 24 * 60])
def test_a_healthy_schedule_more_than_thirty_minutes_overdue_is_a_schedule_error(minutes_overdue):
    ctx = context(heartbeat(collector_next_run_at=utc_iso(-minutes_overdue)))

    assert ctx["collector_diagnostics_lines"][3] == "Collector Schedule: Error — scheduled run overdue"
    assert ctx["pipeline_status_label"] == "Pipeline Schedule Error" and ctx["pipeline_expanded"] is True
    assert (ctx["pipeline_status_color"], ctx["pipeline_status_bg"]) == FAILED_COLORS


def test_a_healthy_status_does_not_stay_green_forever_on_a_stale_next_run():
    # The collector reported healthy, then stopped. Nothing newer ever arrives; only the clock moves.
    row = heartbeat()
    assert context(row)["pipeline_status_label"] == "Pipeline Healthy"

    later = NOW_CT + timedelta(hours=3)
    stale = build_v2_aware_pipeline_context(None, row, pd.DataFrame(columns=["datetime"]), later, APP_TZ, "light")

    assert stale["pipeline_status_label"] == "Pipeline Schedule Error"
    assert stale["collector_diagnostics_lines"][3] == "Collector Schedule: Error — scheduled run overdue"
    assert stale["collector_diagnostics_lines"][1].endswith("(2 hrs ago)")


def test_a_healthy_status_with_no_next_run_at_all_is_not_trusted_as_healthy():
    ctx = context(heartbeat(collector_next_run_at=None))

    assert ctx["collector_diagnostics_lines"][1] == "Next Scheduled Run: N/A"
    assert ctx["collector_diagnostics_lines"][3] == "Collector Schedule: Error — no future run scheduled"
    assert ctx["pipeline_status_label"] == "Pipeline Schedule Error"


# ======================================================================================================================
# structural failures the collector reports: no grace, red
# ======================================================================================================================

STRUCTURAL = [
    ("task_missing", "scheduled task not found"),
    ("task_disabled", "scheduled task is disabled"),
    ("no_next_run", "no future run scheduled"),
]


@pytest.mark.parametrize(("status", "reason"), STRUCTURAL)
def test_a_structural_schedule_failure_overrides_an_otherwise_healthy_pipeline(status, reason):
    # A fresh heartbeat (seconds old) and no next run: the error is immediate, it does not wait for the overdue grace.
    ctx = context(heartbeat(collector_schedule_status=status, collector_next_run_at=None, last_heartbeat_at=utc_iso(-0.1)))

    assert ctx["collector_diagnostics_lines"][3] == f"Collector Schedule: Error — {reason}"
    assert ctx["pipeline_status_label"] == "Pipeline Schedule Error" and ctx["pipeline_expanded"] is True
    assert (ctx["pipeline_status_color"], ctx["pipeline_status_bg"]) == FAILED_COLORS
    assert ctx["pipeline_result_text"] == f"Collector schedule error -- {reason} (ingestion last reported healthy)"
    assert ctx["status_code_text"] == "healthy"  # the ingestion code itself is reported unchanged


@pytest.mark.parametrize(("status", "reason"), STRUCTURAL)
def test_a_structural_schedule_failure_overrides_a_degraded_pipeline_too(status, reason):
    ctx = context(heartbeat(health_status="degraded", collector_schedule_status=status, collector_next_run_at=None))

    assert ctx["pipeline_status_label"] == "Pipeline Schedule Error"
    assert ctx["pipeline_result_text"] == f"Collector schedule error -- {reason} (ingestion last reported degraded)"


def test_the_schedule_error_text_is_words_not_only_a_colour():
    ctx = context(heartbeat(collector_schedule_status="no_next_run", collector_next_run_at=None))

    assert "Error" in ctx["collector_diagnostics_lines"][3] and "no future run scheduled" in ctx["collector_diagnostics_lines"][3]
    assert "Schedule Error" in ctx["pipeline_status_label"]
    assert not any("<" in line or "color" in line for line in ctx["collector_diagnostics_lines"])  # plain text only


# ======================================================================================================================
# query_failed: an explicit error on the line, DEGRADED (not failed) at the top
# ======================================================================================================================

def test_query_failed_degrades_an_otherwise_healthy_pipeline_without_failing_it():
    ctx = context(heartbeat(collector_schedule_status="query_failed", collector_last_run_at=None, collector_next_run_at=None))

    assert ctx["collector_diagnostics_lines"][3] == "Collector Schedule: Error — schedule could not be checked"
    assert ctx["pipeline_status_label"] == "Pipeline Degraded" and ctx["pipeline_expanded"] is True
    assert (ctx["pipeline_status_color"], ctx["pipeline_status_bg"]) == DEGRADED_COLORS
    assert ctx["pipeline_result_text"] == "Contract v2 collector reporting healthy, but its schedule could not be checked"
    assert ctx["collector_diagnostics_lines"][2] == "Latest Run Duration: 3.24 seconds"  # the duration is still real


def test_query_failed_leaves_an_already_degraded_pipeline_as_it_was():
    plain = context(heartbeat(health_status="degraded", quarantined_count=3, **OLD_COLLECTOR))
    ctx = context(heartbeat(health_status="degraded", quarantined_count=3, collector_schedule_status="query_failed",
                            collector_next_run_at=None))

    assert ctx["pipeline_status_label"] == "Pipeline Degraded"
    assert ctx["pipeline_result_text"] == plain["pipeline_result_text"]  # the more specific ingestion text is kept


def test_a_schedule_code_this_dashboard_does_not_know_is_treated_as_not_checked_never_as_healthy():
    ctx = context(heartbeat(collector_schedule_status="some_future_code"))

    assert ctx["collector_diagnostics_lines"][3] == "Collector Schedule: Error — schedule could not be checked"
    assert ctx["pipeline_status_label"] == "Pipeline Degraded"


# ======================================================================================================================
# an existing ingestion error keeps precedence
# ======================================================================================================================

@pytest.mark.parametrize("schedule", ["healthy", "task_missing", "task_disabled", "no_next_run", "query_failed", None])
def test_an_auth_failure_stays_the_headline_whatever_the_schedule_says(schedule):
    ctx = context(heartbeat(health_status="error", last_error_class="auth_failure", collector_schedule_status=schedule,
                            collector_next_run_at=None if schedule != "healthy" else utc_iso(12)))

    assert ctx["pipeline_status_label"] == "Pipeline Auth Failure"
    assert ctx["pipeline_result_text"] == "Contract v2 collector cannot authenticate -- check the ingest key"
    assert (ctx["pipeline_status_color"], ctx["pipeline_status_bg"]) == FAILED_COLORS and ctx["pipeline_expanded"] is True


@pytest.mark.parametrize("schedule", ["task_missing", "query_failed"])
def test_any_other_ingestion_error_also_keeps_its_own_label_and_text(schedule):
    ctx = context(heartbeat(health_status="error", last_error_class="configuration_error",
                            collector_schedule_status=schedule, collector_next_run_at=None))

    assert ctx["pipeline_status_label"] == "Pipeline Failed"
    assert "last_error_class=configuration_error" in ctx["pipeline_result_text"]
    # ... while the schedule line beneath it still tells the truth.
    assert ctx["collector_diagnostics_lines"][3].startswith("Collector Schedule: Error — ")


def test_an_overdue_schedule_does_not_displace_an_auth_failure_either():
    ctx = context(heartbeat(health_status="error", last_error_class="auth_failure", collector_next_run_at=utc_iso(-600)))

    assert ctx["pipeline_status_label"] == "Pipeline Auth Failure"
    assert ctx["collector_diagnostics_lines"][3] == "Collector Schedule: Error — scheduled run overdue"


# ======================================================================================================================
# an unknown health: a demonstrable schedule error still shows; an unconfirmed schedule changes nothing
# ======================================================================================================================

def test_an_unknown_health_with_an_overdue_schedule_is_a_schedule_error():
    ctx = context(heartbeat(health_status=None, collector_next_run_at=utc_iso(-31)))

    assert ctx["pipeline_status_label"] == "Pipeline Schedule Error" and ctx["pipeline_expanded"] is True
    assert (ctx["pipeline_status_color"], ctx["pipeline_status_bg"]) == FAILED_COLORS
    assert ctx["pipeline_result_text"] == "Collector schedule error -- scheduled run overdue (ingestion last reported no status)"


def test_a_cut_over_branch_with_an_unrecognised_legacy_heartbeat_still_shows_its_v2_headline():
    # The frozen v1 row is still read first; a health value pipeline_state does not recognise must not break the panel.
    legacy = {"health_status": "error", "watcher_last_active_at": utc_iso(-1), "last_attempt": utc_iso(-2)}
    ctx = context(heartbeat(), v1_status=legacy)

    assert ctx["pipeline_status_label"] == "Pipeline Healthy"
    assert ctx["pipeline_result_text"] == "Contract v2 collector reporting healthy"


def test_an_unknown_health_with_an_unreadable_schedule_stays_unknown():
    # The dashboard's rule, kept on purpose: pipeline_state.state_for_current_report would call this "degraded".
    ctx = context(heartbeat(health_status=None, collector_schedule_status="query_failed", collector_next_run_at=None))

    assert ctx["pipeline_status_label"] == "Pipeline Status Unknown" and ctx["pipeline_expanded"] is True
    assert (ctx["pipeline_status_color"], ctx["pipeline_status_bg"]) == service._status_colors("unknown", "light")
    assert ctx["collector_diagnostics_lines"][3] == "Collector Schedule: Error — schedule could not be checked"


# ======================================================================================================================
# every code has words (the judgements themselves are tests/test_collector_schedule.py's)
# ======================================================================================================================

def test_every_code_the_collector_can_send_has_a_fixed_line_on_the_dashboard():
    assert ingest_v2_models.SCHEDULE_STATUSES == v2_events.SCHEDULE_STATUSES
    for status in ingest_v2_models.SCHEDULE_STATUSES:
        line = lines(heartbeat(collector_schedule_status=status))[3]
        assert line == "Collector Schedule: Healthy" or line.startswith("Collector Schedule: Error — "), status
    assert set(service._SCHEDULE_REASON_TEXT) | {"healthy", "query_failed"} == set(ingest_v2_models.SCHEDULE_STATUSES)


# ======================================================================================================================
# v1 / no-v2 branches: unchanged
# ======================================================================================================================

V1_STATUS = {"status": "completed", "uploaded_checkins_rows": 5, "uploaded_rejects_rows": 1,
             "updated_at": "2026-10-01T19:59:00Z", "last_run": "2026-10-01T19:58:00Z", "last_attempt": "2026-10-01T19:58:00Z"}


def test_a_v1_branch_has_no_collector_diagnostics_lines_and_an_identical_context():
    v1 = build_pipeline_context(V1_STATUS, pd.DataFrame(columns=["datetime"]), NOW_CT, APP_TZ, "light")
    through_v2_aware = context(None, v1_status=V1_STATUS)

    assert through_v2_aware == v1 and v1["collector_diagnostics_lines"] is None
    assert v1["pipeline_status_label"] == "Pipeline Healthy"


def test_the_v1_panel_shows_a_customer_only_the_state_and_its_freshness():
    # A2 item 3: no attempt or success line, no status code, and a result sentence not made from the run's counters.
    v1 = build_pipeline_context(V1_STATUS, pd.DataFrame(columns=["datetime"]), NOW_CT, APP_TZ, "light")

    panel = next(body for body in _render_run_summary(v1) if "##### Pipeline Status" in body)

    for label in ("Last Collector Run", "Next Scheduled Run", "Latest Run Duration", "Collector Schedule",
                  "Last Successful Upload Run", "Last Pipeline Attempt", "Status Code", "Uploaded 5 new checkins"):
        assert label not in panel, label
    assert "Latest Report Received: " in panel
    assert panel.endswith("Latest Result: Latest run completed")


def test_the_v2_and_v1_contexts_still_have_the_same_keys_and_bind_to_the_view():
    import inspect

    from views import live_today_view

    v1 = build_pipeline_context(V1_STATUS, pd.DataFrame(columns=["datetime"]), NOW_CT, APP_TZ, "light")
    v2 = context(heartbeat())

    assert set(v1) == set(v2)
    parameters = inspect.signature(live_today_view.render_live_today).parameters
    assert set(v2) <= set(parameters)  # dashboard_context splats this dict into render_live_today(**...)
    assert parameters["collector_diagnostics_lines"].default is None


# ======================================================================================================================
# the real view
# ======================================================================================================================

# A2 item 3: the four lines are still built (the context is unchanged, and the headline still follows the schedule),
# but the customer panel renders none of them -- the collector's run and schedule facts are admin-only now.
COLLECTOR_LINES = ("Last Collector Run", "Next Scheduled Run", "Latest Run Duration", "Collector Schedule")


def test_the_real_panel_renders_none_of_the_collector_lines_to_a_customer():
    panel = next(body for body in _render_run_summary(context(heartbeat())) if "##### Pipeline Status" in body)

    for line in COLLECTOR_LINES + ("Last Successful Upload Run", "Last Pipeline Attempt", "Status Code", "<span"):
        assert line not in panel, line
    assert "Latest Report Received: " in panel
    assert panel.endswith("Latest Result: Collector reporting healthy")


def test_the_real_panel_says_a_schedule_error_only_as_a_failure():
    ctx = context(heartbeat(collector_schedule_status="no_next_run", collector_next_run_at=None))

    panel = next(body for body in _render_run_summary(ctx) if "##### Pipeline Status" in body)

    assert ctx["pipeline_status_label"] == "Pipeline Schedule Error"      # the headline is still decided by the schedule
    assert ctx["customer_status_label"] == "Pipeline Failed"
    assert panel.endswith("Latest Result: Collector reporting failed")
    for detail in COLLECTOR_LINES + ("no future run", "schedule", "Error —"):
        assert detail not in panel, detail


def test_the_real_panel_renders_an_old_collectors_heartbeat_without_crashing():
    panel = next(body for body in _render_run_summary(context(heartbeat(**OLD_COLLECTOR))) if "##### Pipeline Status" in body)

    assert "not reported by this collector version" not in panel
    assert "Latest Result: " in panel


# ======================================================================================================================
# the loader
# ======================================================================================================================

@pytest.fixture(autouse=True)
def _clear_loader_cache():
    dl.load_v2_ingest_status.clear()
    yield
    dl.load_v2_ingest_status.clear()


def _loaded(monkeypatch, **columns):
    record = {
        "key_id": "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e", "key_status": "active", "health_status": "healthy",
        "last_error_class": None, "pending_outbox_count": 0, "quarantined_count": 0, "oldest_pending_event_at": pd.NaT,
        "last_success_at": pd.Timestamp("2026-10-01T19:45:00Z"), "watcher_last_active_at": pd.Timestamp("2026-10-01T19:45:00Z"),
        "last_heartbeat_at": pd.Timestamp("2026-10-01T19:45:06Z"), **columns,
    }
    queries = []

    def fake_read_table(query, params=None, *, customer_id=None, branch_id=None):
        queries.append((query, params, customer_id, branch_id))
        return pd.DataFrame.from_records([record], coerce_float=True)

    monkeypatch.setattr(dl, "_read_table", fake_read_table)
    return dl.load_v2_ingest_status(10, 1), queries


def test_the_loader_selects_the_four_columns_scoped_to_the_tenant(monkeypatch):
    row, ((query, params, customer_id, branch_id),) = _loaded(
        monkeypatch, collector_last_run_at=pd.Timestamp("2026-10-01T19:45:00Z"),
        collector_next_run_at=pd.Timestamp("2026-10-01T20:00:00Z"), collector_run_duration_ms=3240,
        collector_schedule_status="healthy")

    for column in ("collector_last_run_at", "collector_next_run_at", "collector_run_duration_ms", "collector_schedule_status"):
        assert column in query
    assert params == {"org_slug": 10, "branch_slug": 1} and (customer_id, branch_id) == (10, 1)
    assert row["collector_last_run_at"] == "2026-10-01T19:45:00+00:00"
    assert row["collector_next_run_at"] == "2026-10-01T20:00:00+00:00"
    assert row["collector_run_duration_ms"] == 3240 and type(row["collector_run_duration_ms"]) is int
    assert row["collector_schedule_status"] == "healthy"


def test_the_loader_turns_null_diagnostics_into_none_not_nan(monkeypatch):
    # An older collector's row: pandas hands NULLs back as NaT / NaN / None.
    row, _queries = _loaded(monkeypatch, collector_last_run_at=pd.NaT, collector_next_run_at=pd.NaT,
                            collector_run_duration_ms=float("nan"), collector_schedule_status=None)

    assert [row[k] for k in OLD_COLLECTOR] == [None, None, None, None]
    assert context(row)["collector_diagnostics_lines"][3] == "Collector Schedule: Unknown (not reported by this collector version)"


def test_a_loaded_row_flows_into_the_panel(monkeypatch):
    row, _queries = _loaded(
        monkeypatch, collector_last_run_at=pd.Timestamp("2026-10-01T19:45:00Z"),
        collector_next_run_at=pd.Timestamp("2026-10-01T20:12:00Z"), collector_run_duration_ms=3240,
        collector_schedule_status="healthy")

    assert context(row)["collector_diagnostics_lines"] == [
        "Last Collector Run: Oct 01, 2026 02:45 PM (15 min ago)",
        "Next Scheduled Run: Oct 01, 2026 03:12 PM (in 12 min)",
        "Latest Run Duration: 3.24 seconds",
        "Collector Schedule: Healthy",
    ]


# ======================================================================================================================
# A2 item 3: what a customer is shown -- the same headline state, worded without its diagnostics
# ======================================================================================================================

V2_CASES = [
    # (heartbeat overrides, the headline label as derived before A2 item 3, the customer label, the customer result)
    ({}, "Pipeline Healthy", "Pipeline Healthy", "Collector reporting healthy"),
    ({"health_status": "degraded", "last_error_class": "retryable_infra", "pending_outbox_count": 12,
      "quarantined_count": 3}, "Pipeline Degraded", "Pipeline Degraded", "Collector reporting degraded"),
    ({"health_status": "error", "last_error_class": "configuration_error"},
     "Pipeline Failed", "Pipeline Failed", "Collector reporting failed"),
    ({"health_status": "error", "last_error_class": "auth_failure"},
     "Pipeline Auth Failure", "Pipeline Failed", "Collector reporting failed"),
    ({"collector_schedule_status": "task_missing", "collector_next_run_at": None},
     "Pipeline Schedule Error", "Pipeline Failed", "Collector reporting failed"),
    ({"collector_next_run_at": utc_iso(-600)}, "Pipeline Schedule Error", "Pipeline Failed", "Collector reporting failed"),
]


@pytest.mark.parametrize(("overrides", "headline", "customer_label", "customer_result"), V2_CASES)
def test_a_v2_headline_is_derived_as_before_and_worded_for_a_customer(overrides, headline, customer_label,
                                                                      customer_result):
    ctx = context(heartbeat(**overrides))

    assert ctx["pipeline_status_label"] == headline
    assert ctx["customer_status_label"] == customer_label
    assert ctx["customer_result_text"] == customer_result


def test_a_v2_branch_with_no_health_yet_says_so_in_plain_words():
    ctx = context(heartbeat(health_status=None, **OLD_COLLECTOR))

    assert ctx["customer_result_text"] == "No collector status has been recorded yet"


V2_DEEP_TEXT = (
    "Pending", "pending", "Quarantine", "quarantine", "last_error_class", "retryable_infra", "configuration_error",
    "auth_failure", "authenticate", "ingest key", "Contract v2", "Collector Schedule", "Next Scheduled Run",
    "Last Collector Run", "Latest Run Duration", "Status Code", "Last Pipeline Attempt", "Last Successful Upload Run",
    "Run Summary", "Destination Breakdown", "<span",
)


@pytest.mark.parametrize(("overrides", "headline", "customer_label", "customer_result"), V2_CASES)
def test_no_deep_v2_diagnostic_is_rendered_to_a_customer(overrides, headline, customer_label, customer_result):
    panel = next(body for body in _render_run_summary(context(heartbeat(**overrides))) if "##### Pipeline Status" in body)

    for text in V2_DEEP_TEXT:
        assert text not in panel, text
    assert panel.endswith(f"Latest Result: {customer_result}")


V1_RUN_CASES = [
    ("completed", "Pipeline Healthy", "Latest run completed"),
    ("completed_no_new_rows", "Pipeline Healthy", "Latest run completed"),
    ("skipped_no_source_changes", "Pipeline Healthy", "Latest run completed"),
    ("failed_upload", "Pipeline Failed", "Latest run failed"),
    ("started", "Pipeline Running", "Run in progress"),
    ("preflight_check", "Pipeline Status Unknown", "Unknown"),
]


@pytest.mark.parametrize(("run_status", "headline", "customer_result"), V1_RUN_CASES)
def test_a_v1_run_keeps_its_headline_and_gets_a_fixed_customer_sentence(run_status, headline, customer_result):
    ctx = build_pipeline_context({**V1_STATUS, "status": run_status}, pd.DataFrame(columns=["datetime"]), NOW_CT,
                                 APP_TZ, "light")

    assert ctx["pipeline_status_label"] == headline == ctx["customer_status_label"]
    assert ctx["customer_result_text"] == customer_result


V1_AGENT_CASES = [
    ("healthy", "Pipeline Healthy", "Pipeline Healthy", "Continuous agent reporting healthy"),
    ("degraded", "Pipeline Degraded", "Pipeline Degraded", "Continuous agent reporting degraded"),
    ("auth_failure", "Pipeline Auth Failure", "Pipeline Failed", "Continuous agent reporting failed"),
    ("something_new", "Pipeline Status Unknown", "Pipeline Status Unknown", "Unknown"),
]


@pytest.mark.parametrize(("health", "headline", "customer_label", "customer_result"), V1_AGENT_CASES)
def test_a_v1_agent_keeps_its_headline_and_gets_a_fixed_customer_sentence(health, headline, customer_label,
                                                                          customer_result):
    agent = {**V1_STATUS, "health_status": health, "watcher_last_active_at": "2026-10-01T19:59:30Z"}
    ctx = build_pipeline_context(agent, pd.DataFrame(columns=["datetime"]), NOW_CT, APP_TZ, "light")

    assert ctx["pipeline_status_label"] == headline
    assert ctx["customer_status_label"] == customer_label
    assert ctx["customer_result_text"] == customer_result


V1_DEEP_TEXT = (
    "Status Code", "Last Pipeline Attempt", "Last Successful Upload Run", "Run Summary", "Destination Breakdown",
    "This Run", "Transit Items", "Problem Items", "Bad Checkin", "Bad Reject", "Bin A", "Uploaded", "completed`",
    "failed_upload", "auth_failure", "agent token", "backlog", "quarantine",
)


@pytest.mark.parametrize("v1_status", [
    {**V1_STATUS, "status": "failed_upload", "transit_items": 7, "problem_items": 3, "checkins_bad_datetime_rows": 4,
     "destination_breakdown": {"Bin A": 12}},
    {**V1_STATUS, "health_status": "auth_failure", "watcher_last_active_at": "2026-10-01T19:59:30Z"},
    {**V1_STATUS, "health_status": "degraded", "watcher_last_active_at": "2026-10-01T19:59:30Z"},
])
def test_no_v1_counter_raw_code_or_destination_is_rendered_to_a_customer(v1_status):
    ctx = build_pipeline_context(v1_status, pd.DataFrame(columns=["datetime"]), NOW_CT, APP_TZ, "light")

    panel = next(body for body in _render_run_summary(ctx) if "##### Pipeline Status" in body)

    for text in V1_DEEP_TEXT:
        assert text not in panel, text


def test_the_screen_reader_announcement_uses_the_customer_label():
    source = (Path(__file__).resolve().parents[1] / "src" / "app.py").read_text(encoding="utf-8")

    assert 'current_pipeline_status_label = live_view_context["live_today_args"]["customer_status_label"]' in source
    assert 'live_today_args"]["pipeline_status_label"]' not in source
