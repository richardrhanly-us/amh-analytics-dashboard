"""C6a M1 on a REAL PostgreSQL: the composite foreign key, upgrade and downgrade, row level security, and both Contract v2
endpoints as the runtime role.

OPT-IN AND SAFE BY CONSTRUCTION, the same convention as tests/test_ingest_v2_postgres.py: it runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server it may create and drop databases on. Each
fixture creates a brand-new throwaway database and drops it afterwards. The host must be local unless
SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also set. The API's own DATABASE_URL is never read.

Every value is SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

import main
from scripts import runtime_role_privileges as privileges
from src.services import ingest_v2_service as service

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
REVISION, PREVIOUS_HEAD = "c1e7a94f2b60", "f7b3d2c81a46"
RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL migration tests)"
)

_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
_BUILTIN_SHA256_EXPR = "encode(sha256(convert_to(:token, 'UTF8')), 'hex')"  # pgcrypto is not on every throwaway server

# org 1: customer 10, branches 1 and 4. org 2: customer 20, branch 2.
# Installations 101 and 102 at branch 1, 104 at branch 4, 201 at branch 2.
CUSTOMER, BRANCH, OTHER_BRANCH = 10, 1, 4
OTHER_CUSTOMER, OTHER_TENANT_BRANCH = 20, 2
OWNER, SAME_BRANCH_INSTALLATION, OTHER_BRANCH_INSTALLATION, OTHER_TENANT_INSTALLATION, UNUSED_INSTALLATION = 101, 102, 104, 201, 105


def _key(n: int) -> str:
    return f"{n:08x}-4d5a-4b6c-8d7e-9f0a1b2c3d4e"


OPEN_KEY, OWNED_KEY, RETIRED_OWNED_KEY, OTHER_TENANT_KEY, OTHER_TENANT_OWNED_KEY = _key(1), _key(2), _key(3), _key(4), _key(5)
UNBOUND, BOUND_OWNER, BOUND_OTHER = "CANARY-UNBOUND-3201", "CANARY-OWNER-3202", "CANARY-OTHER-3203"

client = TestClient(main.app, raise_server_exceptions=False)
KEY_REFUSAL = {"detail": main.V2_KEY_NOT_AUTHORIZED_DETAIL}


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


def _alembic(url, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True, check=False)  # nosec B603


class Throwaway:
    """A brand-new database on the test server, migrated to `revision`, dropped when the context exits."""

    def __init__(self, revision: str):
        self.revision = revision
        self.admin = make_url(ADMIN_URL)
        _guard(self.admin)
        self.name = f"sortview_key_owner_test_{secrets.token_hex(4)}"
        self.admin_engine = create_engine(self.admin, isolation_level="AUTOCOMMIT")

    def __enter__(self):
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{self.name}"'))  # nosec B608 - generated name, no user input
        self.url = self.admin.set(database=self.name)
        migrated = _alembic(self.url, "upgrade", self.revision)
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        return self

    def __exit__(self, *_exc):
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))  # nosec B608
        self.admin_engine.dispose()


@pytest.fixture(scope="module")
def cluster():
    """One database at head, its owner's engine, and a non-owning role holding exactly the recorded baseline."""
    with Throwaway("head") as db:
        owner_engine = create_engine(db.url, hide_parameters=True)
        role = f"sortview_key_owner_role_{secrets.token_hex(4)}"
        with owner_engine.begin() as conn:
            conn.execute(text(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT "  # nosec B608
                f"PASSWORD '{RUNTIME_ROLE_PASSWORD}'"
            ))
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{db.url.database}" TO {role}'))  # nosec B608
            for line in privileges.provisioning_sql(role).splitlines():
                if line and not line.startswith("--") and line not in ("BEGIN;", "COMMIT;"):
                    conn.execute(text(line))
        runtime_engine = create_engine(db.url.set(username=role, password=RUNTIME_ROLE_PASSWORD), hide_parameters=True)
        try:
            yield owner_engine, runtime_engine
        finally:
            runtime_engine.dispose()
            with owner_engine.begin() as conn:
                conn.execute(text(f"DROP OWNED BY {role}"))  # nosec B608
                conn.execute(text(f"DROP ROLE IF EXISTS {role}"))  # nosec B608
            owner_engine.dispose()


