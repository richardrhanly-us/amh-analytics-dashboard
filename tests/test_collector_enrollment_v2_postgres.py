"""Contract v2 enrollment (onboarding stage A1) against a REAL PostgreSQL, as the RUNTIME ROLE: the real migration
chain, the role's real privileges, real row level security on the key insert, real row locks and real transactions.

SQLite (tests/test_collector_enrollment_v2.py) covers the logic. It cannot prove that the migration's columns and
constraints are what PostgreSQL enforces, that a non-owning role with exactly the baseline's privileges can perform the
enrollment (and cannot UPDATE or DELETE a cutover), that ingest_key_ids' policies refuse a key for another tenant, that
a failure rolls back all four writes, or that two enrollments for one branch serialize. Those need a real server.

OPT-IN AND SAFE BY CONSTRUCTION, exactly as tests/test_collector_enrollment_postgres.py: the tests run only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server the tests may create and drop
databases and roles on. Each run creates brand-new throwaway databases and a throwaway role, and drops them afterwards.
The host must be local unless SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also set.

THE ROLE is provisioned from scripts/runtime_role_privileges.py's own provisioning SQL -- the recorded baseline, which
now includes INSERT on v2_cutovers -- so these tests exercise what a real environment's application role is given, not
a hand-written copy of it. (The migration's own GRANT names the literal role sortview_app, which a throwaway server does
not have; what it renders is asserted in tests/test_enrollment_contract_migration.py.)
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from test_ingest_v2_api import checkin, v1_payload

import main
from scripts import runtime_role_privileges as privileges
from src.services import collector_enrollment_service as enrollment
from src.services import collector_rules, ingest_v2_service
from src.services.collector_enrollment_service import EnrollmentRefused

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL Contract v2 enrollment tests)"
)

REVISION, PREVIOUS_HEAD = "f7b3d2c81a46", "e3a7b1c9d4f2"
RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only
_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
_BUILTIN_SHA256_EXPR = "encode(sha256(convert_to(:token, 'UTF8')), 'hex')"  # pgcrypto is not on every throwaway server

LEGACY_KEYS = {"customer_id", "branch_id", "installation_id", "agent_token"}
V2_KEYS = LEGACY_KEYS | {"contract_version", "contract_mode", "key_id", "timezone", "classification_rules"}

# org 1 "lib-a": customer 10, branch 1 -- installations 101 and 102 require contract 2, 103 is legacy.
# org 2 "lib-b": customer 11, branch 2 -- installation 201 requires contract 2.
# org 3 "lib-c": customer 12, branch 3 -- a LEGACY library that was migrated by hand: its own key and cutover.
ORG_A_CREATED = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
ORG_B_CREATED = datetime(2025, 8, 9, 10, 11, 12, tzinfo=UTC)
LEGACY_KEY = "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b"
LEGACY_CUTOVER = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
CANARY_A, CANARY_B = "CANARY-A (ST)STAFFER", "CANARY-B (ST)OTHERLIB"
REVIEWED = collector_rules.rules_review_marker(reviewed_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC), reviewed_by_user_id=5)
SETTINGS_A = {"collector_timezone": "America/New_York", "collector_rules": REVIEWED,
              "transit": {"destinations": [{"key": "north_annex", "label": "North Annex", "enabled": True}]},
              "internal_routing": {"branch_services_names": [CANARY_A], "collection_services_names": [],
                                   "branch_services_da_patterns": [], "collection_services_da_patterns": []}}
SETTINGS_B = {"collector_timezone": "America/Denver", "collector_rules": REVIEWED,
              "internal_routing": {"branch_services_names": [CANARY_B]}}

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


# --- throwaway databases and a throwaway runtime role ----------------------------------------------------------------

def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


def _alembic(url, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
    return subprocess.run(  # nosec B603
        [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )


class Throwaway:
    """A brand-new database on the test server, migrated to `revision`, dropped when the context exits."""

    def __init__(self, revision: str):
        self.revision = revision
        self.admin = make_url(ADMIN_URL)
        _guard(self.admin)
        self.name = f"sortview_enroll_v2_test_{secrets.token_hex(4)}"
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
        owner = create_engine(db.url, hide_parameters=True)
        role = f"sortview_enroll_v2_role_{secrets.token_hex(4)}"
        with owner.begin() as conn:
            conn.execute(text(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT "  # nosec B608
                f"PASSWORD '{RUNTIME_ROLE_PASSWORD}'"
            ))
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{db.url.database}" TO {role}'))  # nosec B608
            for line in privileges.provisioning_sql(role).splitlines():
                if line and not line.startswith("--") and line not in ("BEGIN;", "COMMIT;"):
                    conn.execute(text(line))
        runtime = create_engine(db.url.set(username=role, password=RUNTIME_ROLE_PASSWORD), hide_parameters=True,
                                pool_size=8, max_overflow=4)
        try:
            yield owner, runtime, role
        finally:
            runtime.dispose()
            with owner.begin() as conn:
                conn.execute(text(f"DROP OWNED BY {role}"))  # nosec B608
                conn.execute(text(f"DROP ROLE IF EXISTS {role}"))  # nosec B608
            owner.dispose()


@pytest.fixture
def owner(cluster):
    return cluster[0]


@pytest.fixture
def role(cluster):
    return cluster[2]


@pytest.fixture
def runtime(cluster, monkeypatch):
    """The application's engine: the API and the service run as the runtime role, as in production. Seeded fresh."""
    owner_engine, runtime_engine, _role = cluster
    with owner_engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE v2_cutovers, ingest_key_ids, checkin_events, checkins, rejects, acs_events, "
            "collector_enrollment_codes, agent_tokens, collector_installations, organization_settings, branch_settings, "
            "pipeline_status, branches, organizations, customers RESTART IDENTITY CASCADE"
        ))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Lib A'), (11, 'Lib B'), (12, 'Lib C')"))
        conn.execute(text(
            "INSERT INTO organizations (id, slug, name, status, operational_customer_id, created_at) VALUES "
            "(1, 'lib-a', 'Lib A', 'active', 10, :a), (2, 'lib-b', 'Lib B', 'trial', 11, :b), "
            "(3, 'lib-c', 'Lib C', 'active', 12, '2024-01-01T00:00:00+00:00')"
        ), {"a": ORG_A_CREATED, "b": ORG_B_CREATED})
        # A branch's own created_at is a TIMESTAMP with no zone, and may be NULL: it is never the boundary.
        conn.execute(text(
            "INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id, created_at) VALUES "
            "(1, 1, 'main', 'Main', 'active', 1, NULL), (2, 2, 'main', 'Main', 'active', 2, '2031-01-01 00:00:00'), "
            "(3, 3, 'main', 'Main', 'active', 3, NULL)"
        ))
        conn.execute(text(
            "INSERT INTO organization_settings (organization_id, settings_json) VALUES (1, CAST(:a AS JSONB)), "
            "(2, CAST(:b AS JSONB)), (3, '{}'::jsonb)"
        ), {"a": json.dumps(SETTINGS_A), "b": json.dumps(SETTINGS_B)})
        conn.execute(text(
            "INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract) VALUES "
            "(101, 1, 1, 'Main AMH Sorter', 'provisioning', 2), (102, 1, 1, 'Second Sorter', 'provisioning', 2), "
            "(201, 2, 2, 'Lib B Sorter', 'provisioning', 2)"
        ))
        # No required_contract named: the column's own default.
        conn.execute(text(
            "INSERT INTO collector_installations (id, organization_id, branch_id, name, status) VALUES "
            "(103, 1, 1, 'Old Sorter', 'provisioning'), (301, 3, 3, 'Lib C Sorter', 'active')"
        ))
        conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id) VALUES (:k, 12, 3)"), {"k": LEGACY_KEY})
        conn.execute(text(
            "INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, note) VALUES (12, 3, :at, 'operator', 'by hand')"
        ), {"at": LEGACY_CUTOVER})
    monkeypatch.setattr(main, "engine", runtime_engine)
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", True)
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR))
    return runtime_engine


