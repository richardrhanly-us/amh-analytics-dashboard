"""Tests for agent/outbox_uploader.py -- draining the local SQLite outbox
to the existing FastAPI /upload endpoint, including deterministic bad-row
isolation and quarantine.

Every HTTP call is mocked (session.post is monkeypatched) -- nothing here
ever touches the network. Backoff/sleep is always injected, never real,
so these run fast regardless of the production backoff ceiling.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
import requests

from agent import outbox
from agent import outbox_uploader as ou
from agent.uploader import API_TOKEN

CUSTOMER_ID = 1
BRANCH_ID = 1


class FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text if text else (json.dumps(json_body) if json_body is not None else "")

    def json(self):
        if self._json_body is None:
            raise ValueError("no JSON body configured on this FakeResponse")
        return self._json_body


def success_response(checkins=0, rejects=0, acs=0):
    return FakeResponse(
        200,
        {
            "status": "success",
            "checkins_inserted": checkins,
            "rejects_inserted": rejects,
            "acs_inserted": acs,
        },
    )


class ScriptedPost:
    """Stand-in for session.post: returns (or raises) whatever's next in
    `responses`, in order, recording every call for assertions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        outcome = self.responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class PoisonAwarePost:
    """Fails a configured status/text for any request containing one of
    `poison_barcodes`, succeeds otherwise. Content-driven rather than
    call-order-driven, so tests don't need to hand-trace the exact
    recursion order the isolation algorithm takes."""

    def __init__(self, poison_barcodes, status_code=400, error_text="deterministic failure"):
        self.poison_barcodes = set(poison_barcodes)
        self.status_code = status_code
        self.error_text = error_text
        self.calls = []
        self.on_call = None  # optional hook, called with the payload before responding

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json)
        if self.on_call is not None:
            self.on_call(json)

        all_rows = json["checkins"] + json["rejects"] + json["acs"]
        barcodes = {r.get("barcode") for r in all_rows}

        if barcodes & self.poison_barcodes:
            return FakeResponse(self.status_code, text=self.error_text)

        return success_response(
            checkins=len(json["checkins"]),
            rejects=len(json["rejects"]),
            acs=len(json["acs"]),
        )


@pytest.fixture
def conn(tmp_path):
    connection = outbox.connect(tmp_path / "agent.db")
    yield connection
    connection.close()


def _insert_checkin(conn, barcode, dedup_key=None):
    outbox.insert_event(
        conn,
        event_type="checkin",
        customer_id=CUSTOMER_ID,
        branch_id=BRANCH_ID,
        event_timestamp="2026-08-28T10:00:00",
        dedup_key=dedup_key or outbox.compute_dedup_key({"barcode": barcode}),
        payload={
            "customer_id": CUSTOMER_ID,
            "branch_id": BRANCH_ID,
            "event_time": "2026-08-28T10:00:00",
            "barcode": barcode,
            "title": "Some Book",
            "source_file": "Checkins.txt",
        },
    )


def _insert_reject(conn, barcode):
    outbox.insert_event(
        conn,
        event_type="reject",
        customer_id=CUSTOMER_ID,
        branch_id=BRANCH_ID,
        event_timestamp="2026-08-28T10:01:00",
        dedup_key=outbox.compute_dedup_key({"barcode": barcode, "kind": "reject"}),
        payload={
            "customer_id": CUSTOMER_ID,
            "branch_id": BRANCH_ID,
            "event_time": "2026-08-28T10:01:00",
            "barcode": barcode,
            "message": "Item not found",
            "source_file": "Rejects.txt",
        },
    )


def _insert_acs(conn, barcode):
    outbox.insert_event(
        conn,
        event_type="acs",
        customer_id=CUSTOMER_ID,
        branch_id=BRANCH_ID,
        event_timestamp="2026-08-28T10:02:00",
        dedup_key=outbox.compute_dedup_key({"barcode": barcode, "kind": "acs"}),
        payload={
            "customer_id": CUSTOMER_ID,
            "branch_id": BRANCH_ID,
            "event_time": "2026-08-28T10:02:00",
            "barcode": barcode,
            "message_code": "AB",
            "source_file": "ACS Log.txt",
        },
    )


def _pending_ids(conn):
    rows = conn.execute(
        "SELECT id FROM local_events WHERE uploaded_at IS NULL AND quarantined_at IS NULL ORDER BY id"
    ).fetchall()
    return [r["id"] for r in rows]


def _all_rows(conn):
    return conn.execute("SELECT * FROM local_events ORDER BY id").fetchall()


def _barcode(row):
    return json.loads(row["payload_json"])["barcode"]


# --- batch selection ---------------------------------------------------


