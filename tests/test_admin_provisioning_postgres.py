"""A2 item 7 on a REAL PostgreSQL: provisioning a library, and assigning an existing library's operational identity.

The requests go through the real admin routes (TestClient(main.app)) as a signed-in platform administrator, as a
NON-OWNING, NOBYPASSRLS role holding exactly the recorded runtime baseline (scripts/runtime_role_privileges.py).
Nothing is replaced but the engines and the token-hash expression. What only a real server can prove:

  * provisioning creates exactly one active organization, one active primary branch with its operational bridge, one
    customers row bridged to the organization, one trial subscription on the chosen plan, the two empty settings rows
    and one audit row -- and no user, membership, installation, code, token, key or cutover;
  * one transaction: a failure after the audit row, inside the identity assignment, or after the organization and
    branch were inserted, leaves nothing at all; a taken slug (checked, or lost to a concurrent request at the unique
    constraint) and an unavailable plan write nothing;
  * the new library then works through items 4 and 5 and enrollment: its Collector settings are saved, a Contract 2
    installation is added, a code is issued and redeemed -- and only that enrollment creates the ingest key and cutover;
  * the identity action assigns a missing or partial identity once, confirms a complete one without writing, and
    refuses a cancelled library, one without a single primary branch, and a mapping that contradicts itself;
  * nothing returned or audited carries an internal or operational id; another library is never touched.

OPT-IN AND SAFE BY CONSTRUCTION, as tests/test_admin_lifecycle_postgres.py: runs only when SORTVIEW_TEST_POSTGRES_URL
points at a maintenance database on a NON-PRODUCTION, local server; a throwaway database and role are created and
dropped. Every value is SYNTHETIC.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import database
import main
from scripts import runtime_role_privileges as privileges
from services import session_service, tenant_service

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL tests)")

RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only
ADMIN_ORIGIN = "https://admin.example.invalid"
SESSION_COOKIE = "sortview_api_session"  # plain-HTTP mode: TestClient is not HTTPS
ADMIN_ID = 7
ADMIN_EMAIL = "admin@example.invalid"

_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
_BUILTIN_SHA256_EXPR = "encode(sha256(convert_to(:token, 'UTF8')), 'hex')"  # pgcrypto is not on every throwaway server

BODY = {"name": "Riverbend Public Library", "slug": "riverbend", "branch_name": "Main Branch", "branch_slug": "main",
        "plan_code": "pro"}

# Every table provisioning could conceivably touch, and the ones it must leave alone.
CREATED_TABLES = ("organizations", "branches", "customers", "subscriptions", "organization_settings", "branch_settings",
                  "auth_audit_log")
NEVER_TOUCHED = ("app_users", "memberships", "auth_sessions", "collector_installations", "collector_enrollment_codes",
                 "agent_tokens", "ingest_key_ids", "v2_cutovers", "tenant_lifecycle_events", "password_reset_tokens")


def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


@pytest.fixture(scope="module")
def cluster():
    """One database at head, its owner's engine, and a non-owning role holding exactly the recorded baseline."""
    admin = make_url(ADMIN_URL)
    _guard(admin)
    name = f"sortview_admin_provisioning_test_{secrets.token_hex(4)}"
    role = f"sortview_admin_provisioning_role_{secrets.token_hex(4)}"
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


