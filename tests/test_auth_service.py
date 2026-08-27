from datetime import UTC, datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash

from src.services import auth_service

CORRECT_PASSWORD = "CorrectHorse123"


def make_user(**overrides):
    user = {
        "id": 1,
        "email": "user@example.com",
        "full_name": "Test User",
        "password_hash": generate_password_hash(CORRECT_PASSWORD),
        "is_active": True,
        "failed_login_attempts": 0,
        "locked_until": None,
        "last_login_at": None,
        "last_failed_login_at": None,
        "last_password_changed_at": None,
        "created_at": None,
    }
    user.update(overrides)
    return user


@pytest.fixture(autouse=True)
def stub_audit_log(monkeypatch):
    # log_auth_event writes to the database; not under test here.
    monkeypatch.setattr(auth_service, "log_auth_event", lambda *a, **k: None)


# --- authenticate_user -------------------------------------------------

def test_unknown_email_returns_generic_error(monkeypatch):
    monkeypatch.setattr(auth_service, "get_user_by_email", lambda email: None)

    result = auth_service.authenticate_user("nobody@example.com", "whatever")

    assert result == {
        "ok": False,
        "code": "invalid_credentials",
        "message": "Invalid email or password.",
    }


def test_inactive_account_is_blocked(monkeypatch):
    user = make_user(is_active=False)
    monkeypatch.setattr(auth_service, "get_user_by_email", lambda email: user)

    result = auth_service.authenticate_user(user["email"], CORRECT_PASSWORD)

    assert result["ok"] is False
    assert result["code"] == "inactive"


def test_locked_account_blocks_before_checking_password(monkeypatch):
    user = make_user(locked_until=datetime.now(UTC) + timedelta(minutes=5))
    monkeypatch.setattr(auth_service, "get_user_by_email", lambda email: user)

    failed_login_calls = []
    monkeypatch.setattr(
        auth_service, "record_failed_login", lambda uid: failed_login_calls.append(uid)
    )

    result = auth_service.authenticate_user(user["email"], "wrong-password")

    assert result["ok"] is False
    assert result["code"] == "locked"
    assert result["minutes_remaining"] >= 1
    assert failed_login_calls == []


def test_wrong_password_records_failed_login_and_stays_generic(monkeypatch):
    user = make_user()
    monkeypatch.setattr(auth_service, "get_user_by_email", lambda email: user)

    failed_login_calls = []
    monkeypatch.setattr(
        auth_service, "record_failed_login", lambda uid: failed_login_calls.append(uid)
    )

    result = auth_service.authenticate_user(user["email"], "wrong-password")

    assert result["ok"] is False
    assert result["code"] == "invalid_credentials"
    assert failed_login_calls == [user["id"]]


def test_wrong_password_that_triggers_lockout_reports_it(monkeypatch):
    user = make_user()
    locked_user = make_user(locked_until=datetime.now(UTC) + timedelta(minutes=15))

    lookups = [user, locked_user]
    monkeypatch.setattr(auth_service, "get_user_by_email", lambda email: lookups.pop(0))
    monkeypatch.setattr(auth_service, "record_failed_login", lambda uid: None)

    result = auth_service.authenticate_user(user["email"], "wrong-password")

    assert result["ok"] is False
    assert result["code"] == "locked"
    assert result["minutes_remaining"] == 15


def test_successful_login_resets_lockout_and_returns_safe_fields(monkeypatch):
    user = make_user()
    monkeypatch.setattr(auth_service, "get_user_by_email", lambda email: user)

    success_calls = []
    monkeypatch.setattr(
        auth_service, "record_successful_login", lambda uid: success_calls.append(uid)
    )

    result = auth_service.authenticate_user(user["email"], CORRECT_PASSWORD)

    assert result["ok"] is True
    assert success_calls == [user["id"]]
    assert result["user"] == {
        "id": user["id"],
        "email": user["email"],
        "full_name": user["full_name"],
    }


def test_missing_password_hash_fails_closed(monkeypatch):
    user = make_user(password_hash=None)
    monkeypatch.setattr(auth_service, "get_user_by_email", lambda email: user)

    result = auth_service.authenticate_user(user["email"], CORRECT_PASSWORD)

    assert result["ok"] is False
    assert result["code"] == "invalid_credentials"


# --- change_password -----------------------------------------------------

def test_change_password_rejects_wrong_current_password(monkeypatch):
    user = make_user()
    monkeypatch.setattr(auth_service, "get_user_by_id", lambda uid: user)

    result = auth_service.change_password(
        user["id"], "not-the-current-password", "NewPassword123", "NewPassword123"
    )

    assert result["ok"] is False
    assert result["code"] == "invalid_current_password"


def test_change_password_rejects_short_password(monkeypatch):
    user = make_user()
    monkeypatch.setattr(auth_service, "get_user_by_id", lambda uid: user)

    result = auth_service.change_password(user["id"], CORRECT_PASSWORD, "short", "short")

    assert result["ok"] is False
    assert result["code"] == "password_too_short"


def test_change_password_rejects_mismatched_confirmation(monkeypatch):
    user = make_user()
    monkeypatch.setattr(auth_service, "get_user_by_id", lambda uid: user)

    result = auth_service.change_password(
        user["id"], CORRECT_PASSWORD, "NewPassword123", "Different123"
    )

    assert result["ok"] is False
    assert result["code"] == "password_mismatch"


def test_change_password_rejects_reusing_current_password(monkeypatch):
    user = make_user()
    monkeypatch.setattr(auth_service, "get_user_by_id", lambda uid: user)

    result = auth_service.change_password(
        user["id"], CORRECT_PASSWORD, CORRECT_PASSWORD, CORRECT_PASSWORD
    )

    assert result["ok"] is False
    assert result["code"] == "same_password"


def test_change_password_success_updates_hash(monkeypatch):
    user = make_user()
    monkeypatch.setattr(auth_service, "get_user_by_id", lambda uid: user)

    captured = {}

    class FakeConn:
        def execute(self, stmt, params):
            captured.update(params)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeEngine:
        def begin(self):
            return FakeConn()

    monkeypatch.setattr(auth_service, "get_engine", lambda: FakeEngine())

    result = auth_service.change_password(
        user["id"], CORRECT_PASSWORD, "NewPassword123", "NewPassword123"
    )

    assert result["ok"] is True
    assert captured["user_id"] == user["id"]
    assert captured["password_hash"] != user["password_hash"]