def test_pending_rows_selected_oldest_first(conn):
    _insert_checkin(conn, "111")
    _insert_checkin(conn, "222")
    _insert_checkin(conn, "333")

    rows = ou._select_pending_batch(conn, limit=10)

    payloads = [json.loads(r["payload_json"])["barcode"] for r in rows]
    assert payloads == ["111", "222", "333"]


def test_delivered_rows_excluded_from_selection(conn, monkeypatch):
    _insert_checkin(conn, "111")
    _insert_checkin(conn, "222")

    scripted = ScriptedPost([success_response(checkins=2)])
    monkeypatch.setattr(ou.session, "post", scripted)
    ou.upload_pending_batch(conn)

    _insert_checkin(conn, "333")

    rows = ou._select_pending_batch(conn, limit=10)
    payloads = [json.loads(r["payload_json"])["barcode"] for r in rows]
    assert payloads == ["333"]


def test_batch_size_limit_enforced(conn):
    for i in range(5):
        _insert_checkin(conn, str(i))

    rows = ou._select_pending_batch(conn, limit=3)
    assert len(rows) == 3


# --- payload grouping ----------------------------------------------------


def test_mixed_event_types_become_correct_upload_payload(conn, monkeypatch):
    _insert_checkin(conn, "111")
    _insert_reject(conn, "222")
    _insert_acs(conn, "333")

    scripted = ScriptedPost([success_response(checkins=1, rejects=1, acs=1)])
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)

    assert len(scripted.calls) == 1
    sent = scripted.calls[0]["json"]
    assert set(sent.keys()) == {"checkins", "rejects", "acs"}
    assert [r["barcode"] for r in sent["checkins"]] == ["111"]
    assert [r["barcode"] for r in sent["rejects"]] == ["222"]
    assert [r["barcode"] for r in sent["acs"]] == ["333"]
    assert sent["checkins"][0]["customer_id"] == CUSTOMER_ID
    assert sent["checkins"][0]["branch_id"] == BRANCH_ID


def test_empty_outbox_sends_no_request(conn, monkeypatch):
    scripted = ScriptedPost([])
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)

    assert result is None
    assert scripted.calls == []


# --- delivery / acknowledgement ------------------------------------------


def test_successful_delivery_marks_correct_rows_uploaded(conn, monkeypatch):
    _insert_checkin(conn, "111")
    _insert_checkin(conn, "222")

    scripted = ScriptedPost([success_response(checkins=2)])
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)

    assert len(result.delivered_ids) == 2
    assert result.made_progress is True
    rows = _all_rows(conn)
    assert all(r["uploaded_at"] is not None for r in rows)


def test_duplicate_idempotent_success_still_marks_delivered(conn, monkeypatch):
    # The backend's ON CONFLICT DO NOTHING means a legitimate resend of
    # already-present rows returns 200/"success" with *_inserted: 0. That
    # must still count as delivered, not as a failure.
    _insert_checkin(conn, "111")

    scripted = ScriptedPost([success_response(checkins=0)])
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)

    assert len(result.delivered_ids) == 1
    assert result.quarantined_ids == []
    assert result.pending_ids == []


# --- failure classification: retryable infra ------------------------------


def test_connection_failure_remains_retryable(conn, monkeypatch):
    _insert_checkin(conn, "111")

    scripted = ScriptedPost([requests.ConnectionError("boom")])
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)

    assert result.last_category == ou.FailureCategory.RETRYABLE_INFRA
    assert len(result.pending_ids) == 1
    assert result.quarantined_ids == []
    assert outbox.count_quarantined_events(conn) == 0


def test_timeout_remains_retryable(conn, monkeypatch):
    _insert_checkin(conn, "111")

    scripted = ScriptedPost([requests.Timeout("too slow")])
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)

    assert result.last_category == ou.FailureCategory.RETRYABLE_INFRA
    assert len(result.pending_ids) == 1
    assert outbox.count_quarantined_events(conn) == 0


def test_429_remains_retryable(conn, monkeypatch):
    _insert_checkin(conn, "111")

    scripted = ScriptedPost([FakeResponse(429, text="rate limited")])
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)

    assert result.last_category == ou.FailureCategory.RETRYABLE_INFRA
    assert len(result.pending_ids) == 1
    assert outbox.count_quarantined_events(conn) == 0


def test_5xx_remains_retryable(conn, monkeypatch):
    _insert_checkin(conn, "111")

    scripted = ScriptedPost([FakeResponse(503, text="service unavailable")])
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)

    assert result.last_category == ou.FailureCategory.RETRYABLE_INFRA
    assert len(result.pending_ids) == 1
    assert outbox.count_quarantined_events(conn) == 0


