"""Regression tests: a Collector-created pipeline_status row has NULL counters.

The legacy pipeline agent wrote every counter on every run, so the dashboard
never saw a NULL one. The Collector only sends the counters it computes
(status, *_rows and, since 2db2842, uploaded_*), so when it is the FIRST writer
of a branch's row -- an INSERT, where every omitted column becomes NULL -- these
columns come back as None:

    transit_items, problem_items, checkins/rejects/acs_bad_datetime_rows

and, on Collector runs from before 2db2842, the uploaded_* counters too. Those
None values used to reach alerts.py (`None > 0`) and the Live Today view
(`f"{None:,}"`) and raise TypeError, i.e. "Error running app" on the hosted
dashboard.

The fix is at the data-loading boundary (data_loader.load_pipeline_status), so
these tests drive the real loader with a DataFrame shaped exactly like
pd.read_sql would return for such a row, then feed the result through the real
consumers.
"""

from datetime import date, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import data_loader as dl
from alerts import get_system_alerts
from services.pipeline_context_service import build_pipeline_context
from views import live_today_view

# Exactly the columns load_pipeline_status selects, in order.
COLUMNS = [
    "customer_id", "branch_id", "last_attempt", "last_run", "status",
    "checkins_rows", "rejects_rows", "acs_rows",
    "uploaded_checkins_rows", "uploaded_rejects_rows", "uploaded_acs_rows",
    "checkins_bad_datetime_rows", "rejects_bad_datetime_rows", "acs_bad_datetime_rows",
    "transit_items", "problem_items", "destination_breakdown",
    "health_status", "pending_outbox_count", "quarantined_count",
    "oldest_pending_event_at", "last_success_at", "last_failure_category",
    "last_error", "watcher_last_active_at", "updated_at",
]

# The counters the loader must turn into 0 when NULL.
COUNTER_FIELDS = [
    "checkins_rows", "rejects_rows", "acs_rows",
    "uploaded_checkins_rows", "uploaded_rejects_rows", "uploaded_acs_rows",
    "checkins_bad_datetime_rows", "rejects_bad_datetime_rows", "acs_bad_datetime_rows",
    "transit_items", "problem_items",
]

# The subset the dashboard formats with `:,` or compares with `> 0`.
CONSUMED_BY_VIEWS = [
    "checkins_rows", "rejects_rows", "uploaded_checkins_rows", "uploaded_rejects_rows",
    "checkins_bad_datetime_rows", "rejects_bad_datetime_rows", "transit_items", "problem_items",
]

# Deliberately naive: the pipeline_status columns are TIMESTAMP (no time zone).
RUN_TIME = datetime(2026, 9, 18, 16, 30, 0)  # noqa: DTZ001
LOCAL_TZ = ZoneInfo("America/Chicago")


def _row(**values):
    return tuple(values.get(column) for column in COLUMNS)


def collector_created_row():
    """What the Collector INSERTs into a branch that has no prior row: only the
    fields it sends. Everything else -- including every counter it does not
    compute -- is NULL."""
    return _row(
        customer_id=1, branch_id=1, status="completed",
        last_attempt=RUN_TIME, last_run=RUN_TIME, updated_at=RUN_TIME,
        checkins_rows=20, rejects_rows=0, acs_rows=120,
        uploaded_checkins_rows=20, uploaded_rejects_rows=0, uploaded_acs_rows=120,
    )