def _rows(engine, sql, **params):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]


def _written(owner_engine) -> dict:
    """Everything an enrollment can write, read as the OWNER (who is not subject to row level security)."""
    return {
        "codes": _rows(owner_engine, "SELECT id, installation_id, used_at, revoked_at, required_contract "
                                     "FROM collector_enrollment_codes ORDER BY id"),
        "tokens": _rows(owner_engine, "SELECT id, customer_id, branch_id, installation_id, is_active FROM agent_tokens ORDER BY id"),
        "keys": _rows(owner_engine, "SELECT key_id, customer_id, branch_id, status, retired_at FROM ingest_key_ids ORDER BY id"),
        "cutovers": _rows(owner_engine, "SELECT id, customer_id, branch_id, cutover_at, set_by, set_at, note FROM v2_cutovers ORDER BY id"),
    }


def _code(engine, installation_id) -> str:
    with engine.begin() as conn:
        return enrollment.create_enrollment_code(conn, installation_id)["enrollment_code"]


def _redeem(engine, code, **kwargs):
    with engine.begin() as conn:
        return enrollment.redeem_enrollment_code(conn, code, **kwargs)


def _post(code, **body):
    return client.post("/collector/enroll", json={"enrollment_code": code, **body})


# =====================================================================================================================
# the migration, on a real server
# =====================================================================================================================