def _seed_tenants(conn) -> None:
    conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Lib A'), (20, 'Lib B')"))
    conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES "
                      "(1, 'lib-a', 'Lib A', 'active', 10), (2, 'lib-b', 'Lib B', 'active', 20)"))
    conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) VALUES "
                      "(1, 1, 'main', 'Main', 'active', 1), (4, 1, 'north', 'North', 'active', 4), "
                      "(2, 2, 'main', 'Main', 'active', 2)"))
    conn.execute(text("INSERT INTO collector_installations (id, organization_id, branch_id, name, status) VALUES "
                      "(101, 1, 1, 'Owner', 'active'), (102, 1, 1, 'Second', 'active'), (104, 1, 4, 'North', 'active'), "
                      "(105, 1, 1, 'Unused', 'active'), (201, 2, 2, 'Lib B', 'active')"))


def _insert_key(conn, key, customer, branch, *, status="active", installation_id=None) -> None:
    conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, status, retired_at, installation_id) VALUES "
                      "(:k, :c, :b, :s, CASE WHEN :s = 'retired' THEN now() END, :i)"),
                 {"k": key, "c": customer, "b": branch, "s": status, "i": installation_id})


@pytest.fixture
def owner(cluster):
    owner_engine, _runtime = cluster
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE checkin_events, reject_events, acs_item_events, ingest_key_ids, v2_cutovers, checkins, rejects, "
                          "acs_events, pipeline_status, agent_tokens, collector_installations, branches, organizations, customers "
                          "RESTART IDENTITY CASCADE"))
        _seed_tenants(conn)
        _insert_key(conn, OPEN_KEY, CUSTOMER, BRANCH)
        _insert_key(conn, OWNED_KEY, CUSTOMER, BRANCH, installation_id=OWNER)
        _insert_key(conn, RETIRED_OWNED_KEY, CUSTOMER, BRANCH, status="retired", installation_id=OWNER)
        _insert_key(conn, OTHER_TENANT_KEY, OTHER_CUSTOMER, OTHER_TENANT_BRANCH)
        _insert_key(conn, OTHER_TENANT_OWNED_KEY, OTHER_CUSTOMER, OTHER_TENANT_BRANCH, installation_id=OTHER_TENANT_INSTALLATION)
        for token, installation_id in ((UNBOUND, None), (BOUND_OWNER, OWNER), (BOUND_OTHER, SAME_BRANCH_INSTALLATION)):
            conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active, installation_id) "
                              "VALUES (:h, 10, 1, 't', TRUE, :i)"),
                         {"h": hashlib.sha256(token.encode()).hexdigest(), "i": installation_id})
    return owner_engine


@pytest.fixture
def runtime(cluster):
    return cluster[1]


@pytest.fixture
def api(owner, runtime, monkeypatch):
    monkeypatch.setattr(main, "engine", runtime)
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", True)
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR))
    return client


def _rows(engine, sql, **params):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params)]


def _set_owner(conn, key, installation_id):
    return conn.execute(text("UPDATE ingest_key_ids SET installation_id = :i WHERE key_id = :k"), {"i": installation_id, "k": key})


def _tenant_context(conn, customer, branch):
    conn.execute(text("SELECT set_config('app.operational_customer_id', :v, true)"), {"v": str(customer)})
    conn.execute(text("SELECT set_config('app.operational_branch_id', :v, true)"), {"v": str(branch)})


# =====================================================================================================================
# the schema PostgreSQL really has
# =====================================================================================================================

