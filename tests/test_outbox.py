"""Tests for agent/outbox.py -- the SQLite outbox/file-state data layer.

No network access, no dependency on agent_config.json or SORTVIEW_API_TOKEN:
outbox.py takes an explicit db path and has no agent-internal imports.
"""

import pytest

from agent import outbox


@pytest.fixture
def conn(tmp_path):
    connection = outbox.connect(tmp_path / "agent.db")
    yield connection
    connection.close()


def test_connect_creates_schema(conn):
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "local_events" in tables
    assert "file_state" in tables


def test_connect_enables_wal_mode(tmp_path):
    connection = outbox.connect(tmp_path / "agent.db")
    try:
        (mode,) = connection.execute("PRAGMA journal_mode").fetchone()
        assert mode.lower() == "wal"
    finally:
        connection.close()


def test_connect_is_idempotent(tmp_path):
    db_path = tmp_path / "agent.db"
    first = outbox.connect(db_path)
    first.close()

    # Reconnecting to an existing db must not error on the CREATE TABLE /
    # CREATE INDEX statements (all IF NOT EXISTS), and must preserve data.
    second = outbox.connect(db_path)
    try:
        outbox.insert_event(
            second,
            event_type="checkin",
            customer_id=1,
            branch_id=1,
            event_timestamp="2026-08-27T10:00:00",
            dedup_key=outbox.compute_dedup_key({"barcode": "123"}),
            payload={"barcode": "123"},
        )
        third = outbox.connect(db_path)
        try:
            assert outbox.count_pending_events(third) == 1
        finally:
            third.close()
    finally:
        second.close()


def test_insert_event_new_row_returns_true(conn):
    inserted = outbox.insert_event(
        conn,
        event_type="checkin",
        customer_id=1,
        branch_id=2,
        event_timestamp="2026-08-27T10:00:00",
        dedup_key=outbox.compute_dedup_key({"barcode": "111"}),
        payload={"barcode": "111"},
    )
    assert inserted is True
    assert outbox.count_pending_events(conn) == 1


def test_insert_event_duplicate_dedup_key_returns_false(conn):
    dedup_key = outbox.compute_dedup_key({"barcode": "222"})

    first = outbox.insert_event(
        conn,
        event_type="checkin",
        customer_id=1,
        branch_id=2,
        event_timestamp="2026-08-27T10:00:00",
        dedup_key=dedup_key,
        payload={"barcode": "222"},
    )
    second = outbox.insert_event(
        conn,
        event_type="checkin",
        customer_id=1,
        branch_id=2,
        event_timestamp="2026-08-27T10:00:00",
        dedup_key=dedup_key,
        payload={"barcode": "222", "different": "payload"},
    )

    assert first is True
    assert second is False
    assert outbox.count_pending_events(conn) == 1


def test_dedup_key_is_scoped_per_event_type(conn):
    # Same dedup_key material, different event_type -- both must be kept,
    # since the unique index is on (event_type, dedup_key) together.
    dedup_key = outbox.compute_dedup_key({"x": "1"})

    outbox.insert_event(
        conn,
        event_type="checkin",
        customer_id=1,
        branch_id=1,
        event_timestamp=None,
        dedup_key=dedup_key,
        payload={},
    )
    outbox.insert_event(
        conn,
        event_type="reject",
        customer_id=1,
        branch_id=1,
        event_timestamp=None,
        dedup_key=dedup_key,
        payload={},
    )

    assert outbox.count_pending_events(conn) == 2


def test_compute_dedup_key_stable_and_order_independent():
    a = outbox.compute_dedup_key({"a": 1, "b": 2})
    b = outbox.compute_dedup_key({"b": 2, "a": 1})
    c = outbox.compute_dedup_key({"a": 1, "b": 3})

    assert a == b
    assert a != c


def test_file_state_round_trip(conn):
    assert outbox.get_file_state(conn, "C:\\logs\\Checkins.txt") is None

    outbox.save_file_state(
        conn,
        file_path="C:\\logs\\Checkins.txt",
        file_dev=1,
        file_ino=100,
        last_byte_offset=500,
        last_modified="2026-08-27T10:00:00.000000Z",
    )

    state = outbox.get_file_state(conn, "C:\\logs\\Checkins.txt")
    assert state is not None
    assert state["file_dev"] == 1
    assert state["file_ino"] == 100
    assert state["last_byte_offset"] == 500


def test_file_state_upsert_overwrites(conn):
    outbox.save_file_state(
        conn,
        file_path="C:\\logs\\Checkins.txt",
        file_dev=1,
        file_ino=100,
        last_byte_offset=500,
        last_modified="2026-08-27T10:00:00.000000Z",
    )
    outbox.save_file_state(
        conn,
        file_path="C:\\logs\\Checkins.txt",
        file_dev=1,
        file_ino=100,
        last_byte_offset=900,
        last_modified="2026-08-27T10:05:00.000000Z",
    )

    state = outbox.get_file_state(conn, "C:\\logs\\Checkins.txt")
    assert state["last_byte_offset"] == 900


def test_transaction_commits_on_success(conn):
    with outbox.transaction(conn):
        outbox.insert_event(
            conn,
            event_type="checkin",
            customer_id=1,
            branch_id=1,
            event_timestamp=None,
            dedup_key=outbox.compute_dedup_key({"x": "1"}),
            payload={},
        )

    assert outbox.count_pending_events(conn) == 1


def test_transaction_rolls_back_on_exception(conn):
    with pytest.raises(RuntimeError), outbox.transaction(conn):
        outbox.insert_event(
            conn,
            event_type="checkin",
            customer_id=1,
            branch_id=1,
            event_timestamp=None,
            dedup_key=outbox.compute_dedup_key({"x": "1"}),
            payload={},
        )
        raise RuntimeError("simulated failure mid-batch")

    # The insert above must not have survived the rollback.
    assert outbox.count_pending_events(conn) == 0


def test_transaction_rollback_also_protects_file_state(conn):
    outbox.save_file_state(
        conn,
        file_path="C:\\logs\\Checkins.txt",
        file_dev=1,
        file_ino=100,
        last_byte_offset=0,
        last_modified=None,
    )

    with pytest.raises(RuntimeError), outbox.transaction(conn):
        outbox.save_file_state(
            conn,
            file_path="C:\\logs\\Checkins.txt",
            file_dev=1,
            file_ino=100,
            last_byte_offset=999,
            last_modified=None,
        )
        raise RuntimeError("simulated failure after offset update")

    # The offset update must not have survived the rollback -- otherwise
    # a crash mid-batch would permanently skip events that were never
    # actually committed to local_events.
    state = outbox.get_file_state(conn, "C:\\logs\\Checkins.txt")
    assert state["last_byte_offset"] == 0