def test_the_columns_and_constraints_are_what_postgresql_enforces(owner, runtime):
    columns = _rows(owner, """
        SELECT table_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'required_contract'
        ORDER BY table_name
    """)

    assert columns == [
        {"table_name": "collector_enrollment_codes", "data_type": "smallint", "is_nullable": "NO", "column_default": "1"},
        {"table_name": "collector_installations", "data_type": "smallint", "is_nullable": "NO", "column_default": "1"},
    ]
    constraints = {r["conname"] for r in _rows(owner, "SELECT conname FROM pg_constraint WHERE conname LIKE '%required_contract_chk'")}
    assert constraints == {"collector_installations_required_contract_chk", "collector_enrollment_codes_required_contract_chk"}
    # An installation created without saying so is the legacy contract.
    assert {r["id"]: r["required_contract"] for r in _rows(owner, "SELECT id, required_contract FROM collector_installations")} == {
        101: 2, 102: 2, 103: 1, 201: 2, 301: 1}
    for bad in (0, 3, -1):
        with pytest.raises(IntegrityError), owner.begin() as conn:
            conn.execute(text("UPDATE collector_installations SET required_contract = :v WHERE id = 103"), {"v": bad})
        with pytest.raises(IntegrityError), owner.begin() as conn:
            conn.execute(text(
                "INSERT INTO collector_enrollment_codes (installation_id, code_hash, expires_at, required_contract) "
                "VALUES (103, :h, now() + interval '1 hour', :v)"), {"h": f"hash-{bad}", "v": bad})


def test_existing_rows_become_contract_1_and_the_downgrade_leaves_everything_else_alone():
    with Throwaway(PREVIOUS_HEAD) as db:
        engine = create_engine(db.url, hide_parameters=True)
        try:
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Lib A')"))
                conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) "
                                  "VALUES (1, 'lib-a', 'Lib A', 'active', 10)"))
                conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) "
                                  "VALUES (1, 1, 'main', 'Main', 'active', 1)"))
                conn.execute(text("INSERT INTO collector_installations (id, organization_id, branch_id, name, status) "
                                  "VALUES (7, 1, 1, 'Existing Sorter', 'active')"))
                conn.execute(text("INSERT INTO collector_enrollment_codes (installation_id, code_hash, expires_at) "
                                  "VALUES (7, 'existing-hash', now() + interval '1 hour')"))
                conn.execute(text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by) "
                                  "VALUES (10, 1, '2026-06-01T00:00:00+00:00', 'operator')"))
            before = _rows(engine, "SELECT * FROM collector_installations") + _rows(engine, "SELECT * FROM collector_enrollment_codes")
            cutovers = _rows(engine, "SELECT * FROM v2_cutovers")

            assert _alembic(db.url, "upgrade", REVISION).returncode == 0
            assert _rows(engine, "SELECT required_contract FROM collector_installations") == [{"required_contract": 1}]
            assert _rows(engine, "SELECT required_contract FROM collector_enrollment_codes") == [{"required_contract": 1}]
            after = _rows(engine, "SELECT * FROM collector_installations") + _rows(engine, "SELECT * FROM collector_enrollment_codes")
            assert [{k: v for k, v in row.items() if k != "required_contract"} for row in after] == before

            downgraded = _alembic(db.url, "downgrade", PREVIOUS_HEAD)
            assert downgraded.returncode == 0, downgraded.stderr[-2000:]
            assert _rows(engine, "SELECT * FROM collector_installations") + _rows(engine, "SELECT * FROM collector_enrollment_codes") == before
            assert _rows(engine, "SELECT * FROM v2_cutovers") == cutovers  # no cutover row is touched in either direction
            assert _rows(engine, "SELECT 1 AS found FROM information_schema.columns WHERE column_name = 'required_contract'") == []
            assert _alembic(db.url, "upgrade", "head").returncode == 0  # and it goes forward again
        finally:
            engine.dispose()