def test_attempt_count_increments_on_retryable_failure(conn, monkeypatch):
    _insert_checkin(conn, "111")

    scripted = ScriptedPost(
        [requests.ConnectionError("boom"), requests.ConnectionError("boom again")]
    )
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)
    assert _all_rows(conn)[0]["attempt_count"] == 1

    ou.upload_pending_batch(conn)
    assert _all_rows(conn)[0]["attempt_count"] == 2


def test_last_attempt_at_updates_on_failure(conn, monkeypatch):
    _insert_checkin(conn, "111")

    scripted = ScriptedPost([requests.ConnectionError("boom")])
    monkeypatch.setattr(ou.session, "post", scripted)

    assert _all_rows(conn)[0]["last_attempt_at"] is None
    ou.upload_pending_batch(conn)
    assert _all_rows(conn)[0]["last_attempt_at"] is not None


def test_successful_upload_after_prior_failures_is_marked_delivered(conn, monkeypatch):
    _insert_checkin(conn, "111")

    scripted = ScriptedPost([requests.ConnectionError("boom"), success_response(checkins=1)])
    monkeypatch.setattr(ou.session, "post", scripted)

    first = ou.upload_pending_batch(conn)
    assert first.delivered_ids == []

    second = ou.upload_pending_batch(conn)
    assert len(second.delivered_ids) == 1
    assert _pending_ids(conn) == []


# --- failure classification: auth (agent-wide, never row-level) ----------


def test_401_does_not_quarantine_rows(conn, monkeypatch):
    _insert_checkin(conn, "111")
    scripted = ScriptedPost([FakeResponse(401, text="Invalid agent token")])
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)

    assert result.last_category == ou.FailureCategory.AUTH_FAILURE
    assert result.quarantined_ids == []
    assert outbox.count_quarantined_events(conn) == 0


def test_403_does_not_quarantine_rows(conn, monkeypatch):
    _insert_checkin(conn, "111")
    scripted = ScriptedPost([FakeResponse(403, text="Agent token is inactive")])
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)

    assert result.last_category == ou.FailureCategory.AUTH_FAILURE
    assert result.quarantined_ids == []
    assert outbox.count_quarantined_events(conn) == 0


def test_401_403_preserve_the_backlog(conn, monkeypatch):
    for i in range(5):
        _insert_checkin(conn, str(i))
    scripted = ScriptedPost([FakeResponse(401, text="Invalid agent token")])
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)

    # Nothing delivered, nothing quarantined -- the entire backlog is
    # still there, exactly as it was, just recorded as attempted.
    assert outbox.count_pending_events(conn) == 5
    assert outbox.count_quarantined_events(conn) == 0
    rows = _all_rows(conn)
    assert all(r["uploaded_at"] is None and r["quarantined_at"] is None for r in rows)
    assert all(r["attempt_count"] == 1 for r in rows)


def test_auth_failure_error_never_contains_raw_token(conn, monkeypatch):
    _insert_checkin(conn, "111")
    scripted = ScriptedPost([FakeResponse(401, text="Invalid agent token")])
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)

    last_error = _all_rows(conn)[0]["last_error"]
    assert last_error is not None
    assert "401" in last_error
    assert API_TOKEN not in last_error


# --- failure classification: deterministic 400 -> isolation --------------


def test_single_bad_row_does_not_block_valid_siblings(conn, monkeypatch):
    for i in range(6):
        _insert_checkin(conn, f"good-{i}")
    _insert_checkin(conn, "poison")

    scripted = PoisonAwarePost(poison_barcodes={"poison"})
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)

    assert len(result.delivered_ids) == 6
    assert len(result.quarantined_ids) == 1
    assert result.pending_ids == []

    rows = _all_rows(conn)
    delivered_barcodes = {_barcode(r) for r in rows if r["uploaded_at"] is not None}
    quarantined_barcodes = {_barcode(r) for r in rows if r["quarantined_at"] is not None}

    assert delivered_barcodes == {f"good-{i}" for i in range(6)}
    assert quarantined_barcodes == {"poison"}


def test_valid_subbatches_are_marked_delivered_during_isolation(conn, monkeypatch):
    for i in range(10):
        _insert_checkin(conn, f"good-{i}")
    _insert_checkin(conn, "poison")

    scripted = PoisonAwarePost(poison_barcodes={"poison"})
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)

    assert len(result.delivered_ids) == 10
    assert len(result.quarantined_ids) == 1
    # More than one POST proves splitting actually happened, not a lucky
    # single call.
    assert len(scripted.calls) > 1


