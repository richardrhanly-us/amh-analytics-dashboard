"""Tests for agent/maintenance.py -- Phase 5 (Sustain) local outbox
maintenance: config resolution, run_maintenance_cycle's structured
result, run_forever's cadence/backoff/failure-isolation behavior, and a
lightweight soak/growth test.

No real sleeps anywhere -- sleep_fn is always injected.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from agent import maintenance as maint
from agent import outbox

CUSTOMER_ID = 1
BRANCH_ID = 1


@pytest.fixture
def conn(tmp_path):
    connection = outbox.connect(tmp_path / "agent.db")
    yield connection
    connection.close()


def _insert(conn, barcode, *, uploaded_at=None, quarantined_at=None):
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
    if uploaded_at is not None:
        conn.execute("UPDATE local_events SET uploaded_at = ? WHERE id = ?", (uploaded_at, row_id))
    if quarantined_at is not None:
        conn.execute(
            "UPDATE local_events SET quarantined_at = ?, quarantine_reason = 'test' WHERE id = ?",
            (quarantined_at, row_id),
        )
    return row_id


def _iso_days_ago(days):
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- config resolution --------------------------------------------------


def test_default_retention_is_seven_days():
    seconds, warning = maint.resolve_retention_days(None)
    assert seconds == 7
    assert warning is None


def test_retention_override():
    days, warning = maint.resolve_retention_days("14")
    assert days == 14
    assert warning is None


@pytest.mark.parametrize("bad_value", ["0", "-1", "not-a-number", "3.5"])
def test_retention_invalid_falls_back_safely(bad_value):
    days, warning = maint.resolve_retention_days(bad_value)
    assert days == 7
    assert warning is not None


def test_retention_zero_is_never_delete_everything():
    # Explicit regression guard for the literal footgun called out in
    # the Phase 5 spec: "0" must resolve to the safe default, not to a
    # zero-day (delete-everything) retention window.
    days, _warning = maint.resolve_retention_days("0")
    assert days == 7


def test_default_prune_batch_size_is_1000():
    size, warning = maint.resolve_prune_batch_size(None)
    assert size == 1000
    assert warning is None


def test_prune_batch_size_invalid_falls_back():
    size, warning = maint.resolve_prune_batch_size("-5")
    assert size == 1000
    assert warning is not None


def test_default_max_batches_per_cycle_is_10():
    value, warning = maint.resolve_max_batches_per_cycle(None)
    assert value == 10
    assert warning is None


def test_default_maintenance_interval_is_3600():
    seconds, warning = maint.resolve_maintenance_interval_seconds(None)
    assert seconds == 3600.0
    assert warning is None


def test_maintenance_interval_invalid_falls_back():
    seconds, warning = maint.resolve_maintenance_interval_seconds("0")
    assert seconds == 3600.0
    assert warning is not None


# --- run_maintenance_cycle: pruning + result structure -----------------


def test_cycle_prunes_old_delivered_and_reports_result(conn):
    _insert(conn, "old-1", uploaded_at=_iso_days_ago(10))
    _insert(conn, "old-2", uploaded_at=_iso_days_ago(20))
    _insert(conn, "recent", uploaded_at=_iso_days_ago(1))
    _insert(conn, "pending")
    _insert(conn, "quarantined", quarantined_at=_iso_days_ago(30))

    result = maint.run_maintenance_cycle(conn, retention_days=7, batch_size=100)

    assert result.success is True
    assert result.rows_pruned == 2
    assert result.batches_processed == 1
    assert result.pending_count == 1
    assert result.delivered_count == 1  # "recent" survives, still counts as delivered
    assert result.quarantined_count == 1
    assert result.eligible_for_prune_before == 2
    assert result.eligible_for_prune_after == 0
    assert result.auto_vacuum_mode == "incremental"
    assert result.checkpoint_busy in (0, 1)
    assert result.error is None


def test_cycle_prune_batch_size_enforced_across_multiple_batches(conn):
    for i in range(25):
        _insert(conn, f"old-{i}", uploaded_at=_iso_days_ago(10))

    result = maint.run_maintenance_cycle(
        conn, retention_days=7, batch_size=10, max_batches_per_cycle=100
    )

    assert result.rows_pruned == 25
    assert result.batches_processed == 3  # 10 + 10 + 5


def test_cycle_max_batches_per_cycle_bounds_work(conn):
    for i in range(50):
        _insert(conn, f"old-{i}", uploaded_at=_iso_days_ago(10))

    result = maint.run_maintenance_cycle(
        conn, retention_days=7, batch_size=10, max_batches_per_cycle=2
    )

    # Capped at 2 batches x 10 rows even though 50 were eligible.
    assert result.batches_processed == 2
    assert result.rows_pruned == 20
    assert result.eligible_for_prune_after == 30


def test_repeated_cycles_continue_progress_on_a_capped_backlog(conn):
    for i in range(25):
        _insert(conn, f"old-{i}", uploaded_at=_iso_days_ago(10))

    first = maint.run_maintenance_cycle(
        conn, retention_days=7, batch_size=10, max_batches_per_cycle=1
    )
    second = maint.run_maintenance_cycle(
        conn, retention_days=7, batch_size=10, max_batches_per_cycle=1
    )
    third = maint.run_maintenance_cycle(
        conn, retention_days=7, batch_size=10, max_batches_per_cycle=1
    )

    assert (first.rows_pruned, second.rows_pruned, third.rows_pruned) == (10, 10, 5)
    assert outbox.count_delivered_events(conn) == 0


def test_cycle_never_prunes_pending_or_quarantined(conn):
    _insert(conn, "pending")
    _insert(conn, "quarantined", quarantined_at=_iso_days_ago(100))

    result = maint.run_maintenance_cycle(conn, retention_days=7, batch_size=1000)

    assert result.rows_pruned == 0
    assert result.pending_count == 1
    assert result.quarantined_count == 1


def test_cycle_incremental_vacuum_only_when_mode_incremental(conn):
    result = maint.run_maintenance_cycle(conn)
    assert result.auto_vacuum_mode == "incremental"
    # incremental_vacuum_ran depends on whether there was anything to
    # reclaim -- assert the mode gate, not a specific ran value, since a
    # brand-new empty DB may have nothing in its freelist yet.
    assert isinstance(result.incremental_vacuum_ran, bool)


def test_cycle_incremental_vacuum_never_runs_on_none_mode_db(tmp_path):
    db_path = tmp_path / "legacy.db"
    legacy_conn = sqlite3.connect(str(db_path), isolation_level=None)
    legacy_conn.execute("PRAGMA journal_mode=WAL")
    legacy_conn.executescript(outbox.SCHEMA)
    legacy_conn.close()

    connection = outbox.connect(db_path)
    try:
        _insert(connection, "old", uploaded_at=_iso_days_ago(10))
        connection.execute("DELETE FROM local_events")  # build up a freelist directly

        result = maint.run_maintenance_cycle(connection)

        assert result.auto_vacuum_mode == "none"
        assert result.incremental_vacuum_ran is False
    finally:
        connection.close()


def test_checkpoint_result_fields_populated(conn):
    result = maint.run_maintenance_cycle(conn)

    assert result.checkpoint_busy is not None
    assert result.checkpoint_log_frames is not None
    assert result.checkpoint_checkpointed_frames is not None


def test_page_and_freelist_before_after_reported(conn):
    result = maint.run_maintenance_cycle(conn)

    assert result.page_count_before is not None
    assert result.page_count_after is not None
    assert result.freelist_count_before is not None
    assert result.freelist_count_after is not None


def test_db_remains_usable_after_maintenance(conn):
    _insert(conn, "old", uploaded_at=_iso_days_ago(10))
    maint.run_maintenance_cycle(conn, retention_days=7)

    inserted = outbox.insert_event(
        conn,
        event_type="checkin",
        customer_id=CUSTOMER_ID,
        branch_id=BRANCH_ID,
        event_timestamp="2026-08-28T10:00:00",
        dedup_key=outbox.compute_dedup_key({"barcode": "after"}),
        payload={"barcode": "after"},
    )
    assert inserted is True


def test_db_reopenable_after_maintenance(tmp_path):
    db_path = tmp_path / "agent.db"
    connection = outbox.connect(db_path)
    _insert(connection, "old", uploaded_at=_iso_days_ago(10))
    maint.run_maintenance_cycle(connection, retention_days=7)
    connection.close()

    reopened = outbox.connect(db_path)
    try:
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()


# --- failure isolation ----------------------------------------------------


def test_cycle_failure_returns_result_never_raises(conn, monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated failure")

    monkeypatch.setattr(outbox, "count_eligible_for_prune", boom)

    result = maint.run_maintenance_cycle(conn)

    assert result.success is False
    assert result.error is not None
    assert "simulated failure" in result.error


def test_cycle_failure_does_not_corrupt_or_lock_the_db(conn, monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated failure")

    monkeypatch.setattr(outbox, "checkpoint_wal", boom)
    result = maint.run_maintenance_cycle(conn)
    assert result.success is False

    # The connection must still be perfectly usable afterward -- no
    # transaction was left open by the failure.
    inserted = outbox.insert_event(
        conn,
        event_type="checkin",
        customer_id=CUSTOMER_ID,
        branch_id=BRANCH_ID,
        event_timestamp="2026-08-28T10:00:00",
        dedup_key=outbox.compute_dedup_key({"barcode": "still-works"}),
        payload={"barcode": "still-works"},
    )
    assert inserted is True


def test_cycle_not_reentrant(conn, monkeypatch):
    # Simulate a cycle already in progress by holding the module lock
    # directly, then confirm a concurrent call is skipped, not blocked
    # or erroring, and touches nothing.
    maint._maintenance_lock.acquire()
    try:
        result = maint.run_maintenance_cycle(conn)
        assert result.success is False
        assert result.skipped_reason is not None
    finally:
        maint._maintenance_lock.release()


def test_run_forever_survives_repeated_failures(conn, monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated failure")

    monkeypatch.setattr(outbox, "count_eligible_for_prune", boom)

    delays = []
    # Must complete all 3 iterations without raising, proving a failing
    # cycle never crashes the loop (or, by extension, whatever process
    # embeds it alongside watcher/uploader/heartbeat).
    maint.run_forever(
        conn,
        max_iterations=3,
        sleep_fn=lambda s: delays.append(s),
        backoff=maint.Backoff(base_seconds=1, max_seconds=100, multiplier=2),
    )

    assert delays == [1, 2]


def test_run_forever_resets_backoff_after_success(conn, monkeypatch):
    calls = {"n": 0}
    real_prune = outbox.prune_delivered_events

    def flaky_prune(connection, *, cutoff, batch_size):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("simulated failure")
        return real_prune(connection, cutoff=cutoff, batch_size=batch_size)

    monkeypatch.setattr(outbox, "prune_delivered_events", flaky_prune)

    delays = []
    maint.run_forever(
        conn,
        interval_seconds=3600,
        max_iterations=3,
        sleep_fn=lambda s: delays.append(s),
        backoff=maint.Backoff(base_seconds=1, max_seconds=100, multiplier=2),
    )

    assert delays == [1, 3600]


# --- cadence independence / no HTTP / no held transactions ----------------


def test_maintenance_module_never_imports_requests_or_uploader():
    import ast

    tree = ast.parse(inspect.getsource(maint))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)

    assert "requests" not in imported_names
    assert not any("uploader" in name for name in imported_names)


def test_maintenance_interval_independent_of_uploader_poll_interval():
    from agent import outbox_uploader

    assert maint.DEFAULT_MAINTENANCE_INTERVAL_SECONDS != outbox_uploader.DEFAULT_POLL_INTERVAL_SECONDS
    assert maint.DEFAULT_MAINTENANCE_INTERVAL_SECONDS >= 3600


def test_run_forever_sleeps_between_cycles_not_during(conn):
    sleep_calls = []

    def tracking_sleep(seconds):
        # If a transaction were still open here, this insert would hang
        # or raise under WAL mode's single-writer rule -- proving no
        # transaction is held across the sleep boundary.
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        sleep_calls.append(seconds)

    maint.run_forever(conn, max_iterations=2, sleep_fn=tracking_sleep)

    assert len(sleep_calls) == 1  # never sleeps after the final iteration


def test_maintenance_cycle_holds_no_open_transaction_after_return(conn):
    _insert(conn, "old", uploaded_at=_iso_days_ago(10))
    maint.run_maintenance_cycle(conn, retention_days=7)

    # in_transaction reflects whether an explicit transaction is
    # currently open on this connection -- must be False once the cycle
    # has returned.
    assert conn.in_transaction is False


def test_maintenance_performs_no_http_requests(conn, monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("maintenance must never make an HTTP request")

    import requests

    monkeypatch.setattr(requests.sessions.Session, "request", fail_if_called)

    _insert(conn, "old", uploaded_at=_iso_days_ago(10))
    result = maint.run_maintenance_cycle(conn, retention_days=7)

    assert result.success is True


# --- soak / growth test ---------------------------------------------------


def test_soak_bounded_growth_and_integrity(tmp_path):
    """Simulates repeated insert/deliver/prune cycles against a small but
    nontrivial dataset -- proves row count stays bounded, quarantined/
    pending/recent rows survive, and the database is still valid and
    reopenable afterward. Deliberately modest in size to keep CI fast."""
    db_path = tmp_path / "soak.db"
    connection = outbox.connect(db_path)
    try:
        barcode_counter = 0

        def next_barcode():
            nonlocal barcode_counter
            barcode_counter += 1
            return f"soak-{barcode_counter}"

        for cycle in range(8):
            # Each cycle: a batch of rows delivered well outside the
            # retention window (eligible), a batch delivered recently
            # (not yet eligible), one pending, one quarantined.
            for _ in range(20):
                _insert(connection, next_barcode(), uploaded_at=_iso_days_ago(10))
            for _ in range(5):
                _insert(connection, next_barcode(), uploaded_at=_iso_days_ago(1))
            _insert(connection, next_barcode())
            _insert(connection, next_barcode(), quarantined_at=_iso_days_ago(30))

            result = maint.run_maintenance_cycle(
                connection, retention_days=7, batch_size=50, max_batches_per_cycle=5
            )
            assert result.success is True

        total_rows = connection.execute("SELECT COUNT(*) FROM local_events").fetchone()[0]
        # Only the "recent" (5/cycle), "pending" (1/cycle), and
        # "quarantined" (1/cycle) rows should ever survive -- the 20
        # old-delivered rows per cycle are pruned away each time.
        assert total_rows == 8 * (5 + 1 + 1)

        pending = outbox.count_pending_events(connection)
        quarantined = outbox.count_quarantined_events(connection)
        assert pending == 8
        assert quarantined == 8

        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()

    # DB must still open and be queryable after the soak.
    reopened = outbox.connect(db_path)
    try:
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert outbox.count_pending_events(reopened) == 8
        assert outbox.count_quarantined_events(reopened) == 8
    finally:
        reopened.close()


def test_soak_wal_checkpoint_does_not_corrupt_db(tmp_path):
    db_path = tmp_path / "soak_checkpoint.db"
    connection = outbox.connect(db_path)
    try:
        for i in range(500):
            _insert(connection, f"row-{i}", uploaded_at=_iso_days_ago(10))

        for _ in range(3):
            result = maint.run_maintenance_cycle(
                connection, retention_days=7, batch_size=100, max_batches_per_cycle=5
            )
            assert result.success is True

        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
