"""C6a M2 on a REAL PostgreSQL, as the runtime role: real enrollment through both Contract v2 endpoints, the retirement and
issuance safeguards, credential recovery, unbound-token revocation, and every lock order that can race them.

A race here is made deterministic rather than hoped for: one transaction is held open (or paused behind a row another
connection has locked), the other is started, and pg_stat_activity shows it really waiting before the first is released.

OPT-IN AND SAFE BY CONSTRUCTION, the same convention as tests/test_ingest_key_ownership_postgres.py: it runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server it may create and drop databases on. A
throwaway database and role are created and dropped. The host must be local unless SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also
set. The API's own DATABASE_URL is never read. Every value is SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

import main
from scripts import create_agent_token
from scripts import runtime_role_privileges as privileges
from services import admin_installation_service as service
from services import collector_enrollment_service as enrollment
from services import collector_rules, ingest_v2_service, platform_admin_service
from services.admin_installation_service import InstallationAdminError
from services.collector_enrollment_service import EnrollmentError, EnrollmentRefused

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL tests)")

_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
_BUILTIN_SHA256_EXPR = "encode(sha256(convert_to(:token, 'UTF8')), 'hex')"  # pgcrypto is not on every throwaway server

ACTOR = {"id": 7, "email": "admin@example.invalid"}
# org 1 "acme": customer 10, branches 1 and 3. org 2 "other": customer 20, branch 2.
# At branch 1: 101 and 102 on contract 2, 103 on contract 1. 301 at branch 3. 201 at branch 2.
CUSTOMER, BRANCH, OTHER_BRANCH = 10, 1, 3
OWNER, SECOND, LEGACY, OTHER_BRANCH_INSTALLATION, OTHER_LIBRARY_INSTALLATION = 101, 102, 103, 301, 201
OWNED_KEY = "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"
TOKEN_OWNER, TOKEN_SECOND, TOKEN_UNBOUND, TOKEN_OTHER_LIBRARY = (
    "CANARY-owner-agent-token", "CANARY-second-agent-token", "CANARY-unbound-agent-token", "CANARY-other-library-token")
READY = {"collector_timezone": "America/Chicago", "collector_rules": collector_rules.rules_review_marker(
    reviewed_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC), reviewed_by_user_id=5)}

client = TestClient(main.app, raise_server_exceptions=False)
KEY_REFUSAL = {"detail": main.V2_KEY_NOT_AUTHORIZED_DETAIL}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- a throwaway database and role -----------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cluster():
    admin = make_url(ADMIN_URL)
    host = admin.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(f"refusing to run against non-local PostgreSQL host {host!r}")
    name = f"sortview_m2_test_{secrets.token_hex(4)}"
    role = f"sortview_m2_role_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    url = admin.set(database=name)
    owner = create_engine(url, hide_parameters=True, pool_size=4)
    runtime = None
    role_created = False
    try:
        migrated = subprocess.run(  # nosec B603
            [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
            env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
            capture_output=True, text=True, check=False,
        )
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        with owner.begin() as conn:
            conn.execute(text(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT "  # nosec B608
                f"PASSWORD '{RUNTIME_ROLE_PASSWORD}'"
            ))
            role_created = True
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO {role}'))  # nosec B608
            for line in privileges.provisioning_sql(role).splitlines():
                if line and not line.startswith("--") and line not in ("BEGIN;", "COMMIT;"):
                    conn.execute(text(line))
        runtime = create_engine(url.set(username=role, password=RUNTIME_ROLE_PASSWORD), hide_parameters=True,
                                pool_size=8, max_overflow=4)
        yield owner, runtime
    finally:
        if runtime is not None:
            runtime.dispose()
        if role_created:
            with owner.begin() as conn:
                conn.execute(text(f"DROP OWNED BY {role}"))  # nosec B608
                conn.execute(text(f"DROP ROLE IF EXISTS {role}"))  # nosec B608
        owner.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


def _insert_token(conn, token, customer, branch, installation_id):
    conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active, installation_id) "
                      "VALUES (:h, :c, :b, :d, TRUE, :i)"),
                 {"h": _hash(token), "c": customer, "b": branch, "d": f"synthetic {installation_id}", "i": installation_id})


@pytest.fixture
def db(cluster, monkeypatch):
    """Two libraries with installations and these tokens, and no key yet."""
    owner, runtime = cluster
    with owner.begin() as conn:
        conn.execute(text(
            "TRUNCATE auth_audit_log, tenant_lifecycle_events, v2_cutovers, ingest_key_ids, checkin_events, "
            "collector_enrollment_codes, agent_tokens, collector_installations, organization_settings, branch_settings, "
            "pipeline_status, branches, organizations, customers, app_users RESTART IDENTITY CASCADE"
        ))
        conn.execute(text("INSERT INTO app_users (id, email, full_name, is_platform_admin) "
                          "VALUES (7, 'admin@example.invalid', 'Test Admin', TRUE)"))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Acme'), (20, 'Other')"))
        conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id, created_at) VALUES "
                          "(1, 'acme', 'Acme', 'active', 10, '2026-01-01T00:00:00+00:00'), "
                          "(2, 'other', 'Other', 'active', 20, '2026-01-01T00:00:00+00:00')"))
        conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id, "
                          "onboarding_status) VALUES (1, 1, 'main', 'Main', TRUE, 'active', 1, 'ready'), "
                          "(3, 1, 'north', 'North', FALSE, 'active', 3, 'ready'), (2, 2, 'main', 'Other Main', TRUE, 'active', 2, 'ready')"))
        conn.execute(text("INSERT INTO organization_settings (organization_id, settings_json) VALUES "
                          "(1, CAST(:s AS JSONB)), (2, CAST(:s AS JSONB))"), {"s": json.dumps(READY)})
        conn.execute(text("INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract) VALUES "
                          "(101, 1, 1, 'Owner', 'active', 2), (102, 1, 1, 'Second', 'active', 2), (103, 1, 1, 'Legacy', 'active', 1), "
                          "(301, 1, 3, 'North', 'active', 2), (201, 2, 2, 'Other', 'active', 2)"))
        _insert_token(conn, TOKEN_OWNER, 10, 1, OWNER)
        _insert_token(conn, TOKEN_SECOND, 10, 1, SECOND)
        _insert_token(conn, TOKEN_UNBOUND, 10, 1, None)
        _insert_token(conn, TOKEN_OTHER_LIBRARY, 20, 2, None)
    for module in (service, platform_admin_service):
        monkeypatch.setattr(module, "get_engine", lambda: runtime)
    monkeypatch.setattr(main, "engine", runtime)
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", True)
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR))
    main.limiter.reset()
    return owner, runtime


@pytest.fixture
def owned(db):
    """OWNER owns the branch's one active key."""
    owner, runtime = db
    with owner.begin() as conn:
        _insert_key(conn, OWNED_KEY, OWNER)
    return owner, runtime


