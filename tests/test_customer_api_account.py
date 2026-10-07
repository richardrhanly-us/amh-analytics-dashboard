"""R8A: the signed-in user's own account, and password reset.

    GET  /api/account
    PUT  /api/account/profile
    POST /api/account/change-password
    POST /api/auth/password-reset/request
    POST /api/auth/password-reset/complete

Like tests/test_customer_api_auth.py, these drive the real routes through
TestClient(main.app) and replace the services at the FLAT module identity the
routes use (services.auth_service, services.session_service,
services.email_service). What the routes are responsible for is tested here:
who may call them, what reaches the service, how its answer becomes a
response, and what must never be in one. The two service functions this block
added (get_account_profile, update_profile_name) are tested below against the
statement fakes; the password and token rules themselves are auth_service's
and are tested in tests/test_auth_service.py and, against a real server, in
tests/test_account_postgres.py.

Cookies are sent with an explicit Cookie header, and every test gets its own
client and a reset rate limiter. No email is ever sent: the one function that
would is replaced.
"""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from db_fakes import FakeEngine, FakeQueryResult
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.testclient import TestClient

import main
from customer_api import account_routes, account_schemas, auth_dependencies, settings
from services import auth_service, email_service, session_service

ORIGIN = "https://app.example.test"
COOKIE_NAME = "__Host-sortview_api_session"
TOKEN = "synthetic-opaque-session-token"
COOKIE = {"Cookie": f"{COOKIE_NAME}={TOKEN}"}

USER = {"id": 7, "email": "user@example.invalid", "full_name": "Test User"}
SIGNED_IN = datetime(2026, 9, 30, 14, 5, 0, tzinfo=UTC)  # a stored timestamp, not "now"
CHANGED = datetime(2026, 8, 1, 9, 0, 0, tzinfo=UTC)      # a stored timestamp, not "now"
PROFILE = {"email": "user@example.invalid", "full_name": "Test User", "last_login_at": SIGNED_IN, "last_password_changed_at": CHANGED}
ACCOUNT = {
    "email": "user@example.invalid",
    "full_name": "Test User",
    "last_login_at": "2026-09-30T14:05:00Z",
    "last_password_changed_at": "2026-08-01T09:00:00Z",
}

CURRENT = "synthetic-Current-Password-1"
NEW = "synthetic-New-Password-2"
RESET_TOKEN = "synthetic-RESET-token-5f0c9a"
PASSWORDS = {"current_password": CURRENT, "new_password": NEW, "confirm_password": NEW}
COMPLETE = {"token": RESET_TOKEN, "new_password": NEW, "confirm_password": NEW}

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
ORIGIN_NOT_ALLOWED = {"code": "origin_not_allowed", "message": "Request origin is not allowed."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}
RESET_REQUESTED = {
    "code": "password_reset_requested",
    "message": "If an active account exists for that email address, password reset instructions will be sent.",
}
RESET_UNAVAILABLE = {"code": "password_reset_unavailable", "message": "Password reset is not available right now."}
INVALID_RESET_TOKEN = {"code": "invalid_reset_token", "message": "This password reset link is invalid or has expired."}

# Every write this block added: (method, path, a body it accepts, whether it needs a session).
WRITES = [
    ("put", "/api/account/profile", {"full_name": "New Name"}, True),
    ("post", "/api/account/change-password", PASSWORDS, True),
    ("post", "/api/auth/password-reset/request", {"email": "user@example.invalid"}, False),
    ("post", "/api/auth/password-reset/complete", COMPLETE, False),
]
SECRETS = (CURRENT, NEW, RESET_TOKEN, TOKEN)

