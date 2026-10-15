"""A2 item 6 on a REAL PostgreSQL: suspending, reactivating and offboarding a library from the administration API.

The lifecycle requests go through the real admin routes (TestClient(main.app)) as a signed-in platform administrator,
and the consequences are observed through the real customer API, the real v1 Collector routes, Collector enrollment and
main's own agent authentication -- all as a NON-OWNING, NOBYPASSRLS role holding exactly the recorded runtime baseline
(scripts/runtime_role_privileges.py). Nothing is replaced but the engines and the token-hash expression. What only a
real server can prove:

  * suspension changes organizations.status and NOTHING else: sessions, memberships, tokens, installations, codes, keys,
    cutovers, subscriptions and data stay exactly as they were, members keep read-only access, and the Collector is
    refused at once (v1 upload, v1 heartbeat, Contract v2 agent authentication, enrollment);
  * reactivation returns the library to active (never trial) and every preserved credential works again; a token that was
    already inactive stays inactive, and no credential is created;
  * offboarding through the admin path is the same cutoff Super Admin performs -- and DELETES NOTHING -- with the
    lifecycle evidence naming the administrator as platform_admin:<id>, never by e-mail address;
  * the change, its auth_audit_log row and (for an offboarding) its lifecycle evidence commit or roll back together;
  * another library is never touched.

OPT-IN AND SAFE BY CONSTRUCTION, as tests/test_admin_installations_postgres.py: runs only when SORTVIEW_TEST_POSTGRES_URL
points at a maintenance database on a NON-PRODUCTION, local server; a throwaway database and role are created and
dropped. Every value is SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

import database
import main
from scripts import runtime_role_privileges as privileges
from services import (
    collector_enrollment_service,
    platform_admin_service,
    session_service,
)

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL tests)")

RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only
ADMIN_ORIGIN = "https://admin.example.invalid"
CUSTOMER_ORIGIN = "https://app.example.invalid"
SESSION_COOKIE = "sortview_api_session"  # plain-HTTP mode: TestClient is not HTTPS

ADMIN_ID, OWNER_ID, SHARED_ID, OTHER_ID = 7, 21, 22, 23
ADMIN_EMAIL = "canary.admin@example.invalid"
OWNER_EMAIL, SHARED_EMAIL, OTHER_EMAIL = "owner@example.invalid", "shared@example.invalid", "other@example.invalid"

TOKEN_BOUND = "synthetic-CANARY-bound-agent-token-acme-101"
TOKEN_LEGACY = "synthetic-CANARY-legacy-agent-token-acme"
TOKEN_INACTIVE = "synthetic-CANARY-inactive-agent-token-acme"
TOKEN_OTHER = "synthetic-CANARY-bound-agent-token-other-301"
TOKENS = (TOKEN_BOUND, TOKEN_LEGACY, TOKEN_INACTIVE, TOKEN_OTHER)
ACME_KEY = "a1a1a1a1-1111-4111-8111-111111111111"
OTHER_KEY = "c3c3c3c3-3333-4333-8333-333333333333"
CANARY_HOST = "CANARY-HOST-AMH-PC"
CANARY_BARCODE = "CANARY-BARCODE-31234000777"

_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
_BUILTIN_SHA256_EXPR = "encode(sha256(convert_to(:token, 'UTF8')), 'hex')"  # pgcrypto is not on every throwaway server

# Everything a lifecycle action must leave exactly as it was (suspension: every one of these and the access artifacts).
UNTOUCHED = ("customers", "branches", "branch_settings", "organization_settings", "memberships", "app_users",
             "subscriptions", "v2_cutovers", "checkins", "pipeline_status")
ACCESS_ARTIFACTS = ("agent_tokens", "collector_installations", "collector_enrollment_codes", "ingest_key_ids",
                    "auth_sessions")


def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def cluster():
    """One database at head, its owner's engine, and a non-owning role holding exactly the recorded baseline."""
    admin = make_url(ADMIN_URL)
    _guard(admin)
    name = f"sortview_admin_lifecycle_test_{secrets.token_hex(4)}"
    role = f"sortview_admin_lifecycle_role_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    url = admin.set(database=name)
    owner = create_engine(url, hide_parameters=True)
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
        runtime = create_engine(url.set(username=role, password=RUNTIME_ROLE_PASSWORD), hide_parameters=True)
        yield owner, runtime
    finally:
        if runtime is not None:
            runtime.dispose()
        if role_created:
            with owner.begin() as conn:  # roles are cluster-wide: never left behind
                conn.execute(text(f"DROP OWNED BY {role}"))  # nosec B608
                conn.execute(text(f"DROP ROLE IF EXISTS {role}"))  # nosec B608
        owner.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