def test_only_independently_failing_singleton_is_quarantined(conn, monkeypatch):
    _insert_checkin(conn, "good-1")
    _insert_checkin(conn, "poison")

    scripted = PoisonAwarePost(poison_barcodes={"poison"})
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)

    rows = _all_rows(conn)
    poison_row = next(r for r in rows if _barcode(r) == "poison")
    good_row = next(r for r in rows if _barcode(r) == "good-1")

    assert poison_row["quarantined_at"] is not None
    assert poison_row["quarantine_reason"] is not None
    assert good_row["quarantined_at"] is None
    assert good_row["uploaded_at"] is not None


def test_quarantined_rows_excluded_from_pending_selection(conn, monkeypatch):
    _insert_checkin(conn, "poison")

    scripted = PoisonAwarePost(poison_barcodes={"poison"})
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)

    assert ou._select_pending_batch(conn, limit=10) == []
    assert outbox.count_pending_events(conn) == 0


def test_quarantined_rows_remain_stored_locally(conn, monkeypatch):
    _insert_checkin(conn, "poison")

    scripted = PoisonAwarePost(poison_barcodes={"poison"})
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)

    rows = _all_rows(conn)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["barcode"] == "poison"
    assert outbox.count_quarantined_events(conn) == 1


def test_valid_row_behind_quarantined_row_uploads_successfully(conn, monkeypatch):
    _insert_checkin(conn, "poison")  # oldest -- would head-of-line block a naive design

    scripted = PoisonAwarePost(poison_barcodes={"poison"})
    monkeypatch.setattr(ou.session, "post", scripted)
    ou.upload_pending_batch(conn)  # quarantines "poison"

    _insert_checkin(conn, "later-valid")

    result = ou.upload_pending_batch(conn)

    assert len(result.delivered_ids) == 1
    good_row = next(r for r in _all_rows(conn) if _barcode(r) == "later-valid")
    assert good_row["uploaded_at"] is not None


def test_400_error_never_written_to_healthy_sibling_rows(conn, monkeypatch):
    # The specific correction this design exists for: attempt_count must
    # NOT be bumped on rows that were merely part of a batch that got
    # split around a poison row -- only the quarantined row itself gets
    # attempt-related metadata.
    _insert_checkin(conn, "good-1")
    _insert_checkin(conn, "good-2")
    _insert_checkin(conn, "poison")

    scripted = PoisonAwarePost(poison_barcodes={"poison"})
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)

    rows = _all_rows(conn)
    good_rows = [r for r in rows if _barcode(r) in ("good-1", "good-2")]
    assert all(r["attempt_count"] == 0 for r in good_rows)
    assert all(r["last_error"] is None for r in good_rows)

    poison_row = next(r for r in rows if _barcode(r) == "poison")
    assert poison_row["attempt_count"] == 1
    assert poison_row["last_error"] is None  # quarantine path writes quarantine_reason, not last_error
    assert poison_row["quarantine_reason"] is not None


def test_token_never_appears_in_quarantine_reason(conn, monkeypatch):
    _insert_checkin(conn, "poison")
    scripted = ScriptedPost([FakeResponse(400, text="Invalid payload")])
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)

    row = _all_rows(conn)[0]
    assert row["quarantine_reason"] is not None
    assert API_TOKEN not in row["quarantine_reason"]


# --- failure classification: 413 -------------------------------------------


def test_413_splits_multi_row_candidate(conn, monkeypatch):
    for i in range(4):
        _insert_checkin(conn, f"row-{i}")

    def fake_post(url, json=None, headers=None, timeout=None):
        all_rows = json["checkins"] + json["rejects"] + json["acs"]
        if len(all_rows) > 1:
            return FakeResponse(413, text="Request body too large")
        return success_response(checkins=len(json["checkins"]))

    monkeypatch.setattr(ou.session, "post", fake_post)

    result = ou.upload_pending_batch(conn)

    assert len(result.delivered_ids) == 4
    assert result.quarantined_ids == []


def test_single_row_413_can_be_quarantined(conn, monkeypatch):
    _insert_checkin(conn, "huge-row")
    scripted = ScriptedPost([FakeResponse(413, text="Request body too large")])
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)

    assert len(result.quarantined_ids) == 1
    row = _all_rows(conn)[0]
    assert row["quarantined_at"] is not None
    assert "413" in row["quarantine_reason"]


# --- backoff -------------------------------------------------------------


def test_backoff_grows_exponentially_and_is_bounded():
    backoff = ou.Backoff(base_seconds=1, max_seconds=10, multiplier=2)

    assert backoff.next_delay() == 1
    assert backoff.next_delay() == 2
    assert backoff.next_delay() == 4
    assert backoff.next_delay() == 8
    assert backoff.next_delay() == 10  # capped
    assert backoff.next_delay() == 10  # stays capped


