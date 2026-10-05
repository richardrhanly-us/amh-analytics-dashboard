"""Block 9d: the pipeline status of one resolved tenant.

    get_pipeline_status(conn, tenant, now=...) -> PipelineStatus(state, last_reported_at)

The service decides which of two stored sources speaks for a branch (by its effective cutover, as of `now`), reads
exactly that one, maps what it finds through services.pipeline_state and answers with a state and an aware UTC
instant -- and with nothing that says which source it was.

These tests run the REAL SQL against in-memory SQLite tables that hold the columns the statements touch plus the
ones they must never return. SQLite has no row level security -- and neither, in production, has pipeline_status --
so everything proved here about tenant isolation is proved by each statement's own customer_id / branch_id filter.
SQLite hands a timestamp back as the text it was stored as; what a real driver returns (aware datetimes in the
session's own offset) is exercised with a canned connection further down, and on a real server in
tests/test_pipeline_status_service_postgres.py.

The state mapping itself is tested in tests/test_pipeline_state.py and the cutover lookup in
tests/test_operational_metrics_service.py. This file is about selection: which source, which signal, which row.

Imported the "flat" way (services.pipeline_status_service), the identity the API process uses.
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from services import (
    operational_metrics_service,
    operational_read_service,
    pipeline_state,
    pipeline_status_service,
)
from services.pipeline_status_service import PipelineStatus, get_pipeline_status
from services.tenant_resolution_service import ResolvedOperationalTenant

_DDL = (
    (
        "CREATE TABLE v2_cutovers (id INTEGER PRIMARY KEY, customer_id INTEGER, branch_id INTEGER, cutover_at TEXT, "
        "set_by TEXT, set_at TEXT, note TEXT)"
    ),
    # Deliberately WITHOUT the production primary key, so that a second row for one branch can be stored and the
    # service's refusal of it tested.
    (
        "CREATE TABLE pipeline_status (customer_id INTEGER, branch_id INTEGER, last_attempt TEXT, last_run TEXT, status TEXT, "
        "checkins_rows INTEGER, destination_breakdown TEXT, health_status TEXT, last_error TEXT, last_failure_category TEXT, "
        "watcher_last_active_at TEXT, updated_at TEXT, status_reported_at TEXT, health_status_reported_at TEXT)"
    ),
    (
        "CREATE TABLE ingest_key_ids (id INTEGER PRIMARY KEY, key_id TEXT, customer_id INTEGER, branch_id INTEGER, "
        "algorithm TEXT, status TEXT, created_at TEXT, retired_at TEXT, last_heartbeat_at TEXT, health_status TEXT, "
        "last_error_class TEXT, pending_outbox_count INTEGER, quarantined_count INTEGER, last_success_at TEXT, "
        "watcher_last_active_at TEXT, collector_last_run_at TEXT, collector_next_run_at TEXT, "
        "collector_run_duration_ms INTEGER, collector_schedule_status TEXT)"
    ),
)

CUSTOMER_A, BRANCH_A = 8101, 11
CUSTOMER_B, BRANCH_B = 8202, 21
TENANT_A = ResolvedOperationalTenant(
    org_slug="acme", branch_slug="main", access_mode="full",
    operational_customer_id=CUSTOMER_A, operational_branch_id=BRANCH_A,
)

NOW = datetime(2026, 10, 5, 19, 0, tzinfo=UTC)  # freshness: allow FRESH004 -- passed as now= to every call under test
EARLIER = NOW - timedelta(minutes=30)          # a report half an hour before `now`
LATER = NOW - timedelta(minutes=5)             # a report five minutes before `now`
LONG_AGO = NOW - timedelta(days=30)

NOTHING_REPORTED = PipelineStatus(state="unknown", last_reported_at=None)

# Distinctive values in every column the service must never return.
KEY_ID = "3db44444-931c-43cc-af3c-b1001443e761"
CANARY = "CANARY-raw-error-text-31234000123456"


def _stored(moment: datetime | None) -> str | None:
    """An instant as these tables hold it: ISO text carrying its offset (always +00:00, so text order is time order)."""
    return None if moment is None else moment.astimezone(UTC).isoformat(sep=" ")


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in _DDL:
            conn.execute(text(statement))
    yield engine
    engine.dispose()


def _cutover(engine, cutover_at: datetime | None, *, set_at="2026-01-01 00:00:00+00:00", customer_id=CUSTOMER_A,
             branch_id=BRANCH_A) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, set_at) "
                 "VALUES (:c, :b, :cutover_at, 'operator', :set_at)"),
            {"c": customer_id, "b": branch_id, "cutover_at": _stored(cutover_at), "set_at": set_at},
        )


def _legacy(engine, *, status=None, health_status=None, status_at=None, health_at=None, customer_id=CUSTOMER_A,
            branch_id=BRANCH_A) -> None:
    """A legacy row. `status_at` / `health_at` are the two server stamps: datetimes, raw text, or None."""
    def stamp(value):
        return _stored(value) if isinstance(value, datetime) else value

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO pipeline_status (customer_id, branch_id, last_attempt, last_run, status, checkins_rows, "
                 "destination_breakdown, health_status, last_error, last_failure_category, watcher_last_active_at, "
                 "updated_at, status_reported_at, health_status_reported_at) VALUES (:c, :b, '1988-01-01 00:00:00', "
                 "'1988-01-01 00:00:01', :status, 4001, :breakdown, :health, :error, 'retryable_infra', "
                 "'1988-01-01 00:00:02', '1988-01-01 00:00:03', :status_at, :health_at)"),
            {"c": customer_id, "b": branch_id, "status": status, "health": health_status, "status_at": stamp(status_at),
             "health_at": stamp(health_at), "error": CANARY, "breakdown": '{"CANARY-DESTINATION": 9}'},
        )


def _key(engine, *, health_status="healthy", schedule="healthy", heartbeat_at=LATER, status="active",
         customer_id=CUSTOMER_A, branch_id=BRANCH_A, key_id=KEY_ID) -> None:
    """An ingest key with its heartbeat snapshot. `heartbeat_at` is a datetime, raw text, or None (never reported)."""
    heartbeat = _stored(heartbeat_at) if isinstance(heartbeat_at, datetime) else heartbeat_at
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, algorithm, status, last_heartbeat_at, "
                 "health_status, last_error_class, pending_outbox_count, quarantined_count, last_success_at, "
                 "watcher_last_active_at, collector_last_run_at, collector_next_run_at, collector_run_duration_ms, "
                 "collector_schedule_status) VALUES (:k, :c, :b, 'hmac-sha256-v1', :s, :hb, :h, 'retryable_infra', 7003, "
                 "7004, '1988-01-01 00:00:00+00:00', '1988-01-01 00:00:01+00:00', '1988-01-01 00:00:02+00:00', "
                 "'1988-01-01 00:00:03+00:00', 7005, :sched)"),
            {"k": key_id, "c": customer_id, "b": branch_id, "s": status, "hb": heartbeat, "h": health_status,
             "sched": schedule},
        )


class Recorder:
    """Wraps the caller's connection: records every statement with the parameters it was given, and can fail a
    chosen one."""

    def __init__(self, conn, fail_on: str | None = None):
        self._conn = conn
        self.fail_on = fail_on
        self.statements: list[tuple[str, dict]] = []

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        self.statements.append((sql, dict(parameters or {})))
        if self.fail_on and f"FROM {self.fail_on} " in sql:
            raise RuntimeError(f"synthetic failure reading {self.fail_on}")
        return self._conn.execute(statement, parameters)

    def tables(self) -> list[str]:
        return [sql.split(" FROM ")[1].split(" ")[0] for sql, _ in self.statements]


def _read(engine, *, tenant=TENANT_A, now=NOW, fail_on=None):
    with engine.connect() as conn:
        recorder = Recorder(conn, fail_on=fail_on)
        result = get_pipeline_status(recorder, tenant, now=now)
    return result, recorder


def _status(engine, **kwargs) -> PipelineStatus:
    return _read(engine, **kwargs)[0]


LEGACY, CURRENT = ["v2_cutovers", "pipeline_status"], ["v2_cutovers", "ingest_key_ids"]


# =====================================================================================================================
# A. Which source: the effective cutover, as of `now`
# =====================================================================================================================

def _both_sources(engine) -> None:
    """A legacy row that says `failed` and a current key that says `ok`: the answer shows which one was read."""
    _legacy(engine, status="failed_upload", status_at=EARLIER)
    _key(engine, health_status="healthy", heartbeat_at=LATER)


def test_with_no_cutover_the_legacy_row_is_read(engine):
    _both_sources(engine)

    result, recorder = _read(engine)

    assert result == PipelineStatus("failed", EARLIER)
    assert recorder.tables() == LEGACY


def test_with_a_cutover_still_in_the_future_the_legacy_row_is_read(engine):
    _both_sources(engine)
    _cutover(engine, NOW + timedelta(seconds=1))

    result, recorder = _read(engine)

    assert result == PipelineStatus("failed", EARLIER)
    assert recorder.tables() == LEGACY


def test_at_the_exact_cutover_instant_the_current_source_is_read(engine):
    _both_sources(engine)
    _cutover(engine, NOW)

    result, recorder = _read(engine)

    assert result == PipelineStatus("ok", LATER)
    assert recorder.tables() == CURRENT


def test_one_microsecond_before_the_cutover_the_legacy_row_is_still_read(engine):
    _both_sources(engine)
    _cutover(engine, NOW)

    assert _status(engine, now=NOW - timedelta(microseconds=1)) == PipelineStatus("failed", EARLIER)


def test_after_the_cutover_the_current_source_is_read(engine):
    _both_sources(engine)
    _cutover(engine, LONG_AGO)

    result, recorder = _read(engine)

    assert result == PipelineStatus("ok", LATER)
    assert recorder.tables() == CURRENT


def test_a_latest_rollback_row_returns_the_branch_to_the_legacy_row(engine):
    _both_sources(engine)
    _cutover(engine, LONG_AGO, set_at="2026-06-01 00:00:00+00:00")
    _cutover(engine, None, set_at="2026-06-12 00:00:00+00:00")       # the latest record: a rollback

    result, recorder = _read(engine)

    assert result == PipelineStatus("failed", EARLIER)
    assert recorder.tables() == LEGACY


def test_a_cutover_recorded_again_after_a_rollback_is_the_one_that_counts(engine):
    _both_sources(engine)
    _cutover(engine, LONG_AGO, set_at="2026-06-01 00:00:00+00:00")
    _cutover(engine, None, set_at="2026-06-12 00:00:00+00:00")
    _cutover(engine, NOW + timedelta(days=1), set_at="2026-06-20 00:00:00+00:00")   # re-planned, for tomorrow

    assert _status(engine) == PipelineStatus("failed", EARLIER)                       # not yet
    assert _status(engine, now=NOW + timedelta(days=2)) == PipelineStatus("ok", LATER)


def test_an_active_ingest_key_does_not_make_a_branch_current_before_its_cutover(engine):
    # The dashboard switches to the key's status as soon as a key exists. Here only the cutover record decides.
    _legacy(engine, status="completed", status_at=EARLIER)
    _key(engine, health_status="error", heartbeat_at=LATER)

    result, recorder = _read(engine)

    assert result == PipelineStatus("ok", EARLIER)
    assert "ingest_key_ids" not in recorder.tables()


def test_an_old_cutover_row_does_not_keep_a_rolled_back_branch_current(engine):
    # The operator health check treats ANY non-null cutover row as "cut over". Here the latest row decides.
    _both_sources(engine)
    _cutover(engine, LONG_AGO, set_at="2026-03-01 00:00:00+00:00")
    _cutover(engine, None, set_at="2026-09-01 00:00:00+00:00")

    assert _read(engine)[1].tables() == LEGACY


def test_another_tenants_cutover_does_not_change_this_tenants_source(engine):
    _both_sources(engine)
    _cutover(engine, LONG_AGO, customer_id=CUSTOMER_B, branch_id=BRANCH_B)
    _cutover(engine, LONG_AGO, customer_id=CUSTOMER_A, branch_id=BRANCH_B)     # another branch of the same customer
    _cutover(engine, LONG_AGO, customer_id=CUSTOMER_B, branch_id=BRANCH_A)     # the same branch id, another customer

    assert _read(engine)[1].tables() == LEGACY


def test_the_cutover_is_compared_as_an_instant_whatever_offset_now_is_given_in(engine):
    _both_sources(engine)
    _cutover(engine, NOW)
    tokyo, chicago = timezone(timedelta(hours=9)), timezone(timedelta(hours=-5))

    for zone in (tokyo, chicago, UTC):
        assert _status(engine, now=NOW.astimezone(zone)) == PipelineStatus("ok", LATER), zone
        assert _status(engine, now=(NOW - timedelta(seconds=1)).astimezone(zone)) == PipelineStatus("failed", EARLIER), zone


def test_the_effective_cutover_rule_is_the_metrics_services_own_function():
    assert pipeline_status_service.get_effective_cutover is operational_metrics_service.get_effective_cutover
    source = inspect.getsource(get_pipeline_status)
    assert "get_effective_cutover(conn, tenant)" in source
    assert "cutover_at is not None and cutover_at <= now" in source       # at the instant itself: already current


# =====================================================================================================================
# B. The legacy row: the later stamp says which signal is the last report
# =====================================================================================================================

def test_a_branch_with_no_legacy_row_has_reported_nothing(engine):
    assert _status(engine) == NOTHING_REPORTED


def test_a_row_with_neither_stamp_is_unknown_whatever_it_holds(engine):
    # A row written before the server stamped reports: its status may say "completed", but nothing says WHEN.
    _legacy(engine, status="completed", health_status="healthy")

    assert _status(engine) == NOTHING_REPORTED


def test_with_only_a_status_stamp_the_run_status_is_the_report(engine):
    _legacy(engine, status="completed", health_status="degraded", status_at=LATER)

    assert _status(engine) == PipelineStatus("ok", LATER)       # the unstamped health_status is not used at all


def test_with_only_a_health_stamp_the_health_status_is_the_report(engine):
    _legacy(engine, status="failed_upload", health_status="healthy", health_at=LATER)

    assert _status(engine) == PipelineStatus("ok", LATER)       # the unstamped run status is not used at all


def test_a_later_status_stamp_wins_over_an_earlier_health_stamp(engine):
    _legacy(engine, status="failed_upload", health_status="healthy", status_at=LATER, health_at=EARLIER)

    assert _status(engine) == PipelineStatus("failed", LATER)


def test_a_later_health_stamp_wins_over_an_earlier_status_stamp(engine):
    _legacy(engine, status="failed_upload", health_status="healthy", status_at=EARLIER, health_at=LATER)

    assert _status(engine) == PipelineStatus("ok", LATER)


def test_the_later_stamp_wins_by_any_margin(engine):
    _legacy(engine, status="completed", health_status="auth_failure", status_at=LATER,
            health_at=LATER + timedelta(microseconds=1))
    assert _status(engine) == PipelineStatus("failed", LATER + timedelta(microseconds=1))


def test_on_an_exact_tie_the_health_status_is_the_report(engine):
    # One request carried both signals, so both stamps are the same instant. The health signal -- the closed,
    # structured one -- stands; the tie is never left to row or column order.
    for status, health, expected in (("completed", "degraded", "degraded"), ("failed_upload", "healthy", "ok"),
                                     ("completed", "auth_failure", "failed"), ("failed_upload", None, "unknown")):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM pipeline_status"))
        _legacy(engine, status=status, health_status=health, status_at=LATER, health_at=LATER)

        assert _status(engine) == PipelineStatus(expected, LATER), (status, health)


def test_a_tie_is_decided_on_the_instant_not_on_how_it_is_written(engine):
    # The same instant stored in two different offsets is still a tie.
    _legacy(engine, status="failed_upload", health_status="healthy",
            status_at="2026-10-05 13:55:00-05:00", health_at="2026-10-05 18:55:00+00:00")

    assert _status(engine) == PipelineStatus("ok", LATER)


def test_a_recent_preflight_probe_can_be_the_last_report_and_is_unknown(engine):
    # Accepted behaviour: an install or update probe writes status="preflight_check" and is stamped like any
    # report carrying `status`. Until the next real report it is the branch's last one.
    _legacy(engine, status="preflight_check", health_status="healthy", status_at=LATER, health_at=EARLIER)

    assert _status(engine) == PipelineStatus("unknown", LATER)   # unknown, WITH the time the probe was received


def test_a_run_that_has_only_started_is_unknown_with_its_time(engine):
    _legacy(engine, status="started", status_at=LATER)

    assert _status(engine) == PipelineStatus("unknown", LATER)


@pytest.mark.parametrize(
    ("status", "health_status", "status_at", "health_at", "expected"),
    [
        ("something_new", None, LATER, None, "unknown"),
        ("Completed", None, LATER, None, "unknown"),            # not exactly a known value
        ("", None, LATER, None, "unknown"),
        (None, None, LATER, None, "unknown"),                   # the run status was cleared by an explicit null
        ("completed", "excellent", EARLIER, LATER, "unknown"),  # the heartbeat is the last report and is unreadable
        ("completed", None, EARLIER, LATER, "unknown"),
        ("completed", "error", EARLIER, LATER, "unknown"),      # the CURRENT vocabulary's word: not a legacy value
        ("failed_something_never_seen", "healthy", LATER, EARLIER, "failed"),
    ],
)
def test_a_stored_value_that_is_not_recognised_stays_safe(engine, status, health_status, status_at, health_at, expected):
    _legacy(engine, status=status, health_status=health_status, status_at=status_at, health_at=health_at)

    result = _status(engine)

    assert result.state == expected and result.state != "ok"
    assert result.last_reported_at == LATER                       # the time is still the report's


def test_an_unreadable_last_report_is_never_replaced_by_the_older_readable_one(engine):
    # The heartbeat is the last report and cannot be read. The older run status says "completed" -- and is not used.
    _legacy(engine, status="completed", health_status="excellent", status_at=EARLIER, health_at=LATER)

    assert _status(engine) == PipelineStatus("unknown", LATER)


def test_the_legacy_signals_are_mapped_by_the_block_9c_functions():
    assert pipeline_status_service.state_for_run_status is pipeline_state.state_for_run_status
    assert pipeline_status_service.state_for_legacy_health is pipeline_state.state_for_legacy_health
    assert pipeline_status_service.state_for_current_report is pipeline_state.state_for_current_report


# =====================================================================================================================
# C. The current source
# =====================================================================================================================

@pytest.fixture
def current(engine):
    """A branch whose cutover happened long ago."""
    _cutover(engine, LONG_AGO)
    return engine


def test_a_healthy_heartbeat_with_no_schedule_fault_is_ok(current):
    _key(current, health_status="healthy", schedule="healthy", heartbeat_at=LATER)

    assert _status(current) == PipelineStatus("ok", LATER)


@pytest.mark.parametrize(("health_status", "expected"), [("healthy", "ok"), ("degraded", "degraded"), ("error", "failed")])
@pytest.mark.parametrize("schedule", ["healthy", None])
def test_each_current_health_value_has_its_state(current, health_status, expected, schedule):
    _key(current, health_status=health_status, schedule=schedule, heartbeat_at=LATER)

    assert _status(current) == PipelineStatus(expected, LATER)


@pytest.mark.parametrize(
    ("health_status", "schedule", "expected"),
    [
        ("healthy", "task_missing", "failed"), ("healthy", "task_disabled", "failed"), ("healthy", "no_next_run", "failed"),
        ("healthy", "query_failed", "degraded"), ("degraded", "task_disabled", "failed"),
        ("degraded", "query_failed", "degraded"), ("error", "query_failed", "failed"), ("error", "healthy", "failed"),
    ],
)
def test_a_reported_schedule_fault_worsens_the_state_and_never_improves_it(current, health_status, schedule, expected):
    _key(current, health_status=health_status, schedule=schedule, heartbeat_at=LATER)

    assert _status(current) == PipelineStatus(expected, LATER)


def test_a_current_branch_with_no_key_has_reported_nothing(current):
    assert _status(current) == NOTHING_REPORTED


def test_a_key_that_has_never_reported_is_unknown_with_no_time(current):
    _key(current, health_status=None, schedule=None, heartbeat_at=None)

    assert _status(current) == NOTHING_REPORTED


def test_a_state_is_never_returned_without_a_time_even_if_the_key_holds_one(current):
    # Not a state production can reach (the heartbeat sets both together), but if it did: no time, so no state.
    _key(current, health_status="healthy", schedule="healthy", heartbeat_at=None)

    assert _status(current) == NOTHING_REPORTED


def test_a_retired_key_is_not_a_report(current):
    _key(current, status="retired", health_status="healthy", heartbeat_at=LATER)

    assert _status(current) == NOTHING_REPORTED


def test_of_several_active_keys_the_one_that_reported_most_recently_is_the_report(current):
    _key(current, health_status="error", heartbeat_at=EARLIER, key_id="key-old")
    _key(current, health_status="degraded", heartbeat_at=LATER, key_id="key-new")
    _key(current, health_status="healthy", heartbeat_at=None, key_id="key-never")        # never reported: sorts last
    _key(current, status="retired", health_status="healthy", heartbeat_at=NOW, key_id="key-retired")

    assert _status(current) == PipelineStatus("degraded", LATER)


def test_a_current_branch_never_falls_back_to_its_old_legacy_row(current):
    _legacy(current, status="completed", health_status="healthy", status_at=LATER, health_at=LATER)

    result, recorder = _read(current)

    assert result == NOTHING_REPORTED          # the branch is current and its key has not reported: that is the answer
    assert "pipeline_status" not in recorder.tables()


def test_the_current_statement_reads_the_same_row_as_the_ingest_status_read():
    ours = " ".join(str(pipeline_status_service._CURRENT_PIPELINE_STATUS_SQL).split())
    theirs = " ".join(str(operational_read_service._LATEST_INGEST_STATUS_SQL).split())

    # The same FROM, WHERE, ORDER BY and LIMIT: active keys only, most recent heartbeat first, never-reported last.
    assert ours.split(" FROM ", 1)[1] == theirs.split(" FROM ", 1)[1]
    assert operational_read_service._LATEST_INGEST_STATUS_SQL is not pipeline_status_service._CURRENT_PIPELINE_STATUS_SQL


# =====================================================================================================================
# D. Tenant isolation: the statements' own filter (pipeline_status has no row level security)
# =====================================================================================================================

def _statement(name: str) -> str:
    return " ".join(str(getattr(pipeline_status_service, name)).split())


def test_the_legacy_statement_has_the_approved_shape():
    assert _statement("_LEGACY_PIPELINE_STATUS_SQL") == (
        "SELECT status, health_status, status_reported_at, health_status_reported_at "
        "FROM pipeline_status "
        "WHERE customer_id = :customer_id AND branch_id = :branch_id "
        "LIMIT 2"
    )


def test_the_current_statement_has_the_approved_shape():
    assert _statement("_CURRENT_PIPELINE_STATUS_SQL") == (
        "SELECT health_status, collector_schedule_status, last_heartbeat_at "
        "FROM ingest_key_ids "
        "WHERE customer_id = :customer_id AND branch_id = :branch_id AND status = 'active' "
        "ORDER BY last_heartbeat_at DESC NULLS LAST "
        "LIMIT 1"
    )


@pytest.mark.parametrize("name", ["_LEGACY_PIPELINE_STATUS_SQL", "_CURRENT_PIPELINE_STATUS_SQL"])
def test_each_statement_filters_both_tenant_columns_and_binds_nothing_else(name):
    statement = getattr(pipeline_status_service, name)
    sql = _statement(name)

    assert "customer_id = :customer_id AND branch_id = :branch_id" in sql
    assert set(statement._bindparams) == {"customer_id", "branch_id"}
    assert " OR " not in sql.upper() and sql.upper().count(" FROM ") == 1
    for forbidden in ("SELECT *", "JOIN", "UNION", "NOW()", "CURRENT_", "AT TIME ZONE", "::", "CAST"):
        assert forbidden not in sql.upper(), (name, forbidden)


@pytest.mark.parametrize("cutover_at", [None, LONG_AGO], ids=["legacy", "current"])
def test_every_statement_is_bound_to_exactly_the_resolved_tenants_ids(engine, cutover_at):
    tenant_b = ResolvedOperationalTenant(
        org_slug="beta", branch_slug="north", access_mode="read_only",
        operational_customer_id=CUSTOMER_B, operational_branch_id=BRANCH_B,
    )
    if cutover_at is not None:
        _cutover(engine, cutover_at)
        _cutover(engine, cutover_at, customer_id=CUSTOMER_B, branch_id=BRANCH_B)

    for tenant in (TENANT_A, tenant_b):
        _, recorder = _read(engine, tenant=tenant)

        assert len(recorder.statements) == 2
        for _sql, parameters in recorder.statements:
            assert parameters == {"customer_id": tenant.operational_customer_id,
                                  "branch_id": tenant.operational_branch_id}


OTHER_TENANTS = {
    "another customer and branch": (CUSTOMER_B, BRANCH_B),
    "the same branch id under another customer": (CUSTOMER_B, BRANCH_A),
    "another branch of the same customer": (CUSTOMER_A, BRANCH_B),
}


@pytest.mark.parametrize("other", list(OTHER_TENANTS))
def test_another_tenants_legacy_row_is_never_the_answer(engine, other):
    customer_id, branch_id = OTHER_TENANTS[other]
    _legacy(engine, status="failed_upload", health_status="auth_failure", status_at=LATER, health_at=LATER,
            customer_id=customer_id, branch_id=branch_id)

    assert _status(engine) == NOTHING_REPORTED          # this tenant has no row: the other's is not borrowed

    _legacy(engine, status="completed", status_at=EARLIER)
    assert _status(engine) == PipelineStatus("ok", EARLIER)   # and once it has one, only its own is read


@pytest.mark.parametrize("other", list(OTHER_TENANTS))
def test_another_tenants_ingest_key_is_never_the_answer(current, other):
    customer_id, branch_id = OTHER_TENANTS[other]
    _key(current, health_status="error", heartbeat_at=LATER, customer_id=customer_id, branch_id=branch_id, key_id="theirs")

    assert _status(current) == NOTHING_REPORTED

    _key(current, health_status="healthy", heartbeat_at=EARLIER, key_id="ours")
    assert _status(current) == PipelineStatus("ok", EARLIER)


def test_every_tenant_gets_its_own_answer_from_the_same_tables(engine):
    answers = {}
    for index, (customer_id, branch_id) in enumerate([(CUSTOMER_A, BRANCH_A), *OTHER_TENANTS.values()]):
        status = ("completed", "failed_upload", "started", "skipped_no_source_changes")[index]
        _legacy(engine, status=status, status_at=LATER - timedelta(minutes=index), customer_id=customer_id,
                branch_id=branch_id)
        tenant = ResolvedOperationalTenant(org_slug="o", branch_slug="b", access_mode="full",
                                           operational_customer_id=customer_id, operational_branch_id=branch_id)
        answers[(customer_id, branch_id)] = tenant

    seen = {scope: _status(engine, tenant=tenant) for scope, tenant in answers.items()}

    assert [result.state for result in seen.values()] == ["ok", "failed", "unknown", "ok"]
    assert len({result.last_reported_at for result in seen.values()}) == 4      # four different rows were read


def test_two_legacy_rows_for_one_branch_are_refused_not_answered_from_one_of_them(engine):
    # The primary key makes this impossible in production. If that guarantee were ever gone, picking a row would be
    # a guess -- on a table with no row level security.
    _legacy(engine, status="completed", status_at=LATER)
    _legacy(engine, status="failed_upload", status_at=EARLIER)

    with pytest.raises(RuntimeError, match="More than one pipeline status row"):
        _read(engine)


# =====================================================================================================================
# E. Time: always an aware UTC instant, and `now` must be aware
# =====================================================================================================================

def test_the_time_returned_is_an_aware_utc_datetime(engine):
    _legacy(engine, status="completed", status_at=LATER)

    result = _status(engine)

    assert isinstance(result.last_reported_at, datetime)
    assert result.last_reported_at.tzinfo is UTC and result.last_reported_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "written",
    ["2026-10-05 18:55:00+00:00", "2026-10-05T18:55:00+00:00", "2026-10-05 13:55:00-05:00", "2026-10-06 03:55:00+09:00",
     "2026-10-06 00:25:00+05:30", "2026-10-05 18:55:00.000000+00:00"],
)
def test_one_instant_written_in_any_offset_is_returned_as_the_same_utc_instant(engine, written):
    _legacy(engine, status="completed", status_at=written)

    result = _status(engine)

    assert result.last_reported_at == LATER and result.last_reported_at.tzinfo is UTC
    assert result.last_reported_at.isoformat() == "2026-10-05T18:55:00+00:00"


class _Canned:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class CannedConnection:
    """Answers the three statements with fixed rows -- so a stamp can be a datetime OBJECT exactly as a PostgreSQL
    driver hands one back, which SQLite never does."""

    def __init__(self, *, cutover_at=None, legacy=None, current=None):
        self._answers = {"v2_cutovers": [] if cutover_at is None else [(cutover_at,)],
                         "pipeline_status": [] if legacy is None else [legacy],
                         "ingest_key_ids": [] if current is None else [current]}
        self.tables: list[str] = []

    def execute(self, statement, parameters=None):
        table = " ".join(str(statement).split()).split(" FROM ")[1].split(" ")[0]
        self.tables.append(table)
        return _Canned(self._answers[table])


def _legacy_row(**values):
    return {"status": None, "health_status": None, "status_reported_at": None, "health_status_reported_at": None, **values}


@pytest.mark.parametrize("offset_hours", [0, -5, -6, 9, 5.5, -9.5, 14, -12])
def test_a_datetime_in_any_session_offset_is_normalized_to_the_same_utc_instant(offset_hours):
    # What psycopg2 returns for a TIMESTAMPTZ depends on the session's time zone; the instant does not.
    session_zone = timezone(timedelta(hours=offset_hours))
    as_the_driver_returns_it = LATER.astimezone(session_zone)

    legacy = get_pipeline_status(
        CannedConnection(legacy=_legacy_row(status="completed", status_reported_at=as_the_driver_returns_it)),
        TENANT_A, now=NOW)
    current = get_pipeline_status(
        CannedConnection(cutover_at=LONG_AGO, current={"health_status": "healthy", "collector_schedule_status": "healthy",
                                                       "last_heartbeat_at": as_the_driver_returns_it}),
        TENANT_A, now=NOW)

    for result in (legacy, current):
        assert result == PipelineStatus("ok", LATER)
        assert result.last_reported_at.tzinfo is UTC
        assert result.last_reported_at.isoformat() == "2026-10-05T18:55:00+00:00"


NAIVE = datetime(2026, 10, 5, 18, 55)  # noqa: DTZ001 - a naive datetime is the point


@pytest.mark.parametrize("naive", [NAIVE, "2026-10-05 18:55:00", "2026-10-05T18:55:00"], ids=["datetime", "text", "iso text"])
def test_a_legacy_stamp_with_no_offset_is_refused_never_labelled_utc(naive):
    for column in ("status_reported_at", "health_status_reported_at"):
        conn = CannedConnection(legacy=_legacy_row(status="completed", health_status="healthy", **{column: naive}))

        with pytest.raises(ValueError, match=f"pipeline_status.{column} must be timezone-aware"):
            get_pipeline_status(conn, TENANT_A, now=NOW)


@pytest.mark.parametrize("naive", [NAIVE, "2026-10-05 18:55:00"], ids=["datetime", "text"])
def test_a_current_heartbeat_time_with_no_offset_is_refused_never_labelled_utc(naive):
    conn = CannedConnection(cutover_at=LONG_AGO, current={"health_status": "healthy", "collector_schedule_status": "healthy",
                                                          "last_heartbeat_at": naive})

    with pytest.raises(ValueError, match="ingest_key_ids.last_heartbeat_at must be timezone-aware"):
        get_pipeline_status(conn, TENANT_A, now=NOW)


def test_a_naive_stamp_is_refused_even_when_the_other_signal_would_have_won(engine):
    # Both stamps are read before either is compared: a row with an unusable stamp is not half-trusted.
    _legacy(engine, status="completed", health_status="healthy", status_at="2026-10-05 18:00:00", health_at=LATER)

    with pytest.raises(ValueError, match="status_reported_at must be timezone-aware"):
        _read(engine)


@pytest.mark.parametrize("malformed", ["not-a-time", "yesterday", "2026-13-45 99:00:00+00:00", "1791225903", ""])
def test_a_malformed_stamp_is_an_error_never_a_guess(engine, malformed):
    # The empty string included: it is not a time, and it is not quietly treated as "no stamp" either.
    _legacy(engine, status="completed", status_at=malformed)

    with pytest.raises(ValueError):
        _read(engine)


@pytest.mark.parametrize("value", [1791225903, 1791225903.5, b"2026-10-05 18:55:00+00:00", True, ["2026"], object()],
                         ids=lambda value: type(value).__name__)
def test_a_stamp_that_is_not_a_timestamp_at_all_is_an_error(value):
    conn = CannedConnection(legacy=_legacy_row(status="completed", status_reported_at=value))

    with pytest.raises(ValueError, match="status_reported_at is not a timestamp"):
        get_pipeline_status(conn, TENANT_A, now=NOW)


@pytest.mark.parametrize("now", [datetime(2026, 10, 5, 19, 0), NOW.replace(tzinfo=None)], ids=["naive", "stripped"])  # noqa: DTZ001
def test_a_naive_now_is_refused_before_anything_is_read(engine, now):
    _both_sources(engine)
    _cutover(engine, LONG_AGO)

    with engine.connect() as conn:
        recorder = Recorder(conn)
        with pytest.raises(ValueError, match="now must be timezone-aware"):
            get_pipeline_status(recorder, TENANT_A, now=now)

    assert recorder.statements == []


def test_now_is_required_by_keyword_and_has_no_default():
    parameters = inspect.signature(get_pipeline_status).parameters

    assert list(parameters) == ["conn", "tenant", "now"]
    assert parameters["now"].kind is inspect.Parameter.KEYWORD_ONLY
    assert all(p.default is inspect.Parameter.empty for p in parameters.values())   # the caller always says what "now" is


def test_now_only_decides_the_source_never_the_state_or_the_time(engine):
    # A report of any age has the same state and the same time: nothing here judges staleness.
    _legacy(engine, status="completed", status_at=LATER)

    answers = {_status(engine, now=NOW + timedelta(days=days)) for days in (0, 1, 30, 3650)}

    assert answers == {PipelineStatus("ok", LATER)}
    assert _status(engine, now=LATER - timedelta(days=1)) == PipelineStatus("ok", LATER)   # even a `now` before the report


# =====================================================================================================================
# F. Query discipline
# =====================================================================================================================

@pytest.mark.parametrize(
    ("cutover_at", "expected_tables"),
    [(None, LEGACY), (NOW + timedelta(hours=1), LEGACY), (NOW, CURRENT), (LONG_AGO, CURRENT)],
    ids=["no cutover", "future cutover", "cutover now", "past cutover"],
)
def test_the_cutover_is_looked_up_once_and_then_exactly_one_source_is_read(engine, cutover_at, expected_tables):
    _both_sources(engine)
    if cutover_at is not None:
        _cutover(engine, cutover_at)

    _, recorder = _read(engine)

    assert recorder.tables() == expected_tables
    assert len(recorder.statements) == 2 and recorder.tables().count("v2_cutovers") == 1


def test_the_legacy_source_never_reads_ingest_key_ids_and_needs_no_such_table(engine):
    _legacy(engine, status="completed", status_at=LATER)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE ingest_key_ids"))

    assert _status(engine) == PipelineStatus("ok", LATER)


def test_the_current_source_never_reads_pipeline_status_and_needs_no_such_table(current):
    _key(current, heartbeat_at=LATER)
    with current.begin() as conn:
        conn.execute(text("DROP TABLE pipeline_status"))

    assert _status(current) == PipelineStatus("ok", LATER)


def test_the_statements_select_only_the_columns_the_answer_is_made_from():
    legacy = _statement("_LEGACY_PIPELINE_STATUS_SQL").split(" FROM ")[0]
    current = _statement("_CURRENT_PIPELINE_STATUS_SQL").split(" FROM ")[0]

    assert legacy == "SELECT status, health_status, status_reported_at, health_status_reported_at"
    assert current == "SELECT health_status, collector_schedule_status, last_heartbeat_at"
    for forbidden in ("last_error", "last_failure_category", "rows", "destination_breakdown", "updated_at", "last_attempt",
                      "last_run", "watcher_last_active_at", "last_success_at", "pending", "quarantined", "customer_id",
                      "branch_id"):
        assert forbidden not in legacy, forbidden
    for forbidden in ("key_id", " id", "algorithm", "last_error_class", "pending", "quarantined", "last_success_at",
                      "watcher_last_active_at", "collector_last_run_at", "collector_next_run_at", "duration",
                      "customer_id", "branch_id", "created_at", "retired_at"):
        assert forbidden not in current, forbidden


def test_the_module_has_exactly_two_statements_and_neither_asks_the_database_for_the_time():
    statements = sorted(name for name in vars(pipeline_status_service) if name.endswith("_SQL"))

    assert statements == ["_CURRENT_PIPELINE_STATUS_SQL", "_LEGACY_PIPELINE_STATUS_SQL"]
    for name in statements:
        for forbidden in ("NOW()", "CURRENT_TIMESTAMP", "CURRENT_DATE", "CLOCK_TIMESTAMP", "AT TIME ZONE", "TIMEZONE", "INTERVAL"):
            assert forbidden not in _statement(name).upper(), (name, forbidden)


def test_the_result_holds_a_state_and_a_time_and_nothing_else(engine):
    _legacy(engine, status="failed_upload", health_status="degraded", status_at=LATER, health_at=EARLIER)

    result = _status(engine)

    assert [field.name for field in dataclasses.fields(PipelineStatus)] == ["state", "last_reported_at"]
    assert dataclasses.asdict(result) == {"state": "failed", "last_reported_at": LATER}
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.state = "ok"
    assert not hasattr(result, "__dict__")       # slots: nothing can be attached to it either


@pytest.mark.parametrize("cutover_at", [None, LONG_AGO], ids=["legacy", "current"])
def test_nothing_raw_or_internal_reaches_the_result(engine, cutover_at):
    if cutover_at is not None:
        _cutover(engine, cutover_at)
    _legacy(engine, status="failed_upload", health_status="degraded", status_at=LATER, health_at=EARLIER)
    _key(engine, health_status="error", schedule="task_disabled", heartbeat_at=LATER)

    result = _status(engine)

    rendered = repr(result) + str(dataclasses.asdict(result))
    for leaked in ("CANARY", KEY_ID, "hmac", "retryable_infra", "failed_upload", "task_disabled", "error", "degraded",
                   "1988", "4001", "7003", "7004", "7005", str(CUSTOMER_A), "legacy", "current", "v1", "v2", "cutover",
                   "pipeline_status", "ingest_key_ids", "schedule", "heartbeat"):
        assert leaked not in rendered, leaked
    assert result.state in pipeline_state.PIPELINE_STATES


def test_the_answer_looks_the_same_whichever_source_it_came_from(engine):
    _legacy(engine, status="completed", status_at=LATER)
    legacy = _status(engine)

    _cutover(engine, LONG_AGO)
    _key(engine, health_status="healthy", heartbeat_at=LATER)
    current = _status(engine)

    assert legacy == current == PipelineStatus("ok", LATER)      # nothing in the value says which source it was
    assert type(legacy) is type(current) is PipelineStatus


# =====================================================================================================================
# G. Failures propagate; a failed read is never `unknown`
# =====================================================================================================================

@pytest.mark.parametrize(
    ("cutover_at", "failing"),
    [(None, "v2_cutovers"), (None, "pipeline_status"), (LONG_AGO, "v2_cutovers"), (LONG_AGO, "ingest_key_ids")],
    ids=["legacy: cutover lookup", "legacy: status read", "current: cutover lookup", "current: status read"],
)
def test_a_failure_in_any_statement_propagates_and_no_answer_is_returned(engine, cutover_at, failing):
    _both_sources(engine)
    if cutover_at is not None:
        _cutover(engine, cutover_at)

    with pytest.raises(RuntimeError, match=f"synthetic failure reading {failing}"):
        _read(engine, fail_on=failing)


def test_a_failed_cutover_lookup_is_never_read_as_no_cutover(engine):
    _both_sources(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE v2_cutovers"))

    with pytest.raises(Exception, match="v2_cutovers"):
        _read(engine)


def test_a_missing_status_table_is_an_error_not_an_unknown(engine):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE pipeline_status"))

    with pytest.raises(Exception, match="pipeline_status"):
        _read(engine)


def test_a_cutover_with_no_offset_is_refused_as_the_metrics_service_refuses_it(engine):
    _both_sources(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, set_at) "
                          "VALUES (:c, :b, '2026-01-01 00:00:00', 'operator', '2026-01-01 00:00:00+00:00')"),
                     {"c": CUSTOMER_A, "b": BRANCH_A})

    with pytest.raises(ValueError, match="v2_cutovers.cutover_at must be timezone-aware"):
        _read(engine)


# =====================================================================================================================
# Module boundaries
# =====================================================================================================================

def test_the_module_depends_only_on_the_standard_library_sqlalchemy_and_three_framework_neutral_services():
    imports = [line.strip() for line in inspect.getsource(pipeline_status_service).splitlines()
               if line.startswith(("import ", "from "))]

    assert imports == [
        "from __future__ import annotations",
        "from dataclasses import dataclass",
        "from datetime import UTC, datetime",
        "from sqlalchemy import text",
        "from sqlalchemy.engine import Connection",
        "from services.operational_metrics_service import get_effective_cutover",
        "from services.pipeline_state import (",
        "from services.tenant_resolution_service import ResolvedOperationalTenant",
    ]


def test_the_module_reads_no_clock_no_environment_and_opens_no_connection():
    source = inspect.getsource(pipeline_status_service)
    code = "\n".join(line for line in source.split('"""', 2)[2].splitlines() if not line.lstrip().startswith("#"))

    for forbidden in ("datetime.now", "utcnow", ".today(", "time.time", "os.environ", "getenv", "localtime", "get_engine",
                      "create_engine", ".connect(", ".begin(", "set_config", "tenant_connection", ".commit(", "cache",
                      "logger", "logging", "print(", "streamlit", "fastapi", "pandas", "ZoneInfo("):
        assert forbidden not in code, forbidden
    assert "astimezone()" not in code   # every conversion names its target zone


def test_the_service_takes_a_connection_and_makes_no_tenant_or_role_decision():
    source = inspect.getsource(pipeline_status_service)

    for forbidden in ("resolve_operational_tenant", "access_mode", "org_slug", "branch_slug", "role", "entitlement"):
        assert forbidden not in source.split('"""', 2)[2], forbidden


def test_the_existing_read_and_metrics_services_do_not_depend_on_this_one():
    for module in (operational_read_service, operational_metrics_service, pipeline_state):
        assert "pipeline_status_service" not in inspect.getsource(module), module.__name__