def _insert_key(conn, key, installation_id, *, status="active", customer=CUSTOMER, branch=BRANCH):
    conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, status, retired_at, installation_id) VALUES "
                      "(:k, :c, :b, :s, CASE WHEN :s = 'retired' THEN now() END, :i)"),
                 {"k": key, "c": customer, "b": branch, "s": status, "i": installation_id})


def _rows(engine, sql, **params):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def _acme():
    library = service.get_library("acme")
    assert library is not None
    return library


def _state(owner):
    """Everything these operations can change, read as the owner."""
    return {
        "tokens": _rows(owner, "SELECT id, token_hash, is_active, installation_id FROM agent_tokens ORDER BY id"),
        "keys": _rows(owner, "SELECT key_id, status, installation_id FROM ingest_key_ids ORDER BY id"),
        "installations": _rows(owner, "SELECT id, status FROM collector_installations ORDER BY id"),
        "codes": _rows(owner, "SELECT id, used_at, revoked_at FROM collector_enrollment_codes ORDER BY id"),
        "audit": _rows(owner, "SELECT event_type FROM auth_audit_log ORDER BY id"),
    }


def _active_keys(owner, customer=CUSTOMER, branch=BRANCH):
    return _rows(owner, "SELECT key_id, installation_id FROM ingest_key_ids WHERE customer_id = :c AND branch_id = :b AND status = 'active'",
                 c=customer, b=branch)


def _active_tokens(owner, installation_id):
    return [r["token_hash"] for r in _rows(
        owner, "SELECT token_hash FROM agent_tokens WHERE installation_id = :i AND is_active ORDER BY id", i=installation_id)]


def _set_status(owner, installation_id, status):
    with owner.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET status = :s WHERE id = :i"), {"s": status, "i": installation_id})


# --- both endpoints -----------------------------------------------------------------------------------------------------

def _upload_body(key):
    event = {"event_key": hashlib.sha256(secrets.token_bytes(8)).hexdigest(), "event_time": "2026-10-05T12:00:00Z",
             "item_key": hashlib.sha256(b"item").hexdigest(), "destination": "westside", "bin": "3"}
    return {"contract_version": 2, "key_id": key, "checkins": [event]}


def _status_body(key):
    return {"contract_version": 2, "key_id": key, "status": "healthy"}


ENDPOINTS = [pytest.param("/v2/upload", _upload_body, id="upload"), pytest.param("/v2/status", _status_body, id="status")]


def _post(path, body, token):
    return client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


# --- making a race deterministic -----------------------------------------------------------------------------------------

_HELD: list = []
_STARTED: list = []


@pytest.fixture(autouse=True)
def _nothing_left_holding_a_lock():
    """A test that fails part-way must not leave a row locked: the next test's TRUNCATE would wait for it forever."""
    yield
    while _HELD:
        _HELD.pop().close()
    while _STARTED:
        _STARTED.pop().thread.join(20)


class Background:
    """Runs `work` on its own thread, keeping what it returned or raised."""

    def __init__(self, work):
        self.result = self.error = None
        self.thread = threading.Thread(target=self._run, args=(work,), daemon=True)
        self.thread.start()
        _STARTED.append(self)

    def _run(self, work):
        try:
            self.result = work()
        except Exception as exc:  # the test asserts on it
            self.error = exc

    def finish(self):
        self.thread.join(20)
        assert not self.thread.is_alive(), "still running: a lock was never released"
        assert not (isinstance(self.error, DBAPIError) and "deadlock" in str(self.error).lower()), "deadlock detected"
        return self