def test_the_column_and_the_constraints_are_what_was_approved(owner):
    assert _rows(owner, "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                        "WHERE table_name = 'ingest_key_ids' AND column_name = 'installation_id'") == [("bigint", "YES", None)]
    assert _rows(owner, "SELECT pg_get_constraintdef(oid), confdeltype, confupdtype FROM pg_constraint "
                        "WHERE conname = 'fk_ingest_key_ids_installation_branch'") == [
        ("FOREIGN KEY (installation_id, branch_id) REFERENCES collector_installations(id, branch_id)", "a", "a")]
    assert _rows(owner, "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = 'uq_collector_installations_id_branch_id'") == [("UNIQUE (id, branch_id)",)]


def test_no_index_was_added_to_the_key_table_and_only_the_required_one_to_installations(owner):
    assert {r[0] for r in _rows(owner, "SELECT indexname FROM pg_indexes WHERE tablename = 'ingest_key_ids'")} == {
        "ingest_key_ids_pkey", "ingest_key_ids_key_id_uidx", "ingest_key_ids_scope_idx"}
    assert {r[0] for r in _rows(owner, "SELECT indexname FROM pg_indexes WHERE tablename = 'collector_installations'")} == {
        "collector_installations_pkey", "ix_collector_installations_organization_id", "ix_collector_installations_branch_id",
        "uq_collector_installations_id_branch_id"}


# =====================================================================================================================
# upgrade, downgrade, and the lock timeout
# =====================================================================================================================

def test_every_existing_key_stays_unowned_and_a_round_trip_keeps_every_row():
    with Throwaway(PREVIOUS_HEAD) as db:
        engine = create_engine(db.url, hide_parameters=True)
        try:
            with engine.begin() as conn:
                _seed_tenants(conn)
                conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, health_status) VALUES "
                                  "(:a, 10, 1, 'healthy'), (:b, 20, 2, NULL)"), {"a": OPEN_KEY, "b": OTHER_TENANT_KEY})
            key_columns = ("SELECT key_id, customer_id, branch_id, algorithm, status, created_at, retired_at, health_status "
                           "FROM ingest_key_ids ORDER BY id")
            installation_columns = "SELECT id, organization_id, branch_id, name, status FROM collector_installations ORDER BY id"
            keys, installations = _rows(engine, key_columns), _rows(engine, installation_columns)

            upgraded = _alembic(db.url, "upgrade", REVISION)
            assert upgraded.returncode == 0, upgraded.stderr[-2000:]

            assert _rows(engine, "SELECT installation_id FROM ingest_key_ids") == [(None,), (None,)]
            assert (_rows(engine, key_columns), _rows(engine, installation_columns)) == (keys, installations)

            with engine.begin() as conn:
                _set_owner(conn, OPEN_KEY, OWNER)
            downgraded = _alembic(db.url, "downgrade", PREVIOUS_HEAD)
            assert downgraded.returncode == 0, downgraded.stderr[-2000:]

            assert (_rows(engine, key_columns), _rows(engine, installation_columns)) == (keys, installations)
            assert _rows(engine, "SELECT 1 FROM information_schema.columns "
                                 "WHERE table_name = 'ingest_key_ids' AND column_name = 'installation_id'") == []
            assert _rows(engine, "SELECT 1 FROM pg_constraint WHERE conname IN "
                                 "('fk_ingest_key_ids_installation_branch', 'uq_collector_installations_id_branch_id')") == []

            again = _alembic(db.url, "upgrade", REVISION)
            assert again.returncode == 0, again.stderr[-2000:]
            assert _rows(engine, "SELECT installation_id FROM ingest_key_ids") == [(None,), (None,)]
        finally:
            engine.dispose()


def test_a_blocked_migration_times_out_and_changes_nothing_and_can_then_run():
    with Throwaway(PREVIOUS_HEAD) as db:
        engine = create_engine(db.url, hide_parameters=True)
        try:
            with engine.connect() as blocker, blocker.begin():
                blocker.execute(text("LOCK TABLE collector_installations IN ACCESS SHARE MODE"))
                blocked = _alembic(db.url, "upgrade", REVISION)
            assert blocked.returncode != 0 and "lock timeout" in blocked.stderr.lower()
            assert _rows(engine, "SELECT version_num FROM alembic_version") == [(PREVIOUS_HEAD,)]
            assert _rows(engine, "SELECT 1 FROM pg_constraint WHERE conname = 'uq_collector_installations_id_branch_id'") == []

            unblocked = _alembic(db.url, "upgrade", REVISION)
            assert unblocked.returncode == 0, unblocked.stderr[-2000:]
        finally:
            engine.dispose()


# =====================================================================================================================
# the composite foreign key
# =====================================================================================================================

def test_an_owner_at_the_keys_branch_is_accepted(owner):
    with owner.begin() as conn:
        assert _set_owner(conn, OPEN_KEY, SAME_BRANCH_INSTALLATION).rowcount == 1


@pytest.mark.parametrize("installation_id", [
    pytest.param(OTHER_BRANCH_INSTALLATION, id="another-branch-of-the-tenant"),
    pytest.param(OTHER_TENANT_INSTALLATION, id="another-tenant"),
    pytest.param(999, id="nonexistent"),
])
def test_an_owner_outside_the_keys_branch_is_refused_even_for_the_table_owner(owner, installation_id):
    with pytest.raises(IntegrityError, match="fk_ingest_key_ids_installation_branch"), owner.begin() as conn:
        _set_owner(conn, OPEN_KEY, installation_id)
    with pytest.raises(IntegrityError, match="fk_ingest_key_ids_installation_branch"), owner.begin() as conn:
        _insert_key(conn, _key(9), CUSTOMER, BRANCH, installation_id=installation_id)

    assert _rows(owner, "SELECT installation_id FROM ingest_key_ids WHERE key_id = :k", k=OPEN_KEY) == [(None,)]


def test_an_owned_keys_branch_cannot_move_away_from_its_owner(owner):
    with pytest.raises(IntegrityError, match="fk_ingest_key_ids_installation_branch"), owner.begin() as conn:
        conn.execute(text("UPDATE ingest_key_ids SET branch_id = :b WHERE key_id = :k"), {"b": OTHER_BRANCH, "k": OWNED_KEY})


