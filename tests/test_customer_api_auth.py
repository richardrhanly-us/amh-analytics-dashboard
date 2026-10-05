"""Block 3b: the browser session routes of the customer API.

    POST /api/auth/login
    POST /api/auth/logout
    GET  /api/auth/session

The credential is session_service's opaque token, carried only in an HttpOnly
cookie. These tests drive the real routes through TestClient(main.app) and
replace the services at the FLAT module identity the routes use
(services.auth_service, services.session_service) -- src.services.* would be a
second module object the routes never see.

Cookies are sent with an explicit Cookie header rather than through the
client's cookie jar: TestClient talks plain http, so a jar would refuse to
return a Secure cookie and the secure-mode tests would not be testing anything.
Every test gets its own client and a reset rate limiter, so no state carries
from one test to the next.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, datetime
from http.cookies import SimpleCookie

import pytest
from db_fakes import FakeEngine, FakeQueryResult
from fastapi.testclient import TestClient

import main
from customer_api import settings
from services import auth_service, session_service

ORIGIN = "https://app.example.test"
SECURE_COOKIE = "__Host-sortview_api_session"
INSECURE_COOKIE = "sortview_api_session"
STREAMLIT_COOKIE = "__Host-sortview_session"

USER = {"id": 7, "email": "user@example.invalid", "full_name": "Test User"}
TOKEN = "synthetic-opaque-session-token"
PASSWORD = "synthetic-Password-123"
LOGIN_BODY = {"email": "user@example.invalid", "password": PASSWORD}

INVALID_CREDENTIALS = {"code": "invalid_credentials", "message": "Invalid email or password."}
NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
ORIGIN_NOT_ALLOWED = {"code": "origin_not_allowed", "message": "Request origin is not allowed."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("SORTVIEW_LOGIN_RATE_LIMIT", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


class Services:
    """Recording stand-ins for everything the auth routes call."""

    def __init__(self, monkeypatch, *, login=None, validated=USER, revoked=True):
        self.calls: list[tuple] = []
        self.login_result = login if login is not None else {"ok": True, "user": dict(USER)}
        self.validated = validated
        self.revoked = revoked
        self.raise_on: dict[str, Exception] = {}

        monkeypatch.setattr(auth_service, "authenticate_user", self._authenticate_user)
        monkeypatch.setattr(auth_service, "log_auth_event", self._log_auth_event)
        monkeypatch.setattr(session_service, "create_session", self._create_session)
        monkeypatch.setattr(session_service, "validate_session", self._validate_session)
        monkeypatch.setattr(session_service, "revoke_session", self._revoke_session)

    def _maybe_raise(self, name):
        if name in self.raise_on:
            raise self.raise_on[name]

    def _authenticate_user(self, email, password):
        self.calls.append(("authenticate_user", email, password))
        self._maybe_raise("authenticate_user")
        return self.login_result

    def _create_session(self, user_id):
        self.calls.append(("create_session", user_id))
        self._maybe_raise("create_session")
        return {"token": TOKEN, "expires_at": datetime.now(UTC) + session_service.SESSION_LIFETIME}

    def _validate_session(self, raw_token):
        self.calls.append(("validate_session", raw_token))
        self._maybe_raise("validate_session")
        return dict(self.validated) if self.validated else None

    def _revoke_session(self, raw_token):
        self.calls.append(("revoke_session", raw_token))
        self._maybe_raise("revoke_session")
        return self.revoked

    def _log_auth_event(self, **kwargs):
        self.calls.append(("log_auth_event", kwargs))
        self._maybe_raise("log_auth_event")

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def _set_cookies(response) -> dict[str, dict]:
    """{cookie name: {"value", "attributes"}} for every Set-Cookie header."""
    found = {}
    for header in response.headers.get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        for name, morsel in jar.items():
            found[name] = {
                "value": morsel.value,
                "attributes": {key: value for key, value in morsel.items() if value not in ("", False)},
            }
    return found


def _cookie(name: str, value: str = TOKEN) -> dict[str, str]:
    return {"Cookie": f"{name}={value}"}


def _post(api, path, *, origin: str | None = ORIGIN, headers=None, **kwargs):
    merged = dict(headers or {})
    if origin is not None:
        merged["Origin"] = origin
    return api.post(path, headers=merged, **kwargs)


def _assert_cookie_cleared(response, name: str, *, secure: bool) -> None:
    cookies = _set_cookies(response)
    assert list(cookies) == [name]
    assert cookies[name]["value"] == ""
    attributes = cookies[name]["attributes"]
    assert attributes["max-age"] == "0"
    assert attributes["path"] == "/"
    assert attributes["httponly"] is True
    assert attributes["samesite"] == "lax"
    assert attributes.get("secure", False) is secure
    assert "domain" not in attributes


# =====================================================================================================================
# Login
# =====================================================================================================================

def test_login_returns_the_user_and_sets_the_session_cookie(api, monkeypatch):
    services = Services(monkeypatch)

    response = _post(api, "/api/auth/login", json=LOGIN_BODY)

    assert response.status_code == 200
    assert response.json() == USER
    assert response.headers["cache-control"] == "no-store"
    assert services.calls == [("authenticate_user", "user@example.invalid", PASSWORD), ("create_session", 7)]
    assert _set_cookies(response)[SECURE_COOKIE]["value"] == TOKEN


def test_the_session_cookie_has_exactly_the_secure_mode_attributes(api, monkeypatch):
    Services(monkeypatch)

    cookies = _set_cookies(_post(api, "/api/auth/login", json=LOGIN_BODY))

    assert list(cookies) == [SECURE_COOKIE]  # one cookie, and not the dashboard's
    assert cookies[SECURE_COOKIE]["attributes"] == {
        "httponly": True,
        "secure": True,
        "samesite": "lax",
        "path": "/",
        "max-age": str(int(session_service.SESSION_LIFETIME.total_seconds())),
    }  # in particular: no Domain, so the cookie is host-only as __Host- requires


def test_local_http_mode_uses_a_plain_cookie_name_and_no_secure_flag(api, monkeypatch):
    Services(monkeypatch)
    monkeypatch.setenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", "false")

    cookies = _set_cookies(_post(api, "/api/auth/login", json=LOGIN_BODY))

    assert list(cookies) == [INSECURE_COOKIE]  # a __Host- cookie without Secure would be refused by the browser
    assert cookies[INSECURE_COOKIE]["attributes"] == {
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "max-age": str(int(session_service.SESSION_LIFETIME.total_seconds())),
    }


def test_the_login_response_carries_only_the_three_user_fields_and_never_the_token(api, monkeypatch, caplog):
    leaky_user = dict(USER, password_hash="synthetic-hash", failed_login_attempts=3, locked_until=None)
    Services(monkeypatch, login={"ok": True, "user": leaky_user})

    with caplog.at_level(logging.DEBUG):
        response = _post(api, "/api/auth/login", json=LOGIN_BODY)

    assert set(response.json()) == {"id", "email", "full_name"}
    assert TOKEN not in response.text
    assert "synthetic-hash" not in response.text
    assert TOKEN not in caplog.text
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"ok": False, "code": "invalid_credentials", "message": "Invalid email or password."},
                     id="unknown-email-or-wrong-password"),
        pytest.param({"ok": False, "code": "inactive", "message": "This account is inactive."}, id="inactive"),
        pytest.param({"ok": False, "code": "locked", "minutes_remaining": 12,
                      "message": "Too many failed login attempts. Try again in 12 minute(s)."}, id="locked"),
    ],
)
def test_every_account_failure_gets_the_same_generic_401(api, monkeypatch, failure):
    services = Services(monkeypatch, login=failure)

    response = _post(api, "/api/auth/login", json=LOGIN_BODY)

    # Status, body and headers are identical for all three, so the response
    # cannot be used to learn whether an account exists or what state it is in.
    assert response.status_code == 401
    assert response.json() == INVALID_CREDENTIALS
    assert {k: v for k, v in response.headers.items() if k != "content-length"} == {
        "cache-control": "no-store",
        "content-type": "application/json",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
        "permissions-policy": "camera=(), microphone=(), geolocation=()",
    }
    assert response.headers["content-length"] == str(len(response.content))
    assert _set_cookies(response) == {}
    assert services.names() == ["authenticate_user"]  # no session is created for a failed login


def test_a_failing_session_creation_sets_no_cookie_and_is_a_server_error(api, monkeypatch, caplog):
    services = Services(monkeypatch)
    services.raise_on["create_session"] = RuntimeError(f"synthetic database failure quoting {TOKEN}")

    with caplog.at_level(logging.DEBUG):
        response = _post(api, "/api/auth/login", json=LOGIN_BODY)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert _set_cookies(response) == {}
    assert "Customer API request failed" in caplog.text
    assert TOKEN not in caplog.text  # the safe summary never includes the exception's own text


def test_a_database_failure_during_authentication_is_not_reported_as_bad_credentials(api, monkeypatch):
    services = Services(monkeypatch)
    services.raise_on["authenticate_user"] = RuntimeError("synthetic database failure")

    response = _post(api, "/api/auth/login", json=LOGIN_BODY)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert services.names() == ["authenticate_user"]


@pytest.mark.parametrize(
    "extra",
    [{"customer_id": 1}, {"branch_id": 1}, {"org_slug": "acme"}, {"remember_me": True}],
    ids=["customer_id", "branch_id", "org_slug", "any-other-field"],
)
def test_login_rejects_any_field_it_does_not_name(api, monkeypatch, extra):
    services = Services(monkeypatch)

    response = _post(api, "/api/auth/login", json={**LOGIN_BODY, **extra})

    assert response.status_code == 422
    assert PASSWORD not in response.text  # the hardened 422 never echoes a submitted value
    assert services.calls == []


def test_login_requires_both_fields(api, monkeypatch):
    services = Services(monkeypatch)

    assert _post(api, "/api/auth/login", json={"email": "user@example.invalid"}).status_code == 422
    assert _post(api, "/api/auth/login", json={"password": PASSWORD}).status_code == 422
    assert _post(api, "/api/auth/login", json={"email": "", "password": ""}).status_code == 422
    assert services.calls == []


def test_the_password_is_masked_in_the_request_models_repr():
    from customer_api.auth_schemas import LoginRequest

    request = LoginRequest(email="user@example.invalid", password=PASSWORD)

    assert PASSWORD not in repr(request)
    assert PASSWORD not in str(request.model_dump())


# --- the token is stored only as its hash ----------------------------------------------------------------------------

def test_the_cookie_token_reaches_the_database_only_as_its_sha256_hash(api, monkeypatch):
    # The REAL session_service.create_session, against a recording fake engine.
    engine = FakeEngine([FakeQueryResult(first={"id": 41})])
    monkeypatch.setattr(auth_service, "authenticate_user", lambda email, password: {"ok": True, "user": dict(USER)})
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)
    monkeypatch.setattr(session_service, "_log_auth_event", lambda **_kwargs: None)

    response = _post(api, "/api/auth/login", json=LOGIN_BODY)

    token = _set_cookies(response)[SECURE_COOKIE]["value"]
    assert len(token) >= 40  # an opaque random token, not a structured one
    assert token not in response.text
    (insert,) = engine.calls
    assert insert["params"]["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in str(insert)


# --- Origin guard -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "https://evil.example.test",
        "https://app.example.test.evil.example.test",   # allowed origin as a prefix
        "https://evil-app.example.test",
        "https://app.example.tes",
        "http://app.example.test",                       # same host, other scheme
        "https://app.example.test:8443",                 # same host, other port
        "https://app.example.test/",                     # not a bare origin
        "https://app.example.test/path",
        "https://user@app.example.test",
        "app.example.test",
        "",
    ],
)
@pytest.mark.parametrize("path", ["/api/auth/login", "/api/auth/logout"])
def test_state_changing_routes_refuse_a_missing_malformed_or_unlisted_origin(api, monkeypatch, path, origin):
    services = Services(monkeypatch)

    response = _post(api, path, origin=origin, json=LOGIN_BODY, headers=_cookie(SECURE_COOKIE))

    assert response.status_code == 403
    assert response.json() == ORIGIN_NOT_ALLOWED
    assert services.calls == []
    assert _set_cookies(response) == {}


def test_a_request_with_two_origin_headers_is_refused(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.post(
        "/api/auth/login", json=LOGIN_BODY, headers=[("Origin", ORIGIN), ("Origin", "https://evil.example.test")]
    )

    assert response.status_code == 403
    assert services.calls == []


def test_the_origin_host_is_matched_case_insensitively_and_nothing_else_is_normalised(api, monkeypatch):
    Services(monkeypatch)

    assert _post(api, "/api/auth/login", origin="https://APP.Example.Test", json=LOGIN_BODY).status_code == 200
    assert _post(api, "/api/auth/login", origin="https://app.example.test:443", json=LOGIN_BODY).status_code == 403


def test_with_no_configured_origin_every_state_changing_request_is_refused(api, monkeypatch):
    services = Services(monkeypatch)
    monkeypatch.delenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS")

    assert _post(api, "/api/auth/login", json=LOGIN_BODY).status_code == 403
    assert _post(api, "/api/auth/logout").status_code == 403
    assert services.calls == []


def test_the_collector_cors_allow_list_does_not_authorise_a_customer_origin(api, monkeypatch):
    services = Services(monkeypatch)
    assert "http://localhost:8501" in main.ALLOWED_ORIGINS

    response = _post(api, "/api/auth/login", origin="http://localhost:8501", json=LOGIN_BODY)

    assert response.status_code == 403
    assert services.calls == []


# --- rate limit -----------------------------------------------------------------------------------------------------------

def test_login_is_rate_limited_per_client_address(api, monkeypatch):
    Services(monkeypatch, login={"ok": False, "code": "invalid_credentials", "message": "x"})
    monkeypatch.setenv("SORTVIEW_LOGIN_RATE_LIMIT", "2/minute")

    statuses = [_post(api, "/api/auth/login", json=LOGIN_BODY).status_code for _ in range(4)]

    assert statuses == [401, 401, 429, 429]


def test_the_login_limit_does_not_spend_or_share_any_other_routes_limit(api, monkeypatch):
    Services(monkeypatch, login={"ok": False, "code": "invalid_credentials", "message": "x"})
    monkeypatch.setenv("SORTVIEW_LOGIN_RATE_LIMIT", "1/minute")

    assert _post(api, "/api/auth/login", json=LOGIN_BODY).status_code == 401
    assert _post(api, "/api/auth/login", json=LOGIN_BODY).status_code == 429

    # Other routes -- customer and collector alike -- still answer normally.
    assert api.get("/api/auth/session").status_code == 401
    assert _post(api, "/api/auth/logout").status_code == 204
    assert api.get("/").status_code == 200
    assert api.post("/upload", json={"checkins": [], "rejects": [], "acs": []}).status_code == 400


def test_the_default_login_limit_follows_the_enrollment_convention():
    # POST /collector/enroll is the existing unauthenticated, per-address route.
    assert settings.DEFAULT_LOGIN_RATE_LIMIT == "10/minute"
    assert main.ENROLL_RATE_LIMIT == os.getenv("SORTVIEW_ENROLL_RATE_LIMIT", "10/minute")


def test_an_unparseable_login_limit_falls_back_to_the_default_instead_of_no_limit(monkeypatch):
    monkeypatch.setenv("SORTVIEW_LOGIN_RATE_LIMIT", "not a rate limit")
    assert settings.login_rate_limit() == "10/minute"

    monkeypatch.setenv("SORTVIEW_LOGIN_RATE_LIMIT", "3/minute")
    assert settings.login_rate_limit() == "3/minute"

    monkeypatch.delenv("SORTVIEW_LOGIN_RATE_LIMIT")
    assert settings.login_rate_limit() == "10/minute"


# =====================================================================================================================
# Current session
# =====================================================================================================================

def test_session_without_a_cookie_is_401_and_validates_nothing(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.get("/api/auth/session")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert response.headers["cache-control"] == "no-store"
    assert services.calls == []
    assert _set_cookies(response) == {}


def test_session_with_a_valid_token_returns_the_user_without_an_origin_header(api, monkeypatch):
    services = Services(monkeypatch, validated=dict(USER, password_hash="synthetic-hash"))

    response = api.get("/api/auth/session", headers=_cookie(SECURE_COOKIE))  # no Origin: a GET needs none

    assert response.status_code == 200
    assert response.json() == USER
    assert TOKEN not in response.text
    assert services.calls == [("validate_session", TOKEN)]
    assert _set_cookies(response) == {}


def test_session_with_a_token_that_does_not_validate_is_401_and_clears_the_cookie(api, monkeypatch):
    services = Services(monkeypatch, validated=None)

    response = api.get("/api/auth/session", headers=_cookie(SECURE_COOKIE))

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert services.calls == [("validate_session", TOKEN)]
    _assert_cookie_cleared(response, SECURE_COOKIE, secure=True)


def test_an_expired_revoked_or_inactive_users_token_fails_closed_through_the_real_validation_query(api, monkeypatch):
    # The REAL session_service.validate_session. One query decides all three
    # cases -- its WHERE clause requires an unrevoked, unexpired session of an
    # active user -- and "no row" is what the route turns into 401.
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(session_service, "get_engine", lambda: engine)

    response = api.get("/api/auth/session", headers=_cookie(SECURE_COOKIE))

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    _assert_cookie_cleared(response, SECURE_COOKIE, secure=True)
    (query,) = engine.calls
    assert "s.revoked_at IS NULL" in query["sql"]
    assert "s.expires_at > :now" in query["sql"]
    assert "u.is_active = TRUE" in query["sql"]
    assert query["params"]["token_hash"] == hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()


def test_a_database_failure_during_session_validation_is_500_not_401(api, monkeypatch, caplog):
    services = Services(monkeypatch)
    services.raise_on["validate_session"] = RuntimeError(f"synthetic database failure quoting {TOKEN}")

    with caplog.at_level(logging.DEBUG):
        response = api.get("/api/auth/session", headers=_cookie(SECURE_COOKIE))

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert _set_cookies(response) == {}  # an outage must not log the user out of their browser
    assert TOKEN not in caplog.text


def test_session_ignores_the_dashboards_cookie(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.get("/api/auth/session", headers=_cookie(STREAMLIT_COOKIE))

    assert response.status_code == 401
    assert services.calls == []
    assert _set_cookies(response) == {}


def test_in_local_http_mode_the_plain_cookie_is_read_and_cleared(api, monkeypatch):
    services = Services(monkeypatch, validated=None)
    monkeypatch.setenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", "false")

    response = api.get("/api/auth/session", headers=_cookie(INSECURE_COOKIE))

    assert services.calls == [("validate_session", TOKEN)]
    _assert_cookie_cleared(response, INSECURE_COOKIE, secure=False)


def test_in_secure_mode_the_plain_cookie_name_is_not_accepted(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.get("/api/auth/session", headers=_cookie(INSECURE_COOKIE))

    assert response.status_code == 401
    assert services.calls == []


# =====================================================================================================================
# Logout
# =====================================================================================================================

LOGOUT_EVENT = {
    "event_type": "logout",
    "is_success": True,
    "user_id": 7,
    "email": "user@example.invalid",
    "message": "User logged out.",
    "metadata": {"source": "customer_api"},
}


def test_logout_revokes_the_session_audits_it_and_clears_the_cookie(api, monkeypatch):
    services = Services(monkeypatch)

    response = _post(api, "/api/auth/logout", headers=_cookie(SECURE_COOKIE))

    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert services.calls == [
        ("validate_session", TOKEN),
        ("revoke_session", TOKEN),
        ("log_auth_event", LOGOUT_EVENT),
    ]
    _assert_cookie_cleared(response, SECURE_COOKIE, secure=True)


def test_logout_without_a_cookie_is_still_204_and_clears_the_cookie(api, monkeypatch):
    services = Services(monkeypatch)

    response = _post(api, "/api/auth/logout")

    assert response.status_code == 204
    assert services.calls == []
    _assert_cookie_cleared(response, SECURE_COOKIE, secure=True)


def test_logout_with_an_already_invalid_token_is_still_204(api, monkeypatch):
    services = Services(monkeypatch, validated=None, revoked=False)

    response = _post(api, "/api/auth/logout", headers=_cookie(SECURE_COOKIE))

    assert response.status_code == 204
    # Revocation is still attempted; with no known user there is no logout event to write.
    assert services.calls == [("validate_session", TOKEN), ("revoke_session", TOKEN)]
    _assert_cookie_cleared(response, SECURE_COOKIE, secure=True)


@pytest.mark.parametrize("failing", ["validate_session", "revoke_session", "log_auth_event"])
def test_a_failure_while_ending_the_session_never_stops_the_browser_logout(api, monkeypatch, caplog, failing):
    services = Services(monkeypatch)
    services.raise_on[failing] = RuntimeError(f"synthetic failure quoting {TOKEN}")

    with caplog.at_level(logging.DEBUG):
        response = _post(api, "/api/auth/logout", headers=_cookie(SECURE_COOKIE))

    assert response.status_code == 204
    _assert_cookie_cleared(response, SECURE_COOKIE, secure=True)
    assert "revoke_session" in services.names()  # revocation is attempted even if identifying the session failed
    assert TOKEN not in caplog.text
    assert any(record.name == "sortview.customer_api" for record in caplog.records)  # logged, as a safe summary


def test_logout_leaves_the_dashboards_cookie_and_session_alone(api, monkeypatch):
    services = Services(monkeypatch)

    response = _post(
        api, "/api/auth/logout",
        headers={"Cookie": f"{STREAMLIT_COOKIE}=dashboard-token; {SECURE_COOKIE}={TOKEN}"},
    )

    assert response.status_code == 204
    assert list(_set_cookies(response)) == [SECURE_COOKIE]
    assert [call for call in services.calls if "dashboard-token" in call] == []
    assert ("revoke_session", TOKEN) in services.calls


def test_logout_with_only_the_dashboards_cookie_revokes_nothing(api, monkeypatch):
    services = Services(monkeypatch)

    response = _post(api, "/api/auth/logout", headers=_cookie(STREAMLIT_COOKIE, "dashboard-token"))

    assert response.status_code == 204
    assert services.calls == []
    assert list(_set_cookies(response)) == [SECURE_COOKIE]


# =====================================================================================================================
# Settings
# =====================================================================================================================

@pytest.mark.parametrize(
    ("value", "secure"),
    [(None, True), ("true", True), ("false", False), (" FALSE ", False), ("0", True), ("no", True), ("", True),
     ("flase", True)],
)
def test_only_the_exact_value_false_turns_the_secure_cookie_off(monkeypatch, value, secure):
    if value is None:
        monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    else:
        monkeypatch.setenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", value)

    assert settings.cookie_secure() is secure
    assert settings.session_cookie_name() == (SECURE_COOKIE if secure else INSECURE_COOKIE)
    assert settings.session_cookie_name().startswith("__Host-") is secure  # never __Host- without Secure


def test_the_cookie_name_differs_from_the_dashboards_cookie():
    assert STREAMLIT_COOKIE not in (SECURE_COOKIE, INSECURE_COOKIE)
    assert "sortview_session" not in (SECURE_COOKIE.removeprefix("__Host-"), INSECURE_COOKIE)


def test_the_cookie_lifetime_is_the_server_side_session_lifetime():
    assert settings.session_cookie_max_age_seconds() == int(session_service.SESSION_LIFETIME.total_seconds())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://app.example.test", "https://app.example.test"),
        ("HTTPS://App.Example.Test", "https://app.example.test"),
        ("http://localhost:5173", "http://localhost:5173"),
        ("https://app.example.test:443", "https://app.example.test:443"),  # an explicit port stays explicit
        ("http://[::1]:5173", "http://[::1]:5173"),
        ("https://app.example.test/", None),
        ("https://app.example.test/x", None),
        ("https://app.example.test?x=1", None),
        ("https://app.example.test#x", None),
        ("https://user:pw@app.example.test", None),
        ("https://app.example.test:notaport", None),
        ("ftp://app.example.test", None),
        ("//app.example.test", None),
        ("app.example.test", None),
        ("null", None),
        ("https://", None),
        (" https://app.example.test", None),
        ("https://app.exa\tmple.test", None),
        ("", None),
        (None, None),
    ],
)
def test_canonical_origin(value, expected):
    assert settings.canonical_origin(value) == expected


def test_allowed_origins_are_exact_and_malformed_entries_are_dropped(monkeypatch):
    monkeypatch.setenv(
        "SORTVIEW_CUSTOMER_ALLOWED_ORIGINS",
        " https://app.example.test , http://localhost:5173,https://bad.example.test/, *, ,",
    )

    assert settings.allowed_origins() == frozenset({"https://app.example.test", "http://localhost:5173"})


def test_allowed_origins_default_to_none(monkeypatch):
    monkeypatch.delenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", raising=False)

    assert settings.allowed_origins() == frozenset()