def _waiting_on_a_lock(owner, count, *, unless=None):
    """Until `count` sessions of this database are waiting on a lock, or `unless` has finished instead."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with owner.connect() as conn:
            waiting = conn.execute(text("SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database() "
                                        "AND wait_event_type = 'Lock'")).scalar()
        if waiting >= count or (unless is not None and not unless.thread.is_alive()):
            return
        time.sleep(0.02)
    pytest.fail(f"never saw {count} session(s) waiting on a lock")


class Paused:
    """Holds a row FOR UPDATE, so a transaction that reaches it waits there, holding what it has so far."""

    def __init__(self, owner, sql, **params):
        self.conn = owner.connect()
        _HELD.append(self.conn)
        self.transaction = self.conn.begin()
        assert self.conn.execute(text(sql + " FOR UPDATE"), params).first() is not None

    def release(self):
        self.transaction.rollback()


def _pause_at_installation(owner, installation_id):
    return Paused(owner, "SELECT id FROM collector_installations WHERE id = :i", i=installation_id)


def _pause_at_a_token(owner):
    return Paused(owner, "SELECT id FROM agent_tokens WHERE token_hash = :h", h=_hash(TOKEN_UNBOUND))


class Open:
    """A transaction of the runtime role left open until commit()."""

    def __init__(self, runtime):
        self.conn = runtime.connect()
        _HELD.append(self.conn)
        self.transaction = self.conn.begin()

    def commit(self):
        self.transaction.commit()


def _tenant_context(conn, customer=CUSTOMER, branch=BRANCH):
    enrollment.set_tenant_context(conn, customer, branch)


def _issue_unowned(runtime, customer=CUSTOMER, branch=BRANCH):
    with runtime.begin() as conn:
        _tenant_context(conn, customer, branch)
        return ingest_v2_service.issue_ingest_key(conn, customer, branch, installation_id=None)


def _new_code(owner, runtime, installation_id=OWNER):
    _set_status(owner, installation_id, "provisioning")
    with runtime.begin() as conn:
        return enrollment.create_enrollment_code(conn, installation_id)["enrollment_code"]


def _redeem(runtime, code):
    with runtime.begin() as conn:
        return enrollment.redeem_enrollment_code(conn, code, contract_version=2)


def _offboard():
    return platform_admin_service.offboard_library(1, "acme", actor_user_id=7, actor_label="test")


# =====================================================================================================================
# stamping
# =====================================================================================================================

@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
def test_a_real_enrollment_owns_its_key_and_only_its_own_token_can_use_it(db, path, body):
    owner, runtime = db
    enrolled = _redeem(runtime, _new_code(owner, runtime))

    assert _active_keys(owner) == [{"key_id": enrolled["key_id"], "installation_id": OWNER}]
    assert _post(path, body(enrolled["key_id"]), enrolled["agent_token"]).status_code == 200
    for other in (TOKEN_SECOND, TOKEN_UNBOUND):
        refused = _post(path, body(enrolled["key_id"]), other)
        assert (refused.status_code, refused.json()) == (403, KEY_REFUSAL)


# =====================================================================================================================
# the retirement safeguard
# =====================================================================================================================

@pytest.mark.parametrize("start", ["provisioning", "active", "inactive"])
def test_retiring_the_owner_of_an_active_key_is_refused_with_nothing_written(owned, start):
    owner, _ = owned
    _set_status(owner, OWNER, start)
    before = _state(owner)

    with pytest.raises(InstallationAdminError, match="installation_owns_active_key"):
        service.change_status(_acme(), OWNER, status="retired", actor=ACTOR)

    assert _state(owner) == before


def test_the_guard_reads_the_key_under_row_level_security(owned, monkeypatch):
    # Without the tenant context the runtime role sees no key at all, and the retirement would go through.
    owner, _ = owned
    with pytest.raises(InstallationAdminError, match="installation_owns_active_key"):
        service.change_status(_acme(), OWNER, status="retired", actor=ACTOR)

    monkeypatch.setattr(enrollment, "set_tenant_context", lambda *_args: None)
    service.change_status(_acme(), OWNER, status="retired", actor=ACTOR)
    assert _rows(owner, "SELECT status FROM collector_installations WHERE id = :i", i=OWNER) == [{"status": "retired"}]


@pytest.mark.parametrize(("installation_id", "seed"), [
    pytest.param(OWNER, lambda conn: _insert_key(conn, OWNED_KEY, OWNER, status="retired"), id="its-key-is-retired"),
    pytest.param(OWNER, lambda conn: _insert_key(conn, OWNED_KEY, None), id="the-active-key-has-no-owner"),
    pytest.param(SECOND, lambda conn: _insert_key(conn, OWNED_KEY, OWNER), id="another-installation-owns-it"),
    pytest.param(LEGACY, lambda conn: None, id="contract-1-no-key"),
])
def test_an_installation_that_owns_no_active_key_is_still_retired(db, installation_id, seed):
    owner, _ = db
    with owner.begin() as conn:
        seed(conn)

    service.change_status(_acme(), installation_id, status="retired", actor=ACTOR)

    assert _rows(owner, "SELECT status FROM collector_installations WHERE id = :i", i=installation_id) == [{"status": "retired"}]


def test_a_key_owner_can_still_be_deactivated_and_its_token_stops(owned):
    owner, _ = owned

    service.change_status(_acme(), OWNER, status="inactive", actor=ACTOR)

    assert _rows(owner, "SELECT status FROM collector_installations WHERE id = :i", i=OWNER) == [{"status": "inactive"}]
    assert _post("/v2/status", _status_body(OWNED_KEY), TOKEN_OWNER).status_code == 403


def test_offboarding_retires_the_owner_and_its_key_together(owned):
    owner, _ = owned

    result = _offboard()

    assert result["counts"]["ingest_keys_retired"] == 1
    assert _active_keys(owner) == []
    assert _rows(owner, "SELECT status FROM collector_installations WHERE id = :i", i=OWNER) == [{"status": "retired"}]
    assert _rows(owner, "SELECT installation_id FROM ingest_key_ids WHERE key_id = :k", k=OWNED_KEY) == [{"installation_id": OWNER}]


def test_a_failed_audit_rolls_a_retirement_back(db, monkeypatch):
    owner, _ = db
    before = _state(owner)
    library = _acme()

    def fail(*_args, **_kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(service, "_audit", fail)
    with pytest.raises(RuntimeError):
        service.change_status(library, SECOND, status="retired", actor=ACTOR)

    assert _state(owner) == before


def test_a_retirement_that_commits_first_refuses_the_waiting_enrollment_and_consumes_nothing(db):
    owner, runtime = db
    code = _new_code(owner, runtime)
    library = _acme()
    pause = _pause_at_installation(owner, OWNER)

    retire = Background(lambda: service.change_status(library, OWNER, status="retired", actor=ACTOR))
    _waiting_on_a_lock(owner, 1)  # holds the branch, waits for the installation
    redeem = Background(lambda: _redeem(runtime, code))
    _waiting_on_a_lock(owner, 2)  # holds its code, waits for the branch
    pause.release()

    assert retire.finish().error is None
    assert isinstance(redeem.finish().error, EnrollmentError) and redeem.error.reason == "installation_status_retired"
    assert _active_keys(owner) == []
    assert _rows(owner, "SELECT used_at FROM collector_enrollment_codes") == [{"used_at": None}]
    assert _rows(owner, "SELECT status FROM collector_installations WHERE id = :i", i=OWNER) == [{"status": "retired"}]


def test_an_enrollment_that_commits_first_refuses_the_waiting_retirement(db):
    owner, runtime = db
    code = _new_code(owner, runtime)
    library = _acme()
    held = Open(runtime)
    enrolled = enrollment.redeem_enrollment_code(held.conn, code, contract_version=2)

    retire = Background(lambda: service.change_status(library, OWNER, status="retired", actor=ACTOR))
    _waiting_on_a_lock(owner, 1)
    held.commit()

    assert isinstance(retire.finish().error, InstallationAdminError) and retire.error.code == "installation_owns_active_key"
    assert _active_keys(owner) == [{"key_id": enrolled["key_id"], "installation_id": OWNER}]
    assert _rows(owner, "SELECT status FROM collector_installations WHERE id = :i", i=OWNER) == [{"status": "provisioning"}]


def test_a_retirement_holding_the_branch_does_not_hold_up_an_upload(owned):
    owner, _ = owned
    library = _acme()
    pause = _pause_at_installation(owner, SECOND)
    retire = Background(lambda: service.change_status(library, SECOND, status="retired", actor=ACTOR))
    _waiting_on_a_lock(owner, 1)

    upload = Background(lambda: _post("/v2/upload", _upload_body(OWNED_KEY), TOKEN_OWNER))  # inserts take FOR KEY SHARE on that branch
    upload.thread.join(10)

    assert not upload.thread.is_alive(), "the upload waited for the retirement's branch lock"
    assert upload.result.status_code == 200, upload.result.text
    assert retire.thread.is_alive()
    pause.release()
    assert retire.finish().error is None


def test_a_first_ingest_waits_for_the_branch_and_nothing_deadlocks(owned):
    # The one upload that does update the branch: it waits for a retirement holding it, and the retirement needs nothing it has.
    owner, _ = owned
    with owner.begin() as conn:
        conn.execute(text("UPDATE branches SET onboarding_status = 'pending' WHERE id = :b"), {"b": BRANCH})
    library = _acme()
    pause = _pause_at_installation(owner, SECOND)
    retire = Background(lambda: service.change_status(library, SECOND, status="retired", actor=ACTOR))
    _waiting_on_a_lock(owner, 1)

    upload = Background(lambda: _post("/v2/upload", _upload_body(OWNED_KEY), TOKEN_OWNER))
    _waiting_on_a_lock(owner, 2)
    pause.release()

    assert retire.finish().error is None
    assert upload.finish().error is None and upload.result.status_code == 200
    assert _rows(owner, "SELECT onboarding_status FROM branches WHERE id = :b", b=BRANCH) == [{"onboarding_status": "ready"}]


def test_a_retirement_and_offboarding_on_one_installation_do_not_deadlock(owned):
    owner, _ = owned
    library = _acme()
    pause = _pause_at_installation(owner, SECOND)
    retire = Background(lambda: service.change_status(library, SECOND, status="retired", actor=ACTOR))
    _waiting_on_a_lock(owner, 1)
    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 2)
    pause.release()

    retire.finish()
    assert retire.error is None or (isinstance(retire.error, InstallationAdminError) and retire.error.code == "transition_not_allowed")
    assert offboard.finish().error is None
    assert _active_keys(owner) == []
    assert {r["status"] for r in _rows(owner, "SELECT status FROM collector_installations WHERE organization_id = 1")} == {"retired"}


# =====================================================================================================================
# the issuance safeguard
# =====================================================================================================================

def test_an_unowned_key_is_refused_beside_an_active_owned_one_as_the_runtime_role(owned):
    owner, runtime = owned

    with pytest.raises(ValueError, match="active key owned by an installation"):
        _issue_unowned(runtime)

    assert _active_keys(owner) == [{"key_id": OWNED_KEY, "installation_id": OWNER}]


def test_the_operators_role_is_refused_too_and_another_branch_is_not(owned):
    owner, _ = owned

    with pytest.raises(ValueError, match="active key owned by an installation"), owner.begin() as conn:
        ingest_v2_service.issue_ingest_key(conn, CUSTOMER, BRANCH, installation_id=None)
    with owner.begin() as conn:
        assert ingest_v2_service.issue_ingest_key(conn, CUSTOMER, OTHER_BRANCH, installation_id=None)


def test_a_failed_issue_rolls_back_and_releases_its_locks(db):
    owner, runtime = db

    with pytest.raises(RuntimeError), runtime.begin() as conn:
        _tenant_context(conn)
        ingest_v2_service.issue_ingest_key(conn, CUSTOMER, BRANCH, installation_id=None)
        raise RuntimeError("failed after the insert")

    assert _active_keys(owner) == []
    assert _issue_unowned(runtime)  # nothing was left locked


def test_an_enrollment_that_commits_first_refuses_the_waiting_unowned_key(db):
    owner, runtime = db
    code = _new_code(owner, runtime)
    held = Open(runtime)
    enrolled = enrollment.redeem_enrollment_code(held.conn, code, contract_version=2)

    issue = Background(lambda: _issue_unowned(runtime))
    _waiting_on_a_lock(owner, 1)
    held.commit()

    assert isinstance(issue.finish().error, ValueError) and "owned by an installation" in str(issue.error)
    assert _active_keys(owner) == [{"key_id": enrolled["key_id"], "installation_id": OWNER}]


def test_an_unowned_key_that_commits_first_refuses_the_waiting_enrollment(db):
    owner, runtime = db
    code = _new_code(owner, runtime)
    held = Open(runtime)
    _tenant_context(held.conn)
    key = ingest_v2_service.issue_ingest_key(held.conn, CUSTOMER, BRANCH, installation_id=None)

    redeem = Background(lambda: _redeem(runtime, code))
    _waiting_on_a_lock(owner, 1)
    held.commit()

    assert isinstance(redeem.finish().error, EnrollmentRefused) and redeem.error.reason == "active_ingest_key_exists"
    assert _active_keys(owner) == [{"key_id": key, "installation_id": None}]
    assert _rows(owner, "SELECT used_at FROM collector_enrollment_codes") == [{"used_at": None}]


# =====================================================================================================================
# offboarding against key issuance: no active key survives a completed offboarding
# =====================================================================================================================

def test_an_unowned_key_issued_first_is_waited_for_and_retired_by_offboarding(db):
    # The race: without the organization's lock, offboarding does not wait, its sweep cannot see the uncommitted key, and the
    # key then commits active on a cancelled library.
    owner, runtime = db
    held = Open(runtime)
    _tenant_context(held.conn)
    ingest_v2_service.issue_ingest_key(held.conn, CUSTOMER, BRANCH, installation_id=None)

    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 1, unless=offboard)
    held.commit()

    assert offboard.finish().error is None
    assert _active_keys(owner) == []
    assert offboard.result["counts"]["ingest_keys_retired"] == 1


def test_offboarding_that_commits_first_refuses_the_waiting_unowned_key(db):
    owner, runtime = db
    pause = _pause_at_a_token(owner)
    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 1)  # has cancelled the library, uncommitted; waits at its token sweep

    issue = Background(lambda: _issue_unowned(runtime))
    _waiting_on_a_lock(owner, 2)
    pause.release()

    assert offboard.finish().error is None
    assert isinstance(issue.finish().error, ValueError) and "cancelled" in str(issue.error)
    assert _rows(owner, "SELECT key_id FROM ingest_key_ids") == []


CONTRACTS = [pytest.param(OWNER, 2, id="contract-2"), pytest.param(LEGACY, None, id="contract-1")]


def _live_tokens(owner):
    """What the purge's own check counts as an active token of the library."""
    return _rows(owner, "SELECT id FROM agent_tokens WHERE is_active AND (customer_id = :c OR installation_id IN "
                        "(SELECT id FROM collector_installations WHERE organization_id = 1))", c=CUSTOMER)


