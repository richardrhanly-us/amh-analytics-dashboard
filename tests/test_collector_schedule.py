"""services.collector_schedule: what the collector's report about its own scheduled task means, judged at a given
instant -- the rule the dashboard's Pipeline Status panel shows (services.pipeline_context_service), owned in one
framework-neutral module.

These characterize the dashboard's behaviour as it was when the rule lived inside build_collector_diagnostics. One case
per decision the function makes, plus its boundaries; what each schedule code MEANS is services.pipeline_state's rule and
is pinned exhaustively in tests/test_pipeline_state.py, so here a table only proves the judgement uses it. The wording the
panel shows for each reason, and how the panel combines the judgement with the reported health, stay in
pipeline_context_service and are pinned by tests/test_v2_collector_diagnostics.py.

NOW is 3:00 PM Central on 2026-10-01 and is passed to every call, so nothing here reads a clock.
"""

import ast
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from collector import task_settings
from services import collector_schedule
from services.collector_schedule import (
    NO_NEXT_RUN,
    OVERDUE,
    SCHEDULE_ERROR,
    SCHEDULE_INDETERMINATE,
    SCHEDULE_OK,
    SCHEDULE_OVERDUE_GRACE,
    SCHEDULE_UNKNOWN,
    TASK_DISABLED,
    TASK_MISSING,
    UNREADABLE,
    ScheduleJudgement,
    judge_schedule,
)
from services.pipeline_state import schedule_problem

APP_TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 1, 15, 0, tzinfo=APP_TZ)  # freshness: allow FRESH004 -- passed as now= to every call under test


def minutes_from_now(minutes: float) -> datetime:
    return NOW + timedelta(minutes=minutes)


# ======================================================================================================================
# the grace
# ======================================================================================================================

def test_the_grace_is_two_canonical_cadences_and_is_tied_to_the_collectors_own_constant():
    assert collector_schedule.COLLECTOR_CADENCE_MINUTES == task_settings.DEFAULT_CADENCE_MINUTES == 15
    assert collector_schedule.SCHEDULE_OVERDUE_GRACE_CADENCES == 2
    assert SCHEDULE_OVERDUE_GRACE == timedelta(minutes=2 * task_settings.DEFAULT_CADENCE_MINUTES) == timedelta(minutes=30)
    # Derived, not a bare literal: computed from the two named constants, with the reason for two written beside it.
    source = Path(collector_schedule.__file__).read_text(encoding="utf-8")
    assert "timedelta(minutes=COLLECTOR_CADENCE_MINUTES * SCHEDULE_OVERDUE_GRACE_CADENCES)" in source
    assert "timedelta(minutes=30)" not in source
    assert "MultipleInstancesPolicy=IgnoreNew" in source


# ======================================================================================================================
# not reported: an older collector
# ======================================================================================================================

def test_a_schedule_that_was_not_reported_is_unknown_and_its_next_run_is_not_read():
    # A next run long past would be overdue if it were read: it is not.
    assert judge_schedule(None, minutes_from_now(-600), NOW) == ScheduleJudgement(SCHEDULE_UNKNOWN, None)


def test_the_one_difference_from_pipeline_state_an_empty_value_is_not_reported():
    # Documented on purpose: the dashboard has always read an empty value as "not reported" (an older collector), while
    # pipeline_state.schedule_problem reads any present value other than "healthy" as an unconfirmed schedule.
    assert schedule_problem("") == "degraded"
    assert judge_schedule("", None, NOW) == ScheduleJudgement(SCHEDULE_UNKNOWN, None)


# ======================================================================================================================
# healthy: trusted only with a next run that is not overdue
# ======================================================================================================================

def test_a_healthy_schedule_whose_next_run_is_ahead_is_ok():
    assert judge_schedule("healthy", minutes_from_now(12), NOW) == ScheduleJudgement(SCHEDULE_OK, None)