def _seed(conn) -> None:
    """Four libraries. acme (active) and tri (trial) are acted on; other (active) must never be touched; closed is
    already cancelled. Operational customer ids never equal the organization ids."""
    conn.execute(text(
        "INSERT INTO app_users (id, email, full_name, is_platform_admin) VALUES "
        f"({ADMIN_ID}, '{ADMIN_EMAIL}', 'Test Admin', TRUE), "  # nosec B608 - constants
        f"({OWNER_ID}, '{OWNER_EMAIL}', 'Acme Owner', FALSE), "
        f"({SHARED_ID}, '{SHARED_EMAIL}', 'Shared Owner', FALSE), "
        f"({OTHER_ID}, '{OTHER_EMAIL}', 'Other Owner', FALSE)"
    ))
    conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Acme'), (20, 'Tri'), (30, 'Other')"))
    conn.execute(text(
        "INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES "
        "(1, 'acme', 'Acme Library', 'active', 10), (2, 'tri', 'Tri Library', 'trial', 20), "
        "(3, 'other', 'Other Library', 'active', 30), (4, 'closed', 'Closed Library', 'cancelled', NULL)"
    ))
    conn.execute(text(
        "INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) VALUES "
        "(1, 1, 'main', 'Acme Main', TRUE, 'active', 1), (2, 2, 'main', 'Tri Main', TRUE, 'active', 2), "
        "(3, 3, 'main', 'Other Main', TRUE, 'active', 3), (4, 4, 'main', 'Closed Main', TRUE, 'active', NULL)"
    ))
    conn.execute(text(
        "INSERT INTO subscriptions (organization_id, plan_id, status) "
        "SELECT o.id, (SELECT id FROM plans ORDER BY id LIMIT 1), 'trial' FROM organizations o"
    ))
    conn.execute(text(
        "INSERT INTO organization_settings (organization_id, settings_json) VALUES (1, CAST(:s AS JSONB))"
    ), {"s": json.dumps({"collector_timezone": "America/Chicago"})})
    conn.execute(text(
        "INSERT INTO memberships (organization_id, user_id, role) VALUES "
        f"(1, {ADMIN_ID}, 'viewer'), (1, {OWNER_ID}, 'owner'), (1, {SHARED_ID}, 'owner'), "  # nosec B608 - constants
        f"(3, {SHARED_ID}, 'viewer'), (3, {OTHER_ID}, 'owner'), (2, {OTHER_ID}, 'owner')"
    ))
    conn.execute(text(
        "INSERT INTO collector_installations (id, organization_id, branch_id, name, hostname, status, required_contract, "
        "installed_at) VALUES "
        f"(101, 1, 1, 'Acme Sorter', '{CANARY_HOST}', 'active', 2, now()), "  # nosec B608 - constant
        "(102, 1, 1, 'Acme Spare', NULL, 'provisioning', 1, NULL), "
        "(103, 1, 1, 'Acme Old', NULL, 'retired', 1, NULL), "
        "(301, 3, 3, 'Other Sorter', NULL, 'active', 2, now())"
    ))
    for raw, customer, branch, active, installation in (
        (TOKEN_BOUND, 10, 1, True, 101), (TOKEN_LEGACY, 10, 1, True, None), (TOKEN_INACTIVE, 10, 1, False, None),
        (TOKEN_OTHER, 30, 3, True, 301),
    ):
        conn.execute(text(
            "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active, installation_id) "
            "VALUES (:h, :c, :b, 'synthetic', :a, :i)"
        ), {"h": _sha256(raw), "c": customer, "b": branch, "a": active, "i": installation})
    conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id) VALUES (:a, 10, 1), (:o, 30, 3)"),
                 {"a": ACME_KEY, "o": OTHER_KEY})
    conn.execute(text(
        "INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, note) VALUES "
        "(10, 1, '2026-01-01T00:00:00+00:00', 'collector-enrollment', 'enrolled'), "
        "(30, 3, '2026-01-01T00:00:00+00:00', 'collector-enrollment', 'enrolled')"
    ))
    conn.execute(text(
        "INSERT INTO checkins (customer_id, branch_id, event_time, barcode) VALUES "
        f"(10, 1, '2026-10-01 10:00:00', '{CANARY_BARCODE}'), (30, 3, '2026-10-01 10:00:00', 'other-item')"  # nosec B608
    ))