@pytest.fixture
def db(cluster, monkeypatch):
    """The administrator, and one existing library ("other", fully provisioned) that must never be touched. The plans
    are the migration's own: starter, pro, enterprise, all active."""
    owner, runtime = cluster
    with owner.begin() as conn:
        conn.execute(text(
            "TRUNCATE tenant_lifecycle_events, auth_audit_log, auth_sessions, password_reset_tokens, v2_cutovers, "
            "ingest_key_ids, collector_enrollment_codes, agent_tokens, collector_installations, memberships, "
            "subscriptions, organization_settings, branch_settings, branches, organizations, customers, app_users "
            "RESTART IDENTITY CASCADE"
        ))
        conn.execute(text("UPDATE plans SET is_active = TRUE"))
        conn.execute(text(
            "INSERT INTO app_users (id, email, full_name, is_platform_admin) "
            f"VALUES ({ADMIN_ID}, '{ADMIN_EMAIL}', 'Test Admin', TRUE)"  # nosec B608 - constants
        ))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (500, 'Other')"))
        conn.execute(text("SELECT setval('customers_id_seq', 500)"))
        conn.execute(text(
            "INSERT INTO organizations (id, slug, name, status, operational_customer_id) "
            "VALUES (500, 'other', 'Other Library', 'active', 500)"
        ))
        conn.execute(text("SELECT setval('organizations_id_seq', 500)"))
        conn.execute(text(
            "INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) "
            "VALUES (500, 500, 'main', 'Other Main', TRUE, 'active', 500)"
        ))
        conn.execute(text("SELECT setval('branches_id_seq', 500)"))
        conn.execute(text(
            "INSERT INTO subscriptions (organization_id, plan_id, status) "
            "SELECT 500, id, 'active' FROM plans WHERE code = 'enterprise'"
        ))
    monkeypatch.setattr(database, "_engine", runtime)  # the one flat engine every service uses
    monkeypatch.setattr(main, "engine", runtime)
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", True)
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR))
    monkeypatch.setenv("SORTVIEW_ADMIN_ALLOWED_ORIGINS", ADMIN_ORIGIN)
    monkeypatch.setenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", "false")
    return owner, runtime


@pytest.fixture
def api(db):
    main.limiter.reset()
    session = session_service.create_session(ADMIN_ID)["token"]
    client = TestClient(main.app, raise_server_exceptions=False)
    client.headers.update({"Cookie": f"{SESSION_COOKIE}={session}", "Origin": ADMIN_ORIGIN})
    yield client
    main.limiter.reset()


# --- helpers ---------------------------------------------------------------------------------------------------------

def _rows(engine, sql, **params):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def _table(owner, table):
    return _rows(owner, f"SELECT * FROM {table} ORDER BY 1")  # nosec B608 - constant table names


def _snapshot(owner, tables=CREATED_TABLES + NEVER_TOUCHED):
    snapshot = {table: _table(owner, table) for table in tables}
    # Signing in touches the administrator's own session row only; it is no part of provisioning.
    if "auth_sessions" in snapshot:
        snapshot["auth_sessions"] = [{k: v for k, v in row.items() if k != "last_seen_at"} for row in snapshot["auth_sessions"]]
    return snapshot


def _audit(owner):
    return _rows(owner, "SELECT user_id, email, event_type, is_success, message, metadata FROM auth_audit_log "
                        "WHERE event_type LIKE 'admin_%' ORDER BY id")


def _provision(api, **overrides):
    return api.post("/api/admin/libraries", json={**BODY, **overrides})


def _library(owner, slug="riverbend"):
    rows = _rows(owner, "SELECT * FROM organizations WHERE slug = :s", s=slug)
    return rows[0] if rows else None


def _ids_in(text_: str, owner) -> list[str]:
    """Every internal id of the new library that appears, as a number, in `text_`."""
    library = _library(owner)
    (branch,) = _rows(owner, "SELECT id FROM branches WHERE organization_id = :o", o=library["id"])
    ids = {library["id"], library["operational_customer_id"], branch["id"]}
    return [str(value) for value in ids if f": {value}" in text_ or f":{value}" in text_]


# =====================================================================================================================
# provisioning: exactly these rows
# =====================================================================================================================