# =====================================================================================================================
# the runtime role's privileges
# =====================================================================================================================

def test_the_role_holds_exactly_the_baseline_and_on_v2_cutovers_exactly_select_and_insert(owner, role, runtime):
    with owner.connect() as conn:
        assert privileges.differences(conn, role) == []
        held = {privilege: conn.execute(text("SELECT has_table_privilege(:r, 'public.v2_cutovers', :p)"),
                                        {"r": role, "p": privilege}).scalar()
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")}
        sequence = {privilege: conn.execute(text("SELECT has_sequence_privilege(:r, 'public.v2_cutovers_id_seq', :p)"),
                                            {"r": role, "p": privilege}).scalar()
                    for privilege in ("USAGE", "SELECT", "UPDATE")}
        attributes = conn.execute(text("SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = :r"),
                                  {"r": role}).one()

    assert held == {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False, "TRUNCATE": False,
                    "REFERENCES": False, "TRIGGER": False}
    assert sequence == {"USAGE": True, "SELECT": False, "UPDATE": False}
    assert tuple(attributes) == (False, False, False, False)


def test_the_role_can_append_a_cutover_and_can_never_change_or_remove_one(owner, runtime):
    before = _written(owner)["cutovers"]

    for statement in ("UPDATE v2_cutovers SET cutover_at = now() WHERE customer_id = 12",
                      "UPDATE v2_cutovers SET set_by = 'someone else'",
                      "DELETE FROM v2_cutovers WHERE customer_id = 12",
                      "TRUNCATE v2_cutovers"):
        with pytest.raises(ProgrammingError, match="permission denied"), runtime.begin() as conn:
            conn.execute(text(statement))

    assert _written(owner)["cutovers"] == before
    # The table has no row level security in this stage (database-level scoping of the insert is future hardening):
    # that is why the enrollment service's own scoping is tested below, row by row.
    assert _rows(owner, "SELECT relrowsecurity FROM pg_class WHERE relname = 'v2_cutovers'") == [{"relrowsecurity": False}]


# =====================================================================================================================
# a Contract v2 enrollment, as the runtime role
# =====================================================================================================================

def test_the_code_the_token_the_key_and_the_cutover_are_committed_together(owner, runtime):
    code = _code(runtime, 101)

    response = _post(code, hostname="AMH-PC", collector_version="1.1.0", contract_version=2)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == V2_KEYS
    assert (body["customer_id"], body["branch_id"], body["installation_id"]) == (10, 1, 101)
    assert (body["contract_version"], body["contract_mode"], body["timezone"]) == (2, "v2", "America/New_York")
    assert body["classification_rules"] == collector_rules.build_classification_rules(SETTINGS_A)

    written = _written(owner)
    [code_row] = written["codes"]
    assert code_row["used_at"] is not None and code_row["required_contract"] == 2
    assert written["tokens"] == [{"id": 1, "customer_id": 10, "branch_id": 1, "installation_id": 101, "is_active": True}]
    new_keys = [k for k in written["keys"] if k["key_id"] != LEGACY_KEY]
    assert new_keys == [{"key_id": body["key_id"], "customer_id": 10, "branch_id": 1, "status": "active", "retired_at": None}]
    [cutover] = [c for c in written["cutovers"] if c["customer_id"] == 10]
    # The organization's creation instant, exactly -- not the branch's (NULL here) and not the moment of enrollment.
    assert cutover["cutover_at"] == ORG_A_CREATED and cutover["branch_id"] == 1
    assert cutover["set_by"] == "collector-enrollment"
    assert cutover["note"] == f"initial v2 cutover | enrollment_code_id={code_row['id']} installation_id=101"
    # The effective boundary every reader uses is that row.
    with owner.connect() as conn:
        assert ingest_v2_service.get_effective_v2_cutover(conn, 10, 1) == ORG_A_CREATED


def test_an_enrollment_issues_its_key_owned_by_the_enrolling_installation(owner, runtime):
    key_id = _post(_code(runtime, 101), contract_version=2).json()["key_id"]

    assert _rows(owner, "SELECT installation_id FROM ingest_key_ids WHERE key_id = :k", k=key_id) == [{"installation_id": 101}]


def test_it_writes_only_the_enrolling_tenants_branch_and_leaves_every_other_tenant_untouched(owner, runtime):
    before = _written(owner)

    body = _redeem(runtime, _code(runtime, 201), contract_version=2)

    after = _written(owner)
    # Lib B enrolled: one key and one cutover, both for (11, 2), with ITS organization's creation time -- even though
    # its branch row carries a (zone-less, future) created_at of its own.
    assert [k for k in after["keys"] if k not in before["keys"]] == [
        {"key_id": body["key_id"], "customer_id": 11, "branch_id": 2, "status": "active", "retired_at": None}]
    [cutover] = [c for c in after["cutovers"] if c not in before["cutovers"]]
    assert (cutover["customer_id"], cutover["branch_id"], cutover["cutover_at"]) == (11, 2, ORG_B_CREATED)
    # Every row that existed before is exactly as it was: the hand-migrated library's key and cutover among them.
    assert [k for k in after["keys"] if k["key_id"] == LEGACY_KEY] == [k for k in before["keys"] if k["key_id"] == LEGACY_KEY]
    assert [c for c in after["cutovers"] if c["customer_id"] == 12] == before["cutovers"]
    assert CANARY_B in json.dumps(body) and CANARY_A not in json.dumps(body)
    # Nothing for Lib A at all.
    assert not [k for k in after["keys"] if k["customer_id"] == 10] and not [c for c in after["cutovers"] if c["customer_id"] == 10]


@pytest.mark.parametrize("step", ["new_agent_token", "issue_ingest_key", "record_initial_v2_cutover"])
def test_a_failure_at_any_write_rolls_all_four_back(owner, runtime, monkeypatch, step):
    code = _code(runtime, 101)
    before = _written(owner)

    def explode(*_args, **_kwargs):
        raise RuntimeError("forced failure")

    monkeypatch.setattr(enrollment, step, explode)
    response = _post(code, contract_version=2)

    assert response.status_code == 500 and response.json() == {"detail": "Internal server error"}
    assert _written(owner) == before  # the code unused; no token, no key, no cutover

    monkeypatch.undo()
    monkeypatch.setattr(main, "engine", runtime)
    assert set(_redeem(runtime, code, contract_version=2)) == V2_KEYS  # and the same code still works


def test_a_cutover_that_cannot_be_recorded_takes_the_key_and_the_token_back_with_it(owner, runtime, monkeypatch):
    # A real refusal this time, not a patched function, and at the LAST write -- after the code was claimed and the
    # token and the key were inserted in this transaction: a cutover may not be recorded with a blank set_by.
    code = _code(runtime, 101)
    before = _written(owner)
    monkeypatch.setattr(enrollment, "INITIAL_CUTOVER_SET_BY", "   ")

    with pytest.raises(ValueError, match="set_by is required"):
        _redeem(runtime, code, contract_version=2)

    assert _written(owner) == before


# --- refusals, as the runtime role ------------------------------------------------------------------------------------

def test_an_old_installer_gets_a_409_and_no_token_and_the_code_survives(owner, runtime):
    code = _code(runtime, 101)
    before = _written(owner)

    response = _post(code, hostname="AMH-PC", collector_version="1.0.13")

    assert response.status_code == 409 and response.json()["code"] == "collector_upgrade_required"
    assert _written(owner) == before
    assert _post(code, contract_version=2).status_code == 200


def test_an_active_key_on_the_branch_blocks_and_the_check_sees_it_through_row_level_security(owner, runtime):
    # The existing key is only visible to the runtime role because the enrollment set its tenant context first: with
    # no context the policy would hide the row, and a second identity would be created. This is that proof.
    with owner.begin() as conn:
        conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id) "
                          "VALUES ('1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d', 10, 1)"))
    code = _code(runtime, 101)
    before = _written(owner)

    with pytest.raises(EnrollmentRefused) as excinfo:
        _redeem(runtime, code, contract_version=2)

    assert (excinfo.value.code, excinfo.value.reason) == ("enrollment_not_ready", "active_ingest_key_exists")
    assert _written(owner) == before

    # Retired, it no longer blocks -- and is neither reused nor revived.
    with owner.begin() as conn:
        conn.execute(text("UPDATE ingest_key_ids SET status = 'retired', retired_at = now() WHERE customer_id = 10"))
    body = _redeem(runtime, code, contract_version=2)
    assert {k["key_id"]: k["status"] for k in _written(owner)["keys"] if k["customer_id"] == 10} == {
        "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d": "retired", body["key_id"]: "active"}