@pytest.fixture
def db(cluster, monkeypatch):
    owner, runtime = cluster
    with owner.begin() as conn:
        conn.execute(text(
            "TRUNCATE tenant_lifecycle_events, auth_audit_log, auth_sessions, checkins, pipeline_status, v2_cutovers, "
            "ingest_key_ids, collector_enrollment_codes, agent_tokens, collector_installations, memberships, "
            "subscriptions, organization_settings, branch_settings, branches, organizations, customers, app_users "
            "RESTART IDENTITY CASCADE"
        ))
        _seed(conn)
    monkeypatch.setattr(database, "_engine", runtime)  # the one flat engine every service uses
    monkeypatch.setattr(main, "engine", runtime)
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR))
    monkeypatch.setenv("SORTVIEW_ADMIN_ALLOWED_ORIGINS", ADMIN_ORIGIN)
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", CUSTOMER_ORIGIN)
    monkeypatch.setenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", "false")
    return owner, runtime


@pytest.fixture
def api(db):
    main.limiter.reset()
    yield TestClient(main.app, raise_server_exceptions=False)
    main.limiter.reset()


@pytest.fixture
def sessions(db):
    """A live session for each user, created through the real session service."""
    return {user_id: session_service.create_session(user_id)["token"]
            for user_id in (ADMIN_ID, OWNER_ID, SHARED_ID, OTHER_ID)}


# --- helpers ---------------------------------------------------------------------------------------------------------

def _rows(engine, sql, **params):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def _table(owner, table):
    return _rows(owner, f"SELECT * FROM {table} ORDER BY 1")  # nosec B608 - constant table names


def _snapshot(owner, tables):
    return {table: _table(owner, table) for table in tables}


def _status(owner, slug="acme"):
    return _rows(owner, "SELECT status FROM organizations WHERE slug = :s", s=slug)[0]["status"]


def _audit(owner):
    return _rows(owner, "SELECT user_id, email, event_type, is_success, message, metadata FROM auth_audit_log "
                        "WHERE event_type LIKE 'admin_library_%' ORDER BY id")


def _events(owner):
    return _rows(owner, "SELECT * FROM tenant_lifecycle_events ORDER BY id")


def _lifecycle(api, sessions, action, body=None, slug="acme"):
    headers = {"Origin": ADMIN_ORIGIN, "Cookie": f"{SESSION_COOKIE}={sessions[ADMIN_ID]}"}
    url = f"/api/admin/libraries/{slug}/lifecycle/{action}"
    return api.post(url, json=body, headers=headers) if body is not None else api.post(url, headers=headers)


def _customer_get(api, sessions, user_id, path):
    return api.get(path, headers={"Cookie": f"{SESSION_COOKIE}={sessions[user_id]}"})


def _change_role(api, sessions, role="admin"):
    return api.put("/api/organizations/acme/members/role", json={"email": SHARED_EMAIL, "role": role},
                   headers={"Origin": CUSTOMER_ORIGIN, "Cookie": f"{SESSION_COOKIE}={sessions[OWNER_ID]}"})