@pytest.mark.parametrize(("installation_id", "contract"), CONTRACTS)
def test_offboarding_waits_for_an_enrollment_in_flight_and_leaves_no_active_token_or_key(db, installation_id, contract):
    owner, runtime = db
    code = _new_code(owner, runtime, installation_id)
    held = Open(runtime)
    enrollment.redeem_enrollment_code(held.conn, code, contract_version=contract)

    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 1)
    held.commit()

    assert offboard.finish().error is None
    assert _live_tokens(owner) == []
    assert _active_keys(owner) == []


@pytest.mark.parametrize(("installation_id", "contract"), CONTRACTS)
def test_an_enrollment_reaching_an_offboarding_in_progress_is_refused_and_writes_nothing(db, installation_id, contract):
    # Offboarding has the organization and is about to reach the codes: an enrollment holding its code here would deadlock.
    owner, runtime = db
    code = _new_code(owner, runtime, installation_id)
    pause = _pause_at_a_token(owner)
    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 1)  # has cancelled the library, uncommitted; waits at its token sweep

    def redeem():
        with runtime.begin() as conn:
            return enrollment.redeem_enrollment_code(conn, code, contract_version=contract)

    redemption = Background(redeem)
    _waiting_on_a_lock(owner, 2, unless=redemption)
    pause.release()

    assert offboard.finish().error is None
    assert isinstance(redemption.finish().error, EnrollmentError) and redemption.error.reason == "revoked"
    assert _live_tokens(owner) == []
    assert _active_keys(owner) == []
    assert _rows(owner, "SELECT used_at FROM collector_enrollment_codes") == [{"used_at": None}]