_SMTP = {
    "SORTVIEW_SMTP_HOST": "smtp.example.invalid",
    "SORTVIEW_SMTP_USERNAME": "synthetic-user",
    "SORTVIEW_SMTP_PASSWORD": "synthetic-not-a-real-credential",
    "SORTVIEW_EMAIL_FROM": "no-reply@example.invalid",
}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.setenv("SORTVIEW_CUSTOMER_APP_URL", ORIGIN)
    for name, value in _SMTP.items():
        monkeypatch.setenv(name, value)
    for name in ("SORTVIEW_CUSTOMER_COOKIE_SECURE", "SORTVIEW_PASSWORD_ATTEMPT_RATE_LIMIT",
                 "SORTVIEW_PASSWORD_RESET_REQUEST_RATE_LIMIT", "SORTVIEW_APP_URL"):
        monkeypatch.delenv(name, raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


class Services:
    """Recording stand-ins for everything the account routes call."""

    def __init__(self, monkeypatch, *, validated=USER, profile=PROFILE):
        self.calls: list[tuple] = []
        self.validated = validated
        self.profile = profile
        self.update_result: dict | None = None
        self.change_result = {"ok": True, "code": "password_changed", "message": "Your password was updated successfully."}
        self.request_result = {"ok": True, "code": "reset_requested", "message": "generic", "reset_token": RESET_TOKEN,
                               "reset_email": USER["email"]}
        self.complete_result = {"ok": True, "code": "password_reset", "message": "Your password has been reset successfully."}
        self.raise_on: dict[str, Exception] = {}
        self.emails: list[tuple] = []

        monkeypatch.setattr(session_service, "validate_session", self._validate_session)
        monkeypatch.setattr(auth_service, "get_account_profile", self._get_account_profile)
        monkeypatch.setattr(auth_service, "update_profile_name", self._update_profile_name)
        monkeypatch.setattr(auth_service, "change_password", self._change_password)
        monkeypatch.setattr(auth_service, "request_password_reset", self._request_password_reset)
        monkeypatch.setattr(auth_service, "reset_password_with_token", self._reset_password_with_token)
        monkeypatch.setattr(email_service, "send_password_reset_email", self._send_password_reset_email)

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if name in self.raise_on:
            raise self.raise_on[name]

    def _validate_session(self, raw_token):
        self._call("validate_session", raw_token)
        return dict(self.validated) if self.validated else None

    def _get_account_profile(self, user_id):
        self._call("get_account_profile", user_id)
        return dict(self.profile) if self.profile else None

    def _update_profile_name(self, user_id, full_name):
        self._call("update_profile_name", user_id, full_name)
        if self.update_result is not None:
            return self.update_result
        return {"ok": True, "code": "profile_updated", "profile": {**PROFILE, "full_name": full_name.strip()}}

    def _change_password(self, user_id, current_password, new_password, confirm_password):
        self._call("change_password", user_id, current_password, new_password, confirm_password)
        return self.change_result

    def _request_password_reset(self, email):
        self._call("request_password_reset", email)
        return self.request_result

    def _reset_password_with_token(self, token, new_password, confirm_password):
        self._call("reset_password_with_token", token, new_password, confirm_password)
        return self.complete_result

    def _send_password_reset_email(self, recipient_email, reset_token, *, reset_url=None):
        self.emails.append((recipient_email, reset_token, reset_url))
        self._call("send_password_reset_email")

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def _send(api, method, path, body=None, *, origin: str | None = ORIGIN, cookie=True, headers=None):
    sent = dict(headers or {})
    if origin is not None:
        sent["Origin"] = origin
    if cookie:
        sent.update(COOKIE)
    return api.request(method.upper(), path, json=body, headers=sent)


def _cleared_cookies(response) -> list[str]:
    cleared = []
    for header in response.headers.get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        cleared += [name for name, morsel in jar.items() if morsel.value == "" and morsel["max-age"] == "0"]
    return cleared


def _problem(code: str, message: str, field: str, why: str) -> dict:
    return {"code": code, "message": message, "problems": [{"field": field, "code": why}]}


def _assert_no_secret(response, caplog=None) -> None:
    for secret in SECRETS:
        assert secret not in response.text
        assert secret not in str(dict(response.headers))
        if caplog is not None:
            assert secret not in caplog.text


# =====================================================================================================================
# GET /api/account
# =====================================================================================================================

def test_account_returns_the_signed_in_users_own_profile(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.get("/api/account", headers=COOKIE)

    assert (response.status_code, response.json()) == (200, ACCOUNT)
    assert response.headers["cache-control"] == "no-store"
    # The user is the session's: nothing in the request says whose account to read.
    assert services.calls == [("validate_session", TOKEN), ("get_account_profile", 7)]


def test_account_has_exactly_four_fields_and_nothing_else_a_user_row_holds(api, monkeypatch):
    row = {**PROFILE, "id": 7, "password_hash": "scrypt:CANARY-hash", "is_active": True, "failed_login_attempts": 3,
           "locked_until": SIGNED_IN, "last_failed_login_at": SIGNED_IN, "is_platform_admin": True, "created_at": SIGNED_IN}
    Services(monkeypatch, profile=row)

    response = api.get("/api/account", headers=COOKIE)

    assert list(response.json()) == ["email", "full_name", "last_login_at", "last_password_changed_at"]
    for hidden in ("CANARY", "scrypt", "password_hash", "locked", "failed", "platform", "is_active", '"id"', "created_at"):
        assert hidden not in response.text, hidden


# 2026-09-30 14:05:00 UTC, as the driver hands it back from sessions set to different zones.
_SAME_INSTANT = [
    datetime(2026, 9, 30, 14, 5, 0, tzinfo=UTC),
    datetime(2026, 9, 30, 9, 5, 0, tzinfo=timezone(timedelta(hours=-5))),            # America/Chicago, daylight time
    datetime(2026, 9, 30, 23, 5, 0, tzinfo=timezone(timedelta(hours=9))),            # Asia/Tokyo
    datetime(2026, 9, 30, 19, 35, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),   # Asia/Kolkata
    datetime(2026, 9, 30, 9, 5, 0, tzinfo=ZoneInfo("America/Chicago")),
    datetime(2026, 10, 1, 3, 5, 0, tzinfo=ZoneInfo("Pacific/Auckland")),             # another calendar day there
]


@pytest.mark.parametrize("stored", _SAME_INSTANT, ids=lambda value: value.isoformat())
@pytest.mark.parametrize("field", ["last_login_at", "last_password_changed_at"])
def test_an_account_timestamp_is_written_in_utc_whatever_zone_the_database_session_is_in(api, monkeypatch, field, stored):
    Services(monkeypatch, profile={**PROFILE, "last_login_at": None, "last_password_changed_at": None, field: stored})

    body = api.get("/api/account", headers=COOKIE).json()

    # The same instant, in UTC -- never the session's own offset.
    assert body[field] == "2026-09-30T14:05:00Z"
    assert datetime.fromisoformat(body[field]) == stored
    other = "last_password_changed_at" if field == "last_login_at" else "last_login_at"
    assert body[other] is None


def test_an_account_timestamp_keeps_its_fraction_of_a_second(api, monkeypatch):
    stored = datetime(2026, 10, 6, 21, 42, 21, 544504, tzinfo=timezone(timedelta(hours=-5)))
    Services(monkeypatch, profile={**PROFILE, "last_login_at": stored, "last_password_changed_at": stored})

    body = api.get("/api/account", headers=COOKIE).json()

    assert body["last_login_at"] == body["last_password_changed_at"] == "2026-10-07T02:42:21.544504Z"
    assert datetime.fromisoformat(body["last_login_at"]) == stored


def test_the_profile_update_answer_is_in_utc_too(api, monkeypatch):
    services = Services(monkeypatch)
    services.update_result = {"ok": True, "code": "profile_updated", "profile": {
        **PROFILE, "last_login_at": _SAME_INSTANT[1], "last_password_changed_at": _SAME_INSTANT[2]}}

    body = _send(api, "put", "/api/account/profile", {"full_name": "Pat"}).json()

    assert body["last_login_at"] == body["last_password_changed_at"] == "2026-09-30T14:05:00Z"


@pytest.mark.parametrize("field", ["last_login_at", "last_password_changed_at"])
def test_an_account_timestamp_with_no_zone_is_a_server_error_never_a_guess(api, monkeypatch, field, caplog):
    # A TIMESTAMPTZ column never yields one. If one arrives, no zone is assumed for it.
    naive = datetime(2026, 9, 30, 14, 5, 0)  # noqa: DTZ001 - deliberately without a zone
    Services(monkeypatch, profile={**PROFILE, field: naive})

    response = TestClient(main.app, raise_server_exceptions=False).get("/api/account", headers=COOKIE)

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    assert "2026-09-30" not in response.text


def test_both_account_timestamp_columns_are_timestamptz_so_a_value_always_has_a_zone():
    baseline = (Path(__file__).resolve().parent.parent / "alembic" / "versions" / "26397a3947b1_baseline_current_schema.py").read_text(
        encoding="utf-8")
    app_users = baseline.split("CREATE TABLE IF NOT EXISTS app_users", 1)[1].split(")\n    \"\"\")", 1)[0]

    assert "last_login_at TIMESTAMPTZ" in app_users and "last_password_changed_at TIMESTAMPTZ" in app_users


def test_account_timestamps_are_null_when_it_has_never_happened(api, monkeypatch):
    Services(monkeypatch, profile={**PROFILE, "last_login_at": None, "last_password_changed_at": None, "full_name": ""})

    assert api.get("/api/account", headers=COOKIE).json() == {
        "email": "user@example.invalid", "full_name": "", "last_login_at": None, "last_password_changed_at": None,
    }


def test_account_without_a_session_is_401_and_reads_nothing(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.get("/api/account")

    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert services.calls == []


def test_account_with_a_session_that_does_not_validate_is_401_and_clears_the_cookie(api, monkeypatch):
    services = Services(monkeypatch, validated=None)

    response = api.get("/api/account", headers=COOKIE)

    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert _cleared_cookies(response) == [COOKIE_NAME]
    assert services.names() == ["validate_session"]


def test_account_of_a_user_deactivated_a_moment_ago_is_401_not_an_empty_profile(api, monkeypatch):
    Services(monkeypatch, profile=None)

    response = api.get("/api/account", headers=COOKIE)

    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert _cleared_cookies(response) == [COOKIE_NAME]


def test_account_needs_no_origin_and_takes_no_identifier_from_the_request(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.get("/api/account", headers=COOKIE, params={"user_id": 8, "email": "other@example.invalid", "id": 8})

    assert response.json() == ACCOUNT
    assert ("get_account_profile", 7) in services.calls


def test_a_database_failure_reading_the_account_is_a_500_that_says_nothing(api, monkeypatch, caplog):
    services = Services(monkeypatch)
    services.raise_on["get_account_profile"] = RuntimeError("synthetic failure for user@example.invalid CANARY")

    response = TestClient(main.app, raise_server_exceptions=False).get("/api/account", headers=COOKIE)

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    assert "CANARY" not in response.text and "CANARY" not in caplog.text


# =====================================================================================================================
# PUT /api/account/profile
# =====================================================================================================================

def test_profile_update_changes_the_name_and_returns_the_account(api, monkeypatch):
    services = Services(monkeypatch)

    response = _send(api, "put", "/api/account/profile", {"full_name": "  Pat Example  "})

    assert (response.status_code, response.json()) == (200, {**ACCOUNT, "full_name": "Pat Example"})
    assert response.headers["cache-control"] == "no-store"
    # The name goes to the service exactly as sent: trimming and every rule are the service's.
    assert services.calls == [("validate_session", TOKEN), ("update_profile_name", 7, "  Pat Example  ")]
    # The session is untouched: no cookie is set or cleared.
    assert response.headers.get_list("set-cookie") == []


@pytest.mark.parametrize(("code", "why"), [
    ("name_required", "required"), ("name_too_long", "too_long"), ("name_invalid", "invalid_characters"),
])
def test_a_name_the_service_refuses_is_a_422_that_names_the_field_and_why(api, monkeypatch, code, why):
    services = Services(monkeypatch)
    services.update_result = {"ok": False, "code": code, "message": "a service message that is not shown"}

    response = _send(api, "put", "/api/account/profile", {"full_name": "CANARY-name"})

    assert response.status_code == 422
    assert response.json() == _problem("invalid_profile", "The profile is not valid.", "full_name", why)
    assert "CANARY" not in response.text and "service message" not in response.text


@pytest.mark.parametrize("body", [
    {"full_name": "Pat", "email": "other@example.invalid"},
    {"full_name": "Pat", "id": 8},
    {"full_name": "Pat", "user_id": 8},
    {"full_name": "Pat", "is_active": False},
    {"full_name": "Pat", "is_platform_admin": True},
    {"full_name": "Pat", "role": "owner"},
    {"full_name": "Pat", "password": "x"},
    {"email": "other@example.invalid"},
    {},
    {"full_name": None},
    {"full_name": 7},
    {"full_name": ["Pat"]},
    {"full_name": "x" * 2001},
])
def test_a_body_that_is_not_exactly_one_name_is_refused_before_anything_is_changed(api, monkeypatch, body):
    services = Services(monkeypatch)

    response = _send(api, "put", "/api/account/profile", body)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "update_profile_name" not in services.names()
    for echoed in ("other@example.invalid", "owner"):
        assert echoed not in response.text


def test_the_email_cannot_be_changed_by_any_route(api, monkeypatch):
    Services(monkeypatch)
    fields = set(account_schemas.ProfileUpdateRequest.model_fields)

    assert fields == {"full_name"}
    for method in ("post", "patch", "delete"):
        assert _send(api, method, "/api/account/profile", {"full_name": "Pat"}).status_code == 405
    for method in ("put", "post", "patch", "delete"):
        assert _send(api, method, "/api/account", {"email": "other@example.invalid"}).status_code == 405
    assert _send(api, "put", "/api/account/email", {"email": "other@example.invalid"}).status_code == 404


def test_profile_update_without_a_session_is_401_and_changes_nothing(api, monkeypatch):
    services = Services(monkeypatch)

    response = _send(api, "put", "/api/account/profile", {"full_name": "Pat"}, cookie=False)

    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert services.calls == []


def test_profile_update_for_a_user_who_no_longer_exists_is_401(api, monkeypatch):
    services = Services(monkeypatch)
    services.update_result = {"ok": False, "code": "user_not_found", "message": "User account could not be found."}

    response = _send(api, "put", "/api/account/profile", {"full_name": "Pat"})

    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert _cleared_cookies(response) == [COOKIE_NAME]


def test_an_answer_the_route_does_not_know_is_a_server_error_never_a_guess(api, monkeypatch):
    services = Services(monkeypatch)
    services.update_result = {"ok": False, "code": "something_new", "message": "CANARY"}

    response = _send(TestClient(main.app, raise_server_exceptions=False), "put", "/api/account/profile", {"full_name": "Pat"})

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)


# =====================================================================================================================
# POST /api/account/change-password
# =====================================================================================================================

def test_changing_the_password_is_204_and_clears_the_cookie_because_every_session_is_revoked(api, monkeypatch, caplog):
    services = Services(monkeypatch)
    caplog.set_level(logging.DEBUG)

    response = _send(api, "post", "/api/account/change-password", PASSWORDS)

    assert response.status_code == 204 and response.content == b""
    assert response.headers["cache-control"] == "no-store"
    # The service revokes ALL of the user's sessions, this request's included: the caller signs in again.
    assert _cleared_cookies(response) == [COOKIE_NAME]
    assert services.calls == [("validate_session", TOKEN), ("change_password", 7, CURRENT, NEW, NEW)]
    _assert_no_secret(response, caplog)


@pytest.mark.parametrize(("code", "field", "why"), [
    ("invalid_current_password", "current_password", "incorrect"),
    ("password_too_short", "new_password", "too_short"),
    ("password_mismatch", "confirm_password", "mismatch"),
    ("same_password", "new_password", "same_as_current"),
])
def test_a_password_change_the_service_refuses_is_a_422_and_the_session_survives(api, monkeypatch, caplog, code, field, why):
    services = Services(monkeypatch)
    services.change_result = {"ok": False, "code": code, "message": "a service message that is not shown"}
    caplog.set_level(logging.DEBUG)

    response = _send(api, "post", "/api/account/change-password", PASSWORDS)

    assert response.status_code == 422
    assert response.json() == _problem("invalid_password_change", "The password could not be changed.", field, why)
    # Not a 401 -- the browser must not treat a mistyped password as an expired session -- and the cookie stays.
    assert response.headers.get_list("set-cookie") == []
    _assert_no_secret(response, caplog)


def test_a_wrong_current_password_is_never_a_401(api, monkeypatch):
    services = Services(monkeypatch)
    services.change_result = {"ok": False, "code": "invalid_current_password", "message": "x"}

    assert _send(api, "post", "/api/account/change-password", PASSWORDS).status_code == 422


@pytest.mark.parametrize("code", ["user_not_found", "inactive"])
def test_a_password_change_for_an_account_that_is_gone_or_inactive_is_401(api, monkeypatch, code):
    services = Services(monkeypatch)
    services.change_result = {"ok": False, "code": code, "message": "x"}

    response = _send(api, "post", "/api/account/change-password", PASSWORDS)

    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert _cleared_cookies(response) == [COOKIE_NAME]


def test_changing_the_password_without_a_session_is_401_and_nothing_is_checked(api, monkeypatch):
    services = Services(monkeypatch)

    response = _send(api, "post", "/api/account/change-password", PASSWORDS, cookie=False)

    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert services.calls == []


@pytest.mark.parametrize("body", [
    {**PASSWORDS, "user_id": 8},
    {**PASSWORDS, "email": "other@example.invalid"},
    {"new_password": NEW, "confirm_password": NEW},
    {"current_password": CURRENT, "new_password": NEW},
    {"current_password": CURRENT, "confirm_password": NEW},
    {**PASSWORDS, "new_password": 12345678},
    {**PASSWORDS, "new_password": None},
    {**PASSWORDS, "new_password": "x" * 1025},
    {},
])
def test_a_malformed_password_change_is_refused_without_echoing_a_password(api, monkeypatch, caplog, body):
    services = Services(monkeypatch)
    caplog.set_level(logging.DEBUG)

    response = _send(api, "post", "/api/account/change-password", body)

    assert response.status_code == 422 and response.json()["code"] == "validation_error"
    assert "change_password" not in services.names()
    _assert_no_secret(response, caplog)


def test_passwords_and_the_token_are_masked_in_the_request_models(caplog):
    change = account_schemas.ChangePasswordRequest(**PASSWORDS)
    complete = account_schemas.PasswordResetCompleteRequest(**COMPLETE)

    for model in (change, complete):
        for secret in SECRETS:
            assert secret not in repr(model) and secret not in str(model) and secret not in str(model.model_dump())
    assert change.new_password.get_secret_value() == NEW and complete.token.get_secret_value() == RESET_TOKEN


def test_changing_the_password_is_rate_limited_per_client_address(api, monkeypatch):
    services = Services(monkeypatch)
    services.change_result = {"ok": False, "code": "invalid_current_password", "message": "x"}
    monkeypatch.setenv("SORTVIEW_PASSWORD_ATTEMPT_RATE_LIMIT", "3/minute")

    statuses = [_send(api, "post", "/api/account/change-password", PASSWORDS).status_code for _ in range(5)]

    assert statuses == [422, 422, 422, 429, 429]
    # The limit is spent by guesses that reached the service, and no further guess reaches it.
    assert services.names().count("change_password") == 3


# =====================================================================================================================
# POST /api/auth/password-reset/request
# =====================================================================================================================

def test_a_reset_request_is_accepted_and_the_link_is_emailed_never_returned(api, monkeypatch, caplog):
    services = Services(monkeypatch)
    caplog.set_level(logging.DEBUG)

    response = _send(api, "post", "/api/auth/password-reset/request", {"email": "User@Example.invalid"}, cookie=False)

    assert (response.status_code, response.json()) == (202, RESET_REQUESTED)
    assert response.headers["cache-control"] == "no-store"
    assert services.calls[0] == ("request_password_reset", "User@Example.invalid")
    # One email, to the address the SERVICE found (not the one typed), linking to the customer application.
    # The token is in the link's fragment, which a browser never sends to a server -- not in its query string.
    assert services.emails == [(USER["email"], RESET_TOKEN, f"{ORIGIN}/reset-password#token={RESET_TOKEN}")]
    assert "?" not in services.emails[0][2]
    # The token, the address and the account are nowhere in what the browser gets, or in the log.
    for hidden in (RESET_TOKEN, "user@example.invalid", "reset_token", "reset_email", '"id"'):
        assert hidden not in response.text
    assert RESET_TOKEN not in caplog.text
    assert response.headers.get_list("set-cookie") == []


def test_the_answer_is_identical_whether_or_not_an_account_exists(api, monkeypatch):
    services = Services(monkeypatch)

    # An active account: the service hands back a token and the address to send it to.
    known = _send(api, "post", "/api/auth/password-reset/request", {"email": USER["email"]}, cookie=False)
    # No such account, or an inactive one: the service hands back neither.
    services.request_result = {"ok": True, "code": "reset_requested", "message": "generic"}
    unknown = _send(api, "post", "/api/auth/password-reset/request", {"email": "nobody@example.invalid"}, cookie=False)

    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json()) == (202, RESET_REQUESTED)
    # Byte for byte, and header for header: nothing about the response says which it was.
    assert known.content == unknown.content
    assert dict(known.headers) == dict(unknown.headers)
    assert len(services.emails) == 1


def test_a_failed_delivery_changes_nothing_the_caller_sees(api, monkeypatch, caplog):
    services = Services(monkeypatch)
    services.raise_on["send_password_reset_email"] = RuntimeError(f"synthetic SMTP failure for {USER['email']} {RESET_TOKEN}")

    response = _send(api, "post", "/api/auth/password-reset/request", {"email": USER["email"]}, cookie=False)

    # The same answer as for an address with no account: a mail failure does not reveal that there is one.
    assert (response.status_code, response.json()) == (202, RESET_REQUESTED)
    assert "Password reset email could not be sent" in caplog.text
    assert RESET_TOKEN not in caplog.text and USER["email"] not in caplog.text and "synthetic SMTP" not in caplog.text


def test_the_email_is_sent_after_the_response_is_built(api, monkeypatch):
    services = Services(monkeypatch)
    source = inspect.getsource(account_routes.create_account_router)

    _send(api, "post", "/api/auth/password-reset/request", {"email": USER["email"]}, cookie=False)

    assert "response.background = BackgroundTask(_deliver_reset_email" in source
    assert "send_password_reset_email(" not in source          # never called inline, in the request
    assert services.names() == ["request_password_reset", "send_password_reset_email"]


@pytest.mark.parametrize("missing", ["SORTVIEW_CUSTOMER_APP_URL", *_SMTP])
def test_a_deployment_that_cannot_send_reset_email_says_so_the_same_way_for_every_address(api, monkeypatch, missing):
    services = Services(monkeypatch)
    monkeypatch.delenv(missing)

    known = _send(api, "post", "/api/auth/password-reset/request", {"email": USER["email"]}, cookie=False)
    services.request_result = {"ok": True, "code": "reset_requested", "message": "generic"}
    unknown = _send(api, "post", "/api/auth/password-reset/request", {"email": "nobody@example.invalid"}, cookie=False)

    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json()) == (503, RESET_UNAVAILABLE)
    # Decided before the address is looked at: no token is made that could never be delivered.
    assert services.calls == [] and services.emails == []
    assert missing not in known.text


def test_a_link_is_never_built_to_an_address_this_api_would_not_accept_requests_from(api, monkeypatch):
    services = Services(monkeypatch)

    for elsewhere in ("https://evil.example.test", "https://app.example.test/path", "app.example.test", "javascript:alert(1)",
                      "https://app.example.test:8443", "http://app.example.test", ""):
        monkeypatch.setenv("SORTVIEW_CUSTOMER_APP_URL", elsewhere)
        main.limiter.reset()
        response = _send(api, "post", "/api/auth/password-reset/request", {"email": USER["email"]}, cookie=False)
        assert (response.status_code, response.json()) == (503, RESET_UNAVAILABLE), elsewhere
    assert services.emails == []


@pytest.mark.parametrize("body", [
    {}, {"email": ""}, {"email": None}, {"email": 7}, {"email": ["a@example.invalid"]}, {"email": "x" * 321},
    {"email": "user@example.invalid", "user_id": 7}, {"email": "user@example.invalid", "redirect": "https://evil.example.test"},
    {"email": "user@example.invalid", "reset_url": "https://evil.example.test"},
])
def test_a_malformed_reset_request_is_refused_and_asks_nothing(api, monkeypatch, body):
    services = Services(monkeypatch)

    response = _send(api, "post", "/api/auth/password-reset/request", body, cookie=False)

    assert response.status_code == 422 and response.json()["code"] == "validation_error"
    assert services.calls == []
    assert "evil.example.test" not in response.text


def test_reset_requests_are_rate_limited_per_client_address_whatever_the_address_asked_about(api, monkeypatch):
    services = Services(monkeypatch)
    monkeypatch.setenv("SORTVIEW_PASSWORD_RESET_REQUEST_RATE_LIMIT", "2/minute")

    statuses = [
        _send(api, "post", "/api/auth/password-reset/request", {"email": f"user{n}@example.invalid"}, cookie=False).status_code
        for n in range(4)
    ]

    assert statuses == [202, 202, 429, 429]
    assert services.names().count("request_password_reset") == 2


def test_a_signed_in_user_may_also_ask_for_a_reset(api, monkeypatch):
    Services(monkeypatch)

    assert _send(api, "post", "/api/auth/password-reset/request", {"email": USER["email"]}).status_code == 202


# =====================================================================================================================
# POST /api/auth/password-reset/complete
# =====================================================================================================================

def test_completing_a_reset_is_204_and_signs_nobody_in(api, monkeypatch, caplog):
    services = Services(monkeypatch)
    caplog.set_level(logging.DEBUG)

    response = _send(api, "post", "/api/auth/password-reset/complete", COMPLETE, cookie=False)

    assert response.status_code == 204 and response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert services.calls == [("reset_password_with_token", RESET_TOKEN, NEW, NEW)]
    # No session is created: the person signs in with the new password.
    assert response.headers.get_list("set-cookie") == []
    _assert_no_secret(response, caplog)


def test_an_invalid_expired_or_used_token_is_one_answer_that_says_nothing_more(api, monkeypatch, caplog):
    services = Services(monkeypatch)
    services.complete_result = {"ok": False, "code": "invalid_reset_token", "message": "This password reset link is invalid or has expired."}
    caplog.set_level(logging.DEBUG)

    response = _send(api, "post", "/api/auth/password-reset/complete", COMPLETE, cookie=False)

    assert (response.status_code, response.json()) == (400, INVALID_RESET_TOKEN)
    for hidden in ("expired_at", "used_at", "user", "token_hash", '"id"'):
        assert hidden not in response.text
    _assert_no_secret(response, caplog)


@pytest.mark.parametrize(("code", "field", "why"), [
    ("password_too_short", "new_password", "too_short"),
    ("password_mismatch", "confirm_password", "mismatch"),
    ("same_password", "new_password", "same_as_current"),
])
def test_a_new_password_the_service_refuses_is_a_422_that_names_the_field(api, monkeypatch, caplog, code, field, why):
    services = Services(monkeypatch)
    services.complete_result = {"ok": False, "code": code, "message": "not shown"}
    caplog.set_level(logging.DEBUG)

    response = _send(api, "post", "/api/auth/password-reset/complete", COMPLETE, cookie=False)

    assert response.status_code == 422
    assert response.json() == _problem("invalid_password_reset", "The password could not be reset.", field, why)
    _assert_no_secret(response, caplog)


@pytest.mark.parametrize("body", [
    {"new_password": NEW, "confirm_password": NEW},
    {**COMPLETE, "token": ""},
    {**COMPLETE, "token": None},
    {**COMPLETE, "token": 123456},
    {**COMPLETE, "token": "x" * 513},
    {**COMPLETE, "email": "user@example.invalid"},
    {**COMPLETE, "user_id": 7},
    {"token": RESET_TOKEN, "new_password": NEW},
    {},
])
def test_a_malformed_completion_is_refused_without_echoing_the_token(api, monkeypatch, caplog, body):
    services = Services(monkeypatch)
    caplog.set_level(logging.DEBUG)

    response = _send(api, "post", "/api/auth/password-reset/complete", body, cookie=False)

    assert response.status_code == 422 and response.json()["code"] == "validation_error"
    assert services.calls == []
    _assert_no_secret(response, caplog)


def test_token_guesses_are_rate_limited_per_client_address(api, monkeypatch):
    services = Services(monkeypatch)
    services.complete_result = {"ok": False, "code": "invalid_reset_token", "message": "x"}
    monkeypatch.setenv("SORTVIEW_PASSWORD_ATTEMPT_RATE_LIMIT", "3/minute")

    statuses = [
        _send(api, "post", "/api/auth/password-reset/complete", {**COMPLETE, "token": f"guess-{n}"}, cookie=False).status_code
        for n in range(5)
    ]

    assert statuses == [400, 400, 400, 429, 429]
    assert services.names().count("reset_password_with_token") == 3


def test_the_token_is_never_taken_from_the_url(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.post(f"/api/auth/password-reset/complete?token={RESET_TOKEN}", headers={"Origin": ORIGIN},
                        json={"new_password": NEW, "confirm_password": NEW})

    assert response.status_code == 422 and services.calls == []


# =====================================================================================================================
# Origin, limits and what the routes are
# =====================================================================================================================

@pytest.mark.parametrize(("method", "path", "body", "_needs_session"), WRITES)
@pytest.mark.parametrize("origin", [None, "https://evil.example.test", "null", "https://app.example.test.evil.test",
                                    "http://app.example.test", "https://app.example.test:8443", "app.example.test"])
def test_every_write_refuses_a_missing_malformed_or_unlisted_origin(api, monkeypatch, method, path, body, _needs_session, origin):
    services = Services(monkeypatch)

    response = _send(api, method, path, body, origin=origin)

    assert (response.status_code, response.json()) == (403, ORIGIN_NOT_ALLOWED)
    # Refused before the session is looked at and before any service is asked anything.
    assert services.calls == [] and services.emails == []


@pytest.mark.parametrize(("method", "path", "body", "_needs_session"), WRITES)
def test_every_write_refuses_two_origin_headers_and_every_request_when_no_origin_is_configured(
    api, monkeypatch, method, path, body, _needs_session
):
    services = Services(monkeypatch)

    doubled = getattr(api, method)(path, json=body, headers=[("Origin", ORIGIN), ("Origin", ORIGIN), ("Cookie", COOKIE["Cookie"])])
    monkeypatch.delenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS")
    unconfigured = _send(api, method, path, body)

    assert doubled.status_code == unconfigured.status_code == 403
    assert services.calls == []


@pytest.mark.parametrize(("method", "path", "body", "needs_session"), WRITES)
def test_every_write_is_guarded_by_the_one_origin_dependency_and_an_allowed_origin_passes(
    api, monkeypatch, method, path, body, needs_session
):
    Services(monkeypatch)
    (route,) = [r for r in main.app.routes if getattr(r, "path", "") == path and method.upper() in r.methods]
    calls = [dependency.call for dependency in get_flat_dependant(route.dependant).dependencies]

    assert auth_dependencies.require_allowed_origin in calls
    assert (auth_dependencies.require_current_user in calls) is needs_session
    assert _send(api, method, path, body).status_code in (200, 202, 204)


def test_the_account_routes_are_exactly_these_five():
    routes = sorted(
        (route.path, sorted(route.methods)) for route in main.app.routes
        if getattr(route, "path", "").startswith(("/api/account", "/api/auth/password-reset"))
    )

    assert routes == [
        ("/api/account", ["GET"]),
        ("/api/account/change-password", ["POST"]),
        ("/api/account/profile", ["PUT"]),
        ("/api/auth/password-reset/complete", ["POST"]),
        ("/api/auth/password-reset/request", ["POST"]),
    ]


def test_no_account_route_takes_an_organization_a_user_id_or_anything_from_the_path():
    for route in main.app.routes:
        if getattr(route, "path", "").startswith(("/api/account", "/api/auth/password-reset")):
            dependant = get_flat_dependant(route.dependant)
            assert dependant.path_params == [] and dependant.query_params == [] and dependant.header_params == [], route.path
    source = inspect.getsource(account_routes).split('"""', 2)[2]      # the code, without the module's description
    for scoped in ("org_slug", "branch_slug", "membership", "get_org_role", "entitlement", "access_service", "tenant_scope",
                   "require_organization", "is_platform_admin"):
        assert scoped not in source, scoped


def test_the_routes_decide_nothing_about_passwords_or_tokens_themselves():
    source = inspect.getsource(account_routes)

    for forbidden in ("check_password_hash", "generate_password_hash", "hashlib", "secrets.", "text(", "execute(", "get_engine",
                      "len(", "< 8", "smtplib", "revoke_", "create_session", "import streamlit", "import pandas"):
        assert forbidden not in source, forbidden
    # No password, token, address or link is ever handed to a logger.
    for line in source.splitlines():
        if "logger." in line or "log_safe_exception(" in line:
            for secret in ("password", "token", "email", "url", "data."):
                assert secret not in line.split("(", 1)[1].lower().replace("password reset email could not be sent", ""), line


@pytest.mark.parametrize(("variable", "read", "default"), [
    ("SORTVIEW_PASSWORD_ATTEMPT_RATE_LIMIT", settings.password_attempt_rate_limit, "10/minute"),
    ("SORTVIEW_PASSWORD_RESET_REQUEST_RATE_LIMIT", settings.password_reset_request_rate_limit, "5/minute"),
    ("SORTVIEW_LOGIN_RATE_LIMIT", settings.login_rate_limit, "10/minute"),
])
def test_a_rate_limit_that_does_not_parse_falls_back_to_its_default_instead_of_no_limit(monkeypatch, variable, read, default):
    monkeypatch.delenv(variable, raising=False)
    assert read() == default
    monkeypatch.setenv(variable, "3/minute")
    assert read() == "3/minute"
    for unparseable in ("", "lots", "0", "ten per minute", "10/fortnight"):
        monkeypatch.setenv(variable, unparseable)
        assert read() == default, unparseable


def test_the_account_limits_do_not_spend_logins_limit(api, monkeypatch):
    services = Services(monkeypatch)
    services.change_result = {"ok": False, "code": "invalid_current_password", "message": "x"}
    monkeypatch.setenv("SORTVIEW_PASSWORD_ATTEMPT_RATE_LIMIT", "1/minute")
    monkeypatch.setattr(auth_service, "authenticate_user", lambda email, password: {"ok": False, "code": "invalid_credentials"})

    assert _send(api, "post", "/api/account/change-password", PASSWORDS).status_code == 422
    assert _send(api, "post", "/api/account/change-password", PASSWORDS).status_code == 429
    # Completing a reset has its own bucket of the same size, and login its own.
    assert _send(api, "post", "/api/auth/password-reset/complete", COMPLETE, cookie=False).status_code == 204
    assert _send(api, "post", "/api/auth/login", {"email": "a@example.invalid", "password": "x"}, cookie=False).status_code == 401


# =====================================================================================================================
# Where the reset link goes
# =====================================================================================================================

@pytest.mark.parametrize("token", ["abc-DEF_123", "a b&c=d/e?f#g", RESET_TOKEN, "x" * 43])
def test_the_token_is_only_ever_in_the_fragment_of_the_customer_link(monkeypatch, token):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.setenv("SORTVIEW_CUSTOMER_APP_URL", ORIGIN)

    link = urlsplit(settings.password_reset_url(token))

    # What a browser sends to the server for this link: the origin and the path, and nothing else.
    assert (f"{link.scheme}://{link.netloc}", link.path, link.query) == (ORIGIN, "/reset-password", "")
    assert parse_qs(link.fragment, strict_parsing=True) == {"token": [token]}
    assert token not in f"{link.scheme}://{link.netloc}{link.path}?{link.query}"
    assert "?token=" not in settings.password_reset_url(token)


def test_the_reset_link_is_the_customer_applications_reset_page_with_the_token_encoded(monkeypatch):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", f"{ORIGIN}, https://other.example.test")
    monkeypatch.setenv("SORTVIEW_CUSTOMER_APP_URL", "HTTPS://App.Example.Test")

    assert settings.customer_app_origin() == ORIGIN
    assert settings.password_reset_url("abc-DEF_123") == f"{ORIGIN}/reset-password#token=abc-DEF_123"
    assert settings.password_reset_url("a b&c=d/e?f#g") == f"{ORIGIN}/reset-password#token=a+b%26c%3Dd%2Fe%3Ff%23g"
    assert (settings.PASSWORD_RESET_PATH, settings.PASSWORD_RESET_TOKEN_PARAMETER) == ("/reset-password", "token")


@pytest.mark.parametrize("value", [
    None, "", "   ", "app.example.test", "https://app.example.test/", "https://app.example.test/app", "https://app.example.test?x=1",
    "https://user@app.example.test", "ftp://app.example.test", "https://elsewhere.example.test", "http://localhost:8501",
])
def test_no_link_is_built_without_a_well_formed_allowed_customer_address(monkeypatch, value):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", ORIGIN)
    if value is None:
        monkeypatch.delenv("SORTVIEW_CUSTOMER_APP_URL", raising=False)
    else:
        monkeypatch.setenv("SORTVIEW_CUSTOMER_APP_URL", value)

    assert settings.customer_app_origin() is None
    assert settings.password_reset_url(RESET_TOKEN) is None


def test_no_host_is_written_into_the_code():
    for module in (settings, account_routes, email_service):
        source = inspect.getsource(module)
        for hardcoded in ("localhost", "127.0.0.1", "streamlit.app", "sortview.", ".com", ":8501", ":5173"):
            assert hardcoded not in source.replace("smtp.", ""), (module.__name__, hardcoded)


class _Smtp:
    sent: ClassVar[list] = []

    def __init__(self, host, port, timeout=None):
        self.where = (host, port)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, username, password):
        pass

    def send_message(self, message):
        _Smtp.sent.append(message)


@pytest.fixture
def smtp(monkeypatch):
    _Smtp.sent = []
    for name, value in _SMTP.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(email_service.smtplib, "SMTP", _Smtp)
    return _Smtp


def test_the_dashboards_own_reset_email_still_links_to_the_dashboard(monkeypatch, smtp):
    # Streamlit calls this with no reset_url, exactly as before R8A.
    monkeypatch.setenv("SORTVIEW_APP_URL", "https://dashboard.example.test/")

    email_service.send_password_reset_email(recipient_email="user@example.invalid", reset_token="abc123")

    (message,) = smtp.sent
    assert message["To"] == "user@example.invalid" and message["Subject"] == "Reset your SortView password"
    assert "https://dashboard.example.test/?reset_token=abc123" in message.get_content()
    assert "/reset-password" not in message.get_content()
    assert email_service.build_password_reset_url("abc123") == "https://dashboard.example.test/?reset_token=abc123"


def test_the_customer_apis_reset_email_links_to_the_customer_application_and_needs_no_dashboard_address(monkeypatch, smtp):
    monkeypatch.delenv("SORTVIEW_APP_URL", raising=False)
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.setenv("SORTVIEW_CUSTOMER_APP_URL", ORIGIN)

    email_service.send_password_reset_email("user@example.invalid", "abc123", reset_url=settings.password_reset_url("abc123"))

    (message,) = smtp.sent
    assert f"{ORIGIN}/reset-password#token=abc123" in message.get_content()
    # Not in a query string, in either application's form.
    assert "?token=" not in message.get_content() and "reset_token=" not in message.get_content()
    assert "?" not in message.get_content().split("/reset-password", 1)[1].split()[0]
    assert "expires in 30 minutes" in message.get_content()


def test_smtp_is_configured_only_when_every_setting_is_present(monkeypatch):
    for name, value in _SMTP.items():
        monkeypatch.setenv(name, value)
    assert email_service.smtp_configured() is True

    for name, value in _SMTP.items():
        monkeypatch.setenv(name, "")
        assert email_service.smtp_configured() is False, name
        monkeypatch.setenv(name, value)


# =====================================================================================================================
# auth_service: the two functions this block added
# =====================================================================================================================

@pytest.fixture
def audit(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(auth_service, "log_auth_event", lambda **kwargs: events.append(kwargs))
    return events


def _engine(monkeypatch, *results) -> FakeEngine:
    engine = FakeEngine([FakeQueryResult(first=result) for result in results])
    monkeypatch.setattr(auth_service, "get_engine", lambda: engine)
    return engine


def test_get_account_profile_selects_four_columns_of_one_active_user(monkeypatch):
    engine = _engine(monkeypatch, dict(PROFILE))

    assert auth_service.get_account_profile(7) == PROFILE

    (call,) = engine.calls
    sql = " ".join(call["sql"].split())
    assert sql == ("SELECT email, full_name, last_login_at, last_password_changed_at FROM app_users "
                   "WHERE id = :user_id AND is_active = TRUE LIMIT 1")
    assert call["params"] == {"user_id": 7}
    for hidden in ("password_hash", "locked_until", "failed_login", "is_platform_admin"):
        assert hidden not in sql


def test_get_account_profile_is_none_for_a_missing_or_inactive_user(monkeypatch):
    _engine(monkeypatch, None)

    assert auth_service.get_account_profile(7) is None


def test_update_profile_name_trims_stores_and_audits_without_the_name(monkeypatch, audit):
    engine = _engine(monkeypatch, {**PROFILE, "full_name": "Pat Example"})

    result = auth_service.update_profile_name(7, "  Pat Example \t")

    assert result == {"ok": True, "code": "profile_updated", "profile": {**PROFILE, "full_name": "Pat Example"}}
    (call,) = engine.calls
    sql = " ".join(call["sql"].split())
    assert sql.startswith("UPDATE app_users SET full_name = :full_name WHERE id = :user_id AND is_active = TRUE")
    assert "SET full_name = :full_name WHERE" in sql               # one column, and it is not the email
    assert call["params"] == {"user_id": 7, "full_name": "Pat Example"}
    assert audit == [{"event_type": "profile_name_updated", "is_success": True, "user_id": 7,
                      "email": "user@example.invalid", "message": "Profile name updated."}]
    assert "Pat" not in str(audit)


def test_update_profile_name_to_the_same_name_writes_and_audits_nothing(monkeypatch, audit):
    engine = _engine(monkeypatch, None, dict(PROFILE))

    result = auth_service.update_profile_name(7, "Test User")

    assert result == {"ok": True, "code": "profile_unchanged", "profile": PROFILE}
    assert len(engine.calls) == 2 and "IS DISTINCT FROM :full_name" in engine.calls[0]["sql"]
    assert audit == []


def test_update_profile_name_for_a_missing_or_inactive_user_changes_nothing(monkeypatch, audit):
    _engine(monkeypatch, None, None)

    assert auth_service.update_profile_name(7, "Pat")["code"] == "user_not_found"
    assert audit == []


@pytest.mark.parametrize(("name", "code"), [
    ("", "name_required"), ("   ", "name_required"), ("\t\n", "name_required"), (None, "name_required"), (7, "name_required"),
    ("x" * 121, "name_too_long"), ("x" * 5000, "name_too_long"),
    ("Pat\nExample", "name_invalid"), ("Pat\tExample", "name_invalid"), ("Pat\x00", "name_invalid"), ("Pat\x1b[31m", "name_invalid"),
    (f"Pat{chr(0x202E)}elpmaxE", "name_invalid"), (f"Pat{chr(0x200B)}", "name_invalid"),
])
def test_update_profile_name_refuses_a_name_before_touching_the_database(monkeypatch, audit, name, code):
    engine = _engine(monkeypatch)

    result = auth_service.update_profile_name(7, name)

    assert (result["ok"], result["code"]) == (False, code)
    assert engine.calls == [] and audit == []


@pytest.mark.parametrize("name", ["P", "x" * 120, "Renée O’Connor-Smith", "李小龍", "Dr. J. R. \"Bob\" Dobbs, Jr.", "Ana  María"])
def test_update_profile_name_accepts_ordinary_names_up_to_the_limit(monkeypatch, audit, name):
    engine = _engine(monkeypatch, {**PROFILE, "full_name": name})

    assert auth_service.update_profile_name(7, name)["ok"] is True
    assert engine.calls[0]["params"]["full_name"] == name
    assert auth_service.PROFILE_NAME_MAX_LENGTH == 120