def _visible_slugs(api, sessions, user_id):
    response = _customer_get(api, sessions, user_id, "/api/organizations")
    assert response.status_code == 200, response.text
    return {org["slug"]: org["access_mode"] for org in response.json()}


def _agent(runtime, token, *, v2=False):
    """main's own agent authentication: v1 (/upload, /upload-pipeline-status) names its scope; Contract v2 (/v2/upload,
    /v2/status) names none. Every one of those routes calls exactly this before anything else."""
    with runtime.begin() as conn:
        try:
            if v2:
                main.authenticate_agent_token(conn, f"Bearer {token}")
            else:
                main.authenticate_agent(conn, f"Bearer {token}", 10, 1)
        except HTTPException as refused:
            return refused.status_code
    return 200


def _heartbeat(api, token=TOKEN_LEGACY):
    return api.post("/upload-pipeline-status", json={"customer_id": 10, "branch_id": 1},
                    headers={"Authorization": f"Bearer {token}"})


def _upload(api, token=TOKEN_LEGACY):
    body = {"checkins": [{"customer_id": 10, "branch_id": 1, "event_time": "2026-10-02 10:00:00", "barcode": "x"}]}
    return api.post("/upload", json=body, headers={"Authorization": f"Bearer {token}"})


def _issue_code(runtime) -> str:
    with runtime.begin() as conn:
        return collector_enrollment_service.create_enrollment_code(
            conn, 102, created_by_user_id=ADMIN_ID, expected_organization_id=1)["enrollment_code"]


def _enroll(api, code):
    return api.post("/collector/enroll", json={"enrollment_code": code, "hostname": "synthetic-host",
                                               "collector_version": "1.0.0"})


def _session_valid(token) -> bool:
    return session_service.validate_session(token) is not None


# =====================================================================================================================
# suspend
# =====================================================================================================================

@pytest.mark.parametrize(("slug", "before"), [("acme", "active"), ("tri", "trial")])
def test_suspending_changes_the_organization_status_and_nothing_else(api, db, sessions, slug, before):
    owner, _ = db
    snapshot = _snapshot(owner, UNTOUCHED + ACCESS_ARTIFACTS)
    other_orgs = _rows(owner, "SELECT * FROM organizations WHERE slug <> :s ORDER BY id", s=slug)

    response = _lifecycle(api, sessions, "suspend", slug=slug)

    assert response.status_code == 200, response.text
    assert response.json() == {"library": {"slug": slug, "name": f"{slug.title()} Library", "status": "suspended"},
                               "previous_status": before, "changed": True}
    assert _status(owner, slug) == "suspended"
    assert _rows(owner, "SELECT * FROM organizations WHERE slug <> :s ORDER BY id", s=slug) == other_orgs
    after = _snapshot(owner, UNTOUCHED + ACCESS_ARTIFACTS)
    # Sessions are touched only by their own use (last_seen_at); none is revoked.
    for table in UNTOUCHED + ACCESS_ARTIFACTS:
        if table != "auth_sessions":
            assert after[table] == snapshot[table], table
    assert [row["revoked_at"] for row in after["auth_sessions"]] == [None] * len(sessions)
    assert _events(owner) == []