def _script_transaction(conn):
    """What scripts/create_agent_token.py does in its one transaction."""
    create_agent_token.validate_token_scope(conn, CUSTOMER, BRANCH)
    conn.execute(text(create_agent_token._INSERT_SQL), {"token_hash": _hash("CANARY-script-token"), "customer_id": CUSTOMER,
                                                        "branch_id": BRANCH, "description": "synthetic script token"})


def test_offboarding_waits_for_the_token_script_and_leaves_no_active_token(db):
    owner, _ = db
    held = Open(owner)
    _script_transaction(held.conn)

    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 1, unless=offboard)
    held.commit()

    assert offboard.finish().error is None
    assert _live_tokens(owner) == []


def test_the_token_script_reaching_an_offboarding_in_progress_is_refused_and_writes_nothing(db):
    owner, _ = db
    pause = _pause_at_a_token(owner)
    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 1)

    def script():
        with owner.begin() as conn:
            _script_transaction(conn)

    run = Background(script)
    _waiting_on_a_lock(owner, 2, unless=run)
    pause.release()

    assert offboard.finish().error is None
    assert isinstance(run.finish().error, create_agent_token.ScopeError) and "cancelled" in str(run.error)
    assert _rows(owner, "SELECT id FROM agent_tokens WHERE token_hash = :h", h=_hash("CANARY-script-token")) == []
    assert _live_tokens(owner) == []


def test_a_heartbeat_offboarding_and_a_waiting_enrollment_all_finish(db):
    # The heartbeat holds its token and offboarding waits for it holding the organization, so the enrollment waits behind
    # offboarding. The heartbeat then updates its installation, which nobody holds yet.
    owner, runtime = db
    code = _new_code(owner, runtime, SECOND)
    held = Open(runtime)
    token_row = main.authenticate_agent_token(held.conn, f"Bearer {TOKEN_OWNER}")

    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 1)
    redemption = Background(lambda: _redeem(runtime, code))
    _waiting_on_a_lock(owner, 2, unless=redemption)
    main.record_installation_heartbeat(held.conn, token_row, OWNER, None)
    held.commit()

    assert offboard.finish().error is None
    assert isinstance(redemption.finish().error, EnrollmentError) and redemption.error.reason == "revoked"
    assert _live_tokens(owner) == []