def test_backoff_reset_returns_to_base():
    backoff = ou.Backoff(base_seconds=1, max_seconds=10, multiplier=2)

    backoff.next_delay()
    backoff.next_delay()
    backoff.reset()

    assert backoff.next_delay() == 1


def test_run_forever_resets_backoff_after_success(conn, monkeypatch):
    _insert_checkin(conn, "111")
    _insert_checkin(conn, "222")

    scripted = ScriptedPost(
        [
            requests.ConnectionError("boom"),
            requests.ConnectionError("boom"),
            success_response(checkins=1),
            success_response(checkins=1),
        ]
    )
    monkeypatch.setattr(ou.session, "post", scripted)

    sleeps = []
    backoff = ou.Backoff(base_seconds=1, max_seconds=100, multiplier=2)

    ou.run_forever(
        conn,
        batch_size=1,
        poll_interval_seconds=3,
        backoff=backoff,
        sleep_fn=sleeps.append,
        max_iterations=4,
    )

    # 4 iterations sleep 3 times (no sleep after the final iteration).
    # Two failures back off (1s, then 2s); the third iteration succeeds
    # and resets backoff, so the sleep after it -- and after the fourth
    # -- is back at the steady poll_interval_seconds cadence.
    assert sleeps == [1, 2, 3]


def test_run_forever_resets_backoff_when_isolation_makes_progress(conn, monkeypatch):
    # Even if part of a batch stays pending, resolving *some* rows
    # (delivered or quarantined) during isolation must count as progress
    # for backoff purposes -- a stuck poison row shouldn't force the
    # whole loop into ever-growing backoff once it's been isolated.
    _insert_checkin(conn, "good-1")
    _insert_checkin(conn, "poison")

    scripted = PoisonAwarePost(poison_barcodes={"poison"})
    monkeypatch.setattr(ou.session, "post", scripted)

    sleeps = []
    backoff = ou.Backoff(base_seconds=1, max_seconds=100, multiplier=2)

    ou.run_forever(
        conn,
        poll_interval_seconds=5,
        backoff=backoff,
        sleep_fn=sleeps.append,
        max_iterations=1,
    )

    assert sleeps == []  # only one iteration requested, nothing to sleep after
    assert backoff.next_delay() == 1  # still at base -- reset() was called


def test_run_forever_never_sleeps_on_empty_outbox_beyond_poll_interval(conn):
    sleeps = []

    ou.run_forever(
        conn,
        poll_interval_seconds=5,
        sleep_fn=sleeps.append,
        max_iterations=3,
    )

    assert sleeps == [5, 5]  # max_iterations=3 -> 2 sleeps between iterations


# --- SQLite concurrency ----------------------------------------------------


def test_no_write_lock_held_during_http_request(tmp_path, monkeypatch):
    db_path = tmp_path / "agent.db"
    conn = outbox.connect(db_path)
    _insert_checkin(conn, "111")

    probe_result = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        probe = sqlite3.connect(str(db_path), timeout=0.2)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("COMMIT")
            probe_result["locked"] = False
        except sqlite3.OperationalError:
            probe_result["locked"] = True
        finally:
            probe.close()

        return success_response(checkins=1)

    monkeypatch.setattr(ou.session, "post", fake_post)

    ou.upload_pending_batch(conn)

    assert probe_result.get("locked") is False
    conn.close()


def test_no_write_lock_held_during_any_http_request_across_isolation(tmp_path, monkeypatch):
    """Same check as above, but across multiple recursive isolation
    calls -- every POST during a split must see the database unlocked,
    not just the first one."""
    db_path = tmp_path / "agent.db"
    conn = outbox.connect(db_path)
    for i in range(4):
        _insert_checkin(conn, f"good-{i}")
    _insert_checkin(conn, "poison")

    lock_observations = []

    def probe_during_call(payload):
        probe = sqlite3.connect(str(db_path), timeout=0.2)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("COMMIT")
            lock_observations.append(False)
        except sqlite3.OperationalError:
            lock_observations.append(True)
        finally:
            probe.close()

    scripted = PoisonAwarePost(poison_barcodes={"poison"})
    scripted.on_call = probe_during_call
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)

    assert len(lock_observations) > 1  # isolation really did make multiple calls
    assert all(locked is False for locked in lock_observations)
    conn.close()


# --- restart / backlog ----------------------------------------------------


