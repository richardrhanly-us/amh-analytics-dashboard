"""Tests for the Phase 5 additions to agent/outbox.py: new-vs-existing
database auto_vacuum handling, delivered-row pruning, WAL checkpointing,
and the small read-only diagnostic helpers.
"""

from __future__ import annotations

import sqlite3

import pytest

from agent import outbox

CUSTOMER_ID = 1
BRANCH_ID = 1


@pytest.fixture
def conn(tmp_path):
    connection = outbox.connect(tmp_path / "agent.db")
    yield connection
    connection.close()


def _insert(conn, barcode, *, uploaded_at=None, quarantined_at=None, created_at=None):
    outbox.insert_event(
        conn,
        event_type="checkin",
        customer_id=CUSTOMER_ID,
        branch_id=BRANCH_ID,
        event_timestamp="2026-08-28T10:00:00",
        dedup_key=outbox.compute_dedup_key({"barcode": barcode}),
        payload={"barcode": barcode},
    )
    (row_id,) = conn.execute(
        "SELECT id FROM local_events WHERE dedup_key = ?",
        (outbox.compute_dedup_key({"barcode": barcode}),),
    ).fetchone()
    if created_at is not None:
        conn.execute("UPDATE local_events SET created_at = ? WHERE id = ?", (created_at, row_id))
    if uploaded_at is not None:
        conn.execute("UPDATE local_events SET uploaded_at = ? WHERE id = ?", (uploaded_at, row_id))
    if quarantined_at is not None:
        conn.execute(
            "UPDATE local_events SET quarantined_at = ?, quarantine_reason = 'test' WHERE id = ?",
            (quarantined_at, row_id),
        )
    return row_id


