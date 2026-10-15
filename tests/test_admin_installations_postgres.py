"""A2 item 5 on a REAL PostgreSQL: a library's Collector installations and enrollment codes.

services.admin_installation_service runs as a NON-OWNING, NOBYPASSRLS role holding exactly the recorded runtime
baseline (scripts/runtime_role_privileges.py), so ingest_key_ids' row level security really applies to the
"already enrolled" check. Nothing is replaced but the engine. What only a real server can prove:

  * an installation is added at the active primary branch, provisioning, on the contract asked for -- contract 2 only
    with the library's Collector settings, contract 1 without them; never for a cancelled library;
  * a rename changes the name and nothing else;
  * every allowed transition, every refused one, a concurrent change refused, and installed_at never stamped;
  * the installation's bound agent token stops and starts authenticating through main's own auth path;
  * no ingest key and no cutover is ever touched;
  * an enrollment code: only its hash stored, 30 minutes, single use, contract snapshotted, earlier unused codes revoked
    and kept as history -- and for an already-enrolled v2 branch, nothing written at all;
  * one library can never act on another's installation;
  * the audit rows name the library as target_org_slug and hold ids and counts only, never a code;
  * the customer sorter list reflects a rename and a retire through its own, unchanged rules.

OPT-IN AND SAFE BY CONSTRUCTION, as tests/test_collector_enrollment_v2_postgres.py: runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION, local server; a throwaway database and
role are created and dropped. Every value is SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import main
from scripts import runtime_role_privileges as privileges
from services import admin_installation_service as service
from services import (
    collector_enrollment_service,
    sorter_inventory_service,
    tenant_service,
)
from services.admin_installation_service import InstallationAdminError
from services.collector_enrollment_service import EnrollmentError

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL tests)")

RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only
ACTOR = {"id": 7, "email": "admin@example.invalid"}
TOKEN_101 = "synthetic-agent-token-for-installation-101"
ENROLLED_KEY = "4f1c2b3a-9d8e-4c7b-a6f5-0e1d2c3b4a59"
_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
_BUILTIN_SHA256_EXPR = "encode(sha256(convert_to(:token, 'UTF8')), 'hex')"  # pgcrypto is not on every throwaway server
READY = {"collector_timezone": "America/Chicago",
         "collector_rules": {"reviewed_at": "2026-10-01T00:00:00+00:00", "reviewed_by_user_id": 3}}


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
    name = f"sortview_admin_installations_test_{secrets.token_hex(4)}"
    role = f"sortview_admin_installations_role_{secrets.token_hex(4)}"
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
    owner, runtime = cluster
    with owner.begin() as conn:
        conn.execute(text(
            "TRUNCATE auth_audit_log, v2_cutovers, ingest_key_ids, collector_enrollment_codes, agent_tokens, "
            "collector_installations, organization_settings, branch_settings, branches, organizations, customers, "
            "app_users RESTART IDENTITY CASCADE"
        ))
        conn.execute(text(
            "INSERT INTO app_users (id, email, full_name, is_platform_admin) "
            "VALUES (7, 'admin@example.invalid', 'Test Admin', TRUE)"
        ))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Acme'), (11, 'Enrolled'), (12, 'Other'), "
                          "(15, 'Suspended')"))
        conn.execute(text(
            "INSERT INTO organizations (id, slug, name, status, operational_customer_id, created_at) VALUES "
            "(1, 'acme', 'Acme Library', 'active', 10, '2026-01-01T00:00:00+00:00'), "
            "(2, 'enrolled', 'Enrolled Library', 'active', 11, '2026-01-01T00:00:00+00:00'), "
            "(3, 'other', 'Other Library', 'trial', 12, '2026-01-01T00:00:00+00:00'), "
            "(4, 'closed', 'Closed Library', 'cancelled', NULL, '2026-01-01T00:00:00+00:00'), "
            "(5, 'paused', 'Paused Library', 'suspended', 15, '2026-01-01T00:00:00+00:00')"
        ))
        conn.execute(text(
            "INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) VALUES "
            "(1, 1, 'main', 'Main Branch', TRUE, 'active', 1), (2, 2, 'main', 'Enrolled Main', TRUE, 'active', 2), "
            "(3, 3, 'main', 'Other Main', TRUE, 'active', 3), (4, 4, 'main', 'Closed Main', TRUE, 'active', NULL), "
            "(5, 5, 'main', 'Paused Main', TRUE, 'active', 5)"
        ))
        conn.execute(text(
            "INSERT INTO organization_settings (organization_id, settings_json) VALUES "
            "(1, CAST(:ready AS JSONB)), (2, CAST(:ready AS JSONB)), (5, CAST(:ready AS JSONB))"
        ), {"ready": json.dumps(READY)})
        conn.execute(text(
            "INSERT INTO collector_installations (id, organization_id, branch_id, name, hostname, collector_version, "
            "status, required_contract, installed_at, last_seen_at) VALUES "
            "(101, 1, 1, 'Acme Sorter', 'CANARY-HOST', '9.9.9', 'active', 2, '2026-02-01T00:00:00+00', '2026-10-01T00:00:00+00'), "
            "(102, 1, 1, 'Acme Old', NULL, NULL, 'inactive', 1, NULL, NULL), "
            "(201, 2, 2, 'Enrolled Sorter', NULL, NULL, 'active', 2, '2026-02-01T00:00:00+00', NULL), "
            "(301, 3, 3, 'Other Sorter', NULL, NULL, 'provisioning', 1, NULL, NULL), "
            "(501, 5, 5, 'Paused Sorter', NULL, NULL, 'provisioning', 1, NULL, NULL)"
        ))
        conn.execute(text("SELECT setval('collector_installations_id_seq', 1000)"))
        conn.execute(text(
            "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active, installation_id) "
            "VALUES (:hash, 10, 1, 'synthetic', TRUE, 101)"
        ), {"hash": hashlib.sha256(TOKEN_101.encode()).hexdigest()})
        # The enrolled library's branch: its Collector is enrolled on Contract v2 -- an active key and its cutover.
        conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id) VALUES (:k, 11, 2)"),
                     {"k": ENROLLED_KEY})
        conn.execute(text(
            "INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, note) "
            "VALUES (11, 2, '2026-01-01T00:00:00+00:00', 'collector-enrollment', 'enrolled')"
        ))
        # An unused code the enrolled installation already has: a refused issue must not revoke it.
        conn.execute(text(
            "INSERT INTO collector_enrollment_codes (installation_id, code_hash, expires_at, required_contract) "
            "VALUES (201, 'synthetic-hash-201', now() + interval '20 minutes', 2)"
        ))
    for module in (service, tenant_service, sorter_inventory_service):
        monkeypatch.setattr(module, "get_engine", lambda: runtime)
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR))
    return owner, runtime


def _library(slug):
    library = service.get_library(slug)
    assert library is not None
    return library


def _rows(owner, sql, **params):
    with owner.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def _installation(owner, installation_id):
    (row,) = _rows(owner, "SELECT * FROM collector_installations WHERE id = :id", id=installation_id)
    return row


def _keys_and_cutovers(owner):
    return (_rows(owner, "SELECT * FROM ingest_key_ids ORDER BY id"), _rows(owner, "SELECT * FROM v2_cutovers ORDER BY id"))


def _audit(owner):
    return _rows(owner, "SELECT user_id, email, event_type, message, metadata FROM auth_audit_log ORDER BY id")


def _authenticates(runtime) -> bool:
    with runtime.connect() as conn:
        try:
            main.authenticate_agent_token(conn, f"Bearer {TOKEN_101}")
        except HTTPException as refused:
            assert refused.status_code == 403
            return False
    return True


# =====================================================================================================================
# list
# =====================================================================================================================

def test_every_installation_is_listed_with_a_code_summary_and_nothing_that_identifies_a_machine(db):
    owner, _ = db
    with owner.begin() as conn:
        conn.execute(text(
            "INSERT INTO collector_enrollment_codes (installation_id, code_hash, expires_at, used_at, required_contract) "
            "VALUES (101, 'h-used', now() - interval '1 day', now() - interval '2 days', 2), "
            "(101, 'h-pending', now() + interval '10 minutes', NULL, 2), "
            "(101, 'h-expired', now() - interval '1 minute', NULL, 2)"
        ))

    found = service.list_installations(_library("acme"))

    assert [row["id"] for row in found] == [101, 102]
    assert set(found[0]) == {"id", "name", "branch", "status", "required_contract", "installed_at", "last_seen_at",
                             "created_at", "pending_code_expires_at", "last_code_redeemed_at"}
    assert found[0]["pending_code_expires_at"] is not None and found[0]["last_code_redeemed_at"] is not None
    assert found[1]["pending_code_expires_at"] is None and found[1]["last_code_redeemed_at"] is None
    assert "CANARY-HOST" not in repr(found) and "9.9.9" not in repr(found) and "h-pending" not in repr(found)


# =====================================================================================================================
# add
# =====================================================================================================================

def test_an_installation_is_added_at_the_active_primary_branch_provisioning_and_audited(db):
    owner, _ = db

    added = service.create_installation(_library("acme"), name="  New Sorter ", required_contract=2, actor=ACTOR)

    row = _installation(owner, added["id"])
    assert (row["organization_id"], row["branch_id"], row["name"], row["status"], row["required_contract"]) == (
        1, 1, "New Sorter", "provisioning", 2)
    assert row["hostname"] is None and row["installed_at"] is None
    assert _audit(owner) == [{"user_id": 7, "email": "admin@example.invalid", "event_type": "admin_installation_created",
                              "message": "Collector installation added.",
                              "metadata": {"target_org_slug": "acme", "installation_id": added["id"], "branch_slug": "main",
                                           "required_contract": 2, "actor_user_id": 7}}]


def test_contract_2_needs_the_collector_settings_and_contract_1_does_not(db):
    owner, _ = db
    other = _library("other")

    with pytest.raises(InstallationAdminError, match="collector_settings_not_ready"):
        service.create_installation(other, name="V2 Sorter", required_contract=2, actor=ACTOR)
    added = service.create_installation(other, name="V1 Sorter", required_contract=1, actor=ACTOR)

    assert _installation(owner, added["id"])["required_contract"] == 1
    assert service.add_problems(other, service.get_primary_branch(other)) == ["collector_settings_not_ready"]
    assert service.add_problems(_library("acme"), service.get_primary_branch(_library("acme"))) == []


def test_a_cancelled_library_or_one_without_an_active_primary_branch_gets_nothing(db):
    owner, _ = db
    before = _rows(owner, "SELECT id FROM collector_installations ORDER BY id")

    with pytest.raises(InstallationAdminError, match="library_cancelled"):
        service.create_installation(_library("closed"), name="X", required_contract=1, actor=ACTOR)
    with owner.begin() as conn:
        conn.execute(text("UPDATE branches SET status = 'inactive' WHERE id = 3"))
    with pytest.raises(InstallationAdminError, match="no_active_primary_branch"):
        service.create_installation(_library("other"), name="X", required_contract=1, actor=ACTOR)

    assert _rows(owner, "SELECT id FROM collector_installations ORDER BY id") == before
    assert _audit(owner) == []


# =====================================================================================================================
# rename
# =====================================================================================================================

def test_a_rename_changes_the_name_and_nothing_else(db):
    owner, _ = db
    before = _installation(owner, 101)

    service.rename_installation(_library("acme"), 101, name=" Front Desk Sorter ", actor=ACTOR)

    after = _installation(owner, 101)
    assert after["name"] == "Front Desk Sorter"
    assert {k: v for k, v in after.items() if k not in ("name", "updated_at")} == {
        k: v for k, v in before.items() if k not in ("name", "updated_at")}
    (audit,) = _audit(owner)
    assert audit["event_type"] == "admin_installation_renamed"
    assert audit["metadata"] == {"target_org_slug": "acme", "installation_id": 101, "actor_user_id": 7}
    assert "Front Desk" not in json.dumps(audit)


# =====================================================================================================================
# status
# =====================================================================================================================

def test_deactivating_stops_the_bound_token_and_returning_to_provisioning_restores_it_without_stamping(db):
    owner, runtime = db
    keys_and_cutovers = _keys_and_cutovers(owner)
    installed_at = _installation(owner, 101)["installed_at"]
    assert _authenticates(runtime)

    service.change_status(_library("acme"), 101, status="inactive", actor=ACTOR)
    assert _installation(owner, 101)["status"] == "inactive"
    assert not _authenticates(runtime)

    service.change_status(_library("acme"), 101, status="provisioning", actor=ACTOR)
    row = _installation(owner, 101)
    assert (row["status"], row["installed_at"]) == ("provisioning", installed_at)  # active only after a heartbeat
    assert _authenticates(runtime)

    assert _keys_and_cutovers(owner) == keys_and_cutovers
    assert [(a["metadata"]["previous_status"], a["metadata"]["status"]) for a in _audit(owner)] == [
        ("active", "inactive"), ("inactive", "provisioning")]


@pytest.mark.parametrize(("start", "wanted"), [("active", "retired"), ("provisioning", "retired"), ("inactive", "retired")])
def test_retiring_from_each_live_status_stops_the_token_for_good(db, start, wanted):
    owner, runtime = db
    with owner.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET status = :s WHERE id = 101"), {"s": start})

    service.change_status(_library("acme"), 101, status=wanted, actor=ACTOR)

    assert _installation(owner, 101)["status"] == "retired"
    assert not _authenticates(runtime)
    for back in ("provisioning", "inactive", "retired"):
        with pytest.raises(InstallationAdminError, match="transition_not_allowed"):
            service.change_status(_library("acme"), 101, status=back, actor=ACTOR)
    assert _installation(owner, 101)["status"] == "retired"


def test_retiring_an_enrolled_v2_installation_leaves_its_key_and_cutover_alone(db):
    owner, _ = db
    before = _keys_and_cutovers(owner)

    service.change_status(_library("enrolled"), 201, status="retired", actor=ACTOR)

    assert _keys_and_cutovers(owner) == before


@pytest.mark.parametrize(("start", "wanted"), [
    ("active", "provisioning"), ("inactive", "inactive"), ("provisioning", "provisioning")])
def test_a_transition_the_table_does_not_allow_is_refused_with_nothing_written(db, start, wanted):
    owner, _ = db
    with owner.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET status = :s WHERE id = 101"), {"s": start})

    with pytest.raises(InstallationAdminError, match="transition_not_allowed"):
        service.change_status(_library("acme"), 101, status=wanted, actor=ACTOR)

    assert _installation(owner, 101)["status"] == start and _audit(owner) == []


def test_a_concurrent_status_change_is_refused_not_overwritten(db, monkeypatch):
    owner, _ = db
    real = service.transition_allowed

    def someone_else_retires_it_first(current, wanted):
        with owner.begin() as conn:
            conn.execute(text("UPDATE collector_installations SET status = 'retired' WHERE id = 101"))
        return real(current, wanted)

    monkeypatch.setattr(service, "transition_allowed", someone_else_retires_it_first)

    with pytest.raises(InstallationAdminError, match="transition_not_allowed"):
        service.change_status(_library("acme"), 101, status="inactive", actor=ACTOR)

    assert _installation(owner, 101)["status"] == "retired" and _audit(owner) == []


# =====================================================================================================================
# enrollment codes
# =====================================================================================================================

def test_a_code_is_stored_only_as_its_hash_lasts_30_minutes_snapshots_the_contract_and_is_audited_without_it(db):
    owner, _ = db

    issued = service.issue_enrollment_code(_library("other"), 301, actor=ACTOR)

    code = issued["enrollment_code"]
    (row,) = _rows(owner, "SELECT * FROM collector_enrollment_codes WHERE installation_id = 301")
    assert row["code_hash"] == collector_enrollment_service.hash_enrollment_code(code)
    assert code not in json.dumps(row, default=str)
    # expires_at is the service's clock plus 30 minutes; created_at is the database's own NOW() of the same moment.
    assert row["expires_at"] == issued["expires_at"]
    assert abs(row["expires_at"] - row["created_at"] - timedelta(minutes=30)) < timedelta(seconds=1)
    assert (issued["ttl_minutes"], issued["required_contract"], row["required_contract"]) == (30, 1, 1)
    (audit,) = _audit(owner)
    assert audit["event_type"] == "admin_enrollment_code_issued"
    assert audit["metadata"] == {
        "target_org_slug": "other", "installation_id": 301, "enrollment_code_id": row["id"], "required_contract": 1,
        "expires_at": issued["expires_at"].isoformat(), "revoked_previous_count": 0, "actor_user_id": 7}
    assert code not in json.dumps(_audit(owner), default=str) and row["code_hash"] not in json.dumps(_audit(owner), default=str)


def test_a_new_code_revokes_the_earlier_unused_one_keeps_history_and_is_single_use(db):
    owner, runtime = db
    first = service.issue_enrollment_code(_library("other"), 301, actor=ACTOR)["enrollment_code"]
    second = service.issue_enrollment_code(_library("other"), 301, actor=ACTOR)

    assert second["revoked_previous_count"] == 1
    with runtime.begin() as conn, pytest.raises(EnrollmentError):
        collector_enrollment_service.redeem_enrollment_code(conn, first)
    with runtime.begin() as conn:
        collector_enrollment_service.redeem_enrollment_code(conn, second["enrollment_code"])
    with runtime.begin() as conn, pytest.raises(EnrollmentError):
        collector_enrollment_service.redeem_enrollment_code(conn, second["enrollment_code"])

    rows = _rows(owner, "SELECT used_at, revoked_at FROM collector_enrollment_codes WHERE installation_id = 301 ORDER BY id")
    assert [(row["used_at"] is not None, row["revoked_at"] is not None) for row in rows] == [(False, True), (True, False)]
    assert service.list_installations(_library("other"))[0]["last_code_redeemed_at"] is not None


def test_an_already_enrolled_v2_branch_gets_no_code_and_nothing_is_revoked(db):
    owner, _ = db
    before = _rows(owner, "SELECT * FROM collector_enrollment_codes ORDER BY id")

    with pytest.raises(InstallationAdminError, match="branch_already_enrolled"):
        service.issue_enrollment_code(_library("enrolled"), 201, actor=ACTOR)

    assert _rows(owner, "SELECT * FROM collector_enrollment_codes ORDER BY id") == before
    assert _audit(owner) == []


def test_a_cutover_alone_also_means_already_enrolled(db):
    owner, _ = db
    with owner.begin() as conn:
        conn.execute(text("UPDATE ingest_key_ids SET status = 'retired', retired_at = now()"))

    with pytest.raises(InstallationAdminError, match="branch_already_enrolled"):
        service.issue_enrollment_code(_library("enrolled"), 201, actor=ACTOR)


def test_an_active_key_alone_means_already_enrolled_read_under_row_level_security(db, monkeypatch):
    # No cutover: only the key says so, and ingest_key_ids is under row level security for this role -- so the check
    # must read it in the branch's own tenant context. Without that context it would see no key and issue the code.
    owner, _ = db
    with owner.begin() as conn:
        conn.execute(text("DELETE FROM v2_cutovers"))

    with pytest.raises(InstallationAdminError, match="branch_already_enrolled"):
        service.issue_enrollment_code(_library("enrolled"), 201, actor=ACTOR)

    monkeypatch.setattr(collector_enrollment_service, "_set_tenant_context", lambda conn, scope: None)
    issued = service.issue_enrollment_code(_library("enrolled"), 201, actor=ACTOR)  # the context is what found the key
    assert issued["enrollment_code"].startswith("SV-")


def test_a_v2_installation_of_a_branch_not_yet_enrolled_gets_its_code(db):
    issued = service.issue_enrollment_code(_library("acme"), 101, actor=ACTOR)
    assert issued["required_contract"] == 2 and issued["enrollment_code"].startswith("SV-")


@pytest.mark.parametrize(("slug", "installation_id", "code"), [
    ("acme", 102, "installation_not_enrollable"),     # inactive
    ("paused", 501, "library_not_enrollable"),        # a suspended library
    ("closed", 101, "library_cancelled"),
])
def test_a_code_the_installation_or_library_cannot_have_is_refused_with_nothing_written(db, slug, installation_id, code):
    owner, _ = db
    before = _rows(owner, "SELECT * FROM collector_enrollment_codes ORDER BY id")

    with pytest.raises(InstallationAdminError, match=code):
        service.issue_enrollment_code(_library(slug), installation_id, actor=ACTOR)

    assert _rows(owner, "SELECT * FROM collector_enrollment_codes ORDER BY id") == before


# =====================================================================================================================
# isolation and the customer's view
# =====================================================================================================================

def test_one_library_can_never_act_on_another_librarys_installation(db):
    owner, _ = db
    before = (_installation(owner, 301), _rows(owner, "SELECT * FROM collector_enrollment_codes ORDER BY id"))
    acme = _library("acme")

    for act in (
        lambda: service.rename_installation(acme, 301, name="Hijacked", actor=ACTOR),
        lambda: service.change_status(acme, 301, status="retired", actor=ACTOR),
        lambda: service.issue_enrollment_code(acme, 301, actor=ACTOR),
    ):
        with pytest.raises(InstallationAdminError, match="installation_not_found"):
            act()

    assert (_installation(owner, 301), _rows(owner, "SELECT * FROM collector_enrollment_codes ORDER BY id")) == before
    assert _audit(owner) == []


def test_the_customer_sorter_list_follows_a_rename_and_a_retire_by_its_own_rules(db):
    service.rename_installation(_library("acme"), 101, name="Front Desk Sorter", actor=ACTOR)
    (site,) = sorter_inventory_service.list_sorter_sites("acme")
    assert (site.name, site.collector_count) == ("Front Desk Sorter", 1)

    service.change_status(_library("acme"), 101, status="retired", actor=ACTOR)
    (site,) = sorter_inventory_service.list_sorter_sites("acme")
    assert (site.name, site.status, site.collector_count) == ("Acme Old", "inactive", 0)  # the retired one is gone