@pytest.mark.parametrize("cutover_at", [datetime(2026, 6, 1, tzinfo=UTC), None], ids=["a cutover", "a recorded rollback"])
def test_any_existing_cutover_row_blocks_and_is_not_changed(owner, runtime, cutover_at):
    with owner.begin() as conn:
        conn.execute(text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, note) "
                          "VALUES (10, 1, :at, 'operator', 'decided by an operator')"), {"at": cutover_at})
    code = _code(runtime, 101)
    before = _written(owner)

    with pytest.raises(EnrollmentRefused) as excinfo:
        _redeem(runtime, code, contract_version=2)

    assert excinfo.value.reason == "cutover_exists"
    assert _written(owner) == before  # row for row: nothing added, moved or rewritten


def test_a_site_that_is_not_ready_is_refused_with_nothing_written(owner, runtime):
    with owner.begin() as conn:
        conn.execute(text("UPDATE organization_settings SET settings_json = settings_json - 'collector_rules' WHERE organization_id = 1"))
    code = _code(runtime, 101)
    before = _written(owner)

    response = _post(code, contract_version=2)

    assert response.status_code == 409
    assert response.json() == {"code": "enrollment_not_ready",
                               "detail": "This installation is not ready to enroll. Contact SortView support."}
    assert CANARY_A not in response.text and _written(owner) == before


