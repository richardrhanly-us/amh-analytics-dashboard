import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

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

# --- password reset -------------------------------------------------------


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class FakeResetConn:
    def __init__(self, reset_row=None):
        self.reset_row = reset_row
        self.calls = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}

        self.calls.append({
            "sql": sql,
            "params": params,
        })

        if "SELECT" in sql and "password_reset_tokens" in sql:
            return FakeResult(self.reset_row)

        return FakeResult()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeResetEngine:
    def __init__(self, reset_row=None):
        self.conn = FakeResetConn(reset_row=reset_row)

    def begin(self):
        return self.conn


def test_password_reset_unknown_email_returns_generic_response(monkeypatch):
    monkeypatch.setattr(
        auth_service,
        "get_user_by_email",
        lambda email: None,
    )

    result = auth_service.request_password_reset("nobody@example.com")

    assert result == {
        "ok": True,
        "code": "reset_requested",
        "message": (
            "If an active account exists for that email address, "
            "password reset instructions will be sent."
        ),
    }


def test_password_reset_inactive_user_returns_generic_response(monkeypatch):
    user = make_user(is_active=False)

    monkeypatch.setattr(
        auth_service,
        "get_user_by_email",
        lambda email: user,
    )

    result = auth_service.request_password_reset(user["email"])

    assert result["ok"] is True
    assert result["code"] == "reset_requested"
    assert "reset_token" not in result
    assert "reset_email" not in result


def test_password_reset_request_creates_hashed_token(monkeypatch):
    user = make_user()
    engine = FakeResetEngine()

    monkeypatch.setattr(
        auth_service,
        "get_user_by_email",
        lambda email: user,
    )
    monkeypatch.setattr(
        auth_service,
        "get_engine",
        lambda: engine,
    )

    result = auth_service.request_password_reset(user["email"])

    assert result["ok"] is True
    assert result["code"] == "reset_requested"
    assert result["reset_email"] == user["email"]

    raw_token = result["reset_token"]

    insert_call = next(
        call
        for call in engine.conn.calls
        if "INSERT INTO password_reset_tokens" in call["sql"]
    )

    stored_hash = insert_call["params"]["token_hash"]

    assert stored_hash != raw_token
    assert stored_hash == hashlib.sha256(
        raw_token.encode("utf-8")
    ).hexdigest()


def test_password_reset_request_invalidates_existing_tokens(monkeypatch):
    user = make_user()
    engine = FakeResetEngine()

    monkeypatch.setattr(
        auth_service,
        "get_user_by_email",
        lambda email: user,
    )
    monkeypatch.setattr(
        auth_service,
        "get_engine",
        lambda: engine,
    )

    auth_service.request_password_reset(user["email"])

    invalidate_call = next(
        call
        for call in engine.conn.calls
        if "UPDATE password_reset_tokens" in call["sql"]
        and "used_at = now()" in call["sql"]
    )

    assert invalidate_call["params"]["user_id"] == user["id"]


def test_reset_password_rejects_missing_token():
    result = auth_service.reset_password_with_token(
        "",
        "NewPassword123",
        "NewPassword123",
    )

    assert result["ok"] is False
    assert result["code"] == "invalid_reset_token"


def test_reset_password_rejects_short_password():
    result = auth_service.reset_password_with_token(
        "some-token",
        "short",
        "short",
    )

    assert result["ok"] is False
    assert result["code"] == "password_too_short"


def test_reset_password_rejects_mismatched_confirmation():
    result = auth_service.reset_password_with_token(
        "some-token",
        "NewPassword123",
        "DifferentPassword123",
    )

    assert result["ok"] is False
    assert result["code"] == "password_mismatch"


def test_reset_password_rejects_invalid_or_expired_token(monkeypatch):
    engine = FakeResetEngine(reset_row=None)

    monkeypatch.setattr(
        auth_service,
        "get_engine",
        lambda: engine,
    )

    result = auth_service.reset_password_with_token(
        "invalid-or-expired-token",
        "NewPassword123",
        "NewPassword123",
    )

    assert result["ok"] is False
    assert result["code"] == "invalid_reset_token"


def test_reset_password_rejects_inactive_user(monkeypatch):
    reset_row = {
        "id": 10,
        "user_id": 1,
        "email": "user@example.com",
        "password_hash": generate_password_hash(CORRECT_PASSWORD),
        "is_active": False,
    }

    engine = FakeResetEngine(reset_row=reset_row)

    monkeypatch.setattr(
        auth_service,
        "get_engine",
        lambda: engine,
    )

    result = auth_service.reset_password_with_token(
        "valid-token",
        "NewPassword123",
        "NewPassword123",
    )

    assert result["ok"] is False
    assert result["code"] == "invalid_reset_token"


def test_reset_password_rejects_reusing_current_password(monkeypatch):
    reset_row = {
        "id": 10,
        "user_id": 1,
        "email": "user@example.com",
        "password_hash": generate_password_hash(CORRECT_PASSWORD),
        "is_active": True,
    }

    engine = FakeResetEngine(reset_row=reset_row)

    monkeypatch.setattr(
        auth_service,
        "get_engine",
        lambda: engine,
    )

    result = auth_service.reset_password_with_token(
        "valid-token",
        CORRECT_PASSWORD,
        CORRECT_PASSWORD,
    )

    assert result["ok"] is False
    assert result["code"] == "same_password"


def test_reset_password_success_updates_password_and_invalidates_tokens(monkeypatch):
    old_hash = generate_password_hash(CORRECT_PASSWORD)

    reset_row = {
        "id": 10,
        "user_id": 1,
        "email": "user@example.com",
        "password_hash": old_hash,
        "is_active": True,
    }

    engine = FakeResetEngine(reset_row=reset_row)

    monkeypatch.setattr(
        auth_service,
        "get_engine",
        lambda: engine,
    )

    result = auth_service.reset_password_with_token(
        "valid-token",
        "NewPassword123",
        "NewPassword123",
    )

    assert result["ok"] is True
    assert result["code"] == "password_reset"

    user_update_call = next(
        call
        for call in engine.conn.calls
        if "UPDATE app_users" in call["sql"]
    )

    assert user_update_call["params"]["user_id"] == reset_row["user_id"]

    new_hash = user_update_call["params"]["password_hash"]

    assert new_hash != old_hash
    assert check_password_hash(new_hash, "NewPassword123")

    token_update_call = next(
        call
        for call in engine.conn.calls
        if "UPDATE password_reset_tokens" in call["sql"]
        and "used_at = now()" in call["sql"]
    )

    assert token_update_call["params"]["user_id"] == reset_row["user_id"]


def test_reset_password_success_clears_lockout_state(monkeypatch):
    reset_row = {
        "id": 10,
        "user_id": 1,
        "email": "user@example.com",
        "password_hash": generate_password_hash(CORRECT_PASSWORD),
        "is_active": True,
    }

    engine = FakeResetEngine(reset_row=reset_row)

    monkeypatch.setattr(
        auth_service,
        "get_engine",
        lambda: engine,
    )

    result = auth_service.reset_password_with_token(
        "valid-token",
        "NewPassword123",
        "NewPassword123",
    )

    assert result["ok"] is True

    update_call = next(
        call
        for call in engine.conn.calls
        if "UPDATE app_users" in call["sql"]
    )

    assert "failed_login_attempts = 0" in update_call["sql"]
    assert "locked_until = NULL" in update_call["sql"]