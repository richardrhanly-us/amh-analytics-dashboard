"""R8A: the account API on a REAL PostgreSQL -- the migrated schema, real hashes, real sessions, real tokens.

The requests go through the real production routes (TestClient(main.app)) and NOTHING between a request and the rows is
replaced except the one function that would send an email: sign-in, the session cookie, services.auth_service,
services.session_service and every one of their statements run against the database.

What only a real server can prove:

  * a name change is one column of one row, and the email beside it is exactly as it was;
  * a password change or reset really replaces the hash, really revokes EVERY session of that user and no one else's,
    and really leaves an audit row that holds no password and no token;
  * a reset token is stored only as a hash, works once, stops working when it expires, and is replaced by a newer one;
  * a reset request writes a token for an active account and nothing at all for an unknown or inactive one, while
    answering all three identically;
  * none of this needs an organization: the database here has NO organizations, memberships or branches at all.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_efficiency_settings_postgres.py: runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION, local server. The module creates its
own throwaway database (migrated with the project's real Alembic chain) and drops it afterward. Production is never
touched, and no email is ever sent.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from werkzeug.security import check_password_hash, generate_password_hash

import database
import main
from services import email_service

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL account tests)"
)

ORIGIN = "https://app.example.invalid"
COOKIE = "sortview_api_session"      # plain-HTTP mode: TestClient is not HTTPS, so the __Host- cookie cannot be used

# Database ids. Distinctive, so none can be in a response by accident; never sent in a request.
PAT, SAM, GONE = 9101, 9102, 9103
PAT_EMAIL, SAM_EMAIL, GONE_EMAIL = "pat@example.invalid", "sam@example.invalid", "gone@example.invalid"
OLD = "synthetic-Old-Password-1"
NEW = "synthetic-New-Password-2"
SAM_PASSWORD = "synthetic-Sam-Password-3"

RESET_REQUESTED = {
    "code": "password_reset_requested",
    "message": "If an active account exists for that email address, password reset instructions will be sent.",
}
INVALID_RESET_TOKEN = {"code": "invalid_reset_token", "message": "This password reset link is invalid or has expired."}
NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}


def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


@pytest.fixture(scope="module")
def engine():
    admin = make_url(ADMIN_URL)
    _guard(admin)
    name = f"sortview_account_test_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    url = admin.set(database=name)
    created = create_engine(url, hide_parameters=True)
    try:
        migrated = subprocess.run(  # nosec B603
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        yield created
    finally:
        created.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


class Mail:
    """Stands in for the one function that sends email, and keeps what it was asked to send."""

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, recipient_email, reset_token, *, reset_url=None):
        self.sent.append({"to": recipient_email, "token": reset_token, "url": reset_url})

    @property
    def token(self) -> str:
        return self.sent[-1]["token"]


@pytest.fixture
def db(engine, monkeypatch):
    with engine.begin() as conn:
        # Users only. No organization, membership or branch exists: nothing about an account needs one.
        conn.execute(text(
            "TRUNCATE password_reset_tokens, auth_sessions, auth_audit_log, memberships, branches, organizations, app_users "
            "RESTART IDENTITY CASCADE"
        ))
        for user_id, email, name, password, active in (
            (PAT, PAT_EMAIL, "Pat Example", OLD, True),
            (SAM, SAM_EMAIL, "Sam Example", SAM_PASSWORD, True),
            (GONE, GONE_EMAIL, "Gone Example", OLD, False),
        ):
            conn.execute(
                text("INSERT INTO app_users (id, email, full_name, password_hash, is_active) VALUES (:id, :email, :name, :hash, :active)"),
                {"id": user_id, "email": email, "name": name, "hash": generate_password_hash(password), "active": active},
            )
    # The one flat engine every service uses.
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.setenv("SORTVIEW_CUSTOMER_APP_URL", ORIGIN)
    monkeypatch.setenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", "false")
    for name in ("SORTVIEW_SMTP_HOST", "SORTVIEW_SMTP_USERNAME", "SORTVIEW_SMTP_PASSWORD", "SORTVIEW_EMAIL_FROM"):
        monkeypatch.setenv(name, "synthetic-not-a-real-setting")
    return engine


@pytest.fixture
def mail(monkeypatch):
    stand_in = Mail()
    monkeypatch.setattr(email_service, "send_password_reset_email", stand_in.send)
    return stand_in


@pytest.fixture
def api(db, mail):
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


def _post(api, path, body, session: str | None = None):
    headers = {"Origin": ORIGIN}
    if session is not None:
        headers["Cookie"] = f"{COOKIE}={session}"
    return api.post(path, json=body, headers=headers)


def _login(api, email, password):
    return _post(api, "/api/auth/login", {"email": email, "password": password})


def _sign_in(api, email=PAT_EMAIL, password=OLD) -> str:
    """Signs in for real and returns the session token from the cookie."""
    main.limiter.reset()
    response = _login(api, email, password)
    assert response.status_code == 200, response.text
    jar = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        jar.load(header)
    return jar[COOKIE].value


def _account(api, session: str):
    return api.get("/api/account", headers={"Cookie": f"{COOKIE}={session}"})


def _instant(written: str) -> datetime:
    """The instant an API timestamp names. It must be written in UTC."""
    instant = datetime.fromisoformat(written)
    assert written.endswith("Z") and instant.utcoffset() == timedelta(0), written
    return instant


def _row(db, user_id=PAT) -> dict:
    with db.connect() as conn:
        return dict(conn.execute(text("SELECT * FROM app_users WHERE id = :id"), {"id": user_id}).mappings().one())


def _events(db, user_id=PAT) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT event_type, is_success, email, message, metadata FROM auth_audit_log WHERE user_id = :id ORDER BY id"),
            {"id": user_id},
        ).mappings().all()
    return [dict(row) for row in rows]


def _live_sessions(db, user_id=PAT) -> int:
    with db.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM auth_sessions WHERE user_id = :id AND revoked_at IS NULL"), {"id": user_id}
        ).scalar_one()


def _tokens(db, user_id=PAT) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT token_hash, used_at, expires_at FROM password_reset_tokens WHERE user_id = :id ORDER BY id"), {"id": user_id}
        ).mappings().all()
    return [dict(row) for row in rows]


def _everything_stored(db) -> str:
    """Every audit row and every token row, as text: for checking that a secret is in none of them."""
    with db.connect() as conn:
        audit = conn.execute(text("SELECT * FROM auth_audit_log")).mappings().all()
        tokens = conn.execute(text("SELECT * FROM password_reset_tokens")).mappings().all()
        sessions = conn.execute(text("SELECT * FROM auth_sessions")).mappings().all()
    return repr([dict(row) for row in (*audit, *tokens, *sessions)])


# =====================================================================================================================
# The account, and its name
# =====================================================================================================================

def test_the_account_is_the_signed_in_users_own_and_needs_no_organization(api, db):
    session = _sign_in(api)

    response = _account(api, session)

    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["email", "full_name", "last_login_at", "last_password_changed_at"]
    assert (body["email"], body["full_name"]) == (PAT_EMAIL, "Pat Example")
    # Signing in a moment ago set last_login_at; the password has never been changed through the application.
    assert body["last_login_at"] is not None and body["last_login_at"].endswith("Z")
    assert _instant(body["last_login_at"]) == _row(db)["last_login_at"]
    assert body["last_password_changed_at"] is None
    for hidden in (str(PAT), "password_hash", "scrypt", "pbkdf2", "is_platform_admin", "locked"):
        assert hidden not in response.text
    with db.connect() as conn:
        assert conn.execute(text("SELECT (SELECT COUNT(*) FROM organizations) + (SELECT COUNT(*) FROM memberships)")).scalar_one() == 0


@pytest.mark.parametrize("zone", ["UTC", "America/Chicago", "Asia/Tokyo", "Asia/Kolkata", "Pacific/Auckland"])
def test_account_timestamps_are_utc_whatever_time_zone_the_database_session_is_in(api, db, monkeypatch, zone):
    # A real sign-in and a real password change, so both columns hold instants the server itself stamped.
    session = _sign_in(api)
    assert _post(api, "/api/account/change-password",
                 {"current_password": OLD, "new_password": NEW, "confirm_password": NEW}, session=session).status_code == 204
    session = _sign_in(api, PAT_EMAIL, NEW)
    stored = _row(db)

    # The same database through a connection whose SESSION is set to another zone: the driver now hands every
    # TIMESTAMPTZ back with that zone's offset.
    shifted = create_engine(db.url, hide_parameters=True, connect_args={"options": f"-c timezone={zone}"})
    monkeypatch.setattr(database, "_engine", shifted)
    try:
        with shifted.connect() as conn:
            assert conn.execute(text("SHOW timezone")).scalar_one() == zone
            raw = conn.execute(text("SELECT last_login_at FROM app_users WHERE id = :id"), {"id": PAT}).scalar_one()
        response = _account(api, session)
        renamed = api.put("/api/account/profile", json={"full_name": f"Pat in {zone}"},
                          headers={"Origin": ORIGIN, "Cookie": f"{COOKIE}={session}"})
    finally:
        shifted.dispose()

    # What the driver returned is in the session's zone (the control: this is what used to reach the browser) ...
    if zone != "UTC":
        assert raw.utcoffset() != timedelta(0), zone
    # ... and what the API answers is the same instants, written in UTC, from both routes that return the account.
    for answer in (response, renamed):
        assert answer.status_code == 200, answer.text
        body = answer.json()
        for field in ("last_login_at", "last_password_changed_at"):
            assert body[field].endswith("Z"), (zone, field, body[field])
            assert _instant(body[field]) == stored[field], (zone, field)


def test_timestamps_that_have_never_happened_are_null_in_any_session_zone(api, db, monkeypatch):
    # Sam has signed in (to get a session) but never changed a password.
    session = _sign_in(api, SAM_EMAIL, SAM_PASSWORD)
    shifted = create_engine(db.url, hide_parameters=True, connect_args={"options": "-c timezone=Asia/Tokyo"})
    monkeypatch.setattr(database, "_engine", shifted)
    try:
        with shifted.begin() as conn:
            conn.execute(text("UPDATE app_users SET last_login_at = NULL WHERE id = :id"), {"id": SAM})
        body = _account(api, session).json()
    finally:
        shifted.dispose()

    assert (body["last_login_at"], body["last_password_changed_at"]) == (None, None)


def test_a_name_change_persists_trimmed_and_touches_nothing_else(api, db):
    session = _sign_in(api)
    before, sam_before = _row(db), _row(db, SAM)

    response = api.put("/api/account/profile", json={"full_name": "  Patricia Example-Ruiz  "},
                       headers={"Origin": ORIGIN, "Cookie": f"{COOKIE}={session}"})

    assert response.status_code == 200
    assert response.json()["full_name"] == "Patricia Example-Ruiz" and response.json()["email"] == PAT_EMAIL
    after = _row(db)
    assert after["full_name"] == "Patricia Example-Ruiz"
    # Every other column of the row, the email and the password hash included, is exactly as it was.
    assert {key: value for key, value in after.items() if key != "full_name"} == {
        key: value for key, value in before.items() if key != "full_name"}
    assert _row(db, SAM) == sam_before
    # The session is still good, and the change is audited without either name.
    assert _account(api, session).json()["full_name"] == "Patricia Example-Ruiz"
    (event,) = [event for event in _events(db) if event["event_type"] == "profile_name_updated"]
    assert (event["is_success"], event["email"], event["metadata"]) == (True, PAT_EMAIL, {})
    assert "Patricia" not in repr(event) and "Pat Example" not in repr(event)


def test_saving_the_same_name_again_writes_no_second_audit_row(api, db):
    session = _sign_in(api)
    headers = {"Origin": ORIGIN, "Cookie": f"{COOKIE}={session}"}

    for _ in range(3):
        assert api.put("/api/account/profile", json={"full_name": "Pat Renamed"}, headers=headers).status_code == 200

    assert [event["event_type"] for event in _events(db)].count("profile_name_updated") == 1


@pytest.mark.parametrize(("name", "why"), [("   ", "required"), ("x" * 121, "too_long"), ("Pat\nExample", "invalid_characters")])
def test_a_refused_name_changes_nothing(api, db, name, why):
    session = _sign_in(api)

    response = api.put("/api/account/profile", json={"full_name": name}, headers={"Origin": ORIGIN, "Cookie": f"{COOKIE}={session}"})

    assert response.status_code == 422
    assert response.json()["problems"] == [{"field": "full_name", "code": why}]
    assert _row(db)["full_name"] == "Pat Example"
    assert "profile_name_updated" not in [event["event_type"] for event in _events(db)]


def test_a_name_of_exactly_the_longest_length_is_stored_whole(api, db):
    session = _sign_in(api)
    longest = "é" * 120

    assert api.put("/api/account/profile", json={"full_name": longest},
                   headers={"Origin": ORIGIN, "Cookie": f"{COOKIE}={session}"}).status_code == 200
    assert _row(db)["full_name"] == longest


# =====================================================================================================================
# Changing the password
# =====================================================================================================================

def test_changing_the_password_replaces_the_hash_and_revokes_every_session_of_that_user_only(api, db):
    first, second = _sign_in(api), _sign_in(api)
    sam = _sign_in(api, SAM_EMAIL, SAM_PASSWORD)
    old_hash = _row(db)["password_hash"]
    assert _live_sessions(db) == 2

    response = _post(api, "/api/account/change-password",
                     {"current_password": OLD, "new_password": NEW, "confirm_password": NEW}, session=first)

    assert response.status_code == 204
    row = _row(db)
    assert row["password_hash"] != old_hash and check_password_hash(row["password_hash"], NEW)
    assert row["last_password_changed_at"] is not None and row["email"] == PAT_EMAIL
    # Every session Pat had is gone -- the one that made the request too. Sam's is untouched.
    assert _live_sessions(db) == 0
    for session in (first, second):
        assert (_account(api, session).status_code, _account(api, session).json()) == (401, NOT_AUTHENTICATED)
    assert _account(api, sam).status_code == 200 and _live_sessions(db, SAM) == 1
    # The old password no longer signs in; the new one does.
    main.limiter.reset()
    assert _login(api, PAT_EMAIL, OLD).status_code == 401
    assert _login(api, PAT_EMAIL, NEW).status_code == 200
    (event,) = [event for event in _events(db) if event["event_type"] == "password_change_success"]
    assert event["metadata"] == {"revoked_session_count": 2}
    stored = _everything_stored(db)
    assert OLD not in stored and NEW not in stored


@pytest.mark.parametrize(("body", "field", "why"), [
    ({"current_password": "not-the-password", "new_password": NEW, "confirm_password": NEW}, "current_password", "incorrect"),
    ({"current_password": OLD, "new_password": "short", "confirm_password": "short"}, "new_password", "too_short"),
    ({"current_password": OLD, "new_password": NEW, "confirm_password": NEW + "x"}, "confirm_password", "mismatch"),
    ({"current_password": OLD, "new_password": OLD, "confirm_password": OLD}, "new_password", "same_as_current"),
])
def test_a_refused_password_change_leaves_the_hash_and_the_session_alone(api, db, body, field, why):
    session = _sign_in(api)
    old_hash = _row(db)["password_hash"]

    response = _post(api, "/api/account/change-password", body, session=session)

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_password_change", "message": "The password could not be changed.",
                               "problems": [{"field": field, "code": why}]}
    assert _row(db)["password_hash"] == old_hash and _row(db)["last_password_changed_at"] is None
    assert _account(api, session).status_code == 200 and _live_sessions(db) == 1
    assert [event["event_type"] for event in _events(db)][-1] == "password_change_failed"
    stored = _everything_stored(db)
    for secret in (OLD, NEW, "not-the-password"):
        assert secret not in stored


# =====================================================================================================================
# Asking for a reset
# =====================================================================================================================

def test_a_reset_request_stores_only_a_hash_and_emails_a_link_to_the_customer_application(api, db, mail):
    response = _post(api, "/api/auth/password-reset/request", {"email": "PAT@example.invalid"})

    assert (response.status_code, response.json()) == (202, RESET_REQUESTED)
    (sent,) = mail.sent
    link = urlsplit(sent["url"])
    assert sent["to"] == PAT_EMAIL
    # The token is in the link's fragment -- never sent to a server -- and its query string is empty.
    assert (f"{link.scheme}://{link.netloc}", link.path, link.query) == (ORIGIN, "/reset-password", "")
    assert parse_qs(link.fragment) == {"token": [sent["token"]]}
    # The database holds the token's SHA-256 and nothing that could be turned back into it.
    (stored,) = _tokens(db)
    assert stored["token_hash"] == hashlib.sha256(sent["token"].encode()).hexdigest()
    assert stored["used_at"] is None and stored["expires_at"] is not None
    assert sent["token"] not in _everything_stored(db) and sent["token"] not in response.text


def test_an_unknown_and_an_inactive_address_get_the_same_answer_and_nothing_is_stored_or_sent(api, db, mail):
    known = _post(api, "/api/auth/password-reset/request", {"email": PAT_EMAIL})
    unknown = _post(api, "/api/auth/password-reset/request", {"email": "nobody@example.invalid"})
    inactive = _post(api, "/api/auth/password-reset/request", {"email": GONE_EMAIL})

    for response in (unknown, inactive):
        assert (response.status_code, response.content, dict(response.headers)) == (known.status_code, known.content, dict(known.headers))
    assert [sent["to"] for sent in mail.sent] == [PAT_EMAIL]
    assert _tokens(db, GONE) == []
    with db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM password_reset_tokens")).scalar_one() == 1


def test_a_newer_request_replaces_the_older_link(api, db, mail):
    _post(api, "/api/auth/password-reset/request", {"email": PAT_EMAIL})
    first = mail.token
    _post(api, "/api/auth/password-reset/request", {"email": PAT_EMAIL})
    second = mail.token

    assert first != second
    stale = _post(api, "/api/auth/password-reset/complete", {"token": first, "new_password": NEW, "confirm_password": NEW})
    assert (stale.status_code, stale.json()) == (400, INVALID_RESET_TOKEN)
    assert _post(api, "/api/auth/password-reset/complete",
                 {"token": second, "new_password": NEW, "confirm_password": NEW}).status_code == 204


# =====================================================================================================================
# Completing a reset
# =====================================================================================================================

def test_completing_a_reset_changes_the_password_revokes_every_session_and_works_once(api, db, mail):
    first, second = _sign_in(api), _sign_in(api)
    sam = _sign_in(api, SAM_EMAIL, SAM_PASSWORD)
    _post(api, "/api/auth/password-reset/request", {"email": PAT_EMAIL})
    token = mail.token

    response = _post(api, "/api/auth/password-reset/complete", {"token": token, "new_password": NEW, "confirm_password": NEW})

    assert response.status_code == 204 and response.headers.get_list("set-cookie") == []
    row = _row(db)
    assert check_password_hash(row["password_hash"], NEW) and row["last_password_changed_at"] is not None
    assert _live_sessions(db) == 0 and _account(api, first).status_code == _account(api, second).status_code == 401
    assert _account(api, sam).status_code == 200
    main.limiter.reset()
    assert _login(api, PAT_EMAIL, OLD).status_code == 401 and _login(api, PAT_EMAIL, NEW).status_code == 200
    # Single use: the same link, again, is the one generic answer and changes nothing.
    again = _post(api, "/api/auth/password-reset/complete",
                  {"token": token, "new_password": "synthetic-Third-Password-4", "confirm_password": "synthetic-Third-Password-4"})
    assert (again.status_code, again.json()) == (400, INVALID_RESET_TOKEN)
    assert check_password_hash(_row(db)["password_hash"], NEW)
    assert _tokens(db)[0]["used_at"] is not None
    (event,) = [event for event in _events(db) if event["event_type"] == "password_reset_success"]
    assert event["metadata"] == {"revoked_session_count": 2}
    stored = _everything_stored(db)
    for secret in (token, OLD, NEW):
        assert secret not in stored


def test_an_expired_a_made_up_and_an_inactive_accounts_token_are_all_the_same_refusal(api, db, mail):
    _post(api, "/api/auth/password-reset/request", {"email": PAT_EMAIL})
    expired = mail.token
    with db.begin() as conn:
        conn.execute(text("UPDATE password_reset_tokens SET expires_at = now() - interval '1 second'"))
        # A token that exists and has not expired, for an account that is no longer active.
        conn.execute(
            text("INSERT INTO password_reset_tokens (user_id, token_hash, expires_at) VALUES (:id, :hash, now() + interval '10 minutes')"),
            {"id": GONE, "hash": hashlib.sha256(b"token-of-an-inactive-account").hexdigest()},
        )
    old_hash = _row(db)["password_hash"]

    answers = [
        _post(api, "/api/auth/password-reset/complete", {"token": token, "new_password": NEW, "confirm_password": NEW})
        for token in (expired, "a-token-nobody-was-ever-sent", "token-of-an-inactive-account", " ")
    ]

    for answer in answers:
        assert (answer.status_code, answer.json()) == (400, INVALID_RESET_TOKEN)
    assert len({answer.content for answer in answers}) == 1
    assert _row(db)["password_hash"] == old_hash and _row(db, GONE)["is_active"] is False


@pytest.mark.parametrize(("new", "confirm", "field", "why"), [
    ("short", "short", "new_password", "too_short"),
    (NEW, NEW + "x", "confirm_password", "mismatch"),
    (OLD, OLD, "new_password", "same_as_current"),
])
def test_a_refused_new_password_does_not_use_up_the_link(api, db, mail, new, confirm, field, why):
    _post(api, "/api/auth/password-reset/request", {"email": PAT_EMAIL})
    token = mail.token

    refused = _post(api, "/api/auth/password-reset/complete", {"token": token, "new_password": new, "confirm_password": confirm})

    assert refused.status_code == 422
    assert refused.json()["problems"] == [{"field": field, "code": why}]
    assert _tokens(db)[0]["used_at"] is None
    # The same link still works with an acceptable password.
    assert _post(api, "/api/auth/password-reset/complete",
                 {"token": token, "new_password": NEW, "confirm_password": NEW}).status_code == 204


def test_a_reset_unlocks_an_account_that_was_locked_out(api, db, mail):
    with db.begin() as conn:
        conn.execute(text("UPDATE app_users SET failed_login_attempts = 5, locked_until = now() + interval '15 minutes' WHERE id = :id"),
                     {"id": PAT})
    assert _login(api, PAT_EMAIL, OLD).status_code == 401

    _post(api, "/api/auth/password-reset/request", {"email": PAT_EMAIL})
    assert _post(api, "/api/auth/password-reset/complete",
                 {"token": mail.token, "new_password": NEW, "confirm_password": NEW}).status_code == 204

    row = _row(db)
    assert (row["failed_login_attempts"], row["locked_until"]) == (0, None)
    main.limiter.reset()
    assert _login(api, PAT_EMAIL, NEW).status_code == 200


def test_the_dashboards_reset_token_can_be_completed_here_and_the_other_way_round(api, db, mail):
    # The dashboard starts a reset by calling the same service directly. Its token is the same kind of token.
    from services import auth_service

    started_by_the_dashboard = auth_service.request_password_reset(PAT_EMAIL)["reset_token"]
    assert _post(api, "/api/auth/password-reset/complete",
                 {"token": started_by_the_dashboard, "new_password": NEW, "confirm_password": NEW}).status_code == 204

    # And one this API started can be completed by the dashboard's own call.
    _post(api, "/api/auth/password-reset/request", {"email": PAT_EMAIL})
    result = auth_service.reset_password_with_token(mail.token, "synthetic-Third-Password-4", "synthetic-Third-Password-4")
    assert result["ok"] is True
    assert check_password_hash(_row(db)["password_hash"], "synthetic-Third-Password-4")