def test_provisioning_creates_exactly_the_tenant_and_nothing_else(api, db):
    owner, _ = db
    before = _snapshot(owner)

    response = _provision(api, name="  Riverbend Public Library ")

    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "library": {"slug": "riverbend", "name": "Riverbend Public Library", "status": "active"},
        "primary_branch": {"slug": "main", "name": "Main Branch", "status": "active"},
        "subscription": {"plan_code": "pro", "plan_name": "Pro", "status": "trial"},
        "operational_identity": "assigned",
    }
    after = _snapshot(owner)
    for table in NEVER_TOUCHED:
        assert after[table] == before[table], table

    library = _library(owner)
    assert (library["name"], library["status"]) == ("Riverbend Public Library", "active")
    new = {table: [row for row in after[table] if row not in before[table]] for table in CREATED_TABLES}
    assert {table: len(rows) for table, rows in new.items()} == {
        "organizations": 1, "branches": 1, "customers": 1, "subscriptions": 1, "organization_settings": 1,
        "branch_settings": 1, "auth_audit_log": 1}
    (branch,) = new["branches"]
    assert (branch["organization_id"], branch["slug"], branch["name"], branch["is_primary"], branch["status"],
            branch["operational_branch_id"], branch["onboarding_status"]) == (
        library["id"], "main", "Main Branch", True, "active", branch["id"], "pending")
    (customer,) = new["customers"]
    assert customer["name"] == "Riverbend Public Library"
    assert library["operational_customer_id"] == customer["id"]
    (subscription,) = new["subscriptions"]
    (pro,) = _rows(owner, "SELECT id FROM plans WHERE code = 'pro'")
    assert (subscription["organization_id"], subscription["plan_id"], subscription["status"], subscription["ends_at"]) == (
        library["id"], pro["id"], "trial", None)
    assert subscription["started_at"] is not None
    # The two compatibility rows are empty: no contacts, notes, names, plan copy, security, routing or Collector settings.
    assert new["organization_settings"][0]["settings_json"] == {}
    assert new["branch_settings"][0]["settings_json"] == {}
    # Another library: untouched.
    assert [row for row in after["organizations"] if row["slug"] == "other"] == [
        row for row in before["organizations"] if row["slug"] == "other"]


def test_provisioning_is_audited_with_slugs_states_and_the_plan_only(api, db):
    owner, _ = db

    _provision(api)

    assert _audit(owner) == [{
        "user_id": ADMIN_ID, "email": ADMIN_EMAIL, "event_type": "admin_library_provisioned", "is_success": True,
        "message": "Library provisioned.",
        "metadata": {"target_org_slug": "riverbend", "branch_slug": "main", "status": "active", "plan_code": "pro",
                     "operational_identity": "assigned", "actor_user_id": ADMIN_ID},
    }]
    audit_text = json.dumps(_audit(owner))
    assert "Riverbend Public Library" not in audit_text and "Main Branch" not in audit_text
    assert '"org_slug"' not in audit_text
    assert _ids_in(audit_text.replace(f'"actor_user_id": {ADMIN_ID}', "").replace(f'"user_id": {ADMIN_ID}', ""), owner) == []


def test_nothing_returned_carries_an_internal_or_operational_id(api, db):
    owner, _ = db

    response = _provision(api)

    assert _ids_in(response.text, owner) == []
    for key in ("id", "organization_id", "branch_id", "customer_id", "operational_customer", "operational_branch",
                "plan_id"):
        assert f'"{key}' not in response.text


def test_names_may_repeat_but_a_slug_may_not(api, db):
    owner, _ = db
    assert _provision(api).status_code == 201

    assert _provision(api, slug="riverbend-2").status_code == 201  # the same name again
    before = _snapshot(owner)
    repeat = _provision(api)  # the same request again: refused, not repeated

    assert (repeat.status_code, repeat.json()) == (409, {"code": "library_slug_taken",
                                                         "message": "A library with this slug already exists."})
    assert _snapshot(owner) == before


