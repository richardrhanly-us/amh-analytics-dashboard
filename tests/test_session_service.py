"""Unit tests for session_service, using db_fakes.FakeEngine/FakeQueryResult
(matching test_user_admin_service.py's approach) rather than a real database.
No FOR UPDATE / race-condition behavior is exercised here because this
module has none -- see session_service.py's own CONCURRENCY note.

_log_auth_event is monkeypatched directly (not session_service.auth_service,
which no longer exists as a module-level attribute -- see session_service's
lazy-import design to avoid a future auth_service <-> session_service cycle).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from db_fakes import FakeEngine, FakeQueryResult

from src.services import session_service


class _UpdateResult:
    """Minimal stand-in for a SQLAlchemy CursorResult's .rowcount, which
    db_fakes.FakeQueryResult does not model. Scoped to this file only,
    matching test_user_admin_service.py's identical local class."""

    def __init__(self, rowcount: int):
        self.rowcount = rowcount


def _normalize(sql: str) -> str:
    return " ".join(sql.lower().split())


NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _stub_audit_log(monkeypatch):
    monkeypatch.setattr(session_service, "_log_auth_event", lambda **_kwargs: None)


# --- SQL predicate lock-in ---------------------------------------------------
#
# FakeEngine cannot behaviorally distinguish an expired/revoked/inactive-user
# session from a valid one -- that enforcement lives entirely in PostgreSQL's
# evaluation of these WHERE clauses. Without these tests, someone could
# accidentally remove one of the predicates below and every other test in
# this file would still pass.

def test_validate_session_sql_enforces_revoked_expired_and_active_predicates():
    sql = _normalize(session_service._VALIDATE_SESSION_SQL)

    assert "s.revoked_at is null" in sql
    assert "s.expires_at > :now" in sql
    assert "u.is_active = true" in sql


def test_touch_last_seen_sql_enforces_revoked_expired_and_throttle_predicates():
    sql = _normalize(session_service._TOUCH_LAST_SEEN_SQL)

    assert "revoked_at is null" in sql
    assert "expires_at > :now" in sql
    assert "last_seen_at is null or last_seen_at < :stale_before" in sql


# --- create_session -----------------------------------------------------------

def test_create_session_stores_hash_not_raw_token(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first={"id": 1})])
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    result = session_service.create_session(user_id=7, now=NOW)

    params = engine.calls[0]["params"]
    assert params["token_hash"] == session_service._hash_session_token(result["token"])
    assert params["token_hash"] != result["token"]
    assert len(params["token_hash"]) == 64  # sha256 hex digest


def test_create_session_computes_expires_at_from_lifetime(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first={"id": 1})])
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    result = session_service.create_session(user_id=7, now=NOW)

    assert result["expires_at"] == NOW + session_service.SESSION_LIFETIME
    assert engine.calls[0]["params"]["expires_at"] == NOW + session_service.SESSION_LIFETIME
    assert engine.calls[0]["params"]["last_seen_at"] == NOW


def test_create_session_logs_session_created_without_the_token(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first={"id": 42})])
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    logged = {}
    monkeypatch.setattr(session_service, "_log_auth_event", lambda **kwargs: logged.update(kwargs))

    result = session_service.create_session(user_id=7, now=NOW)

    assert logged["event_type"] == "session_created"
    assert logged["user_id"] == 7
    assert logged["metadata"]["session_id"] == 42

    token_hash = session_service._hash_session_token(result["token"])
    assert result["token"] not in str(logged)
    assert token_hash not in str(logged)


# --- validate_session ---------------------------------------------------------

@pytest.mark.parametrize("bad_input", [None, "", 12345, "x" * 600])
def test_validate_session_rejects_malformed_input_without_hitting_db(monkeypatch, bad_input):
    engine = FakeEngine([])  # no query may run
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    assert session_service.validate_session(bad_input, now=NOW) is None


def test_validate_session_returns_none_when_no_row_found(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    result = session_service.validate_session("some-raw-token", now=NOW)

    assert result is None
    assert len(engine.calls) == 1  # SELECT only -- no touch-last-seen UPDATE issued


def test_validate_session_returns_minimal_auth_user_shape_and_touches_last_seen(monkeypatch):
    row = {"session_id": 9, "id": 5, "email": "a@example.com", "full_name": "A B"}
    engine = FakeEngine([FakeQueryResult(first=row), _UpdateResult(rowcount=1)])
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    result = session_service.validate_session("some-raw-token", now=NOW)

    assert result == {"id": 5, "email": "a@example.com", "full_name": "A B"}
    assert len(engine.calls) == 2
    assert engine.calls[0]["params"]["token_hash"] == session_service._hash_session_token("some-raw-token")
    assert engine.calls[0]["params"]["now"] == NOW


def test_touch_last_seen_binds_the_throttle_window(monkeypatch):
    row = {"session_id": 9, "id": 5, "email": "a@example.com", "full_name": "A B"}
    engine = FakeEngine([FakeQueryResult(first=row), _UpdateResult(rowcount=1)])
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    session_service.validate_session("some-raw-token", now=NOW)

    touch_params = engine.calls[1]["params"]
    assert touch_params["session_id"] == 9
    assert touch_params["now"] == NOW
    assert touch_params["stale_before"] == NOW - session_service.LAST_SEEN_THROTTLE


# --- revoke_session -------------------------------------------------------------

def test_revoke_session_sets_revoked_at_and_logs(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first={"id": 3, "user_id": 7})])
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    logged = {}
    monkeypatch.setattr(session_service, "_log_auth_event", lambda **kwargs: logged.update(kwargs))

    revoked = session_service.revoke_session("some-raw-token", now=NOW)

    assert revoked is True
    assert logged["event_type"] == "session_revoked"
    assert logged["user_id"] == 7
    assert logged["metadata"]["session_id"] == 3


def test_revoke_session_is_idempotent_for_unknown_or_already_revoked_token(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    logged_calls = []
    monkeypatch.setattr(session_service, "_log_auth_event", lambda **kwargs: logged_calls.append(kwargs))

    revoked = session_service.revoke_session("some-raw-token", now=NOW)

    assert revoked is False
    assert logged_calls == []


def test_revoke_session_rejects_malformed_input_without_hitting_db(monkeypatch):
    engine = FakeEngine([])  # no query may run
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    assert session_service.revoke_session("", now=NOW) is False
    assert session_service.revoke_session(None, now=NOW) is False


# --- revoke_all_sessions_for_user ------------------------------------------------

def test_revoke_all_sessions_for_user_returns_count_and_logs(monkeypatch):
    engine = FakeEngine([_UpdateResult(rowcount=3)])
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    logged = {}
    monkeypatch.setattr(session_service, "_log_auth_event", lambda **kwargs: logged.update(kwargs))

    revoked_count = session_service.revoke_all_sessions_for_user(user_id=7, now=NOW)

    assert revoked_count == 3
    assert logged["event_type"] == "session_revoked_all"
    assert logged["user_id"] == 7
    assert logged["metadata"]["revoked_count"] == 3
    assert engine.calls[0]["params"]["user_id"] == 7


def test_revoke_all_sessions_for_user_logs_even_when_nothing_to_revoke(monkeypatch):
    engine = FakeEngine([_UpdateResult(rowcount=0)])
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    logged = {}
    monkeypatch.setattr(session_service, "_log_auth_event", lambda **kwargs: logged.update(kwargs))

    revoked_count = session_service.revoke_all_sessions_for_user(user_id=7, now=NOW)

    assert revoked_count == 0
    assert logged["metadata"]["revoked_count"] == 0