def test_uploader_survives_restart_with_pending_rows_intact(tmp_path, monkeypatch):
    db_path = tmp_path / "agent.db"

    conn_a = outbox.connect(db_path)
    _insert_checkin(conn_a, "111")
    conn_a.close()  # simulate the process exiting before any upload happened

    conn_b = outbox.connect(db_path)
    try:
        assert len(_pending_ids(conn_b)) == 1

        scripted = ScriptedPost([success_response(checkins=1)])
        monkeypatch.setattr(ou.session, "post", scripted)

        result = ou.upload_pending_batch(conn_b)
        assert len(result.delivered_ids) == 1
        assert _pending_ids(conn_b) == []
    finally:
        conn_b.close()


def test_restart_preserves_quarantine_state(tmp_path, monkeypatch):
    db_path = tmp_path / "agent.db"

    conn_a = outbox.connect(db_path)
    _insert_checkin(conn_a, "poison")
    scripted = ScriptedPost([FakeResponse(400, text="bad row")])
    monkeypatch.setattr(ou.session, "post", scripted)
    ou.upload_pending_batch(conn_a)
    conn_a.close()

    conn_b = outbox.connect(db_path)
    try:
        row = _all_rows(conn_b)[0]
        assert row["quarantined_at"] is not None
        assert row["quarantine_reason"] is not None
        assert outbox.count_quarantined_events(conn_b) == 1
        assert outbox.count_pending_events(conn_b) == 0
    finally:
        conn_b.close()


def test_backlog_drains_across_multiple_batches(conn, monkeypatch):
    for i in range(7):
        _insert_checkin(conn, str(i))

    scripted = ScriptedPost(
        [success_response(checkins=3), success_response(checkins=3), success_response(checkins=1)]
    )
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn, batch_size=3)
    assert len(_pending_ids(conn)) == 4

    ou.upload_pending_batch(conn, batch_size=3)
    assert len(_pending_ids(conn)) == 1

    ou.upload_pending_batch(conn, batch_size=3)
    assert len(_pending_ids(conn)) == 0

    assert len(scripted.calls) == 3


# --- byte-size batching ----------------------------------------------------


def test_oversized_batch_shrinks_to_fit_byte_budget(conn):
    for i in range(20):
        _insert_checkin(conn, f"barcode-{i:04d}")

    rows = ou._select_pending_batch(conn, limit=20)
    # A tiny budget forces repeated halving.
    candidate = ou._fit_batch_to_byte_budget(rows, max_bytes=500)
    grouped, row_ids = ou._group_rows_for_upload(candidate)
    size = ou._payload_byte_size(grouped)

    assert size <= 500 or len(row_ids) == 1
    assert len(row_ids) < 20


def test_batch_fitting_never_returns_zero_rows_when_input_nonempty(conn):
    _insert_checkin(conn, "111")
    rows = ou._select_pending_batch(conn, limit=10)

    # Even an absurdly small budget must still return the one row rather
    # than nothing -- reactive 413 handling (isolation) is the backstop
    # for a single pathologically large row, not silent data loss here.
    candidate = ou._fit_batch_to_byte_budget(rows, max_bytes=1)

    assert len(candidate) == 1


# --- secrets hygiene ---------------------------------------------------


def test_api_token_never_written_into_sqlite(tmp_path, monkeypatch):
    db_path = tmp_path / "agent.db"
    conn = outbox.connect(db_path)
    _insert_checkin(conn, "111")

    scripted = ScriptedPost([FakeResponse(401, text="Invalid agent token")])
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)
    conn.close()

    raw_bytes = db_path.read_bytes()
    assert API_TOKEN.encode("utf-8") not in raw_bytes


def test_raw_bearer_token_never_used_in_logs(conn, monkeypatch, caplog):
    _insert_checkin(conn, "111")

    scripted = ScriptedPost([FakeResponse(401, text="Invalid agent token")])
    monkeypatch.setattr(ou.session, "post", scripted)

    with caplog.at_level("DEBUG"):
        ou.upload_pending_batch(conn)

    for record in caplog.records:
        assert API_TOKEN not in record.getMessage()


def test_auth_headers_sent_but_not_logged(conn, monkeypatch, caplog):
    _insert_checkin(conn, "111")

    scripted = ScriptedPost([success_response(checkins=1)])
    monkeypatch.setattr(ou.session, "post", scripted)

    with caplog.at_level("DEBUG"):
        ou.upload_pending_batch(conn)

    # The token *is* sent as part of the real request headers ...
    assert scripted.calls[0]["headers"]["Authorization"] == f"Bearer {API_TOKEN}"
    # ... but never appears in anything logged.
    for record in caplog.records:
        assert API_TOKEN not in record.getMessage()