# --- legacy, as the runtime role --------------------------------------------------------------------------------------

def test_a_legacy_enrollment_is_exactly_what_it_was_and_touches_no_v2_state(owner, runtime):
    before = _written(owner)

    for installation_id, scope in ((103, (10, 1)), (301, (12, 3))):
        response = _post(_code(runtime, installation_id), hostname="AMH-PC", collector_version="1.0.13")
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == LEGACY_KEYS and (body["customer_id"], body["branch_id"]) == scope

    after = _written(owner)
    # The hand-migrated library (its own key and cutover) re-enrolled the legacy way: both are exactly as they were.
    assert after["keys"] == before["keys"] and after["cutovers"] == before["cutovers"]
    assert [c["required_contract"] for c in after["codes"]] == [1, 1]


def test_a_legacy_code_does_not_permit_a_contract_2_request(owner, runtime):
    code = _code(runtime, 301)
    before = _written(owner)

    response = _post(code, contract_version=2)

    assert response.status_code == 409 and response.json()["code"] == "contract_not_permitted"
    assert _written(owner) == before  # the hand-migrated library gets no second identity


# =====================================================================================================================
# row level security on the key, and the cutover's scoping
# =====================================================================================================================

def test_the_role_cannot_issue_a_key_without_a_tenant_context_or_for_another_tenant(owner, runtime):
    before = _written(owner)["keys"]

    # No context at all: the INSERT policy's check fails.
    with pytest.raises(DBAPIError, match="row-level security"), runtime.begin() as conn:
        ingest_v2_service.issue_ingest_key(conn, 10, 1, installation_id=None)
    # Tenant A's context, a key for tenant B: refused by the same policy.
    with pytest.raises(DBAPIError, match="row-level security"), runtime.begin() as conn:
        conn.execute(text("SELECT set_config('app.operational_customer_id', '10', true)"))
        conn.execute(text("SELECT set_config('app.operational_branch_id', '1', true)"))
        ingest_v2_service.issue_ingest_key(conn, 11, 2, installation_id=None)
    # The same customer, another branch: refused as well.
    with pytest.raises(DBAPIError, match="row-level security"), runtime.begin() as conn:
        conn.execute(text("SELECT set_config('app.operational_customer_id', '10', true)"))
        conn.execute(text("SELECT set_config('app.operational_branch_id', '1', true)"))
        conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id) "
                          "VALUES ('2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e', 10, 2)"))

    assert _written(owner)["keys"] == before