def test_a_live_heartbeat_and_upload_do_not_wait_for_an_enrollment_holding_the_organization(owned):
    owner, runtime = owned
    code = _new_code(owner, runtime, LEGACY)
    _set_status(owner, OWNER, "active")
    held = Open(runtime)
    enrollment.redeem_enrollment_code(held.conn, code)

    for path, body in (("/v2/status", _status_body(OWNED_KEY)), ("/v2/upload", _upload_body(OWNED_KEY))):
        request = Background(lambda path=path, body=body: _post(path, body, TOKEN_OWNER))
        request.thread.join(10)
        assert not request.thread.is_alive(), f"{path} waited for the enrollment"
        assert request.result.status_code == 200, request.result.text
    held.commit()


def test_a_recovery_waits_for_an_enrollment_in_flight_and_neither_deadlocks(db):
    owner, runtime = db
    code = _new_code(owner, runtime)
    library = _acme()
    held = Open(runtime)
    enrolled = enrollment.redeem_enrollment_code(held.conn, code, contract_version=2)

    recover = Background(lambda: service.issue_recovery_token(library, OWNER, actor=ACTOR))
    _waiting_on_a_lock(owner, 1, unless=recover)
    held.commit()

    assert recover.finish().error is None
    assert _active_tokens(owner, OWNER) == [_hash(recover.result["agent_token"])]
    assert _active_keys(owner) == [{"key_id": enrolled["key_id"], "installation_id": OWNER}]


def test_a_revocation_waits_for_an_enrollment_in_flight_and_neither_deadlocks(db):
    owner, runtime = db
    code = _new_code(owner, runtime)
    (token_id,) = [r["id"] for r in _rows(owner, "SELECT id FROM agent_tokens WHERE token_hash = :h", h=_hash(TOKEN_UNBOUND))]
    library = _acme()
    held = Open(runtime)
    enrollment.redeem_enrollment_code(held.conn, code, contract_version=2)

    revoke = Background(lambda: service.revoke_unbound_token(library, token_id, actor=ACTOR))
    _waiting_on_a_lock(owner, 1, unless=revoke)
    held.commit()

    assert revoke.finish().error is None
    assert _rows(owner, "SELECT is_active FROM agent_tokens WHERE id = :i", i=token_id) == [{"is_active": False}]


def test_an_enrollment_after_offboarding_is_refused(db):
    owner, runtime = db
    code = _new_code(owner, runtime)

    _offboard()

    with pytest.raises(EnrollmentError):
        _redeem(runtime, code)
    assert _active_keys(owner) == []


# =====================================================================================================================
# credential recovery
# =====================================================================================================================

@pytest.mark.parametrize("start", ["provisioning", "active"])
def test_recovery_leaves_one_active_bound_token_and_touches_no_other(owned, start):
    owner, _ = owned
    _set_status(owner, OWNER, start)
    others = _rows(owner, "SELECT id, is_active FROM agent_tokens WHERE installation_id IS DISTINCT FROM :i ORDER BY id", i=OWNER)

    issued = service.issue_recovery_token(_acme(), OWNER, actor=ACTOR)

    assert issued["installation"] == {"id": OWNER, "name": "Owner"} and issued["revoked_previous_count"] == 1
    assert _active_tokens(owner, OWNER) == [_hash(issued["agent_token"])]
    assert _rows(owner, "SELECT id, is_active FROM agent_tokens WHERE installation_id IS DISTINCT FROM :i ORDER BY id", i=OWNER) == others
    assert _rows(owner, "SELECT customer_id, branch_id FROM agent_tokens WHERE token_hash = :h", h=_hash(issued["agent_token"])) == [
        {"customer_id": CUSTOMER, "branch_id": BRANCH}]
    assert _active_keys(owner) == [{"key_id": OWNED_KEY, "installation_id": OWNER}]


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
def test_after_recovery_the_new_token_works_and_the_old_one_is_refused(owned, path, body):
    issued = service.issue_recovery_token(_acme(), OWNER, actor=ACTOR)

    assert _post(path, body(OWNED_KEY), issued["agent_token"]).status_code == 200
    assert (_post(path, body(OWNED_KEY), TOKEN_OWNER).status_code, _post(path, body(OWNED_KEY), TOKEN_OWNER).json()) == (
        403, {"detail": "Agent token is inactive"})


def test_the_raw_token_and_its_hash_reach_no_audit_row_log_or_description(owned, caplog):
    owner, _ = owned

    with caplog.at_level(logging.DEBUG):
        issued = service.issue_recovery_token(_acme(), OWNER, actor=ACTOR)

    raw, hashed = issued["agent_token"], _hash(issued["agent_token"])
    (audit,) = _rows(owner, "SELECT event_type, message, metadata FROM auth_audit_log")
    (token,) = _rows(owner, "SELECT id, description FROM agent_tokens WHERE token_hash = :h", h=hashed)
    assert audit == {"event_type": "admin_recovery_token_issued", "message": "Collector recovery token issued.",
                     "metadata": {"target_org_slug": "acme", "installation_id": OWNER, "token_id": token["id"],
                                  "revoked_previous_count": 1, "actor_user_id": 7}}
    for text_seen in (json.dumps(audit), caplog.text, token["description"]):
        assert raw not in text_seen and hashed not in text_seen
    assert raw not in json.dumps(_rows(owner, "SELECT * FROM agent_tokens"), default=str)