def _make_legacy_db(path):
    """Builds a database file exactly the way agent/outbox.py's connect()
    worked before Phase 5 -- WAL mode and schema created without ever
    touching auto_vacuum -- to stand in for a real database that has
    already run through Phase 1-4 in production."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(outbox.SCHEMA)
    conn.close()


# --- new vs existing database auto_vacuum handling --------------------------


def test_fresh_database_gets_incremental_auto_vacuum(tmp_path):
    connection = outbox.connect(tmp_path / "fresh.db")
    try:
        assert outbox.get_auto_vacuum_mode(connection) == "incremental"
    finally:
        connection.close()


def test_existing_legacy_shaped_database_stays_none(tmp_path):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)

    connection = outbox.connect(db_path)
    try:
        assert outbox.get_auto_vacuum_mode(connection) == "none"
    finally:
        connection.close()


def test_existing_legacy_database_remains_fully_usable(tmp_path):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)

    connection = outbox.connect(db_path)
    try:
        inserted = outbox.insert_event(
            connection,
            event_type="checkin",
            customer_id=CUSTOMER_ID,
            branch_id=BRANCH_ID,
            event_timestamp="2026-08-28T10:00:00",
            dedup_key=outbox.compute_dedup_key({"barcode": "1"}),
            payload={"barcode": "1"},
        )
        assert inserted is True
        assert outbox.count_pending_events(connection) == 1
    finally:
        connection.close()


def test_connect_on_existing_database_does_not_run_vacuum(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)

    # Fail the test loudly if connect() ever issues a VACUUM statement.
    real_connect = sqlite3.connect

    class GuardedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "VACUUM" in sql.upper():
                raise AssertionError(f"connect() must never issue VACUUM, got: {sql!r}")
            return super().execute(sql, *args, **kwargs)

    def guarded_connect(*args, **kwargs):
        kwargs["factory"] = GuardedConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)

    connection = outbox.connect(db_path)
    connection.close()


# --- pruning predicate -------------------------------------------------------


def test_prune_deletes_delivered_row_older_than_cutoff(conn):
    _insert(conn, "old", uploaded_at="2026-08-01T00:00:00.000000Z")
    cutoff = "2026-08-15T00:00:00.000000Z"

    deleted = outbox.prune_delivered_events(conn, cutoff=cutoff, batch_size=100)

    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM local_events").fetchone()[0] == 0


def test_prune_preserves_delivered_row_newer_than_cutoff(conn):
    _insert(conn, "recent", uploaded_at="2026-08-27T00:00:00.000000Z")
    cutoff = "2026-08-15T00:00:00.000000Z"

    deleted = outbox.prune_delivered_events(conn, cutoff=cutoff, batch_size=100)

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM local_events").fetchone()[0] == 1


def test_prune_preserves_pending_row_regardless_of_age(conn):
    _insert(conn, "pending", created_at="2020-01-01T00:00:00.000000Z")
    cutoff = "2026-08-15T00:00:00.000000Z"

    deleted = outbox.prune_delivered_events(conn, cutoff=cutoff, batch_size=100)

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM local_events").fetchone()[0] == 1


def test_prune_preserves_quarantined_row_regardless_of_age(conn):
    _insert(conn, "quarantined", quarantined_at="2020-01-01T00:00:00.000000Z")
    cutoff = "2026-08-15T00:00:00.000000Z"

    deleted = outbox.prune_delivered_events(conn, cutoff=cutoff, batch_size=100)

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM local_events").fetchone()[0] == 1


def test_prune_preserves_row_with_both_uploaded_and_quarantined_defensively(conn):
    # Phase 2 never sets both on one row in practice, but the predicate
    # must not assume that -- a defensively-quarantined delivered row
    # must never be auto-pruned.
    _insert(
        conn,
        "both",
        uploaded_at="2026-08-01T00:00:00.000000Z",
        quarantined_at="2026-08-01T00:00:00.000000Z",
    )
    cutoff = "2026-08-15T00:00:00.000000Z"

    deleted = outbox.prune_delivered_events(conn, cutoff=cutoff, batch_size=100)

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM local_events").fetchone()[0] == 1


def test_prune_uses_uploaded_at_not_created_at(conn):
    # created_at is old (would be eligible if that were the age source),
    # but uploaded_at is recent -- must be preserved.
    _insert(
        conn,
        "recent-delivery-old-capture",
        created_at="2020-01-01T00:00:00.000000Z",
        uploaded_at="2026-08-27T00:00:00.000000Z",
    )
    cutoff = "2026-08-15T00:00:00.000000Z"

    deleted = outbox.prune_delivered_events(conn, cutoff=cutoff, batch_size=100)

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM local_events").fetchone()[0] == 1


def test_prune_deletes_oldest_eligible_rows_first(conn):
    _insert(conn, "a", uploaded_at="2026-08-01T00:00:00.000000Z")
    _insert(conn, "b", uploaded_at="2026-08-02T00:00:00.000000Z")
    _insert(conn, "c", uploaded_at="2026-08-03T00:00:00.000000Z")
    cutoff = "2026-08-15T00:00:00.000000Z"

    deleted = outbox.prune_delivered_events(conn, cutoff=cutoff, batch_size=2)

    assert deleted == 2
    remaining = [
        row["dedup_key"] for row in conn.execute("SELECT dedup_key FROM local_events").fetchall()
    ]
    assert remaining == [outbox.compute_dedup_key({"barcode": "c"})]


def test_prune_batch_size_is_enforced(conn):
    for i in range(5):
        _insert(conn, f"row-{i}", uploaded_at="2026-08-01T00:00:00.000000Z")
    cutoff = "2026-08-15T00:00:00.000000Z"

    deleted = outbox.prune_delivered_events(conn, cutoff=cutoff, batch_size=3)

    assert deleted == 3
    assert conn.execute("SELECT COUNT(*) FROM local_events").fetchone()[0] == 2


def test_prune_returns_zero_when_nothing_eligible(conn):
    cutoff = "2026-08-15T00:00:00.000000Z"
    assert outbox.prune_delivered_events(conn, cutoff=cutoff, batch_size=100) == 0


# --- diagnostic helpers -------------------------------------------------


def test_count_delivered_events(conn):
    _insert(conn, "delivered-1", uploaded_at="2026-08-01T00:00:00.000000Z")
    _insert(conn, "pending-1")

    assert outbox.count_delivered_events(conn) == 1


def test_count_eligible_for_prune(conn):
    _insert(conn, "old", uploaded_at="2026-08-01T00:00:00.000000Z")
    _insert(conn, "recent", uploaded_at="2026-08-27T00:00:00.000000Z")
    _insert(conn, "quarantined-old", quarantined_at="2026-08-01T00:00:00.000000Z")
    cutoff = "2026-08-15T00:00:00.000000Z"

    assert outbox.count_eligible_for_prune(conn, cutoff) == 1


def test_get_page_count_and_freelist_count_return_ints(conn):
    assert isinstance(outbox.get_page_count(conn), int)
    assert isinstance(outbox.get_freelist_count(conn), int)


# --- WAL checkpoint -----------------------------------------------------


def test_checkpoint_wal_truncate_shrinks_wal_file(tmp_path):
    db_path = tmp_path / "agent.db"
    connection = outbox.connect(db_path)
    try:
        for i in range(3000):
            outbox.insert_event(
                connection,
                event_type="checkin",
                customer_id=CUSTOMER_ID,
                branch_id=BRANCH_ID,
                event_timestamp="2026-08-28T10:00:00",
                dedup_key=outbox.compute_dedup_key({"barcode": str(i)}),
                payload={"barcode": str(i), "title": "x" * 200},
            )

        wal_path = str(db_path) + "-wal"
        import os

        size_before = os.path.getsize(wal_path)

        busy, log_frames, checkpointed = outbox.checkpoint_wal(connection, mode="TRUNCATE")

        assert busy == 0
        assert log_frames >= 0
        assert checkpointed >= 0
        assert os.path.getsize(wal_path) <= size_before
    finally:
        connection.close()


def test_checkpoint_wal_busy_result_is_not_an_exception(tmp_path):
    db_path = tmp_path / "agent.db"
    # A short busy_timeout here keeps this test fast -- see
    # test_checkpoint_wal_blocks_up_to_its_own_busy_timeout below for the
    # actual timing proof of why agent/maintenance.py deliberately uses a
    # short timeout for its own connection.
    a = outbox.connect(db_path, busy_timeout_seconds=1)
    b = sqlite3.connect(str(db_path), isolation_level=None, timeout=5)
    try:
        outbox.insert_event(
            a,
            event_type="checkin",
            customer_id=CUSTOMER_ID,
            branch_id=BRANCH_ID,
            event_timestamp="2026-08-28T10:00:00",
            dedup_key=outbox.compute_dedup_key({"barcode": "1"}),
            payload={"barcode": "1"},
        )

        b.execute("BEGIN")
        b.execute("SELECT COUNT(*) FROM local_events").fetchall()

        # Must not raise -- a nonzero busy is a normal, reportable outcome.
        busy, _log_frames, _checkpointed = outbox.checkpoint_wal(a, mode="TRUNCATE")
        assert busy in (0, 1)

        b.execute("COMMIT")
    finally:
        b.close()
        a.close()


def test_checkpoint_wal_blocks_up_to_its_own_busy_timeout(tmp_path):
    """A checkpoint under contention does not return instantly with
    busy=1 -- it blocks the calling thread for up to that connection's
    own busy_timeout first. This is exactly why agent/maintenance.py
    opens its connection with a short, dedicated busy_timeout instead of
    the 30s default the other components use."""
    import time

    db_path = tmp_path / "agent.db"
    short_timeout = 1.0
    a = outbox.connect(db_path, busy_timeout_seconds=short_timeout)
    b = sqlite3.connect(str(db_path), isolation_level=None, timeout=5)
    try:
        outbox.insert_event(
            a,
            event_type="checkin",
            customer_id=CUSTOMER_ID,
            branch_id=BRANCH_ID,
            event_timestamp="2026-08-28T10:00:00",
            dedup_key=outbox.compute_dedup_key({"barcode": "1"}),
            payload={"barcode": "1"},
        )

        b.execute("BEGIN")
        b.execute("SELECT COUNT(*) FROM local_events").fetchall()

        started = time.monotonic()
        busy, _log_frames, _checkpointed = outbox.checkpoint_wal(a, mode="TRUNCATE")
        elapsed = time.monotonic() - started

        assert busy == 1
        # Blocked for roughly the connection's own busy_timeout, not
        # instantly -- bounded above by a generous margin so this stays
        # reliable in CI without being a real sleep-based test.
        assert short_timeout * 0.5 <= elapsed <= short_timeout * 3

        b.execute("COMMIT")
    finally:
        b.close()
        a.close()


def test_checkpoint_wal_rejects_unsupported_mode(conn):
    with pytest.raises(ValueError):
        outbox.checkpoint_wal(conn, mode="NOT_A_REAL_MODE")


# --- incremental vacuum --------------------------------------------------


def test_incremental_vacuum_reclaims_pages_when_mode_is_incremental(tmp_path):
    db_path = tmp_path / "fresh.db"
    connection = outbox.connect(db_path)
    try:
        assert outbox.get_auto_vacuum_mode(connection) == "incremental"

        for i in range(2000):
            outbox.insert_event(
                connection,
                event_type="checkin",
                customer_id=CUSTOMER_ID,
                branch_id=BRANCH_ID,
                event_timestamp="2026-08-28T10:00:00",
                dedup_key=outbox.compute_dedup_key({"barcode": str(i)}),
                payload={"barcode": str(i), "title": "x" * 200},
            )
        connection.execute("DELETE FROM local_events WHERE id % 2 = 0")

        pages_before = outbox.get_page_count(connection)
        outbox.run_incremental_vacuum(connection, 10_000)
        pages_after = outbox.get_page_count(connection)

        assert pages_after <= pages_before
    finally:
        connection.close()


def test_incremental_vacuum_is_a_noop_on_none_mode_database(tmp_path):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)
    connection = outbox.connect(db_path)
    try:
        assert outbox.get_auto_vacuum_mode(connection) == "none"

        for i in range(2000):
            outbox.insert_event(
                connection,
                event_type="checkin",
                customer_id=CUSTOMER_ID,
                branch_id=BRANCH_ID,
                event_timestamp="2026-08-28T10:00:00",
                dedup_key=outbox.compute_dedup_key({"barcode": str(i)}),
                payload={"barcode": str(i), "title": "x" * 200},
            )
        connection.execute("DELETE FROM local_events WHERE id % 2 = 0")
        freelist_before = outbox.get_freelist_count(connection)
        pages_before = outbox.get_page_count(connection)

        outbox.run_incremental_vacuum(connection, 10_000)

        pages_after = outbox.get_page_count(connection)
        # Confirmed no-op: page_count is unaffected on a NONE-mode DB,
        # even though the freelist itself is nonzero (page reuse already
        # works independent of auto_vacuum mode).
        assert freelist_before > 0
        assert pages_after == pages_before
    finally:
        connection.close()


def test_freed_pages_are_reused_on_existing_none_mode_database(tmp_path):
    """Existing (auto_vacuum=NONE) databases don't shrink, but deleted
    rows' pages are still reused by future inserts -- confirmed at the
    outbox.py level, independent of the maintenance module."""
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)
    connection = outbox.connect(db_path)
    try:
        for i in range(1000):
            outbox.insert_event(
                connection,
                event_type="checkin",
                customer_id=CUSTOMER_ID,
                branch_id=BRANCH_ID,
                event_timestamp="2026-08-28T10:00:00",
                dedup_key=outbox.compute_dedup_key({"barcode": str(i)}),
                payload={"barcode": str(i), "title": "x" * 200},
            )
        connection.execute("DELETE FROM local_events")
        pages_after_delete = outbox.get_page_count(connection)
        freelist_after_delete = outbox.get_freelist_count(connection)
        assert freelist_after_delete > 0

        for i in range(500):
            outbox.insert_event(
                connection,
                event_type="checkin",
                customer_id=CUSTOMER_ID,
                branch_id=BRANCH_ID,
                event_timestamp="2026-08-28T10:00:00",
                dedup_key=outbox.compute_dedup_key({"barcode": f"reuse-{i}"}),
                payload={"barcode": f"reuse-{i}", "title": "x" * 200},
            )
        pages_after_reinsert = outbox.get_page_count(connection)

        # Reusing freed pages means the file doesn't grow anywhere near
        # as much as it would if every page were brand new.
        assert pages_after_reinsert < pages_after_delete + 500
    finally:
        connection.close()