def test_the_enrollment_sets_the_context_from_the_installation_and_it_does_not_outlive_the_transaction(owner, runtime):
    code = _code(runtime, 101)

    with runtime.begin() as conn:
        result = enrollment.redeem_enrollment_code(conn, code, contract_version=2)
        context = conn.execute(text("SELECT current_setting('app.operational_customer_id', true), "
                                    "current_setting('app.operational_branch_id', true)")).one()
        # Under that context the role sees its own tenant's keys and nobody else's.
        visible = [dict(r) for r in conn.execute(text("SELECT key_id, customer_id, branch_id FROM ingest_key_ids")).mappings()]

    assert tuple(context) == ("10", "1")
    assert visible == [{"key_id": result["key_id"], "customer_id": 10, "branch_id": 1}]
    with runtime.connect() as conn:
        after = conn.execute(text("SELECT current_setting('app.operational_customer_id', true), "
                                  "current_setting('app.operational_branch_id', true)")).one()
        assert tuple(after) in {("", ""), (None, None)}  # transaction-local: gone with the transaction
        assert conn.execute(text("SELECT COUNT(*) FROM ingest_key_ids")).scalar() == 0


def test_the_request_cannot_choose_the_tenant_a_key_or_a_cutover_is_written_for(owner, runtime):
    # Nothing in the request names a tenant; extra fields that try to are ignored, and the code alone decides.
    code = _code(runtime, 101)

    response = client.post("/collector/enroll", json={
        "enrollment_code": code, "contract_version": 2, "customer_id": 12, "branch_id": 3, "installation_id": 301,
        "key_id": LEGACY_KEY, "timezone": "UTC", "cutover_at": "2020-01-01T00:00:00+00:00"})

    assert response.status_code == 200
    body = response.json()
    assert (body["customer_id"], body["branch_id"], body["installation_id"]) == (10, 1, 101)
    assert body["key_id"] != LEGACY_KEY and body["timezone"] == "America/New_York"
    written = _written(owner)
    assert {(k["customer_id"], k["branch_id"]) for k in written["keys"]} == {(10, 1), (12, 3)}
    assert {(c["customer_id"], c["branch_id"], c["cutover_at"]) for c in written["cutovers"]} == {
        (10, 1, ORG_A_CREATED), (12, 3, LEGACY_CUTOVER)}


# =====================================================================================================================
# concurrency
# =====================================================================================================================

