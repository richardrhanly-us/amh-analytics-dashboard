"""Characterization tests for the tenant-context sequencing inside
src/data_loader.py::_read_table, using only recording fakes (no database).

Pins what the real-PostgreSQL RLS test (test_rls_phase1_postgres.py) proves
end to end but CI never runs: with both tenant ids, exactly two
transaction-local set_config calls (customer first, then branch) with bound
string values run on the SAME connection the read then uses; with either id
missing, no tenant context is set at all. A later change that routes
_read_table through a shared tenant-context helper (Phase 0, Block 1e) must
leave every case below unchanged.

data_loader.get_engine and pandas.read_sql are replaced at call time --
the same patch points the existing failure-path tests rely on.
"""

import pandas as pd
import pytest

import data_loader as dl

CUSTOMER_SQL = "SELECT set_config('app.operational_customer_id', :v, true)"
BRANCH_SQL = "SELECT set_config('app.operational_branch_id', :v, true)"
QUERY = "SELECT event_time FROM checkins WHERE customer_id = :org_slug AND branch_id = :branch_slug"


class RecordingConnection:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        self.events.append(("enter", self))
        return self

    def __exit__(self, *_exc):
        self.events.append(("exit", self))
        return False

    def execute(self, statement, params=None):
        self.events.append(("execute", self, str(statement), params))


class RecordingEngine:
    def __init__(self, events):
        self.events = events
        self.connections = []

    def connect(self):
        connection = RecordingConnection(self.events)
        self.connections.append(connection)
        return connection


@pytest.fixture
def recorder(monkeypatch):
    events = []
    engine = RecordingEngine(events)
    result = pd.DataFrame({"event_time": []})
    shown = []

    def recording_read_sql(sql, con, params=None, **_kwargs):
        events.append(("read_sql", con, str(sql), params))
        return result

    monkeypatch.setattr(dl, "get_engine", lambda: engine)
    monkeypatch.setattr(pd, "read_sql", recording_read_sql)
    monkeypatch.setattr(dl, "_show_db_error_once", lambda key, message: shown.append((key, message)))
    return {"events": events, "engine": engine, "result": result, "shown": shown}


def test_both_ids_set_customer_then_branch_context_on_the_reading_connection(recorder):
    params = {"org_slug": 10, "branch_slug": 2}

    frame = dl._read_table(QUERY, params=params, customer_id=10, branch_id=2)

    assert frame is recorder["result"]
    assert len(recorder["engine"].connections) == 1
    conn = recorder["engine"].connections[0]
    assert recorder["events"] == [
        ("enter", conn),
        ("execute", conn, CUSTOMER_SQL, {"v": "10"}),
        ("execute", conn, BRANCH_SQL, {"v": "2"}),
        ("read_sql", conn, QUERY, params),
        ("exit", conn),
    ]
    assert recorder["shown"] == []


def test_a_zero_id_still_counts_as_present(recorder):
    # The guard is `is not None`, not truthiness.
    dl._read_table(QUERY, params={}, customer_id=0, branch_id=0)

    executes = [event for event in recorder["events"] if event[0] == "execute"]
    assert [(sql, values) for _kind, _conn, sql, values in executes] == [
        (CUSTOMER_SQL, {"v": "0"}),
        (BRANCH_SQL, {"v": "0"}),
    ]


@pytest.mark.parametrize(
    ("customer_id", "branch_id"),
    [(None, 2), (10, None), (None, None)],
)
def test_a_missing_id_sets_no_tenant_context(recorder, customer_id, branch_id):
    frame = dl._read_table(QUERY, params={"org_slug": 10}, customer_id=customer_id, branch_id=branch_id)

    assert frame is recorder["result"]
    conn = recorder["engine"].connections[0]
    assert recorder["events"] == [
        ("enter", conn),
        ("read_sql", conn, QUERY, {"org_slug": 10}),
        ("exit", conn),
    ]


def test_tenant_ids_are_keyword_only_and_default_to_no_context(recorder):
    # The information_schema path (validate_tenant_schema) passes neither id.
    dl._read_table(QUERY)

    conn = recorder["engine"].connections[0]
    assert recorder["events"] == [
        ("enter", conn),
        ("read_sql", conn, QUERY, {}),  # params=None is passed to pandas as {}
        ("exit", conn),
    ]
    with pytest.raises(TypeError):
        dl._read_table(QUERY, {}, 10, 2)