def test_a_suspended_library_refuses_its_collector_at_once_while_its_credentials_stay_active(api, db, sessions):
    owner, runtime = db
    code = _issue_code(runtime)
    assert _agent(runtime, TOKEN_BOUND, v2=True) == 200 and _agent(runtime, TOKEN_LEGACY) == 200

    _lifecycle(api, sessions, "suspend")

    # v1 upload and v1 heartbeat, through the real routes; the generic answer reveals nothing about why.
    for response in (_upload(api), _heartbeat(api)):
        assert (response.status_code, response.json()) == (403, {"detail": "Agent is not currently authorized to upload data"})
    # Contract v2 upload and status authenticate through this, before anything else.
    assert _agent(runtime, TOKEN_BOUND, v2=True) == 403
    assert _agent(runtime, TOKEN_LEGACY) == 403
    # Enrollment: the code is refused (and not consumed).
    assert _enroll(api, code).status_code == 400
    tokens = {row["token_hash"]: row["is_active"] for row in _table(owner, "agent_tokens")}
    assert tokens[_sha256(TOKEN_BOUND)] is True and tokens[_sha256(TOKEN_LEGACY)] is True
    (code_row,) = _rows(owner, "SELECT used_at, revoked_at FROM collector_enrollment_codes")
    assert code_row == {"used_at": None, "revoked_at": None}
    assert [row["status"] for row in _table(owner, "ingest_key_ids")] == ["active", "active"]
    # Another library's Collector is not affected.
    assert _agent(runtime, TOKEN_OTHER, v2=True) == 200


def test_a_suspended_librarys_members_keep_their_sessions_and_read_only_access(api, db, sessions):
    _lifecycle(api, sessions, "suspend")

    assert _session_valid(sessions[OWNER_ID])
    assert _visible_slugs(api, sessions, OWNER_ID) == {"acme": "read_only"}
    assert _customer_get(api, sessions, OWNER_ID, "/api/organizations/acme/members").status_code == 200
    refused = _change_role(api, sessions)
    assert (refused.status_code, refused.json()["code"]) == (403, "organization_read_only")


def test_suspending_a_suspended_library_is_unchanged_and_not_audited(api, db, sessions):
    owner, _ = db
    _lifecycle(api, sessions, "suspend")

    again = _lifecycle(api, sessions, "suspend")

    assert again.status_code == 200
    assert again.json() == {"library": {"slug": "acme", "name": "Acme Library", "status": "suspended"},
                            "previous_status": "suspended", "changed": False}
    assert [row["event_type"] for row in _audit(owner)] == ["admin_library_suspended"]


def test_suspension_is_audited_with_the_target_and_status_names_only(api, db, sessions):
    owner, _ = db

    _lifecycle(api, sessions, "suspend")

    assert _audit(owner) == [{"user_id": ADMIN_ID, "email": ADMIN_EMAIL, "event_type": "admin_library_suspended",
                              "is_success": True, "message": "Library suspended.",
                              "metadata": {"target_org_slug": "acme", "previous_status": "active",
                                           "status": "suspended", "actor_user_id": ADMIN_ID}}]


# =====================================================================================================================
# reactivate
# =====================================================================================================================

def test_reactivation_restores_every_preserved_credential_and_creates_none(api, db, sessions):
    owner, runtime = db
    code = _issue_code(runtime)
    _lifecycle(api, sessions, "suspend")
    before = _snapshot(owner, ("agent_tokens", "collector_installations", "collector_enrollment_codes",
                               "ingest_key_ids", "v2_cutovers"))

    response = _lifecycle(api, sessions, "reactivate")

    assert response.json() == {"library": {"slug": "acme", "name": "Acme Library", "status": "active"},
                               "previous_status": "suspended", "changed": True}
    assert _snapshot(owner, ("agent_tokens", "collector_installations", "collector_enrollment_codes",
                             "ingest_key_ids", "v2_cutovers")) == before
    assert _agent(runtime, TOKEN_BOUND, v2=True) == 200
    assert _agent(runtime, TOKEN_LEGACY) == 200
    assert _agent(runtime, TOKEN_INACTIVE) == 403  # inactive before the suspension: still inactive
    assert _heartbeat(api).status_code == 200
    # The unexpired, unrevoked code can be redeemed again (that redemption is what creates a token, not reactivation).
    assert _enroll(api, code).status_code == 200


def test_reactivation_gives_a_preserved_session_full_access_again(api, db, sessions):
    _lifecycle(api, sessions, "suspend")
    _lifecycle(api, sessions, "reactivate")

    assert _visible_slugs(api, sessions, OWNER_ID) == {"acme": "full"}
    assert _change_role(api, sessions).status_code == 204