def test_the_grace_itself_is_still_ok_and_one_microsecond_past_it_is_overdue():
    exactly = NOW - SCHEDULE_OVERDUE_GRACE
    assert judge_schedule("healthy", exactly, NOW) == ScheduleJudgement(SCHEDULE_OK, None)
    assert judge_schedule("healthy", exactly - timedelta(microseconds=1), NOW) == ScheduleJudgement(SCHEDULE_ERROR, OVERDUE)


def test_a_healthy_schedule_with_no_next_run_is_not_trusted():
    assert judge_schedule("healthy", None, NOW) == ScheduleJudgement(SCHEDULE_ERROR, NO_NEXT_RUN)


def test_only_the_clock_moving_turns_a_healthy_schedule_into_an_error():
    next_run_at = minutes_from_now(12)

    assert judge_schedule("healthy", next_run_at, NOW).judgement == SCHEDULE_OK
    assert judge_schedule("healthy", next_run_at, NOW + timedelta(hours=3)) == ScheduleJudgement(SCHEDULE_ERROR, OVERDUE)


def test_the_next_run_is_compared_as_an_instant_whatever_zone_it_is_expressed_in():
    in_utc = minutes_from_now(-31).astimezone(ZoneInfo("UTC"))

    assert judge_schedule("healthy", in_utc, NOW) == ScheduleJudgement(SCHEDULE_ERROR, OVERDUE)
    assert judge_schedule("healthy", minutes_from_now(-29).astimezone(ZoneInfo("UTC")), NOW).judgement == SCHEDULE_OK


# ======================================================================================================================
# structural failures and an unreadable schedule: no grace, the next run is not read
# ======================================================================================================================

@pytest.mark.parametrize(("status", "reason"), [
    ("task_missing", TASK_MISSING), ("task_disabled", TASK_DISABLED), ("no_next_run", NO_NEXT_RUN),
])
def test_a_structural_schedule_failure_is_an_error_even_with_a_next_run_ahead(status, reason):
    assert judge_schedule(status, minutes_from_now(12), NOW) == ScheduleJudgement(SCHEDULE_ERROR, reason)


def test_a_schedule_that_could_not_be_checked_is_indeterminate_even_with_a_next_run_ahead():
    assert judge_schedule("query_failed", minutes_from_now(12), NOW) == ScheduleJudgement(SCHEDULE_INDETERMINATE, UNREADABLE)


# ======================================================================================================================
# one table of codes: pipeline_state's
# ======================================================================================================================

def test_a_reported_code_is_classified_exactly_as_pipeline_state_classifies_it():
    # With the next run ahead, only the code decides -- and it decides as pipeline_state.schedule_problem does. The
    # table holds every code the collector sends and values no collector sends: a present value that is not exactly
    # "healthy" (this module's own comparison) is never ok.
    codes = ["healthy", "task_missing", "task_disabled", "no_next_run", "query_failed",
             "some_future_code", "HEALTHY", " healthy", 5, True]
    expected = {None: SCHEDULE_OK, "failed": SCHEDULE_ERROR, "degraded": SCHEDULE_INDETERMINATE}

    mismatches = [
        (code, judged, expected[schedule_problem(code)]) for code in codes
        if (judged := judge_schedule(code, minutes_from_now(12), NOW).judgement) != expected[schedule_problem(code)]
    ]
    assert mismatches == []
    assert {judge_schedule(code, minutes_from_now(12), NOW).judgement for code in codes} == {
        SCHEDULE_OK, SCHEDULE_ERROR, SCHEDULE_INDETERMINATE}  # the table reaches all three


# ======================================================================================================================
# purity
# ======================================================================================================================

def test_the_module_imports_nothing_but_the_standard_library_and_pipeline_state():
    tree = ast.parse(Path(collector_schedule.__file__).read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}

    assert imported == {"__future__", "dataclasses", "datetime", "services.pipeline_state"}
