"""Unit tests for persistent_auth_service coordination behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.services import persistent_auth_service

NOW = datetime(
    2026,
    9,
    25,
    12,
    0,
    0,
    tzinfo=UTC,
)  # freshness: allow FRESH004 -- fixed expiry fixture only; no wall-clock read


@pytest.fixture(autouse=True)
def _isolated_session_state(monkeypatch):
    monkeypatch.setattr(persistent_auth_service.st, "session_state", {})


def test_restore_returns_existing_auth_user_without_reading_cookie(monkeypatch):
    user = {
        "id": 7,
        "email": "user@example.com",
        "full_name": "Example User",
    }
    persistent_auth_service.st.session_state["auth_user"] = user

    def unexpected_cookie_read():
        raise AssertionError("cookie reader should not run")

    monkeypatch.setattr(
        persistent_auth_service.cookie_service,
        "get_session_cookie_state",
        unexpected_cookie_read,
    )

    assert persistent_auth_service.restore_persistent_auth() == user


def test_restore_honors_suppression_without_reading_cookie(monkeypatch):
    persistent_auth_service.st.session_state["auth_user"] = None
    persistent_auth_service.st.session_state[
        persistent_auth_service._SUPPRESS_RESTORE_KEY
    ] = True

    def unexpected_cookie_read():
        raise AssertionError("cookie reader should not run")

    monkeypatch.setattr(
        persistent_auth_service.cookie_service,
        "get_session_cookie_state",
        unexpected_cookie_read,
    )

    assert persistent_auth_service.restore_persistent_auth() is None


def test_restore_state_reports_ready_when_browser_has_no_cookie(monkeypatch):
    persistent_auth_service.st.session_state["auth_user"] = None

    monkeypatch.setattr(
        persistent_auth_service.cookie_service,
        "get_session_cookie_state",
        lambda: (True, None),
    )

    assert persistent_auth_service.restore_persistent_auth_state() == (
        True,
        None,
    )


def test_restore_state_reports_not_ready_before_browser_sync(monkeypatch):
    persistent_auth_service.st.session_state["auth_user"] = None

    monkeypatch.setattr(
        persistent_auth_service.cookie_service,
        "get_session_cookie_state",
        lambda: (False, None),
    )

    assert persistent_auth_service.restore_persistent_auth_state() == (
        False,
        None,
    )


def test_restore_valid_session_sets_auth_user_and_current_token(monkeypatch):
    persistent_auth_service.st.session_state["auth_user"] = None

    user = {
        "id": 7,
        "email": "user@example.com",
        "full_name": "Example User",
    }

    monkeypatch.setattr(
        persistent_auth_service.cookie_service,
        "get_session_cookie_state",
        lambda: (True, "raw-token"),
    )
    monkeypatch.setattr(
        persistent_auth_service.session_service,
        "validate_session",
        lambda token: user if token == "raw-token" else None,
    )

    result = persistent_auth_service.restore_persistent_auth()

    assert result == user
    assert persistent_auth_service.st.session_state["auth_user"] == user
    assert (
        persistent_auth_service.st.session_state[
            persistent_auth_service._PERSISTENT_SESSION_STATE_KEY
        ]
        == "raw-token"
    )


def test_restore_invalid_session_stages_cookie_clear(monkeypatch):
    persistent_auth_service.st.session_state["auth_user"] = None
    calls = []

    monkeypatch.setattr(
        persistent_auth_service.cookie_service,
        "get_session_cookie_state",
        lambda: (True, "invalid-token"),
    )
    monkeypatch.setattr(
        persistent_auth_service.session_service,
        "validate_session",
        lambda token: None,
    )
    monkeypatch.setattr(
        persistent_auth_service.cookie_service,
        "clear_session_cookie",
        lambda: calls.append("clear"),
    )

    assert persistent_auth_service.restore_persistent_auth() is None
    assert calls == ["clear"]
    assert (
        persistent_auth_service._PERSISTENT_SESSION_STATE_KEY
        not in persistent_auth_service.st.session_state
    )


def test_create_persistent_auth_stores_token_and_stages_cookie(monkeypatch):
    expires_at = NOW + timedelta(days=14)
    calls = []

    persistent_auth_service.st.session_state[
        persistent_auth_service._SUPPRESS_RESTORE_KEY
    ] = True

    monkeypatch.setattr(
        persistent_auth_service.session_service,
        "create_session",
        lambda user_id: {
            "token": "new-token",
            "expires_at": expires_at,
        },
    )
    monkeypatch.setattr(
        persistent_auth_service.cookie_service,
        "set_session_cookie",
        lambda token, expiry: calls.append((token, expiry)),
    )

    result = persistent_auth_service.create_persistent_auth(7)

    assert result == {
        "token": "new-token",
        "expires_at": expires_at,
    }
    assert (
        persistent_auth_service.st.session_state[
            persistent_auth_service._PERSISTENT_SESSION_STATE_KEY
        ]
        == "new-token"
    )
    assert (
        persistent_auth_service._SUPPRESS_RESTORE_KEY
        not in persistent_auth_service.st.session_state
    )
    assert calls == [("new-token", expires_at)]


def test_clear_persistent_auth_revokes_current_token_and_clears_cookie(monkeypatch):
    persistent_auth_service.st.session_state[
        persistent_auth_service._PERSISTENT_SESSION_STATE_KEY
    ] = "raw-token"

    revoked = []
    cleared = []

    monkeypatch.setattr(
        persistent_auth_service.session_service,
        "revoke_session",
        lambda token: revoked.append(token) or True,
    )
    monkeypatch.setattr(
        persistent_auth_service.cookie_service,
        "clear_session_cookie",
        lambda: cleared.append(True),
    )

    assert persistent_auth_service.clear_persistent_auth() is True

    assert revoked == ["raw-token"]
    assert cleared == [True]
    assert (
        persistent_auth_service.st.session_state[
            persistent_auth_service._SUPPRESS_RESTORE_KEY
        ]
        is True
    )
    assert (
        persistent_auth_service._PERSISTENT_SESSION_STATE_KEY
        not in persistent_auth_service.st.session_state
    )


def test_clear_without_current_token_still_clears_browser_cookie(monkeypatch):
    cleared = []

    def unexpected_revoke(token):
        raise AssertionError("revoke_session should not run")

    monkeypatch.setattr(
        persistent_auth_service.session_service,
        "revoke_session",
        unexpected_revoke,
    )
    monkeypatch.setattr(
        persistent_auth_service.cookie_service,
        "clear_session_cookie",
        lambda: cleared.append(True),
    )

    assert persistent_auth_service.clear_persistent_auth() is False

    assert cleared == [True]
    assert (
        persistent_auth_service.st.session_state[
            persistent_auth_service._SUPPRESS_RESTORE_KEY
        ]
        is True
    )


def test_clear_all_for_current_user_revokes_all_and_clears_browser(monkeypatch):
    persistent_auth_service.st.session_state[
        persistent_auth_service._PERSISTENT_SESSION_STATE_KEY
    ] = "raw-token"

    users = []
    cleared = []

    monkeypatch.setattr(
        persistent_auth_service.session_service,
        "revoke_all_sessions_for_user",
        lambda user_id: users.append(user_id) or 3,
    )
    monkeypatch.setattr(
        persistent_auth_service.cookie_service,
        "clear_session_cookie",
        lambda: cleared.append(True),
    )

    result = persistent_auth_service.clear_all_persistent_auth_for_current_user(7)

    assert result == 3
    assert users == [7]
    assert cleared == [True]
    assert (
        persistent_auth_service.st.session_state[
            persistent_auth_service._SUPPRESS_RESTORE_KEY
        ]
        is True
    )
    assert (
        persistent_auth_service._PERSISTENT_SESSION_STATE_KEY
        not in persistent_auth_service.st.session_state
    )