@pytest.mark.parametrize(("code", "installation_id", "seed"), [
    pytest.param("installation_not_recoverable", OWNER, lambda c: c.execute(text("UPDATE collector_installations SET status = 'inactive' WHERE id = 101")), id="inactive"),
    pytest.param("installation_not_recoverable", OWNER, lambda c: c.execute(text("UPDATE collector_installations SET status = 'retired' WHERE id = 101")), id="retired"),
    pytest.param("installation_not_recoverable", OWNER, lambda c: c.execute(text("UPDATE ingest_key_ids SET installation_id = NULL")), id="the-key-has-no-owner"),
    pytest.param("installation_not_recoverable", OWNER, lambda c: c.execute(text("UPDATE ingest_key_ids SET status = 'retired', retired_at = now()")), id="the-key-is-retired"),
    pytest.param("installation_not_recoverable", OWNER, lambda c: _insert_key(c, "8d9e0f1a-2b3c-4d4e-9f5a-6b7c8d9e0f1a", None), id="a-second-active-key"),
    pytest.param("installation_not_recoverable", SECOND, lambda c: None, id="another-installation-owns-it"),
    pytest.param("installation_not_recoverable", LEGACY, lambda c: None, id="contract-1"),
    pytest.param("installation_not_recoverable", OWNER, lambda c: c.execute(text("UPDATE organizations SET status = 'suspended' WHERE id = 1")), id="suspended-library"),
    pytest.param("installation_not_found", OTHER_LIBRARY_INSTALLATION, lambda c: None, id="another-librarys-installation"),
    pytest.param("installation_not_found", 999, lambda c: None, id="unknown"),
])
def test_recovery_is_refused_with_nothing_written(owned, code, installation_id, seed):
    owner, _ = owned
    library = _acme()
    with owner.begin() as conn:
        seed(conn)
    before = _state(owner)

    with pytest.raises(InstallationAdminError, match=code):
        service.issue_recovery_token(library, installation_id, actor=ACTOR)

    assert _state(owner) == before


def test_recovery_reads_the_key_under_row_level_security_and_fails_closed_without_the_context(owned, monkeypatch):
    owner, _ = owned
    before = _state(owner)
    monkeypatch.setattr(enrollment, "_set_tenant_context", lambda conn, scope: None)

    with pytest.raises(InstallationAdminError, match="installation_not_recoverable"):
        service.issue_recovery_token(_acme(), OWNER, actor=ACTOR)

    assert _state(owner) == before


@pytest.mark.parametrize(("target", "attribute"), [
    pytest.param(service, "_audit", id="the-audit-write"),
    pytest.param(enrollment, "new_agent_token", id="creating-the-token"),
    pytest.param(enrollment, "active_ingest_key_owners", id="verifying-ownership"),
])
def test_a_failure_at_any_step_of_recovery_rolls_everything_back(owned, monkeypatch, target, attribute):
    owner, _ = owned
    before = _state(owner)
    library = _acme()

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(target, attribute, fail)
    with pytest.raises(RuntimeError):
        service.issue_recovery_token(library, OWNER, actor=ACTOR)

    assert _state(owner) == before
    assert _post("/v2/status", _status_body(OWNED_KEY), TOKEN_OWNER).status_code == 200


def test_recovery_waits_for_a_heartbeat_holding_its_token_and_neither_deadlocks(owned):
    # The heartbeat's real order: its token, then the installation. Recovery locking the installation first would deadlock here.
    owner, runtime = owned
    library = _acme()
    held = Open(runtime)
    token_row = main.authenticate_agent_token(held.conn, f"Bearer {TOKEN_OWNER}")

    recover = Background(lambda: service.issue_recovery_token(library, OWNER, actor=ACTOR))
    _waiting_on_a_lock(owner, 1)
    main.record_installation_heartbeat(held.conn, token_row, OWNER, None)
    held.commit()

    assert recover.finish().error is None
    assert _active_tokens(owner, OWNER) == [_hash(recover.result["agent_token"])]


def test_two_recoveries_serialize_and_leave_one_active_token(owned):
    owner, _ = owned
    library = _acme()
    pause = _pause_at_installation(owner, OWNER)
    first = Background(lambda: service.issue_recovery_token(library, OWNER, actor=ACTOR))
    _waiting_on_a_lock(owner, 1)
    second = Background(lambda: service.issue_recovery_token(library, OWNER, actor=ACTOR))
    _waiting_on_a_lock(owner, 2)
    pause.release()

    assert first.finish().error is None and second.finish().error is None
    assert len(_active_tokens(owner, OWNER)) == 1
    assert _active_tokens(owner, OWNER)[0] in {_hash(first.result["agent_token"]), _hash(second.result["agent_token"])}


def test_recovery_that_commits_first_has_its_token_revoked_by_the_waiting_offboarding(owned):
    owner, _ = owned
    library = _acme()
    pause = _pause_at_installation(owner, OWNER)
    recover = Background(lambda: service.issue_recovery_token(library, OWNER, actor=ACTOR))
    _waiting_on_a_lock(owner, 1)
    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 2)
    pause.release()

    assert recover.finish().error is None and offboard.finish().error is None
    assert _active_tokens(owner, OWNER) == []
    assert _active_keys(owner) == []


def test_offboarding_that_commits_first_refuses_the_waiting_recovery(owned):
    owner, _ = owned
    library = _acme()  # read before the library was cancelled
    pause = _pause_at_a_token(owner)
    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 1)
    recover = Background(lambda: service.issue_recovery_token(library, OWNER, actor=ACTOR))
    _waiting_on_a_lock(owner, 2)
    pause.release()

    assert offboard.finish().error is None
    assert isinstance(recover.finish().error, InstallationAdminError) and recover.error.code == "library_cancelled"
    assert _active_tokens(owner, OWNER) == []


def test_a_retirement_and_a_recovery_on_one_owner_do_not_deadlock_and_the_owner_stays(owned):
    owner, _ = owned
    library = _acme()
    pause = _pause_at_installation(owner, OWNER)
    recover = Background(lambda: service.issue_recovery_token(library, OWNER, actor=ACTOR))
    _waiting_on_a_lock(owner, 1)
    retire = Background(lambda: service.change_status(library, OWNER, status="retired", actor=ACTOR))
    time.sleep(0.2)  # refused from the key alone, so it may or may not wait
    pause.release()

    assert recover.finish().error is None
    assert isinstance(retire.finish().error, InstallationAdminError) and retire.error.code == "installation_owns_active_key"
    assert _rows(owner, "SELECT status FROM collector_installations WHERE id = :i", i=OWNER) == [{"status": "active"}]