def test_two_installations_of_one_branch_enrolling_at_once_create_exactly_one_identity(owner, runtime):
    codes = [_code(runtime, 101), _code(runtime, 102)]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def enroll(code: str) -> None:
        barrier.wait(timeout=10)
        try:
            _redeem(runtime, code, contract_version=2)
            outcomes.append("enrolled")
        except EnrollmentRefused as refused:
            outcomes.append(refused.reason)
        except Exception as exc:  # anything else is a failure of the test
            outcomes.append(f"error:{type(exc).__name__}")

    threads = [threading.Thread(target=enroll, args=(code,)) for code in codes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["active_ingest_key_exists", "enrolled"]
    written = _written(owner)
    assert len([k for k in written["keys"] if k["customer_id"] == 10]) == 1
    assert len([c for c in written["cutovers"] if c["customer_id"] == 10]) == 1
    assert len(written["tokens"]) == 1
    assert sorted(c["used_at"] is not None for c in written["codes"]) == [False, True]


def test_one_code_redeemed_twice_at_once_enrolls_once(owner, runtime):
    code = _code(runtime, 201)
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def post() -> None:
        barrier.wait(timeout=10)
        statuses.append(_post(code, contract_version=2).status_code)

    threads = [threading.Thread(target=post) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(statuses) == [200, 400]  # the loser sees a used code: the generic refusal
    written = _written(owner)
    assert len(written["tokens"]) == 1
    assert len([k for k in written["keys"] if k["customer_id"] == 11]) == 1
    assert len([c for c in written["cutovers"] if c["customer_id"] == 11]) == 1


# =====================================================================================================================
# what the enrollment means for the two upload routes
# =====================================================================================================================

def test_the_new_branchs_v1_upload_is_closed_and_its_v2_upload_works_with_the_issued_key(owner, runtime):
    body = _post(_code(runtime, 101), contract_version=2).json()
    auth = {"Authorization": f"Bearer {body['agent_token']}"}

    legacy = client.post("/upload", json=v1_payload(customer=10, branch=1), headers=auth)
    assert legacy.status_code == 403 and legacy.json() == {"detail": main.V1_UPLOAD_CLOSED_DETAIL}
    assert _rows(owner, "SELECT COUNT(*) AS n FROM checkins") == [{"n": 0}]
    assert _rows(owner, "SELECT COUNT(*) AS n FROM acs_events") == [{"n": 0}]

    accepted = client.post("/v2/upload", json={"contract_version": 2, "key_id": body["key_id"], "checkins": [checkin()]}, headers=auth)
    assert accepted.status_code == 200, accepted.text
    assert _rows(owner, "SELECT customer_id, branch_id, key_id FROM checkin_events") == [
        {"customer_id": 10, "branch_id": 1, "key_id": body["key_id"]}]

    # Another tenant's key with this token is refused, and stores nothing more.
    crossed = client.post("/v2/upload", json={"contract_version": 2, "key_id": LEGACY_KEY, "checkins": [checkin(2)]}, headers=auth)
    assert crossed.status_code == 403
    assert _rows(owner, "SELECT COUNT(*) AS n FROM checkin_events") == [{"n": 1}]


def test_a_branch_that_never_enrolled_on_contract_2_keeps_its_v1_upload(owner, runtime):
    # Lib B, enrolled the legacy way through a legacy installation -- while Lib A enrolls on Contract v2.
    with owner.begin() as conn:
        conn.execute(text("INSERT INTO collector_installations (id, organization_id, branch_id, name, status) "
                          "VALUES (202, 2, 2, 'Lib B Legacy Sorter', 'provisioning')"))
    legacy = _post(_code(runtime, 202)).json()
    _post(_code(runtime, 101), contract_version=2)

    response = client.post("/upload", json=v1_payload(customer=11, branch=2),
                           headers={"Authorization": f"Bearer {legacy['agent_token']}"})

    assert response.status_code == 200, response.text
    assert _rows(owner, "SELECT customer_id, branch_id FROM checkins") == [{"customer_id": 11, "branch_id": 2}]
    assert hashlib.sha256(legacy["agent_token"].encode()).hexdigest() in {
        r["token_hash"] for r in _rows(owner, "SELECT token_hash FROM agent_tokens")}