# --- isolation request budget ----------------------------------------------
#
# Fixed scenario used by several tests below: 2 healthy rows inserted
# before 2 poison rows. With max_isolation_requests=2, the recursion is
# fully deterministic (traced by hand, not guessed):
#   call 1 (budget 0->1): whole batch of 4 -> poison present -> 400 ->
#       split into left=[good-1, good-2], right=[poison-1, poison-2]
#   call 2 (budget 1->2): left -> no poison -> 200 -> delivered
#   right: budget already at 2/2 -> exhausted -> no request, left pending
# Result: delivered=[good-1, good-2], quarantined=[], pending=[poison-1,
# poison-2], isolation_budget_exhausted=True, exactly 2 calls total.


def _two_good_two_poison(conn):
    _insert_checkin(conn, "good-1")
    _insert_checkin(conn, "good-2")
    _insert_checkin(conn, "poison-1")
    _insert_checkin(conn, "poison-2")
    return PoisonAwarePost(poison_barcodes={"poison-1", "poison-2"})


def test_one_poison_row_isolated_under_default_budget(conn, monkeypatch):
    for i in range(5):
        _insert_checkin(conn, f"good-{i}")
    _insert_checkin(conn, "poison")

    scripted = PoisonAwarePost(poison_barcodes={"poison"})
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn)  # default budget (20)

    assert result.isolation_budget_exhausted is False
    assert len(result.delivered_ids) == 5
    assert len(result.quarantined_ids) == 1


def test_budget_shared_across_entire_recursive_operation(conn, monkeypatch):
    # If the budget were reset per branch instead of shared, a split into
    # two branches could use up to 2x the configured value. It must not
    # exceed the configured total.
    scripted = _two_good_two_poison(conn)
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn, max_isolation_requests=2)

    assert len(scripted.calls) == 2


def test_all_poison_batch_stops_once_budget_exhausted(conn, monkeypatch):
    barcodes = [f"poison-{i}" for i in range(8)]
    for b in barcodes:
        _insert_checkin(conn, b)
    scripted = PoisonAwarePost(poison_barcodes=set(barcodes))
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn, max_isolation_requests=3)

    assert result.isolation_budget_exhausted is True
    assert len(scripted.calls) == 3
    resolved = len(result.delivered_ids) + len(result.quarantined_ids)
    assert resolved < 8  # far short of the up-to-15 requests full resolution would need
    assert len(result.pending_ids) == 8 - resolved


def test_unresolved_rows_remain_pending_after_budget_exhaustion(conn, monkeypatch):
    scripted = _two_good_two_poison(conn)
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn, max_isolation_requests=2)

    assert len(result.pending_ids) == 2
    for row in _all_rows(conn):
        if row["id"] in result.pending_ids:
            assert row["uploaded_at"] is None
            assert row["quarantined_at"] is None


def test_unresolved_rows_not_quarantined_merely_because_budget_ran_out(conn, monkeypatch):
    scripted = _two_good_two_poison(conn)
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn, max_isolation_requests=2)

    assert result.quarantined_ids == []
    assert outbox.count_quarantined_events(conn) == 0


def test_rows_delivered_before_exhaustion_remain_delivered(conn, monkeypatch):
    scripted = _two_good_two_poison(conn)
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn, max_isolation_requests=2)

    assert len(result.delivered_ids) == 2
    delivered_barcodes = {_barcode(r) for r in _all_rows(conn) if r["uploaded_at"] is not None}
    assert delivered_barcodes == {"good-1", "good-2"}


def test_rows_quarantined_before_exhaustion_remain_quarantined(conn, monkeypatch):
    # 4 all-poison rows, budget=3: whole(1) -> split -> left half(2) ->
    # split -> [poison-0] singleton(3, quarantined) -- budget exhausted
    # exactly there, before poison-1 or the right half [poison-2,
    # poison-3] are ever attempted. Traced by hand, same method as the
    # module-level scenario above.
    barcodes = [f"poison-{i}" for i in range(4)]
    for b in barcodes:
        _insert_checkin(conn, b)
    scripted = PoisonAwarePost(poison_barcodes=set(barcodes))
    monkeypatch.setattr(ou.session, "post", scripted)

    first = ou.upload_pending_batch(conn, max_isolation_requests=3)

    assert len(first.quarantined_ids) == 1
    assert first.isolation_budget_exhausted is True

    # A later cycle with a full budget must not revert the earlier
    # quarantine -- it stays quarantined regardless of what happens to
    # its siblings.
    ou.upload_pending_batch(conn, max_isolation_requests=20)

    row = next(r for r in _all_rows(conn) if r["id"] == first.quarantined_ids[0])
    assert row["quarantined_at"] is not None