def legacy_agent_row():
    """A fully populated row, as the legacy scheduled agent always wrote."""
    return _row(
        customer_id=1, branch_id=1, status="completed",
        last_attempt=RUN_TIME, last_run=RUN_TIME, updated_at=RUN_TIME,
        checkins_rows=20, rejects_rows=2, acs_rows=120,
        uploaded_checkins_rows=18, uploaded_rejects_rows=1, uploaded_acs_rows=110,
        checkins_bad_datetime_rows=0, rejects_bad_datetime_rows=0, acs_bad_datetime_rows=0,
        transit_items=7, problem_items=3, destination_breakdown={"Bin A": 12, "Bin B": 8},
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    dl.load_pipeline_status.clear()
    yield
    dl.load_pipeline_status.clear()


def load(monkeypatch, record):
    # from_records(coerce_float=True) is what pd.read_sql uses, so NULL integer
    # columns arrive as None/NaN exactly as they do from the real database.
    frame = pd.DataFrame.from_records([record], columns=COLUMNS, coerce_float=True)
    monkeypatch.setattr(dl, "_read_table", lambda query, params=None: frame)
    return dl.load_pipeline_status("org", "branch")


def _render_run_summary(context):
    """Run the REAL render_live_today up to and including the Run Summary block.

    render_live_today needs ~60 arguments and a live Streamlit session, so
    `st` is replaced with a recorder and every argument except the ones under
    test is a MagicMock. The first st.caption() after the Run Summary
    (destination breakdown) stops the render -- nothing after that block is
    relevant here. Returns every string passed to st.markdown().
    """
    import inspect

    class _StopAfterRunSummary(Exception):
        pass

    markdown_calls = []
    fake_st = MagicMock()
    fake_st.columns.side_effect = lambda spec: [MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))]
    fake_st.button.return_value = False
    fake_st.markdown.side_effect = lambda body, **kwargs: markdown_calls.append(body)
    fake_st.caption.side_effect = _StopAfterRunSummary

    kwargs = {name: MagicMock() for name in inspect.signature(live_today_view.render_live_today).parameters}
    kwargs.update(context)
    kwargs["today"] = date(2026, 9, 18)
    kwargs["can_view_transits"] = False
    kwargs["can_view_internal_workflow"] = False

    original_st = live_today_view.st
    live_today_view.st = fake_st
    try:
        with pytest.raises(_StopAfterRunSummary):
            live_today_view.render_live_today(**kwargs)
    finally:
        live_today_view.st = original_st

    return markdown_calls


# --- the loader: NULL counters become 0 -------------------------------------


def test_collector_created_row_loads_with_every_null_counter_as_zero(monkeypatch):
    status = load(monkeypatch, collector_created_row())

    assert status["transit_items"] == 0
    assert status["problem_items"] == 0
    assert status["checkins_bad_datetime_rows"] == 0
    assert status["rejects_bad_datetime_rows"] == 0
    assert status["acs_bad_datetime_rows"] == 0


def test_every_normalized_counter_is_a_real_number_never_none_or_nan(monkeypatch):
    status = load(monkeypatch, collector_created_row())

    for field in COUNTER_FIELDS:
        value = status[field]
        assert value is not None and not pd.isna(value), f"{field} = {value!r}"
        assert format(value, ",")  # the exact formatting the Live Today view applies


def test_counters_the_collector_did_send_keep_their_values(monkeypatch):
    status = load(monkeypatch, collector_created_row())

    assert status["checkins_rows"] == 20
    assert status["rejects_rows"] == 0
    assert status["acs_rows"] == 120
    assert status["uploaded_checkins_rows"] == 20
    assert status["uploaded_rejects_rows"] == 0
    assert status["uploaded_acs_rows"] == 120


def test_pre_2db2842_collector_row_with_null_uploaded_counters_loads(monkeypatch):
    # Collector runs before 2db2842 did not send uploaded_*, so a row that
    # Collector created has status="completed" with these NULL -- the exact
    # state that raised in pipeline_context_service's f-string.
    record = list(collector_created_row())
    for field in ("uploaded_checkins_rows", "uploaded_rejects_rows", "uploaded_acs_rows"):
        record[COLUMNS.index(field)] = None

    status = load(monkeypatch, tuple(record))

    assert status["uploaded_checkins_rows"] == 0
    assert status["uploaded_rejects_rows"] == 0
    assert status["uploaded_acs_rows"] == 0


def test_fully_null_counter_row_loads(monkeypatch):
    record = _row(customer_id=1, branch_id=1, status="started", last_attempt=RUN_TIME, updated_at=RUN_TIME)

    status = load(monkeypatch, record)

    assert all(status[field] == 0 for field in COUNTER_FIELDS)


def test_non_null_row_is_unchanged(monkeypatch):
    status = load(monkeypatch, legacy_agent_row())

    assert status["checkins_rows"] == 20
    assert status["rejects_rows"] == 2
    assert status["uploaded_checkins_rows"] == 18
    assert status["uploaded_rejects_rows"] == 1
    assert status["uploaded_acs_rows"] == 110
    assert status["transit_items"] == 7
    assert status["problem_items"] == 3
    assert status["checkins_bad_datetime_rows"] == 0
    assert status["destination_breakdown"] == {"Bin A": 12, "Bin B": 8}