def test_a_trial_library_comes_back_active_and_its_subscription_is_untouched(api, db, sessions):
    owner, _ = db
    subscriptions = _table(owner, "subscriptions")

    _lifecycle(api, sessions, "suspend", slug="tri")
    response = _lifecycle(api, sessions, "reactivate", slug="tri")

    assert response.json()["library"]["status"] == "active" and _status(owner, "tri") == "active"
    assert _table(owner, "subscriptions") == subscriptions


@pytest.mark.parametrize(("slug", "status"), [("acme", "active"), ("tri", "trial")])
def test_reactivating_a_usable_library_is_unchanged_and_not_audited(api, db, sessions, slug, status):
    owner, _ = db

    response = _lifecycle(api, sessions, "reactivate", slug=slug)

    assert response.json() == {"library": {"slug": slug, "name": f"{slug.title()} Library", "status": status},
                               "previous_status": status, "changed": False}
    assert _status(owner, slug) == status
    assert _audit(owner) == []


def test_reactivation_is_audited_with_the_target_and_status_names_only(api, db, sessions):
    owner, _ = db
    _lifecycle(api, sessions, "suspend")

    _lifecycle(api, sessions, "reactivate")

    assert _audit(owner)[-1] == {"user_id": ADMIN_ID, "email": ADMIN_EMAIL, "event_type": "admin_library_reactivated",
                                 "is_success": True, "message": "Library reactivated.",
                                 "metadata": {"target_org_slug": "acme", "previous_status": "suspended",
                                              "status": "active", "actor_user_id": ADMIN_ID}}


# =====================================================================================================================
# offboard
# =====================================================================================================================

def test_offboarding_cuts_off_every_access_artifact_and_deletes_nothing(api, db, sessions):
    owner, runtime = db
    code = _issue_code(runtime)
    kept = _snapshot(owner, UNTOUCHED)
    other_artifacts = {
        "tokens": _rows(owner, "SELECT * FROM agent_tokens WHERE customer_id = 30"),
        "installations": _rows(owner, "SELECT * FROM collector_installations WHERE organization_id = 3"),
        "keys": _rows(owner, "SELECT * FROM ingest_key_ids WHERE customer_id = 30"),
    }
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(runtime, "before_cursor_execute", record)
    try:
        response = _lifecycle(api, sessions, "offboard", {"confirm_slug": "acme"})
    finally:
        event.remove(runtime, "before_cursor_execute", record)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "library": {"slug": "acme", "name": "Acme Library", "status": "cancelled"},
        "previous_status": "active",
        "changed": True,
        # bound + legacy (the inactive one was already off); 101 and 102 (103 was retired); the one code; acme's key;
        # the owner's session (the shared owner keeps "other", the admin is a platform admin).
        "counts": {"agent_tokens_deactivated": 2, "installations_retired": 2, "enrollment_codes_revoked": 1,
                   "ingest_keys_retired": 1, "sessions_revoked": 1},
    }
    assert not any(statement.lstrip().upper().startswith(("DELETE", "TRUNCATE")) for statement in statements)
    assert _status(owner) == "cancelled"
    assert [row["is_active"] for row in _rows(owner, "SELECT is_active FROM agent_tokens WHERE customer_id = 10")] == [
        False, False, False]
    assert {row["status"] for row in _rows(owner, "SELECT status FROM collector_installations WHERE organization_id = 1")} == {
        "retired"}
    assert _rows(owner, "SELECT status FROM ingest_key_ids WHERE customer_id = 10") == [{"status": "retired"}]
    assert _rows(owner, "SELECT revoked_at IS NOT NULL AS revoked FROM collector_enrollment_codes") == [{"revoked": True}]
    # Kept, row for row: memberships, users, subscriptions, branches, settings, cutovers, data, the operational identity.
    assert _snapshot(owner, UNTOUCHED) == kept
    assert _rows(owner, "SELECT operational_customer_id FROM organizations WHERE slug = 'acme'") == [
        {"operational_customer_id": 10}]
    # Another library: untouched.
    assert other_artifacts == {
        "tokens": _rows(owner, "SELECT * FROM agent_tokens WHERE customer_id = 30"),
        "installations": _rows(owner, "SELECT * FROM collector_installations WHERE organization_id = 3"),
        "keys": _rows(owner, "SELECT * FROM ingest_key_ids WHERE customer_id = 30"),
    }
    assert _status(owner, "other") == "active"
    # And the cut-off library's Collector is refused everywhere.
    assert _agent(runtime, TOKEN_BOUND, v2=True) == 403 and _agent(runtime, TOKEN_LEGACY) == 403
    assert _heartbeat(api).status_code == 403
    assert _enroll(api, code).status_code == 400
    assert _agent(runtime, TOKEN_OTHER, v2=True) == 200


