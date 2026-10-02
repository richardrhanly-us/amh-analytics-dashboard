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
from collector import task_settings, v2_events
from services import ingest_v2_models
from services import pipeline_context_service as service
from services.pipeline_context_service import (
    build_collector_diagnostics,
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
# the overdue rule: two cadences
# ======================================================================================================================

def test_the_overdue_grace_is_two_canonical_cadences_and_is_tied_to_the_collectors_own_constant():
    assert service.COLLECTOR_CADENCE_MINUTES == task_settings.DEFAULT_CADENCE_MINUTES == 15
    assert service.SCHEDULE_OVERDUE_GRACE_CADENCES == 2
    assert service.SCHEDULE_OVERDUE_GRACE == timedelta(minutes=2 * task_settings.DEFAULT_CADENCE_MINUTES) == timedelta(minutes=30)
    # Derived, not a bare literal: the service computes it from the two named constants.
    source = Path(service.__file__).read_text(encoding="utf-8")
    assert "timedelta(minutes=COLLECTOR_CADENCE_MINUTES * SCHEDULE_OVERDUE_GRACE_CADENCES)" in source
    assert "timedelta(minutes=30)" not in source
    assert "MultipleInstancesPolicy=IgnoreNew" in source  # and the reason for two is written down next to it


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
# the judgement function on its own
# ======================================================================================================================

def test_the_four_judgements():
    def judge(**overrides):
        return build_collector_diagnostics(heartbeat(**overrides), NOW_CT, APP_TZ)[:2]

    assert judge() == (service.SCHEDULE_OK, None)
    assert judge(**OLD_COLLECTOR) == (service.SCHEDULE_UNKNOWN, None)
    assert judge(collector_schedule_status="task_missing") == (service.SCHEDULE_ERROR, "scheduled task not found")
    assert judge(collector_next_run_at=utc_iso(-31)) == (service.SCHEDULE_ERROR, "scheduled run overdue")
    assert judge(collector_schedule_status="query_failed") == (service.SCHEDULE_INDETERMINATE, "schedule could not be checked")


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


def test_the_v1_panel_renders_exactly_as_before_with_no_new_line():
    v1 = build_pipeline_context(V1_STATUS, pd.DataFrame(columns=["datetime"]), NOW_CT, APP_TZ, "light")

    panel = next(body for body in _render_run_summary(v1) if "##### Pipeline Status" in body)

    for label in ("Last Collector Run", "Next Scheduled Run", "Latest Run Duration", "Collector Schedule"):
        assert label not in panel
    assert ("Last Successful Upload Run: Oct 01, 2026 02:58 PM (2 min ago)  \n"
            "Latest Result: Uploaded 5 new checkins and 1 new rejects this run  \n"
            "Status Code: `completed`") in panel  # the lines around the insertion point are untouched


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

def test_the_real_panel_renders_the_four_lines_as_plain_text_between_the_run_and_result_lines():
    panel = next(body for body in _render_run_summary(context(heartbeat())) if "##### Pipeline Status" in body)

    expected = (
        "Last Collector Run: Oct 01, 2026 02:45 PM (15 min ago)  \n"
        "Next Scheduled Run: Oct 01, 2026 03:12 PM (in 12 min)  \n"
        "Latest Run Duration: 3.24 seconds  \n"
        "Collector Schedule: Healthy  \n"
        "Latest Result: Contract v2 collector reporting healthy  \n"
    )
    assert expected in panel
    assert panel.index("Last Successful Upload Run:") < panel.index("Last Collector Run:")


def test_the_real_panel_shows_a_schedule_error_in_words():
    ctx = context(heartbeat(collector_schedule_status="no_next_run", collector_next_run_at=None))

    panel = next(body for body in _render_run_summary(ctx) if "##### Pipeline Status" in body)

    assert "Collector Schedule: Error — no future run scheduled  \n" in panel
    assert "Next Scheduled Run: N/A  \n" in panel


def test_the_real_panel_renders_an_old_collectors_heartbeat_without_crashing():
    panel = next(body for body in _render_run_summary(context(heartbeat(**OLD_COLLECTOR))) if "##### Pipeline Status" in body)

    assert "Latest Run Duration: N/A  \n" in panel
    assert "Collector Schedule: Unknown (not reported by this collector version)  \n" in panel


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