def test_a_slug_lost_to_a_concurrent_request_is_409_and_leaves_nothing(api, db, monkeypatch):
    owner, _ = db
    assert _provision(api).status_code == 201
    before = _snapshot(owner)
    # As if the other request committed between this one's check and its insert: the check finds nothing.
    monkeypatch.setattr(tenant_service, "_SLUG_TAKEN_SQL", "SELECT 1 FROM organizations WHERE slug = :slug AND FALSE")

    response = _provision(api, name="Someone Else")

    assert (response.status_code, response.json()["code"]) == (409, "library_slug_taken")
    assert _snapshot(owner) == before


@pytest.mark.parametrize("plan_code", ["starter-old", "PRO", "pro "])
def test_a_plan_that_is_not_an_active_plan_is_409_and_writes_nothing(api, db, plan_code):
    owner, _ = db
    before = _snapshot(owner)

    response = _provision(api, plan_code=plan_code)

    assert (response.status_code, response.json()) == (409, {"code": "plan_not_available",
                                                             "message": "This plan is not available for a new library."})
    assert _snapshot(owner) == before


def test_an_inactive_plan_is_not_listed_and_cannot_be_chosen(api, db):
    owner, _ = db
    with owner.begin() as conn:
        conn.execute(text("UPDATE plans SET is_active = FALSE WHERE code = 'starter'"))

    listed = api.get("/api/admin/plans")
    refused = _provision(api, plan_code="starter")

    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    assert listed.json() == {"plans": [{"code": "pro", "name": "Pro"}, {"code": "enterprise", "name": "Enterprise"}]}
    assert (refused.status_code, refused.json()["code"]) == (409, "plan_not_available")
    assert _library(owner) is None


def test_a_refused_value_writes_nothing(api, db):
    owner, _ = db
    before = _snapshot(owner)

    response = _provision(api, slug="Riverbend")

    assert response.status_code == 422
    assert _snapshot(owner) == before


# =====================================================================================================================
# one transaction
# =====================================================================================================================

def _fail_after(monkeypatch, name):
    real = getattr(tenant_service, name)

    def call_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(tenant_service, name, call_then_fail)


@pytest.mark.parametrize("step", [
    "_audit_admin",                               # A: after the audit row was inserted
    "assign_operational_identity",                # B: after the customers row and both bridges were written
    "create_organization_with_primary_branch",    # C: after the organization, branch, subscription and settings
])
def test_a_failure_at_any_step_leaves_no_tenant_at_all(api, db, monkeypatch, step):
    owner, _ = db
    before = _snapshot(owner)
    _fail_after(monkeypatch, step)

    response = _provision(api)

    assert (response.status_code, response.json()) == (500, {"code": "internal_error", "message": "Internal server error."})
    assert "injected" not in response.text
    assert _snapshot(owner) == before
    assert _library(owner) is None
    assert _rows(owner, "SELECT count(*) AS n FROM customers")[0]["n"] == 1  # the other library's only


def test_a_failure_inside_the_identity_assignment_itself_leaves_nothing(api, db, monkeypatch):
    owner, _ = db
    before = _snapshot(owner)
    # A real database error inside the identity step, after its customers row and bridges were written: the branch's
    # bridge is pointed somewhere the schema's CHECK forbids.
    real = tenant_service.assign_operational_identity

    def failing(organization_id, branch_id, *, conn):
        real(organization_id, branch_id, conn=conn)
        conn.execute(text("UPDATE branches SET operational_branch_id = 999999 WHERE id = :b"), {"b": branch_id})

    monkeypatch.setattr(tenant_service, "assign_operational_identity", failing)

    response = _provision(api)

    assert response.status_code == 500
    assert _snapshot(owner) == before


# =====================================================================================================================
# the new library through items 4 and 5 and enrollment
# =====================================================================================================================