def test_offboarding_revokes_only_stranded_sessions(api, db, sessions):
    _lifecycle(api, sessions, "offboard", {"confirm_slug": "acme"})

    assert not _session_valid(sessions[OWNER_ID])          # acme was their only library
    assert _session_valid(sessions[SHARED_ID])             # still a member of "other"
    assert _session_valid(sessions[ADMIN_ID])              # a platform administrator
    assert _session_valid(sessions[OTHER_ID])              # never a member of acme
    assert _visible_slugs(api, sessions, SHARED_ID) == {"other": "full"}
    assert _customer_get(api, sessions, SHARED_ID, "/api/organizations/acme/members").status_code == 404


@pytest.mark.parametrize("slug", ["tri", "acme"])
def test_a_trial_or_suspended_library_can_be_offboarded(api, db, sessions, slug):
    owner, _ = db
    if slug == "acme":
        _lifecycle(api, sessions, "suspend")
    previous = _status(owner, slug)

    response = _lifecycle(api, sessions, "offboard", {"confirm_slug": slug}, slug=slug)

    assert response.status_code == 200, response.text
    assert response.json()["previous_status"] == previous
    assert _status(owner, slug) == "cancelled"


def test_a_cancelled_library_is_terminal_through_the_administration_api(api, db, sessions):
    owner, runtime = db
    _lifecycle(api, sessions, "offboard", {"confirm_slug": "acme"})
    audit, events = _audit(owner), _events(owner)

    for action, body in (("suspend", None), ("reactivate", None), ("offboard", {"confirm_slug": "acme"})):
        response = _lifecycle(api, sessions, action, body)
        assert (response.status_code, response.json()["code"]) == (409, "library_cancelled"), action

    assert _status(owner) == "cancelled"
    assert (_audit(owner), _events(owner)) == (audit, events)  # no repeat sweep through React
    # The creation paths stay closed.
    with pytest.raises(collector_enrollment_service.EnrollmentError), runtime.begin() as conn:
        collector_enrollment_service.create_enrollment_code(conn, 102, created_by_user_id=ADMIN_ID)


def test_a_wrong_confirmation_writes_nothing(api, db, sessions):
    owner, _ = db
    before = _snapshot(owner, UNTOUCHED + ACCESS_ARTIFACTS[:-1] + ("organizations",))

    response = _lifecycle(api, sessions, "offboard", {"confirm_slug": "other"})

    assert (response.status_code, response.json()["code"]) == (422, "confirmation_mismatch")
    assert _snapshot(owner, UNTOUCHED + ACCESS_ARTIFACTS[:-1] + ("organizations",)) == before
    assert _audit(owner) == [] and _events(owner) == []


