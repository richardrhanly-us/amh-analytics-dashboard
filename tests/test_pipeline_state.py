"""Block 9c: the customer API's pipeline states.

    PIPELINE_STATES / PipelineState                 the closed set of four public states
    state_for_run_status(status)                    legacy pipeline_status.status         -> a state
    state_for_legacy_health(health_status)          legacy pipeline_status.health_status  -> a state
    state_for_current_health(health_status)         ingest_key_ids.health_status          -> a state
    schedule_problem(schedule_status)               ingest_key_ids.collector_schedule_status -> a fault, or None
    state_for_current_report(health, schedule)      the two above combined: a fault only ever worsens
    worse_state(first, second)                      unknown < ok < degraded < failed

Everything here is pure: no database, no request, no clock. A state says what was last REPORTED; nothing in the
module, and nothing in these tests, is about how long ago that was.

The values the mapping is written against are the ones the repository's own writers produce. Those writers -- the
scheduled collector, the legacy agent, the install preflight, the ingestion API's enums and the database's CHECK
lists -- are read HERE, never by the module under test, so a new stored value shows up as a failing test and a
decision, not as a silent `unknown`.

Imported the "flat" way (services.pipeline_state), the identity the API process uses.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import get_args
from zoneinfo import ZoneInfo

import pytest

from services import pipeline_state
from services.pipeline_state import (
    PIPELINE_STATES,
    PipelineState,
    schedule_problem,
    state_for_current_health,
    state_for_current_report,
    state_for_legacy_health,
    state_for_run_status,
    worse_state,
)

ROOT = Path(__file__).resolve().parent.parent


class _Unprintable:
    def __str__(self):
        raise RuntimeError("never asked for")

    __repr__ = __str__


# Values that are not a string at all. None of them is ever converted to text and matched.
NOT_TEXT = [None, 0, 1, True, False, 1.5, float("nan"), b"completed", b"healthy", bytearray(b"failed"), ["completed"],
            ("healthy",), {"completed"}, {"healthy": 1}, object(), _Unprintable()]
NOT_TEXT_IDS = [type(value).__name__ for value in NOT_TEXT]


# =====================================================================================================================
# The closed set
# =====================================================================================================================

def test_the_public_states_are_exactly_ok_degraded_failed_and_unknown():
    assert PIPELINE_STATES == ("ok", "degraded", "failed", "unknown")
    assert isinstance(PIPELINE_STATES, tuple) and len(set(PIPELINE_STATES)) == 4


def test_the_state_type_names_the_same_four_states_in_the_same_order():
    assert get_args(PipelineState) == PIPELINE_STATES


def test_the_states_carry_no_era_no_age_and_no_implementation_wording():
    for state in PIPELINE_STATES:
        for leaked in ("v1", "v2", "legacy", "contract", "stale", "fresh", "late", "old", "healthy", "error", "auth",
                       "schedule", "task"):
            assert leaked not in state, (state, leaked)


# =====================================================================================================================
# state_for_run_status: legacy pipeline_status.status
# =====================================================================================================================

@pytest.mark.parametrize("status", ["completed", "completed_no_new_rows", "skipped_no_source_changes"])
def test_a_finished_run_is_ok(status):
    assert state_for_run_status(status) == "ok"


@pytest.mark.parametrize(
    "status",
    ["failed_upload", "failed_upload_none", "failed_upload_invalid_type", "failed_upload_missing_keys",
     "failed_corrupt_state", "failed_config", "failed_parser_not_configured", "failed_status", "failed_unhandled"],
)
def test_every_failure_a_collector_spells_out_is_failed(status):
    assert state_for_run_status(status) == "failed"


@pytest.mark.parametrize(
    "status",
    ["failed", "failed_", "failed_something_never_seen_before", "failed_x" * 20, "failed-upload", "failed upload",
     "failed:timeout", "failedcompleted", "failed_completed", "failed_ok"],
)
def test_any_value_starting_with_failed_is_failed(status):
    # The prefix is the rule: a failure nobody has written down yet is still a failure, and no suffix -- not even
    # one that spells a success -- makes it anything else.
    assert state_for_run_status(status) == "failed"


def test_a_run_that_has_only_started_is_unknown():
    # The legacy agent reports this at the START of every run. A run that began says nothing about how it ended.
    assert state_for_run_status("started") == "unknown"


def test_an_install_preflight_probe_is_unknown():
    # Not a run at all: the collector's install and update check, which writes this into the same column.
    assert state_for_run_status("preflight_check") == "unknown"


@pytest.mark.parametrize("status", ["", " ", "   ", "\t", "\n", " \t\r\n "])
def test_an_empty_or_blank_run_status_is_unknown(status):
    assert state_for_run_status(status) == "unknown"


@pytest.mark.parametrize(
    "status",
    ["ok", "success", "succeeded", "done", "complete", "completed_", "completed_with_errors", "completed-no-new-rows",
     "completed no new rows", "skipped", "skipped_no_source_change", "running", "unknown", "healthy", "degraded",
     "error", "auth_failure", "nan", "null", "None", "0", "200", "task_missing", "unfailed", "not_failed",
     "upload_failed", "has failed", "run failed_upload"],
)
def test_any_other_text_is_unknown_and_never_ok(status):
    # Including words that MEAN success, near-misses of the three known values, the other signals' vocabularies,
    # and text that merely contains "failed": only a value that BEGINS with it is a failure.
    assert state_for_run_status(status) == "unknown"


@pytest.mark.parametrize("status", NOT_TEXT, ids=NOT_TEXT_IDS)
def test_a_run_status_that_is_not_text_is_unknown_and_never_raises(status):
    assert state_for_run_status(status) == "unknown"


# --- normalization: there is none ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status",
    ["Completed", "COMPLETED", "cOmPlEtEd", "Completed_No_New_Rows", "SKIPPED_NO_SOURCE_CHANGES", " completed",
     "completed ", " completed ", "completed\n", "\tcompleted", "completed\x00"],
)
def test_a_success_is_matched_exactly_so_a_differently_cased_or_padded_value_is_never_ok(status):
    # Every writer stores the lower-case token as it is. A value that is not exactly one of them was not written by
    # a known writer, and is not known to mean what the nearest token means: it fails closed.
    assert state_for_run_status(status) == "unknown"


@pytest.mark.parametrize("status", ["Failed", "FAILED_UPLOAD", "Failed_upload", " failed_upload", "\tfailed", "\nfailed_x"])
def test_a_differently_cased_or_padded_failure_is_not_recognised_either_and_is_still_never_ok(status):
    # Exact matching cuts both ways. Not being recognised as a failure never makes a value a success.
    assert state_for_run_status(status) == "unknown"
    assert state_for_run_status(status) != "ok"


def test_trailing_text_after_a_failure_prefix_does_not_matter_but_leading_text_does():
    assert state_for_run_status("failed_upload ") == "failed"       # still begins with it
    assert state_for_run_status(" failed_upload") == "unknown"      # does not


# =====================================================================================================================
# state_for_legacy_health: legacy pipeline_status.health_status
# =====================================================================================================================

LEGACY_HEALTH = {"healthy": "ok", "degraded": "degraded", "auth_failure": "failed"}
CURRENT_HEALTH = {"healthy": "ok", "degraded": "degraded", "error": "failed"}


@pytest.mark.parametrize(("health_status", "expected"), LEGACY_HEALTH.items())
def test_each_legacy_health_value_maps_to_its_state(health_status, expected):
    assert state_for_legacy_health(health_status) == expected


@pytest.mark.parametrize(
    "health_status",
    ["", " ", "error", "ok", "failed", "unknown", "Healthy", "HEALTHY", " healthy", "healthy ", "auth_failure ",
     "Auth_Failure", "auth-failure", "auth failure", "completed", "started", "retryable_infra", "unhealthy",
     "healthyish", "not degraded"],
)
def test_any_other_legacy_health_text_is_unknown(health_status):
    # "error" is the CURRENT vocabulary's word for failed; the legacy heartbeat never sends it, so here it means nothing.
    assert state_for_legacy_health(health_status) == "unknown"


@pytest.mark.parametrize("health_status", NOT_TEXT, ids=NOT_TEXT_IDS)
def test_a_legacy_health_that_is_not_text_is_unknown_and_never_raises(health_status):
    assert state_for_legacy_health(health_status) == "unknown"


# =====================================================================================================================
# state_for_current_health: ingest_key_ids.health_status
# =====================================================================================================================

@pytest.mark.parametrize(("health_status", "expected"), CURRENT_HEALTH.items())
def test_each_current_health_value_maps_to_its_state(health_status, expected):
    assert state_for_current_health(health_status) == expected


@pytest.mark.parametrize(
    "health_status",
    ["", " ", "auth_failure", "ok", "failed", "unknown", "Healthy", "ERROR", "Error", " error", "error ", "errors",
     "completed", "retryable_infra", "permanent_rejection", "task_missing", "query_failed"],
)
def test_any_other_current_health_text_is_unknown(health_status):
    # "auth_failure" is a LEGACY health value and a current last_error_class -- never a current health status.
    assert state_for_current_health(health_status) == "unknown"


@pytest.mark.parametrize("health_status", NOT_TEXT, ids=NOT_TEXT_IDS)
def test_a_current_health_that_is_not_text_is_unknown_and_never_raises(health_status):
    assert state_for_current_health(health_status) == "unknown"


def test_the_two_health_vocabularies_are_kept_apart():
    # They share "healthy" and "degraded" and differ in how they say failed. Neither function accepts the other's word.
    assert state_for_legacy_health("auth_failure") == "failed" and state_for_current_health("auth_failure") == "unknown"
    assert state_for_current_health("error") == "failed" and state_for_legacy_health("error") == "unknown"


# =====================================================================================================================
# schedule_problem: ingest_key_ids.collector_schedule_status
# =====================================================================================================================

SCHEDULE_FAULTS = {"task_missing": "failed", "task_disabled": "failed", "no_next_run": "failed", "query_failed": "degraded"}


@pytest.mark.parametrize(("schedule_status", "expected"), SCHEDULE_FAULTS.items())
def test_each_schedule_fault_maps_to_its_state(schedule_status, expected):
    assert schedule_problem(schedule_status) == expected


def test_a_healthy_schedule_is_no_problem():
    assert schedule_problem("healthy") is None


def test_a_schedule_that_was_not_reported_is_no_problem():
    # A collector too old to report its schedule: nothing is known to be wrong with it.
    assert schedule_problem(None) is None


@pytest.mark.parametrize(
    "schedule_status",
    ["", " ", "Healthy", "HEALTHY", " healthy", "healthy ", "ok", "unknown", "task_paused", "TASK_MISSING",
     "task missing", "overdue", "error", "degraded", "failed", 0, 1, True, False, 2.5, b"healthy", ["healthy"],
     {"task_missing"}, object()],
    ids=repr,
)
def test_a_schedule_value_that_is_present_but_not_a_known_code_is_degraded_never_no_problem(schedule_status):
    # The dashboard's own rule (build_collector_diagnostics): a code it does not know means the schedule's real state
    # is not known -- treated as "could not be checked", never as healthy.
    assert schedule_problem(schedule_status) == "degraded"


def test_a_schedule_problem_is_never_ok_or_unknown():
    for value in [*SCHEDULE_FAULTS, "healthy", None, "", "anything", 5, object()]:
        assert schedule_problem(value) in (None, "degraded", "failed"), repr(value)


# =====================================================================================================================
# state_for_current_report: a schedule fault only ever worsens the health
# =====================================================================================================================

HEALTH_VALUES = ["healthy", "degraded", "error", None, "something_else"]
SCHEDULE_VALUES = ["healthy", None, "task_missing", "task_disabled", "no_next_run", "query_failed", "something_else"]

EXPECTED_REPORT_STATES = {
    # health:           healthy     None        task_missing  task_disabled  no_next_run  query_failed  something_else
    "healthy":         ("ok",       "ok",       "failed",     "failed",      "failed",    "degraded",   "degraded"),
    "degraded":        ("degraded", "degraded", "failed",     "failed",      "failed",    "degraded",   "degraded"),
    "error":           ("failed",   "failed",   "failed",     "failed",      "failed",    "failed",     "failed"),
    None:              ("unknown",  "unknown",  "failed",     "failed",      "failed",    "degraded",   "degraded"),
    "something_else":  ("unknown",  "unknown",  "failed",     "failed",      "failed",    "degraded",   "degraded"),
}


@pytest.mark.parametrize("health", HEALTH_VALUES, ids=repr)
@pytest.mark.parametrize("schedule", SCHEDULE_VALUES, ids=repr)
def test_every_combination_of_health_and_schedule_has_the_approved_state(health, schedule):
    expected = EXPECTED_REPORT_STATES[health][SCHEDULE_VALUES.index(schedule)]

    assert state_for_current_report(health, schedule) == expected


@pytest.mark.parametrize("health", [*HEALTH_VALUES, "", "Healthy", 0, object()], ids=repr)
def test_with_no_schedule_fault_the_health_stands_exactly_as_it_is(health):
    alone = state_for_current_health(health)

    assert state_for_current_report(health, "healthy") == alone
    assert state_for_current_report(health, None) == alone


@pytest.mark.parametrize("schedule", ["healthy", None])
def test_a_healthy_or_unreported_schedule_never_turns_an_unknown_health_into_ok(schedule):
    for health in (None, "", "something_else", "Healthy", "auth_failure", 0, object()):
        assert state_for_current_report(health, schedule) == "unknown", repr(health)


@pytest.mark.parametrize("health", [*HEALTH_VALUES, "", 0], ids=repr)
@pytest.mark.parametrize("schedule", [*SCHEDULE_VALUES, "", 0], ids=repr)
def test_a_schedule_only_ever_worsens_the_health_never_improves_it(health, schedule):
    alone = state_for_current_health(health)
    combined = state_for_current_report(health, schedule)

    assert pipeline_state._SEVERITY[combined] >= pipeline_state._SEVERITY[alone]
    if alone == "failed":
        assert combined == "failed"             # nothing about the schedule softens a failure
    if combined == "ok":
        assert alone == "ok"                    # `ok` only ever comes from the health itself


@pytest.mark.parametrize(("schedule", "fault"), SCHEDULE_FAULTS.items())
def test_a_reported_schedule_fault_shows_even_when_the_health_is_unknown(schedule, fault):
    # A fact the collector reported about itself: it is not hidden because the health happens to be missing.
    assert state_for_current_report(None, schedule) == fault
    assert state_for_current_report("something_else", schedule) == fault


def test_a_schedule_fault_never_hides_a_worse_health():
    assert state_for_current_report("error", "query_failed") == "failed"     # degraded fault, failed health
    assert state_for_current_report("degraded", "query_failed") == "degraded"
    assert state_for_current_report("degraded", "task_disabled") == "failed"  # failed fault, degraded health


# =====================================================================================================================
# worse_state
# =====================================================================================================================

def test_severity_runs_unknown_ok_degraded_failed():
    assert sorted(PIPELINE_STATES, key=pipeline_state._SEVERITY.__getitem__) == ["unknown", "ok", "degraded", "failed"]
    assert set(pipeline_state._SEVERITY) == set(PIPELINE_STATES)
    assert len(set(pipeline_state._SEVERITY.values())) == 4      # a strict order: no two states tie


@pytest.mark.parametrize("first", PIPELINE_STATES)
@pytest.mark.parametrize("second", PIPELINE_STATES)
def test_worse_state_returns_the_more_severe_of_the_two_in_either_order(first, second):
    order = ["unknown", "ok", "degraded", "failed"]
    expected = max(first, second, key=order.index)

    assert worse_state(first, second) == expected
    assert worse_state(second, first) == expected
    assert worse_state(first, first) == first


def test_worse_state_is_only_reached_for_a_real_fault():
    # By the ordering alone, worse_state("unknown", "ok") is "ok" -- so "no problem" must never be passed to it as a
    # state. The one caller does not: with no fault it returns the health untouched.
    assert worse_state("unknown", "ok") == "ok"
    source = inspect.getsource(state_for_current_report)
    assert source.index("if problem is None:") < source.index("worse_state(health, problem)")
    assert "return health" in source


# =====================================================================================================================
# The functions as a whole
# =====================================================================================================================

EVERYTHING = [*NOT_TEXT, "", " ", "completed", "completed_no_new_rows", "skipped_no_source_changes", "started",
              "preflight_check", "failed", "failed_upload", "Completed", "healthy", "degraded", "auth_failure", "error",
              "task_missing", "task_disabled", "no_next_run", "query_failed", "something_else", "ok", "unknown"]


def test_every_function_answers_with_one_of_the_four_states_for_any_value():
    for value in EVERYTHING:
        for function in (state_for_run_status, state_for_legacy_health, state_for_current_health):
            assert function(value) in PIPELINE_STATES, (function.__name__, repr(value))
        assert schedule_problem(value) in (None, *PIPELINE_STATES), repr(value)
        for other in ("healthy", None, "task_missing", "something_else"):
            assert state_for_current_report(value, other) in PIPELINE_STATES, repr(value)
            assert state_for_current_report(other, value) in PIPELINE_STATES, repr(value)


def test_the_same_value_always_gets_the_same_answer():
    for value in EVERYTHING:
        for function in (state_for_run_status, state_for_legacy_health, state_for_current_health, schedule_problem):
            answers = {function(value) for _ in range(5)}
            assert len(answers) == 1, (function.__name__, repr(value))
        assert len({state_for_current_report(value, value) for _ in range(5)}) == 1, repr(value)


def test_ok_is_only_ever_the_answer_for_an_exact_known_success():
    reaches_ok = {
        "run": {value for value in EVERYTHING if isinstance(value, str) and state_for_run_status(value) == "ok"},
        "legacy": {value for value in EVERYTHING if isinstance(value, str) and state_for_legacy_health(value) == "ok"},
        "current": {value for value in EVERYTHING if isinstance(value, str) and state_for_current_health(value) == "ok"},
    }

    assert reaches_ok == {
        "run": {"completed", "completed_no_new_rows", "skipped_no_source_changes"},
        "legacy": {"healthy"},
        "current": {"healthy"},
    }
    assert not any(function(value) == "ok" for value in NOT_TEXT
                   for function in (state_for_run_status, state_for_legacy_health, state_for_current_health))


def test_each_function_takes_exactly_the_stored_values_it_maps():
    assert list(inspect.signature(state_for_run_status).parameters) == ["status"]
    assert list(inspect.signature(state_for_legacy_health).parameters) == ["health_status"]
    assert list(inspect.signature(state_for_current_health).parameters) == ["health_status"]
    assert list(inspect.signature(schedule_problem).parameters) == ["schedule_status"]
    assert list(inspect.signature(state_for_current_report).parameters) == ["health_status", "schedule_status"]
    assert list(inspect.signature(worse_state).parameters) == ["first", "second"]


def test_no_function_takes_a_time_a_tenant_or_a_source():
    for name, function in inspect.getmembers(pipeline_state, inspect.isfunction):
        for parameter in inspect.signature(function).parameters:
            for forbidden in ("at", "time", "now", "stamp", "age", "customer", "branch", "tenant", "cutover", "era", "row"):
                assert forbidden not in parameter.split("_"), (name, parameter)


# =====================================================================================================================
# Against the values the repository's writers actually produce (test-only reads; none of them is changed)
# =====================================================================================================================

def _status_literals(relative_path: str) -> set[str]:
    """Every string a source file assigns as a pipeline `status`: a "status" dictionary key or subscript, or the
    variable the legacy agent builds its final status in. Read from the file's syntax tree -- the module is not
    imported (the legacy agent and the collector both do work at import time)."""
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    found: set[str] = set()

    def strings(node: ast.AST) -> set[str]:
        return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}

    def is_status_key(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and node.value == "status"

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is not None and is_status_key(key):
                    found |= strings(value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                status_subscript = isinstance(target, ast.Subscript) and is_status_key(target.slice)
                final_status_name = isinstance(target, ast.Name) and target.id == "final_status_code"
                if status_subscript or final_status_name:
                    found |= strings(node.value)
    return found


PRODUCED_RUN_STATUSES = {
    "collector/run.py": {"completed": "ok", "completed_no_new_rows": "ok", "failed_upload": "failed",
                         "failed_corrupt_state": "failed"},
    "agent/run_pipeline.py": {"started": "unknown", "skipped_no_source_changes": "ok", "completed": "ok",
                              "completed_no_new_rows": "ok", "failed_upload_none": "failed",
                              "failed_upload_invalid_type": "failed", "failed_upload_missing_keys": "failed"},
    "collector/preflight.py": {"preflight_check": "unknown"},
}


@pytest.mark.parametrize("path", list(PRODUCED_RUN_STATUSES))
def test_every_run_status_a_writer_produces_is_one_this_mapping_was_written_for(path):
    # If a writer gains a status, this fails -- on purpose: what the new value means is decided here, in the open.
    assert _status_literals(path) == set(PRODUCED_RUN_STATUSES[path])


@pytest.mark.parametrize("path", list(PRODUCED_RUN_STATUSES))
def test_every_run_status_a_writer_produces_gets_the_approved_state(path):
    for status, expected in PRODUCED_RUN_STATUSES[path].items():
        assert state_for_run_status(status) == expected, status


def test_every_produced_run_status_is_a_lower_case_token_stored_as_it_is():
    # Why matching is exact: no writer pads a value or changes its case, so normalizing could only ever admit a
    # value no writer wrote.
    for statuses in PRODUCED_RUN_STATUSES.values():
        for status in statuses:
            assert status == status.strip() == status.lower() and status.replace("_", "").isalpha(), status


def test_the_legacy_health_values_are_exactly_the_ones_the_api_accepts():
    import main

    accepted = set(get_args(get_args(main.PipelineStatusRequest.model_fields["health_status"].annotation)[0]))

    assert accepted == set(LEGACY_HEALTH) == {"healthy", "degraded", "auth_failure"}
    for health_status in accepted:
        assert state_for_legacy_health(health_status) != "unknown", health_status


def test_the_current_health_values_are_exactly_the_ones_the_collector_sends_and_the_api_accepts():
    from collector import v2_events
    from src.services import ingest_v2_models

    assert set(CURRENT_HEALTH) == set(v2_events.HEALTH_STATUSES) == set(ingest_v2_models.HEALTH_STATUSES)
    assert set(get_args(ingest_v2_models.HealthStatus)) == set(CURRENT_HEALTH)
    for health_status in ingest_v2_models.HEALTH_STATUSES:
        assert state_for_current_health(health_status) != "unknown", health_status


def test_the_schedule_codes_are_exactly_the_ones_the_collector_sends_and_the_api_accepts():
    from collector import v2_events
    from src.services import ingest_v2_models

    known = {"healthy", *SCHEDULE_FAULTS}
    assert known == set(v2_events.SCHEDULE_STATUSES) == set(ingest_v2_models.SCHEDULE_STATUSES)
    assert set(get_args(ingest_v2_models.ScheduleStatus)) == known
    assert set(pipeline_state._SCHEDULE_PROBLEMS) == known - {"healthy"} and pipeline_state._SCHEDULE_OK == "healthy"


def _migration(revision: str):
    (path,) = (ROOT / "alembic" / "versions").glob(f"{revision}_*.py")
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path.read_text(encoding="utf-8")


def test_the_databases_check_lists_admit_no_value_this_mapping_does_not_know():
    schedule_migration, _ = _migration("c8d5f2a47e91")
    _, ingest_ddl = _migration("d3f1a8c95b27")

    assert set(schedule_migration.SCHEDULE_STATUSES) == {"healthy", *SCHEDULE_FAULTS}
    assert "health_status IS NULL OR health_status IN ('healthy', 'degraded', 'error')" in ingest_ddl
    # So for a stored current row: health is one of three values or NULL, schedule one of five or NULL -- and every
    # one of those 24 combinations is in the table above.
    for health in ("healthy", "degraded", "error", None):
        for schedule in (*schedule_migration.SCHEDULE_STATUSES, None):
            assert state_for_current_report(health, schedule) == EXPECTED_REPORT_STATES[health][SCHEDULE_VALUES.index(schedule)]


def test_the_collector_can_only_ever_decide_one_of_the_three_current_health_values():
    from collector import v2_status

    decided = {
        v2_status.decide(failure=failure, new_quarantined=quarantined, sources_missing=missing,
                         consecutive_failures=failures, error_after=3)[0]
        for failure in (None, *v2_status.FAILURE_CATEGORIES, "something_else")
        for quarantined in (0, 1) for missing in (0, 1) for failures in (0, 1, 3, 9)
    }

    assert decided == set(CURRENT_HEALTH)


# =====================================================================================================================
# Against the Streamlit dashboard (characterization only; the dashboard is not changed)
# =====================================================================================================================

APP_TZ = ZoneInfo("America/Chicago")
NOW_CT = datetime(2026, 10, 1, 15, 0, tzinfo=APP_TZ)  # freshness: allow FRESH004 -- passed as now_ct= to every call under test

DASHBOARD_LABEL_STATES = {
    "Pipeline Healthy": "ok",
    "Pipeline Degraded": "degraded",
    "Pipeline Failed": "failed",
    "Pipeline Auth Failure": "failed",
    "Pipeline Schedule Error": "failed",
    "Pipeline Status Unknown": "unknown",
}


def _dashboard_run_label(status: str) -> str:
    import pandas as pd

    from services.pipeline_context_service import build_pipeline_context

    return build_pipeline_context({"status": status}, pd.DataFrame(columns=["datetime"]), NOW_CT, APP_TZ, "light")[
        "pipeline_status_label"]


def _dashboard_current_label(health_status, schedule_status) -> str:
    import pandas as pd

    from services.pipeline_context_service import build_v2_aware_pipeline_context

    def instant(minutes: float) -> str:
        return (NOW_CT + timedelta(minutes=minutes)).astimezone(ZoneInfo("UTC")).isoformat()

    # A current heartbeat whose next run is still ahead: the dashboard's own "overdue" rule (which is about TIME, and
    # is deliberately not part of a state) cannot fire.
    heartbeat = {"health_status": health_status, "last_error_class": None, "pending_outbox_count": 0, "quarantined_count": 0,
                 "last_heartbeat_at": instant(-1), "watcher_last_active_at": instant(-1), "last_success_at": instant(-1),
                 "collector_last_run_at": instant(-1), "collector_next_run_at": instant(12),
                 "collector_run_duration_ms": 1000, "collector_schedule_status": schedule_status}
    return build_v2_aware_pipeline_context(None, heartbeat, pd.DataFrame(columns=["datetime"]), NOW_CT, APP_TZ, "light")[
        "pipeline_status_label"]


@pytest.mark.parametrize(
    "status",
    ["completed", "completed_no_new_rows", "skipped_no_source_changes", "failed_upload", "failed_upload_none",
     "failed", "preflight_check", "something_else"],
)
def test_a_run_status_gets_the_state_of_the_dashboards_own_label(status):
    assert state_for_run_status(status) == DASHBOARD_LABEL_STATES[_dashboard_run_label(status)]


def test_a_started_run_is_intentionally_unknown_where_the_dashboard_says_running():
    # The dashboard has a fifth label for this ("Pipeline Running", in its degraded colour). The public set has four
    # states and none of them means "in progress": the outcome is not known, so the state is `unknown`.
    assert _dashboard_run_label("started") == "Pipeline Running"
    assert state_for_run_status("started") == "unknown"


@pytest.mark.parametrize("health", ["healthy", "degraded", "error", None])
@pytest.mark.parametrize("schedule", ["healthy", None, "task_missing", "task_disabled", "no_next_run"])
def test_a_current_report_gets_the_state_of_the_dashboards_own_label(health, schedule):
    assert state_for_current_report(health, schedule) == DASHBOARD_LABEL_STATES[_dashboard_current_label(health, schedule)]


@pytest.mark.parametrize("health", ["healthy", "degraded", "error"])
def test_a_schedule_that_could_not_be_checked_gets_the_state_of_the_dashboards_own_label(health):
    for schedule in ("query_failed", "something_else"):
        assert state_for_current_report(health, schedule) == DASHBOARD_LABEL_STATES[_dashboard_current_label(health, schedule)]


def test_an_unchecked_schedule_with_no_health_is_intentionally_degraded_where_the_dashboard_says_unknown():
    # The one place the two differ for a current report. The dashboard lets "could not be checked" worsen only a
    # HEALTHY pipeline; here a reported schedule fault always shows, an unknown health included.
    for schedule in ("query_failed", "something_else"):
        assert _dashboard_current_label(None, schedule) == "Pipeline Status Unknown"
        assert state_for_current_report(None, schedule) == "degraded"


# =====================================================================================================================
# Framework-neutral
# =====================================================================================================================

def _imported_modules() -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(pipeline_state))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import"
            found.add(node.module or "")
    return found


def test_the_module_imports_only_the_standard_library():
    imported = _imported_modules()

    assert imported == {"__future__", "typing"}
    assert {name.split(".")[0] for name in imported} <= set(sys.stdlib_module_names) | {"__future__"}


def test_the_module_touches_no_framework_database_environment_clock_or_log():
    for imported in _imported_modules():
        for forbidden in ("streamlit", "fastapi", "starlette", "sqlalchemy", "pandas", "database", "tenant_db", "os",
                          "datetime", "time", "zoneinfo", "logging", "collector", "agent", "main", "pipeline_context",
                          "ingest_v2", "operational"):
            assert forbidden != imported.split(".")[0], imported

    code = inspect.getsource(pipeline_state).split('"""', 2)[2]
    for forbidden in ("import_module", "__import__", "getenv", "environ", ".now(", "utcnow", "time(", "logger", "print(",
                      "execute(", "SELECT", "open(", "st.", "pd."):
        assert forbidden not in code, forbidden


def test_the_module_holds_only_the_approved_public_names():
    public = {name for name in vars(pipeline_state) if not name.startswith("_")}

    assert public == {"PIPELINE_STATES", "PipelineState", "state_for_run_status", "state_for_legacy_health",
                      "state_for_current_health", "schedule_problem", "state_for_current_report", "worse_state",
                      "annotations", "Literal"}