def test_a_provisioned_library_is_configured_given_an_installation_and_a_code_and_enrolls(api, db):
    owner, _ = db
    assert _provision(api).status_code == 201
    base = "/api/admin/libraries/riverbend"
    library = _library(owner)

    # Contract 2 cannot be added before item 4's settings.
    early = api.post(f"{base}/installations", json={"name": "Main Sorter", "required_contract": 2})
    assert (early.status_code, early.json()["code"]) == (409, "collector_settings_not_ready")

    # Item 4.
    settings = api.put(f"{base}/collector-settings", json={"collector_timezone": "America/Chicago", "rules_reviewed": True})
    assert settings.status_code == 200, settings.text
    routing = api.put(f"{base}/internal-routing", json={
        "branch_services_names": [], "collection_services_names": [],
        "branch_services_da_patterns": [], "collection_services_da_patterns": []})
    assert routing.status_code == 200, routing.text

    # Item 5.
    added = api.post(f"{base}/installations", json={"name": "Main Sorter", "required_contract": 2})
    assert added.status_code == 201, added.text
    issued = api.post(f"{base}/installations/{added.json()['id']}/enrollment-codes")
    assert issued.status_code == 201, issued.text

    # Nothing of v2 exists before enrollment.
    assert _table(owner, "ingest_key_ids") == [] and _table(owner, "v2_cutovers") == [] and _table(owner, "agent_tokens") == []

    enrolled = TestClient(main.app).post("/collector/enroll", json={
        "enrollment_code": issued.json()["enrollment_code"], "hostname": "synthetic-host",
        "collector_version": "1.1.0", "contract_version": 2})

    assert enrolled.status_code == 200, enrolled.text
    body = enrolled.json()
    assert (body["customer_id"], body["contract_version"], body["timezone"]) == (
        library["operational_customer_id"], 2, "America/Chicago")
    assert [(row["customer_id"], row["status"]) for row in _table(owner, "ingest_key_ids")] == [
        (library["operational_customer_id"], "active")]
    assert len(_table(owner, "v2_cutovers")) == 1 and len(_table(owner, "agent_tokens")) == 1


def test_a_provisioned_library_has_no_member_so_no_customer_can_sign_in(api, db):
    owner, _ = db

    _provision(api)

    assert _rows(owner, "SELECT count(*) AS n FROM memberships")[0]["n"] == 0
    assert [row["id"] for row in _table(owner, "app_users")] == [ADMIN_ID]


# =====================================================================================================================
# assigning an existing library's operational identity
# =====================================================================================================================

def _unmapped(owner, *, slug="legacy", status="active", customer=None, primaries=1):
    with owner.begin() as conn:
        organization_id = conn.execute(text(
            "INSERT INTO organizations (slug, name, status, operational_customer_id) "
            "VALUES (:s, 'Legacy Library', :st, :c) RETURNING id"), {"s": slug, "st": status, "c": customer}).scalar_one()
        for index in range(primaries):
            conn.execute(text(
                "INSERT INTO branches (organization_id, slug, name, is_primary, status) "
                "VALUES (:o, :s, 'Legacy Main', TRUE, 'active')"), {"o": organization_id, "s": f"main-{index}"})
        if primaries == 0:
            conn.execute(text("INSERT INTO branches (organization_id, slug, name, is_primary, status) "
                              "VALUES (:o, 'side', 'Side', FALSE, 'active')"), {"o": organization_id})
    return organization_id


def _identity(api, slug="legacy"):
    return api.post(f"/api/admin/libraries/{slug}/operational-identity")