def test_an_owning_installation_cannot_move_to_another_branch_but_any_other_can(owner):
    move = text("UPDATE collector_installations SET branch_id = 4 WHERE id = :i")

    with pytest.raises(IntegrityError, match="fk_ingest_key_ids_installation_branch"), owner.begin() as conn:
        conn.execute(move, {"i": OWNER})
    with owner.begin() as conn:
        assert conn.execute(move, {"i": UNUSED_INSTALLATION}).rowcount == 1


def test_an_owning_installation_cannot_be_deleted_and_the_key_survives(owner):
    with pytest.raises(IntegrityError, match="fk_ingest_key_ids_installation_branch"), owner.begin() as conn:
        conn.execute(text("DELETE FROM collector_installations WHERE id = :i"), {"i": OWNER})

    assert _rows(owner, "SELECT installation_id FROM ingest_key_ids WHERE key_id = :k", k=OWNED_KEY) == [(OWNER,)]


def test_an_installation_that_owns_nothing_is_still_deleted_with_its_tokens_cascading(owner):
    with owner.begin() as conn:
        conn.execute(text("DELETE FROM collector_installations WHERE id = :i"), {"i": SAME_BRANCH_INSTALLATION})

    assert _rows(owner, "SELECT COUNT(*) FROM agent_tokens WHERE installation_id = :i", i=SAME_BRANCH_INSTALLATION) == [(0,)]
    assert _rows(owner, "SELECT COUNT(*) FROM ingest_key_ids WHERE key_id = :k", k=OPEN_KEY) == [(1,)]


def test_one_installation_may_own_several_keys_in_this_milestone(owner):
    with owner.begin() as conn:
        _set_owner(conn, OPEN_KEY, OWNER)

    assert _rows(owner, "SELECT COUNT(*) FROM ingest_key_ids WHERE installation_id = :i", i=OWNER) == [(3,)]


# =====================================================================================================================
# row level security, as the runtime role
# =====================================================================================================================

def test_the_runtime_role_sees_the_owner_only_in_its_own_tenants_context(owner, runtime):
    with runtime.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM ingest_key_ids")).scalar() == 0
        _tenant_context(conn, CUSTOMER, BRANCH)
        assert conn.execute(text("SELECT installation_id FROM ingest_key_ids WHERE key_id = :k"), {"k": OWNED_KEY}).scalar() == OWNER
        assert conn.execute(text("SELECT COUNT(*) FROM ingest_key_ids WHERE key_id = :k"), {"k": OTHER_TENANT_OWNED_KEY}).scalar() == 0


def test_the_runtime_role_cannot_change_another_tenants_owner(owner, runtime):
    with runtime.begin() as conn:
        _tenant_context(conn, CUSTOMER, BRANCH)
        assert _set_owner(conn, OTHER_TENANT_OWNED_KEY, None).rowcount == 0

    assert _rows(owner, "SELECT installation_id FROM ingest_key_ids WHERE key_id = :k", k=OTHER_TENANT_OWNED_KEY) == [(OTHER_TENANT_INSTALLATION,)]


@pytest.mark.parametrize("installation_id", [OTHER_BRANCH_INSTALLATION, OTHER_TENANT_INSTALLATION])
def test_row_level_security_does_not_stop_a_foreign_owner_on_its_own_key_but_the_foreign_key_does(owner, runtime, installation_id):
    with pytest.raises(IntegrityError, match="fk_ingest_key_ids_installation_branch"), runtime.begin() as conn:
        _tenant_context(conn, CUSTOMER, BRANCH)
        _set_owner(conn, OPEN_KEY, installation_id)


def test_the_runtime_role_cannot_move_a_key_to_another_branch(owner, runtime):
    with pytest.raises(DBAPIError, match="row-level security"), runtime.begin() as conn:
        _tenant_context(conn, CUSTOMER, BRANCH)
        conn.execute(text("UPDATE ingest_key_ids SET branch_id = :b WHERE key_id = :k"), {"b": OTHER_BRANCH, "k": OPEN_KEY})


def test_issuing_a_key_still_needs_the_tenant_context(owner, runtime):
    with pytest.raises(DBAPIError, match="row-level security"), runtime.begin() as conn:
        service.issue_ingest_key(conn, CUSTOMER, BRANCH, installation_id=None)


