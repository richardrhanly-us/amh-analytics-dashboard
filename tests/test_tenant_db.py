"""Tests for src/tenant_db.py: the Streamlit-free tenant-context seam.

Recording fakes only -- no database. The SQL and bound values asserted here
are the exact ones data_loader._read_table and main._authenticate_agent use
today (see tests/test_read_table_tenant_context.py); real RLS enforcement is
covered by the opt-in tests in tests/test_rls_phase1_postgres.py.
"""

import pytest

from tenant_db import apply_tenant_context, tenant_connection

CUSTOMER_SQL = "SELECT set_config('app.operational_customer_id', :v, true)"
BRANCH_SQL = "SELECT set_config('app.operational_branch_id', :v, true)"


class RecordingConnection:
    def __init__(self, events, fail_on=None):
        self.events = events
        self.fail_on = fail_on

    def __enter__(self):
        self.events.append(("enter", self))
        return self

    def __exit__(self, exc_type, *_exc):
        self.events.append(("exit", self, exc_type))
        return False

    def execute(self, statement, params=None):
        self.events.append(("execute", self, str(statement), params))
        if self.fail_on is not None and self.fail_on in str(statement):
            raise RuntimeError("database error")


class RecordingEngine:
    def __init__(self, events, fail_on=None):
        self.events = events
        self.fail_on = fail_on
        self.connections = []

    def connect(self):
        connection = RecordingConnection(self.events, self.fail_on)
        self.connections.append(connection)
        return connection


@pytest.fixture
def events():
    return []


# --- apply_tenant_context ---------------------------------------------------


def test_apply_sets_customer_then_branch_as_bound_strings(events):
    conn = RecordingConnection(events)

    assert apply_tenant_context(conn, 10, 2) is None

    assert events == [
        ("execute", conn, CUSTOMER_SQL, {"v": "10"}),
        ("execute", conn, BRANCH_SQL, {"v": "2"}),
    ]


def test_apply_accepts_zero_ids(events):
    conn = RecordingConnection(events)

    apply_tenant_context(conn, 0, 0)

    assert [(sql, values) for _kind, _conn, sql, values in events] == [
        (CUSTOMER_SQL, {"v": "0"}),
        (BRANCH_SQL, {"v": "0"}),
    ]


@pytest.mark.parametrize(("customer_id", "branch_id"), [(None, 2), (10, None), (None, None)])
def test_apply_requires_both_ids_and_executes_nothing(events, customer_id, branch_id):
    with pytest.raises(ValueError):
        apply_tenant_context(RecordingConnection(events), customer_id, branch_id)

    assert events == []


def test_apply_propagates_a_database_error_after_the_customer_setting(events):
    conn = RecordingConnection(events, fail_on="operational_customer_id")

    with pytest.raises(RuntimeError, match="database error"):
        apply_tenant_context(conn, 10, 2)

    assert [event[2] for event in events] == [CUSTOMER_SQL]  # the branch setting never ran


# --- tenant_connection ------------------------------------------------------


def test_connection_applies_context_before_yielding_the_same_connection(events):
    engine = RecordingEngine(events)

    with tenant_connection(engine, 10, 2) as conn:
        events.append(("body", conn))

    assert len(engine.connections) == 1
    assert conn is engine.connections[0]
    assert events == [
        ("enter", conn),
        ("execute", conn, CUSTOMER_SQL, {"v": "10"}),
        ("execute", conn, BRANCH_SQL, {"v": "2"}),
        ("body", conn),
        ("exit", conn, None),
    ]


def test_connection_accepts_zero_ids(events):
    engine = RecordingEngine(events)

    with tenant_connection(engine, 0, 0):
        pass

    assert [event[3] for event in events if event[0] == "execute"] == [{"v": "0"}, {"v": "0"}]


@pytest.mark.parametrize(("customer_id", "branch_id"), [(None, 2), (10, None), (None, None)])
def test_connection_validates_ids_before_connecting(events, customer_id, branch_id):
    engine = RecordingEngine(events)

    with pytest.raises(ValueError), tenant_connection(engine, customer_id, branch_id):
        pytest.fail("the body must not run")

    assert engine.connections == []
    assert events == []


def test_connection_propagates_a_context_error_and_still_closes(events):
    engine = RecordingEngine(events, fail_on="operational_branch_id")

    with pytest.raises(RuntimeError, match="database error"), tenant_connection(engine, 10, 2):
        pytest.fail("the body must not run")

    conn = engine.connections[0]
    assert events[-1] == ("exit", conn, RuntimeError)


def test_connection_propagates_a_body_error_and_still_closes(events):
    engine = RecordingEngine(events)

    with pytest.raises(LookupError, match="query failed"), tenant_connection(engine, 10, 2):
        raise LookupError("query failed")

    conn = engine.connections[0]
    assert events[-1] == ("exit", conn, LookupError)