def test_offboarding_evidence_names_the_administrator_by_id_never_by_email(api, db, sessions):
    owner, runtime = db
    _issue_code(runtime)

    _lifecycle(api, sessions, "offboard", {"confirm_slug": "acme"})

    (evidence,) = _events(owner)
    assert (evidence["event_type"], evidence["actor_user_id"], evidence["actor_label"]) == (
        "access_cutoff", ADMIN_ID, f"platform_admin:{ADMIN_ID}")
    assert evidence["details"]["status_before"] == "active" and evidence["details"]["repeat_sweep"] is False
    assert ADMIN_EMAIL not in json.dumps(evidence, default=str)
    (audit,) = _audit(owner)
    assert audit == {"user_id": ADMIN_ID, "email": ADMIN_EMAIL, "event_type": "admin_library_offboarded",
                     "is_success": True, "message": "Library offboarded.",
                     "metadata": {"target_org_slug": "acme", "previous_status": "active", "status": "cancelled",
                                  "counts": {"agent_tokens_deactivated": 2, "installations_retired": 2,
                                             "enrollment_codes_revoked": 1, "ingest_keys_retired": 1,
                                             "sessions_revoked": 1},
                                  "actor_user_id": ADMIN_ID}}


def test_nothing_secret_or_identifying_reaches_an_answer_or_an_audit_row(api, db, sessions):
    owner, runtime = db
    _issue_code(runtime)
    code_hash = _rows(owner, "SELECT code_hash FROM collector_enrollment_codes")[0]["code_hash"]
    answers = [_lifecycle(api, sessions, "suspend").text, _lifecycle(api, sessions, "reactivate").text,
               _lifecycle(api, sessions, "offboard", {"confirm_slug": "acme"}).text]
    audit = json.dumps(_audit(owner), default=str)
    evidence = json.dumps(_events(owner), default=str)

    canaries = [*TOKENS, *(_sha256(token) for token in TOKENS), code_hash, ACME_KEY, CANARY_HOST, CANARY_BARCODE]
    for text_ in (*answers, audit):
        for canary in canaries:
            assert canary not in text_
        assert '"org_slug"' not in text_
        assert '"organization_id"' not in text_ and '"operational_customer_id"' not in text_
    for canary in canaries:
        assert canary not in evidence
    for answer in answers:
        assert ADMIN_EMAIL not in answer
    assert ADMIN_EMAIL not in evidence


# =====================================================================================================================
# one transaction
# =====================================================================================================================

def _failing_after_audit(monkeypatch):
    real = platform_admin_service._audit_admin_lifecycle

    def audit_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("injected failure after the audit row")

    monkeypatch.setattr(platform_admin_service, "_audit_admin_lifecycle", audit_then_fail)


def _failing_audit(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(platform_admin_service, "_audit_admin_lifecycle", fail)


@pytest.mark.parametrize("inject", [_failing_after_audit, _failing_audit])
def test_an_offboarding_that_fails_at_the_end_leaves_nothing_behind(api, db, sessions, monkeypatch, inject):
    owner, runtime = db
    _issue_code(runtime)
    before = _snapshot(owner, UNTOUCHED + ACCESS_ARTIFACTS + ("organizations",))
    inject(monkeypatch)

    response = _lifecycle(api, sessions, "offboard", {"confirm_slug": "acme"})

    assert (response.status_code, response.json()) == (500, {"code": "internal_error", "message": "Internal server error."})
    assert "injected" not in response.text
    after = _snapshot(owner, UNTOUCHED + ACCESS_ARTIFACTS + ("organizations",))
    for table in after:
        if table != "auth_sessions":
            assert after[table] == before[table], table
    assert [row["revoked_at"] for row in after["auth_sessions"]] == [None] * len(sessions)
    assert _status(owner) == "active"
    assert _events(owner) == [] and _audit(owner) == []


@pytest.mark.parametrize("inject", [_failing_after_audit, _failing_audit])
def test_a_suspension_or_reactivation_that_fails_at_its_audit_leaves_the_status_as_it_was(
    api, db, sessions, monkeypatch, inject
):
    owner, _ = db
    with monkeypatch.context() as patched:
        inject(patched)
        assert _lifecycle(api, sessions, "suspend").status_code == 500
    assert _status(owner) == "active" and _audit(owner) == []

    _lifecycle(api, sessions, "suspend")
    inject(monkeypatch)
    assert _lifecycle(api, sessions, "reactivate").status_code == 500
    assert _status(owner) == "suspended"
    assert [row["event_type"] for row in _audit(owner)] == ["admin_library_suspended"]