def test_a_missing_identity_is_assigned_once_and_audited_without_ids(api, db):
    owner, _ = db
    organization_id = _unmapped(owner)
    other = _snapshot(owner, ("customers",))["customers"]

    response = _identity(api)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"operational_identity": "assigned", "changed": True}
    (library,) = _rows(owner, "SELECT operational_customer_id FROM organizations WHERE id = :o", o=organization_id)
    (branch,) = _rows(owner, "SELECT id, operational_branch_id FROM branches WHERE organization_id = :o", o=organization_id)
    assert library["operational_customer_id"] is not None and branch["operational_branch_id"] == branch["id"]
    assert len(_table(owner, "customers")) == len(other) + 1
    assert _audit(owner) == [{
        "user_id": ADMIN_ID, "email": ADMIN_EMAIL, "event_type": "admin_operational_identity_assigned",
        "is_success": True, "message": "Operational identity assigned.",
        "metadata": {"target_org_slug": "legacy", "changed": True, "created_customer": True, "actor_user_id": ADMIN_ID}}]
    for value in (organization_id, library["operational_customer_id"], branch["id"]):
        assert f": {value}," not in response.text and f": {value}}}" not in response.text

    repeat = _identity(api)

    assert repeat.json() == {"operational_identity": "assigned", "changed": False}
    assert len(_table(owner, "customers")) == len(other) + 1  # no second customer
    assert len(_audit(owner)) == 1  # a confirmation writes nothing


def test_a_partial_identity_is_completed_without_a_second_customer(api, db):
    owner, _ = db
    with owner.begin() as conn:
        customer = conn.execute(text("INSERT INTO customers (name) VALUES ('Legacy') RETURNING id")).scalar_one()
    organization_id = _unmapped(owner, customer=customer)
    customers = len(_table(owner, "customers"))

    response = _identity(api)

    assert response.json() == {"operational_identity": "assigned", "changed": True}
    assert len(_table(owner, "customers")) == customers
    (branch,) = _rows(owner, "SELECT id, operational_branch_id FROM branches WHERE organization_id = :o", o=organization_id)
    assert branch["operational_branch_id"] == branch["id"]
    assert _audit(owner)[0]["metadata"]["created_customer"] is False


@pytest.mark.parametrize(("setup", "status", "code"), [
    ({"status": "cancelled"}, 409, "library_cancelled"),
    ({"primaries": 0}, 409, "no_primary_branch"),
    ({"primaries": 2}, 409, "no_primary_branch"),
])
def test_an_identity_that_cannot_be_assigned_is_refused_and_nothing_written(api, db, setup, status, code):
    owner, _ = db
    _unmapped(owner, **setup)
    before = _snapshot(owner)

    response = _identity(api)

    assert (response.status_code, response.json()["code"]) == (status, code)
    assert _snapshot(owner) == before


def test_an_unknown_library_is_404(api, db):
    response = _identity(api, slug="nowhere")

    assert (response.status_code, response.json()) == (404, {"code": "library_not_found", "message": "Library not found."})


def test_an_inconsistent_mapping_is_refused_never_rebuilt(api, db):
    """The schema makes a contradictory mapping unrepresentable (ck_branches_operational_branch_id_matches_id, the
    customers foreign key, the partial unique index). In this throwaway database the CHECK is lifted for the one test,
    to prove that if such a row ever existed the action would refuse it rather than overwrite it."""
    owner, _ = db
    organization_id = _unmapped(owner)
    with owner.begin() as conn:
        conn.execute(text("ALTER TABLE branches DROP CONSTRAINT ck_branches_operational_branch_id_matches_id"))
        conn.execute(text("UPDATE branches SET operational_branch_id = 999999 WHERE organization_id = :o"),
                     {"o": organization_id})
    try:
        before = _snapshot(owner)

        response = _identity(api)

        assert (response.status_code, response.json()["code"]) == (409, "operational_identity_inconsistent")
        assert _snapshot(owner) == before
    finally:
        with owner.begin() as conn:
            conn.execute(text("UPDATE branches SET operational_branch_id = NULL WHERE organization_id = :o"),
                         {"o": organization_id})
            conn.execute(text("ALTER TABLE branches ADD CONSTRAINT ck_branches_operational_branch_id_matches_id "
                              "CHECK (operational_branch_id IS NULL OR operational_branch_id = id)"))


def test_a_complete_identity_is_confirmed_and_another_library_is_untouched(api, db):
    owner, _ = db
    before = _snapshot(owner)

    response = _identity(api, slug="other")

    assert response.json() == {"operational_identity": "assigned", "changed": False}
    assert _snapshot(owner) == before
