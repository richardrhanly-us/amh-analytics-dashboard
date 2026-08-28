"""Tests for agent/heartbeat.py -- the continuous-agent health snapshot
and its delivery to /upload-pipeline-status (Phase 3: Signal).

compute_health_snapshot is pure/read-only against the local outbox, tested
directly against a real (tmp_path) SQLite connection, same pattern as
test_outbox.py and test_outbox_uploader.py. send_heartbeat/run_forever
mock session.post -- nothing here ever touches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import requests

from agent import heartbeat as hb
from agent import outbox
from agent import outbox_uploader as ou
from agent.uploader import API_TOKEN

CUSTOMER_ID = 1
BRANCH_ID = 1


@pytest.fixture
def conn(tmp_path):
    connection = outbox.connect(tmp_path / "agent.db")
    yield connection
    connection.close()


def _insert(conn, barcode, created_at=None):
    outbox.insert_event(
        conn,
        event_type="checkin",
        customer_id=CUSTOMER_ID,
        branch_id=BRANCH_ID,
        event_timestamp="2026-08-28T10:00:00",
        dedup_key=outbox.compute_dedup_key({"barcode": barcode}),
        payload={"barcode": barcode},
    )
    if created_at is not None:
        conn.execute(
            "UPDATE local_events SET created_at = ? WHERE id = (SELECT MAX(id) FROM local_events)",
            (created_at,),
        )


class FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text if text else (json.dumps(json_body) if json_body is not None else "")

    def json(self):
        if self._json_body is None:
            raise ValueError("no JSON body configured on this FakeResponse")
        return self._json_body


def success_response():
    return FakeResponse(200, {"status": "success", "message": "Pipeline status uploaded"})


class ScriptedPost:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        outcome = self.responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


# --- compute_health_snapshot: healthy -----------------------------------


def test_healthy_when_outbox_empty(conn):
    snapshot = hb.compute_health_snapshot(conn)

    assert snapshot.health_status == "healthy"
    assert snapshot.pending_outbox_count == 0
    assert snapshot.quarantined_count == 0
    assert snapshot.oldest_pending_event_at is None
    assert snapshot.last_failure_category is None
    assert snapshot.last_error is None


def test_healthy_with_recent_successful_delivery(conn):
    _insert(conn, "111")
    (row_id,) = conn.execute("SELECT id FROM local_events").fetchone()
    conn.execute(
        "UPDATE local_events SET uploaded_at = ? WHERE id = ?",
        ("2026-08-28T10:05:00.000000Z", row_id),
    )

    snapshot = hb.compute_health_snapshot(conn)

    assert snapshot.health_status == "healthy"
    assert snapshot.last_success_at == "2026-08-28T10:05:00.000000Z"


def test_idle_branch_with_no_new_events_is_healthy_not_unhealthy(conn):
    # No inserts at all -- an idle branch with zero AMH activity for an
    # extended period must never be classified as anything but healthy
    # purely because nothing new has arrived.
    snapshot = hb.compute_health_snapshot(conn)
    assert snapshot.health_status == "healthy"


def test_health_status_healthy_with_recent_watcher_activity(conn):
    # Watcher activity (file_state) and health are different concepts --
    # health_status must not depend on watcher_last_active_at either way.
    outbox.save_file_state(
        conn, file_path="Checkins.txt", file_dev=1, file_ino=1,
        last_byte_offset=0, last_modified=None,
    )
    snapshot = hb.compute_health_snapshot(conn)
    assert snapshot.health_status == "healthy"
    assert snapshot.watcher_last_active_at is not None


def test_health_status_healthy_with_no_watcher_activity_at_all(conn):
    # No file_state rows at all (e.g. watcher hasn't polled yet, or this
    # is a fresh outbox) -- still healthy, since health_status never
    # factors in watcher_last_active_at.
    snapshot = hb.compute_health_snapshot(conn)
    assert snapshot.health_status == "healthy"
    assert snapshot.watcher_last_active_at is None


# --- compute_health_snapshot: pending backlog presence vs. age -------------


def test_small_pending_backlog_is_not_degraded(conn):
    now = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
    _insert(conn, "111", created_at="2026-08-28T11:59:50.000000Z")  # 10s old

    snapshot = hb.compute_health_snapshot(conn, now=now)

    assert snapshot.pending_outbox_count == 1
    assert snapshot.health_status == "healthy"


def test_pending_backlog_older_than_threshold_is_degraded(conn):
    now = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
    _insert(conn, "111", created_at="2026-08-28T11:00:00.000000Z")  # 60 min old

    snapshot = hb.compute_health_snapshot(
        conn, now=now, degraded_backlog_age_minutes=15
    )

    assert snapshot.health_status == "degraded"


def test_pending_backlog_age_threshold_is_configurable(conn):
    now = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
    _insert(conn, "111", created_at="2026-08-28T11:50:00.000000Z")  # 10 min old

    # Default threshold (15 min) -- not yet degraded.
    default_snapshot = hb.compute_health_snapshot(conn, now=now)
    assert default_snapshot.health_status == "healthy"

    # A tighter threshold flips it to degraded.
    tight_snapshot = hb.compute_health_snapshot(
        conn, now=now, degraded_backlog_age_minutes=5
    )
    assert tight_snapshot.health_status == "degraded"


def test_oldest_pending_timestamp_reported_correctly(conn):
    _insert(conn, "111", created_at="2026-08-28T11:00:00.000000Z")
    _insert(conn, "222", created_at="2026-08-28T11:30:00.000000Z")

    snapshot = hb.compute_health_snapshot(conn)

    assert snapshot.oldest_pending_event_at == "2026-08-28T11:00:00.000000Z"


def test_oldest_pending_clears_once_backlog_drains(conn):
    _insert(conn, "111", created_at="2026-08-28T11:00:00.000000Z")
    before = hb.compute_health_snapshot(conn)
    assert before.oldest_pending_event_at is not None

    (row_id,) = conn.execute("SELECT id FROM local_events").fetchone()
    conn.execute(
        "UPDATE local_events SET uploaded_at = ? WHERE id = ?",
        ("2026-08-28T11:05:00.000000Z", row_id),
    )

    after = hb.compute_health_snapshot(conn)
    assert after.oldest_pending_event_at is None


# --- compute_health_snapshot: quarantine ------------------------------------


def test_degraded_when_quarantined_rows_exist(conn):
    _insert(conn, "111")
    (row_id,) = conn.execute("SELECT id FROM local_events").fetchone()
    conn.execute(
        "UPDATE local_events SET quarantined_at = ?, quarantine_reason = ? WHERE id = ?",
        ("2026-08-28T11:00:00.000000Z", "bad row", row_id),
    )

    snapshot = hb.compute_health_snapshot(conn)

    assert snapshot.health_status == "degraded"
    assert snapshot.quarantined_count == 1


def test_quarantined_count_correct_with_mixed_rows(conn):
    _insert(conn, "111")
    _insert(conn, "222")
    ids = [r["id"] for r in conn.execute("SELECT id FROM local_events ORDER BY id").fetchall()]
    conn.execute(
        "UPDATE local_events SET quarantined_at = ?, quarantine_reason = ? WHERE id = ?",
        ("2026-08-28T11:00:00.000000Z", "bad row", ids[0]),
    )

    snapshot = hb.compute_health_snapshot(conn)

    assert snapshot.quarantined_count == 1
    assert snapshot.pending_outbox_count == 1


# --- compute_health_snapshot: unresolved failures ---------------------------


def test_degraded_on_unresolved_retryable_failure(conn):
    _insert(conn, "111")
    (row_id,) = conn.execute("SELECT id FROM local_events").fetchone()
    conn.execute(
        "UPDATE local_events SET last_attempt_at = ?, last_error = ?, last_error_category = ? WHERE id = ?",
        ("2026-08-28T11:00:00.000000Z", "connection refused", "retryable_infra", row_id),
    )

    snapshot = hb.compute_health_snapshot(conn)

    assert snapshot.health_status == "degraded"
    assert snapshot.last_failure_category == "retryable_infra"
    assert snapshot.last_error == "connection refused"


def test_auth_failure_takes_precedence_over_degraded(conn):
    # Two rows: one quarantined (would independently mean degraded), one
    # pending with an unresolved auth_failure. auth_failure must win.
    _insert(conn, "111")
    _insert(conn, "222")
    ids = [r["id"] for r in conn.execute("SELECT id FROM local_events ORDER BY id").fetchall()]
    conn.execute(
        "UPDATE local_events SET quarantined_at = ?, quarantine_reason = ? WHERE id = ?",
        ("2026-08-28T11:00:00.000000Z", "bad row", ids[0]),
    )
    conn.execute(
        "UPDATE local_events SET last_attempt_at = ?, last_error = ?, last_error_category = ? WHERE id = ?",
        ("2026-08-28T11:05:00.000000Z", "bad token", "auth_failure", ids[1]),
    )

    snapshot = hb.compute_health_snapshot(conn)

    assert snapshot.health_status == "auth_failure"
    assert snapshot.last_failure_category == "auth_failure"


def test_recovered_uploader_does_not_stay_auth_failure_forever(conn):
    _insert(conn, "111")
    (row_id,) = conn.execute("SELECT id FROM local_events").fetchone()
    conn.execute(
        "UPDATE local_events SET last_attempt_at = ?, last_error = ?, last_error_category = ? WHERE id = ?",
        ("2026-08-28T11:00:00.000000Z", "bad token", "auth_failure", row_id),
    )
    stuck = hb.compute_health_snapshot(conn)
    assert stuck.health_status == "auth_failure"

    # The token gets fixed and the row is retried successfully -- the
    # uploader only ever sets uploaded_at on delivery, never clears
    # last_error/last_error_category, so this is exactly what a real
    # recovery looks like at the row level.
    conn.execute(
        "UPDATE local_events SET uploaded_at = ? WHERE id = ?",
        ("2026-08-28T11:10:00.000000Z", row_id),
    )

    recovered = hb.compute_health_snapshot(conn)
    assert recovered.health_status == "healthy"
    assert recovered.last_failure_category is None
    assert recovered.last_error is None


def test_last_success_at_survives_restart(tmp_path):
    db_path = tmp_path / "agent.db"
    first = outbox.connect(db_path)
    try:
        _insert(first, "111")
        (row_id,) = first.execute("SELECT id FROM local_events").fetchone()
        first.execute(
            "UPDATE local_events SET uploaded_at = ? WHERE id = ?",
            ("2026-08-28T11:00:00.000000Z", row_id),
        )
    finally:
        first.close()

    second = outbox.connect(db_path)
    try:
        snapshot = hb.compute_health_snapshot(second)
        assert snapshot.last_success_at == "2026-08-28T11:00:00.000000Z"
    finally:
        second.close()


def test_last_error_never_contains_raw_token(conn):
    _insert(conn, "111")
    (row_id,) = conn.execute("SELECT id FROM local_events").fetchone()
    conn.execute(
        "UPDATE local_events SET last_attempt_at = ?, last_error = ?, last_error_category = ? WHERE id = ?",
        ("2026-08-28T11:00:00.000000Z", "connection refused: no token here", "retryable_infra", row_id),
    )

    snapshot = hb.compute_health_snapshot(conn)
    assert API_TOKEN not in (snapshot.last_error or "")


# --- send_heartbeat / payload construction ----------------------------------


def test_payload_always_includes_all_heartbeat_fields_explicitly():
    snapshot = hb.HealthSnapshot(
        health_status="healthy",
        pending_outbox_count=0,
        quarantined_count=0,
        oldest_pending_event_at=None,
        last_success_at=None,
        last_failure_category=None,
        last_error=None,
        watcher_last_active_at=None,
    )
    payload = hb._build_payload(snapshot)

    for field in [
        "health_status", "pending_outbox_count", "quarantined_count",
        "oldest_pending_event_at", "last_success_at",
        "last_failure_category", "last_error", "watcher_last_active_at",
    ]:
        assert field in payload  # present even though every value is None

    # Never includes any legacy per-run field.
    for legacy_field in ["status", "last_run", "last_attempt", "checkins_rows", "destination_breakdown"]:
        assert legacy_field not in payload


def test_send_heartbeat_uses_existing_auth_mechanism(monkeypatch):
    scripted = ScriptedPost([success_response()])
    monkeypatch.setattr(hb.session, "post", scripted)

    snapshot = hb.HealthSnapshot(
        health_status="healthy", pending_outbox_count=0, quarantined_count=0,
        oldest_pending_event_at=None, last_success_at=None,
        last_failure_category=None, last_error=None, watcher_last_active_at=None,
    )
    ok = hb.send_heartbeat(snapshot)

    assert ok is True
    assert scripted.calls[0]["headers"]["Authorization"] == f"Bearer {API_TOKEN}"
    assert scripted.calls[0]["url"].endswith("/upload-pipeline-status")


def test_send_heartbeat_returns_false_on_non_200(monkeypatch):
    scripted = ScriptedPost([FakeResponse(500, text="server error")])
    monkeypatch.setattr(hb.session, "post", scripted)

    snapshot = hb.HealthSnapshot(
        health_status="healthy", pending_outbox_count=0, quarantined_count=0,
        oldest_pending_event_at=None, last_success_at=None,
        last_failure_category=None, last_error=None, watcher_last_active_at=None,
    )
    assert hb.send_heartbeat(snapshot) is False


def test_send_heartbeat_returns_false_on_connection_error(monkeypatch):
    scripted = ScriptedPost([requests.ConnectionError("boom")])
    monkeypatch.setattr(hb.session, "post", scripted)

    snapshot = hb.HealthSnapshot(
        health_status="healthy", pending_outbox_count=0, quarantined_count=0,
        oldest_pending_event_at=None, last_success_at=None,
        last_failure_category=None, last_error=None, watcher_last_active_at=None,
    )
    assert hb.send_heartbeat(snapshot) is False


def test_heartbeat_failure_does_not_touch_outbox_contents(conn, monkeypatch):
    _insert(conn, "111")
    before = conn.execute("SELECT * FROM local_events").fetchall()

    scripted = ScriptedPost([requests.ConnectionError("boom")])
    monkeypatch.setattr(hb.session, "post", scripted)

    hb.run_forever(conn, max_iterations=1, sleep_fn=lambda s: None)

    after = conn.execute("SELECT * FROM local_events").fetchall()
    assert [dict(r) for r in before] == [dict(r) for r in after]


# --- run_forever: backoff ----------------------------------------------------


def test_run_forever_backs_off_on_repeated_failure(monkeypatch, conn):
    scripted = ScriptedPost([
        requests.ConnectionError("boom"),
        requests.ConnectionError("boom"),
        requests.ConnectionError("boom"),
    ])
    monkeypatch.setattr(hb.session, "post", scripted)

    delays = []
    hb.run_forever(
        conn,
        max_iterations=3,
        sleep_fn=lambda s: delays.append(s),
        backoff=ou.Backoff(base_seconds=1, max_seconds=100, multiplier=2),
    )

    assert delays == [1, 2]


def test_run_forever_resets_backoff_after_success(monkeypatch, conn):
    scripted = ScriptedPost([
        requests.ConnectionError("boom"),
        success_response(),
        requests.ConnectionError("boom"),
    ])
    monkeypatch.setattr(hb.session, "post", scripted)

    delays = []
    hb.run_forever(
        conn,
        interval_seconds=60,
        max_iterations=3,
        sleep_fn=lambda s: delays.append(s),
        backoff=ou.Backoff(base_seconds=1, max_seconds=100, multiplier=2),
    )

    assert delays == [1, 60]