def test_a_key_issued_through_the_service_with_no_installation_has_no_owner(owner, runtime):
    with runtime.begin() as conn:
        _tenant_context(conn, CUSTOMER, OTHER_BRANCH)
        key = service.issue_ingest_key(conn, CUSTOMER, OTHER_BRANCH, installation_id=None)

    assert _rows(owner, "SELECT installation_id FROM ingest_key_ids WHERE key_id = :k", k=key) == [(None,)]


# =====================================================================================================================
# both endpoints, as the runtime role
# =====================================================================================================================

def _upload_body(key):
    event = {"event_key": hashlib.sha256(key.encode()).hexdigest(), "event_time": "2026-10-05T12:00:00Z",
             "item_key": hashlib.sha256(b"item").hexdigest(), "destination": "westside", "bin": "3"}
    return {"contract_version": 2, "key_id": key, "checkins": [event]}


def _status_body(key):
    return {"contract_version": 2, "key_id": key, "status": "healthy"}


ENDPOINTS = [pytest.param("/v2/upload", _upload_body, id="upload"), pytest.param("/v2/status", _status_body, id="status")]


def _post(api, path, body, token):
    return api.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


def _state(owner):
    return {
        "events": _rows(owner, "SELECT COUNT(*) FROM checkin_events"),
        "keys": _rows(owner, "SELECT key_id, status, installation_id, last_heartbeat_at, health_status FROM ingest_key_ids ORDER BY id"),
        "tokens": _rows(owner, "SELECT id, last_used_at FROM agent_tokens ORDER BY id"),
        "installations": _rows(owner, "SELECT id, status, last_seen_at, installed_at FROM collector_installations ORDER BY id"),
        "branches": _rows(owner, "SELECT id, onboarding_status FROM branches ORDER BY id"),
    }


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
@pytest.mark.parametrize(("key", "token"), [
    pytest.param(OPEN_KEY, UNBOUND, id="no-owner-unbound"),
    pytest.param(OPEN_KEY, BOUND_OWNER, id="no-owner-bound"),
    pytest.param(OWNED_KEY, BOUND_OWNER, id="owner"),
])
def test_accepted(api, owner, path, body, key, token):
    response = _post(api, path, body(key), token)

    assert response.status_code == 200, response.text
    if path == "/v2/upload":
        assert _rows(owner, "SELECT key_id FROM checkin_events") == [(key,)]
    else:
        assert _rows(owner, "SELECT last_heartbeat_at IS NOT NULL FROM ingest_key_ids WHERE key_id = :k", k=key) == [(True,)]


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
@pytest.mark.parametrize(("key", "token"), [
    pytest.param(OWNED_KEY, BOUND_OTHER, id="another-installation"),
    pytest.param(OWNED_KEY, UNBOUND, id="unbound"),
    pytest.param(RETIRED_OWNED_KEY, BOUND_OWNER, id="retired-owned"),
    pytest.param(OTHER_TENANT_KEY, UNBOUND, id="other-tenant"),
    pytest.param(OTHER_TENANT_OWNED_KEY, BOUND_OWNER, id="other-tenant-owned"),
    pytest.param(_key(99), BOUND_OWNER, id="unknown"),
])
def test_refused_with_the_generic_answer_and_nothing_written(api, owner, path, body, key, token):
    before = _state(owner)

    response = _post(api, path, body(key), token)

    assert (response.status_code, response.json()) == (403, KEY_REFUSAL)
    assert _state(owner) == before


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
@pytest.mark.parametrize("status", ["inactive", "retired"])
def test_the_owners_token_stops_with_its_installation(api, owner, path, body, status):
    with owner.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET status = :s WHERE id = :i"), {"s": status, "i": OWNER})
    before = _state(owner)

    response = _post(api, path, body(OWNED_KEY), BOUND_OWNER)

    assert response.status_code == 403 and response.json() != KEY_REFUSAL
    assert _state(owner) == before


# =====================================================================================================================
# Contract v1 is outside the check
# =====================================================================================================================

def _v1_body():
    return {"checkins": [{"customer_id": CUSTOMER, "branch_id": BRANCH, "event_time": "2026-01-01 10:00:00", "barcode": "CANARY-BARCODE-1",
                          "title": "CANARY-TITLE", "destination": "Main", "bin": "1"}]}


def test_v1_routes_do_not_consult_a_key_owner(api, owner):
    upload = _post(api, "/upload", _v1_body(), UNBOUND)
    heartbeat = _post(api, "/upload-pipeline-status", {"customer_id": CUSTOMER, "branch_id": BRANCH, "status": "ok"}, UNBOUND)

    assert upload.status_code == 200, upload.text
    assert heartbeat.status_code == 200, heartbeat.text
    assert _rows(owner, "SELECT COUNT(*) FROM checkins") == [(1,)]