def test_drain_result_exposes_budget_exhaustion(conn, monkeypatch, tmp_path):
    scripted = _two_good_two_poison(conn)
    monkeypatch.setattr(ou.session, "post", scripted)

    exhausted = ou.upload_pending_batch(conn, max_isolation_requests=2)
    assert exhausted.isolation_budget_exhausted is True

    # Fresh outbox, generous budget -- must clearly read False, not just
    # be falsy/absent.
    other_conn = outbox.connect(tmp_path / "other.db")
    _insert_checkin(other_conn, "solo")
    monkeypatch.setattr(ou.session, "post", ScriptedPost([success_response(checkins=1)]))
    not_exhausted = ou.upload_pending_batch(other_conn, max_isolation_requests=20)
    assert not_exhausted.isolation_budget_exhausted is False
    other_conn.close()


def test_subsequent_drain_cycles_continue_on_remaining_backlog(conn, monkeypatch):
    scripted = _two_good_two_poison(conn)
    monkeypatch.setattr(ou.session, "post", scripted)

    first = ou.upload_pending_batch(conn, max_isolation_requests=2)
    assert first.isolation_budget_exhausted is True
    assert len(first.pending_ids) == 2

    second = ou.upload_pending_batch(conn, max_isolation_requests=20)
    assert second.isolation_budget_exhausted is False
    assert len(second.quarantined_ids) == 2

    assert outbox.count_pending_events(conn) == 0
    assert outbox.count_quarantined_events(conn) == 2


def test_budget_exhaustion_causes_backoff_not_tight_loop(conn, monkeypatch):
    scripted = _two_good_two_poison(conn)
    monkeypatch.setattr(ou.session, "post", scripted)

    sleeps = []
    backoff = ou.Backoff(base_seconds=1, max_seconds=100, multiplier=2)

    ou.run_forever(
        conn,
        max_isolation_requests=2,
        poll_interval_seconds=5,
        backoff=backoff,
        sleep_fn=sleeps.append,
        max_iterations=2,
    )

    # Budget exhaustion on iteration 1 (2 delivered, 2 still pending)
    # must cause a backoff-governed sleep (base_seconds=1), never the
    # steady poll_interval_seconds=5 cadence -- even though real progress
    # (2 deliveries) also happened that same cycle.
    assert sleeps == [1]


def test_token_never_appears_in_budget_or_error_state(conn, monkeypatch):
    barcodes = [f"poison-{i}" for i in range(4)]
    for b in barcodes:
        _insert_checkin(conn, b)
    scripted = PoisonAwarePost(poison_barcodes=set(barcodes), error_text="bad request")
    monkeypatch.setattr(ou.session, "post", scripted)

    result = ou.upload_pending_batch(conn, max_isolation_requests=3)

    assert API_TOKEN not in repr(result)
    for row in _all_rows(conn):
        if row["quarantine_reason"]:
            assert API_TOKEN not in row["quarantine_reason"]
        if row["last_error"]:
            assert API_TOKEN not in row["last_error"]


# --- last_error_category persistence (Phase 3: heartbeat needs the ----------
# --- structured category, not just the error text) -------------------------


def test_retryable_infra_failure_persists_category(conn, monkeypatch):
    _insert_checkin(conn, "111")
    scripted = ScriptedPost([requests.ConnectionError("boom")])
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)

    assert _all_rows(conn)[0]["last_error_category"] == "retryable_infra"


def test_auth_failure_persists_category(conn, monkeypatch):
    _insert_checkin(conn, "111")
    scripted = ScriptedPost([FakeResponse(401, text="unauthorized")])
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)

    assert _all_rows(conn)[0]["last_error_category"] == "auth_failure"


def test_quarantined_row_persists_category(conn, monkeypatch):
    _insert_checkin(conn, "111")
    scripted = PoisonAwarePost(poison_barcodes={"111"}, status_code=400)
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)

    row = _all_rows(conn)[0]
    assert row["quarantined_at"] is not None
    assert row["last_error_category"] == "request_failure"


def test_category_cleared_on_row_reaching_delivery_via_a_later_row(conn, monkeypatch):
    """last_error_category is never explicitly cleared on delivery -- it's
    simply excluded from future "unresolved failure" queries once
    uploaded_at is set (see outbox.get_latest_unresolved_failure and
    tests/test_heartbeat.py). This just confirms delivery doesn't crash or
    corrupt a row that previously recorded a category."""
    _insert_checkin(conn, "111")
    scripted = ScriptedPost([requests.ConnectionError("boom"), success_response(checkins=1)])
    monkeypatch.setattr(ou.session, "post", scripted)

    ou.upload_pending_batch(conn)
    ou.upload_pending_batch(conn)

    row = _all_rows(conn)[0]
    assert row["uploaded_at"] is not None
    assert row["last_error_category"] == "retryable_infra"  # historical, row is no longer pending