def test_a_request_past_the_token_check_may_still_finish_which_is_why_this_is_not_an_upload_fence(owned):
    # The documented limitation until C6a M7: the check takes no lock, so a request that passed it completes after the commit.
    owner, _ = owned
    library = _acme()
    pause = _pause_at_installation(owner, OWNER)
    recover = Background(lambda: service.issue_recovery_token(library, OWNER, actor=ACTOR))
    _waiting_on_a_lock(owner, 1)  # holds the old token's row, uncommitted

    in_flight = Background(lambda: _post("/v2/status", _status_body(OWNED_KEY), TOKEN_OWNER))
    _waiting_on_a_lock(owner, 2)  # past the check, waiting to stamp last_used_at
    pause.release()

    assert recover.finish().error is None
    assert in_flight.finish().result.status_code == 200
    assert _post("/v2/status", _status_body(OWNED_KEY), TOKEN_OWNER).status_code == 403


# =====================================================================================================================
# legacy unbound tokens
# =====================================================================================================================

def test_only_this_librarys_active_unbound_tokens_are_listed_and_never_a_hash(db):
    owner, _ = db
    with owner.begin() as conn:
        _insert_token(conn, "CANARY-revoked-unbound", 10, 1, None)
        conn.execute(text("UPDATE agent_tokens SET is_active = FALSE WHERE token_hash = :h"), {"h": _hash("CANARY-revoked-unbound")})
        conn.execute(text("UPDATE agent_tokens SET last_used_at = CURRENT_TIMESTAMP WHERE token_hash = :h"), {"h": _hash(TOKEN_UNBOUND)})

    (token,) = service.list_unbound_tokens(_acme())

    assert set(token) == {"id", "description", "branch_name", "created_at", "last_used_at"}
    assert token["branch_name"] == "Main"
    assert token["created_at"].tzinfo is not None and token["last_used_at"].tzinfo is not None
    assert _hash(TOKEN_UNBOUND) not in repr(token)
    assert _rows(owner, "SELECT id FROM agent_tokens WHERE token_hash = :h", h=_hash(TOKEN_UNBOUND)) == [{"id": token["id"]}]


def test_revoking_stops_only_that_token_and_is_audited_with_its_row_id_alone(owned):
    owner, _ = owned
    with owner.begin() as conn:
        _insert_token(conn, "CANARY-another-unbound", 10, 1, None)
    (token_id,) = [r["id"] for r in _rows(owner, "SELECT id FROM agent_tokens WHERE token_hash = :h", h=_hash(TOKEN_UNBOUND))]

    assert _post("/upload-pipeline-status", {"customer_id": CUSTOMER, "branch_id": BRANCH, "status": "ok"}, TOKEN_UNBOUND).status_code == 200
    service.revoke_unbound_token(_acme(), token_id, actor=ACTOR)

    refused = _post("/upload-pipeline-status", {"customer_id": CUSTOMER, "branch_id": BRANCH, "status": "ok"}, TOKEN_UNBOUND)
    assert (refused.status_code, refused.json()) == (403, {"detail": "Agent token is inactive"})
    for still_works in (TOKEN_OWNER, "CANARY-another-unbound"):
        assert _post("/upload-pipeline-status", {"customer_id": CUSTOMER, "branch_id": BRANCH, "status": "ok"}, still_works).status_code == 200
    (audit,) = _rows(owner, "SELECT event_type, metadata FROM auth_audit_log")
    assert audit == {"event_type": "admin_unbound_token_revoked",
                     "metadata": {"target_org_slug": "acme", "token_id": token_id, "actor_user_id": 7}}
    assert _hash(TOKEN_UNBOUND) not in json.dumps(audit)


@pytest.mark.parametrize("token", [
    pytest.param(TOKEN_OWNER, id="bound"),
    pytest.param(TOKEN_OTHER_LIBRARY, id="another-library"),
    pytest.param("inactive", id="already-inactive"),
    pytest.param("unknown", id="unknown"),
])
def test_anything_but_an_active_unbound_token_of_this_library_is_refused_alike(db, token):
    owner, _ = db
    if token == "inactive":
        with owner.begin() as conn:
            conn.execute(text("UPDATE agent_tokens SET is_active = FALSE WHERE token_hash = :h"), {"h": _hash(TOKEN_UNBOUND)})
    lookup = TOKEN_UNBOUND if token == "inactive" else token
    found = _rows(owner, "SELECT id FROM agent_tokens WHERE token_hash = :h", h=_hash(lookup))
    token_id = found[0]["id"] if found else 99999
    before = _state(owner)

    with pytest.raises(InstallationAdminError, match="token_not_found"):
        service.revoke_unbound_token(_acme(), token_id, actor=ACTOR)

    assert _state(owner) == before


def test_a_failed_audit_rolls_a_revocation_back(db, monkeypatch):
    owner, _ = db
    (token_id,) = [r["id"] for r in _rows(owner, "SELECT id FROM agent_tokens WHERE token_hash = :h", h=_hash(TOKEN_UNBOUND))]
    before = _state(owner)
    library = _acme()

    def fail(*_args, **_kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(service, "_audit", fail)
    with pytest.raises(RuntimeError):
        service.revoke_unbound_token(library, token_id, actor=ACTOR)

    assert _state(owner) == before


def test_offboarding_that_commits_first_refuses_the_waiting_revocation(db):
    owner, _ = db
    (token_id,) = [r["id"] for r in _rows(owner, "SELECT id FROM agent_tokens WHERE token_hash = :h", h=_hash(TOKEN_UNBOUND))]
    library = _acme()
    pause = _pause_at_a_token(owner)
    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 1)
    revoke = Background(lambda: service.revoke_unbound_token(library, token_id, actor=ACTOR))
    _waiting_on_a_lock(owner, 2)
    pause.release()

    assert offboard.finish().error is None
    assert isinstance(revoke.finish().error, InstallationAdminError) and revoke.error.code == "library_cancelled"