def test_a_genuine_zero_stays_zero_and_a_real_count_is_never_replaced(monkeypatch):
    record = list(legacy_agent_row())
    record[COLUMNS.index("problem_items")] = 0
    record[COLUMNS.index("transit_items")] = 41

    status = load(monkeypatch, tuple(record))

    assert status["problem_items"] == 0
    assert status["transit_items"] == 41


def test_heartbeat_counters_are_not_touched(monkeypatch):
    # pending_outbox_count / quarantined_count NULL means "no heartbeat was ever
    # reported" -- not zero -- and nothing in src/ consumes them, so the loader
    # must leave them alone.
    status = load(monkeypatch, collector_created_row())

    assert pd.isna(status["pending_outbox_count"])  # still NULL (None/NaN), not 0
    assert pd.isna(status["quarantined_count"])
    assert status["health_status"] is None


def test_other_normalization_still_works_alongside(monkeypatch):
    status = load(monkeypatch, collector_created_row())

    assert status["destination_breakdown"] == {}  # NULL JSONB -> {}
    assert status["last_run"] == RUN_TIME.isoformat()  # timestamps -> ISO strings
    assert status["watcher_last_active_at"] is None


# --- the consumers: nothing raises on a Collector-created row ---------------


def test_system_alerts_do_not_crash_and_report_no_data_quality_problem(monkeypatch):
    status = load(monkeypatch, collector_created_row())

    alerts = get_system_alerts(status, False, 0.0, 0.0)

    texts = " ".join(alert["text"] for alert in alerts)
    assert "invalid datetime" not in texts
    assert "missing destination routing" not in texts


def test_pipeline_context_builds_for_a_collector_created_row(monkeypatch):
    status = load(monkeypatch, collector_created_row())
    now_ct = datetime(2026, 9, 18, 12, 0, tzinfo=LOCAL_TZ)

    context = build_pipeline_context(status, pd.DataFrame(), now_ct, LOCAL_TZ, "light")

    assert context["pipeline_status_label"] == "Pipeline Healthy"
    assert context["transit_items"] == 0
    assert context["problem_items"] == 0
    assert context["uploaded_checkins_rows"] == 20


def test_live_today_run_summary_renders_and_shows_zero_for_null_counters(monkeypatch):
    status = load(monkeypatch, collector_created_row())
    now_ct = datetime(2026, 9, 18, 12, 0, tzinfo=LOCAL_TZ)
    context = build_pipeline_context(status, pd.DataFrame(), now_ct, LOCAL_TZ, "light")

    rendered = "\n".join(_render_run_summary(context))

    assert "Transit Items: 0" in rendered
    assert "Problem Items: 0" in rendered
    assert "Bad Checkin Datetimes: 0" in rendered
    assert "Bad Reject Datetimes: 0" in rendered
    assert "Uploaded Checkins This Run: 20" in rendered
    assert "New Checkins This Run: 20" in rendered


def test_live_today_run_summary_is_unchanged_for_a_full_legacy_row(monkeypatch):
    status = load(monkeypatch, legacy_agent_row())
    now_ct = datetime(2026, 9, 18, 12, 0, tzinfo=LOCAL_TZ)
    context = build_pipeline_context(status, pd.DataFrame(), now_ct, LOCAL_TZ, "light")

    rendered = "\n".join(_render_run_summary(context))

    assert "Transit Items: 7" in rendered
    assert "Problem Items: 3" in rendered
    assert "New Rejects This Run: 2" in rendered
    assert "Uploaded Rejects This Run: 1" in rendered


def test_every_view_consumed_counter_is_covered_by_the_normalized_set():
    # If a new counter starts being formatted/compared by the views, it must be
    # added to the loader's normalized set or this Collector-row hazard returns.
    assert set(CONSUMED_BY_VIEWS) <= set(dl.PIPELINE_STATUS_COUNTER_FIELDS)
    assert set(COUNTER_FIELDS) == set(dl.PIPELINE_STATUS_COUNTER_FIELDS)
