"""Row Level Security, phase 1, on a REAL PostgreSQL server: checkins, rejects,
acs_events, checkin_events, reject_events, acs_item_events, ingest_key_ids.

pipeline_status is deliberately OUT OF SCOPE (see the phase 1 migration's own
docstring) and is not touched by anything in this file.

THREAT MODEL, stated explicitly so these tests aren't read as claiming more
than they prove: this is Category A defense-in-depth against an application
query that accidentally omits its own tenant WHERE clause. It is NOT a
boundary against a compromised runtime credential -- any session actually
authenticated as the runtime role can set the same session context these
policies check. These tests prove the Category-A property (an unscoped query
is still isolated, missing/emptied context fails closed) -- they do not, and
cannot, prove protection against a session that deliberately asserts another
tenant's context, because that is not what this design provides.

EVERY RLS-behavior test here runs against a genuinely non-owning,
non-BYPASSRLS role created fresh in each throwaway database, matching
production sortview_app's attributes exactly (LOGIN, NOSUPERUSER, NOCREATEDB,
NOCREATEROLE, NOREPLICATION, NOBYPASSRLS, INHERIT, owns nothing). Running
these assertions through the table owner or a superuser would prove nothing,
since RLS does not apply to either.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as
tests/test_ingest_v2_postgres.py: runs only when SORTVIEW_TEST_POSTGRES_URL
points at a maintenance database on a NON-PRODUCTION, local server. Each test
module creates its own throwaway database, migrates it with the project's
real Alembic chain (through and including this phase's RLS migration),
creates and later drops its own throwaway runtime role, and drops the
database afterward. Production is never touched.

PRODUCTION GRANT GAP DISCOVERED WHILE WRITING THESE TESTS, NOW FIXED BY
MIGRATION 67d06f4ccd24 (not yet applied to production -- that requires
separate, explicit authorization): INSERT ... ON CONFLICT (id) DO NOTHING
(the exact statement the checkins_clean/rejects_clean sync triggers use)
requires SELECT privilege on the target table in PostgreSQL, not just
INSERT -- confirmed empirically, not merely assumed from documentation.
The already-executed production sortview_app grant script gave
checkins_clean/rejects_clean INSERT only, which is insufficient on its
own. Rather than granting the missing SELECT (which would open an
unprotected cross-tenant read path on two tables outside the RLS tranche
that carry the same sensitive columns as checkins/rejects), migration
67d06f4ccd24 makes both sync trigger functions SECURITY DEFINER instead,
and REVOKEs sortview_app's INSERT on both tables -- the runtime role needs,
and after that migration has, NO privilege of any kind on either table.
This file's _create_runtime_role reflects that end state: zero grants on
checkins_clean/rejects_clean. See tests/test_trigger_security_postgres.py
for the dedicated before/after migration tests.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import database
import main
from customer_api import tenant_scope
from services import session_service, tenant_resolution_service
from tenant_db import apply_tenant_context, tenant_connection

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL RLS tests)"
)

# Tenant A / tenant B, in the OPERATIONAL (customer_id, branch_id) domain --
# the same domain the RLS policies and set_config() calls use.
CUSTOMER_A, BRANCH_A = 101, 11
CUSTOMER_B, BRANCH_B = 202, 22
TOKEN_A = "CANARY-RLS-TOKEN-A-9101"
TOKEN_B = "CANARY-RLS-TOKEN-B-9102"

# pgcrypto's digest() is not installed on every throwaway/local server --
# same substitution tests/test_ingest_v2_postgres.py already uses.
_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
_BUILTIN_SHA256_EXPR = "encode(sha256(convert_to(:token, 'UTF8')), 'hex')"

RLS_TABLES = ("checkins", "rejects", "acs_events", "checkin_events", "reject_events", "acs_item_events", "ingest_key_ids")


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
    """A brand-new database on the test server, migrated to head (which
    includes this phase's RLS migration), dropped when the context exits."""

    def __init__(self, revision: str | None):
        self.revision = revision
        admin = make_url(ADMIN_URL)
        _guard(admin)
        self.admin = admin
        self.name = f"sortview_rls_test_{secrets.token_hex(4)}"
        self.admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")

    def __enter__(self):
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{self.name}"'))  # nosec B608 - generated name, no user input
        self.url = self.admin.set(database=self.name)
        if self.revision:
            migrated = _alembic(self.url, "upgrade", self.revision)
            assert migrated.returncode == 0, migrated.stderr[-2000:]
        return self

    def __exit__(self, *_exc):
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))  # nosec B608
        self.admin_engine.dispose()


RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only


def _create_runtime_role(owner_engine, role_name: str) -> None:
    """Mirrors production sortview_app's reviewed attributes and the phase 1
    grant subset (the seven RLS tables + their sequences, plus read access
    to the tables main.py's agent-token lookup joins against)."""
    with owner_engine.begin() as conn:
        conn.execute(text(f"""
            CREATE ROLE {role_name}
                LOGIN
                NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT
                CONNECTION LIMIT -1
                PASSWORD '{RUNTIME_ROLE_PASSWORD}'
        """))  # nosec B608 - role_name is a fixed test constant, password is a fresh local secret
        conn.execute(text(f"GRANT CONNECT ON DATABASE {conn.engine.url.database} TO {role_name}"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role_name}"))

        for table in RLS_TABLES:
            privs = "SELECT, INSERT, UPDATE" if table == "ingest_key_ids" else "SELECT, INSERT"
            conn.execute(text(f"GRANT {privs} ON TABLE public.{table} TO {role_name}"))
            conn.execute(text(f"GRANT USAGE ON SEQUENCE public.{table}_id_seq TO {role_name}"))

        # Needed by main.py's agent-token lookup/last_used_at update, and by
        # the collector-installation heartbeat linkage -- outside this
        # phase's RLS scope, but required for the v1/v2 endpoint tests to
        # exercise the real API end to end as this role.
        conn.execute(text(f"GRANT SELECT, UPDATE ON TABLE public.agent_tokens TO {role_name}"))
        conn.execute(text(f"GRANT SELECT ON TABLE public.organizations TO {role_name}"))
        conn.execute(text(f"GRANT SELECT ON TABLE public.branches TO {role_name}"))
        conn.execute(text(f"GRANT SELECT ON TABLE public.collector_installations TO {role_name}"))
        conn.execute(text(f"GRANT SELECT ON TABLE public.customers TO {role_name}"))
        # v1 POST /upload reads v2_cutovers on every request since commit
        # 1394b41 ("Close v1 uploads after v2 cutover":
        # main.v1_upload_closed_since -> get_effective_v2_cutover). That
        # commit landed after this fixture was written and the grant was
        # never added here, so the upload answered 500 (permission denied)
        # as this role. Production sortview_app holds exactly this: SELECT
        # only on v2_cutovers, and no access to its sequence (confirmed in
        # the 2026-10-01 privilege inventory; recorded in
        # scripts/runtime_role_privileges.py).
        conn.execute(text(f"GRANT SELECT ON TABLE public.v2_cutovers TO {role_name}"))
        # Deliberately NO grant of any kind on checkins_clean/rejects_clean.
        # sync_checkins_to_clean()/sync_rejects_to_clean() are SECURITY
        # DEFINER as of migration 67d06f4ccd24, so their INSERT ... ON
        # CONFLICT (id) DO NOTHING runs with the function OWNER's
        # privileges, never the calling role's -- the runtime role needs
        # no SELECT and no INSERT on either table for the trigger to work.
        # Both tables are outside the RLS tranche and carry the same
        # patron-checkout-adjacent columns as checkins/rejects, so a grant
        # here would be a genuine unprotected cross-tenant read path, not
        # a redundant convenience -- see tests/test_trigger_security_postgres.py
        # for the dedicated tests proving both the denial and that the
        # trigger still works despite it.
        # pipeline_status already has this grant in production from the
        # completed runtime-role-separation phase, independent of RLS (it
        # gets no RLS policy this phase -- deliberately out of scope).
        # Granted here only so validate_tenant_schema()'s information_schema
        # check sees it exactly as production does.
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE ON TABLE public.pipeline_status TO {role_name}"))


def _drop_runtime_role(owner_engine, role_name: str) -> None:
    with owner_engine.begin() as conn:
        # DROP ROLE refuses while any privilege grant to the role still
        # exists (Postgres: "cannot be dropped because some objects depend
        # on it" -- every GRANT counts as a dependency). DROP OWNED BY
        # revokes every privilege this role holds (and drops anything it
        # owns, though this role owns nothing) before the role itself goes.
        conn.execute(text(f"DROP OWNED BY {role_name}"))  # nosec B608
        conn.execute(text(f"DROP ROLE IF EXISTS {role_name}"))  # nosec B608


def _seed(owner_engine) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE checkin_events, reject_events, acs_item_events, ingest_key_ids, checkins, rejects, "
            "acs_events, checkins_clean, rejects_clean, agent_tokens, branches, organizations, customers "
            "RESTART IDENTITY CASCADE"
        ))
        for org_id, customer, branch, slug in ((1, CUSTOMER_A, BRANCH_A, "tenant-a"), (2, CUSTOMER_B, BRANCH_B, "tenant-b")):
            conn.execute(text("INSERT INTO customers (id, name) VALUES (:c, :n)"), {"c": customer, "n": slug})
            conn.execute(text(
                "INSERT INTO organizations (id, slug, name, status, operational_customer_id) "
                "VALUES (:o, :s, :s, 'active', :c)"
            ), {"o": org_id, "s": slug, "c": customer})
            conn.execute(text(
                "INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) "
                "VALUES (:b, :o, 'main', 'Main', 'active', :b)"
            ), {"b": branch, "o": org_id})
        for token, customer, branch in ((TOKEN_A, CUSTOMER_A, BRANCH_A), (TOKEN_B, CUSTOMER_B, BRANCH_B)):
            conn.execute(text(
                "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active) "
                "VALUES (:h, :c, :b, 't', TRUE)"
            ), {"h": hashlib.sha256(token.encode()).hexdigest(), "c": customer, "b": branch})


@pytest.fixture(scope="module")
def cluster():
    """One throwaway database + one throwaway runtime role for the whole
    module -- the role is a cluster-level object in Postgres (not scoped to
    one database), so it's created and dropped explicitly, independent of
    the database's own lifecycle."""
    with Throwaway("head") as db:
        owner_engine = create_engine(db.url, hide_parameters=True)
        role_name = f"sortview_rls_test_role_{secrets.token_hex(4)}"
        _create_runtime_role(owner_engine, role_name)
        try:
            runtime_url = db.url.set(username=role_name, password=RUNTIME_ROLE_PASSWORD)
            runtime_engine = create_engine(runtime_url, hide_parameters=True)
            try:
                yield owner_engine, runtime_engine
            finally:
                runtime_engine.dispose()
        finally:
            _drop_runtime_role(owner_engine, role_name)
            owner_engine.dispose()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


@pytest.fixture
def owner_engine(cluster):
    owner_engine, _runtime_engine = cluster
    _seed(owner_engine)
    return owner_engine


@pytest.fixture
def runtime_engine(cluster):
    _owner_engine, runtime_engine = cluster
    return runtime_engine


def _set_context(conn, customer_id, branch_id) -> None:
    conn.execute(text("SELECT set_config('app.operational_customer_id', :v, true)"), {"v": str(customer_id)})
    conn.execute(text("SELECT set_config('app.operational_branch_id', :v, true)"), {"v": str(branch_id)})


def _insert_checkin(conn, customer_id, branch_id, barcode="CANARY-BC-0001"):
    conn.execute(text("""
        INSERT INTO checkins (customer_id, branch_id, event_time, title, barcode, destination, bin, source_file)
        VALUES (:c, :b, now(), 'title', :barcode, 'Main', 'bin1', 'rls_test.csv')
    """), {"c": customer_id, "b": branch_id, "barcode": barcode})


# --- same-tenant SELECT / cross-tenant isolation -----------------------------

def test_same_tenant_select_sees_its_own_rows(owner_engine, runtime_engine):
    with owner_engine.begin() as conn:
        _insert_checkin(conn, CUSTOMER_A, BRANCH_A, "CANARY-BC-A")
        _insert_checkin(conn, CUSTOMER_B, BRANCH_B, "CANARY-BC-B")

    with runtime_engine.connect() as conn:
        _set_context(conn, CUSTOMER_A, BRANCH_A)
        rows = conn.execute(text("SELECT barcode FROM checkins")).fetchall()

    assert [r[0] for r in rows] == ["CANARY-BC-A"]


def test_deliberately_unscoped_select_is_still_isolated_across_all_seven_tables(owner_engine, runtime_engine):
    with owner_engine.begin() as conn:
        _insert_checkin(conn, CUSTOMER_A, BRANCH_A, "CANARY-A")
        _insert_checkin(conn, CUSTOMER_B, BRANCH_B, "CANARY-B")
        conn.execute(text("""
            INSERT INTO rejects (customer_id, branch_id, event_time, barcode, error_message, source_file)
            VALUES (:c, :b, now(), 'RJ-1', 'jam', 'rls_test.csv')
        """), {"c": CUSTOMER_B, "b": BRANCH_B})
        conn.execute(text("""
            INSERT INTO acs_events (customer_id, branch_id, event_time, message_code, barcode, barcode_key, source_file)
            VALUES (:c, :b, now(), '101YNY', 'HOLD-1', 'HOLDKEY-1', 'rls_test.csv')
        """), {"c": CUSTOMER_B, "b": BRANCH_B})
        conn.execute(text("""
            INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, algorithm, status)
            VALUES ('3db44444-931c-43cc-af3c-b1001443e761', :c, :b, 'hmac-sha256-v1', 'active')
        """), {"c": CUSTOMER_B, "b": BRANCH_B})

    with runtime_engine.connect() as conn:
        _set_context(conn, CUSTOMER_A, BRANCH_A)
        # deliberately unscoped -- no WHERE clause at all, proving RLS (not
        # application-layer filtering) is what's protecting these reads
        assert conn.execute(text("SELECT COUNT(*) FROM checkins")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM rejects")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM acs_events")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM ingest_key_ids")).scalar() == 0


# --- cross-tenant write denial ------------------------------------------------

def test_cross_tenant_insert_is_denied_on_every_table(owner_engine, runtime_engine):
    inserts = {
        "checkins": "INSERT INTO checkins (customer_id, branch_id, event_time, barcode) VALUES (:c, :b, now(), 'x')",
        "rejects": "INSERT INTO rejects (customer_id, branch_id, event_time, barcode, error_message) VALUES (:c, :b, now(), 'x', 'jam')",
        "acs_events": "INSERT INTO acs_events (customer_id, branch_id, event_time, message_code) VALUES (:c, :b, now(), '101YNY')",
        "ingest_key_ids": (
            "INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, algorithm, status) "
            "VALUES ('6b0c9b37-aba4-4c93-b09b-c562977ff157', :c, :b, 'hmac-sha256-v1', 'active')"
        ),
    }
    for sql in inserts.values():
        # conn.begin() must run BEFORE any execute() -- _set_context's own
        # execute() calls would otherwise trigger SQLAlchemy's autobegin
        # first, and a later conn.begin() then conflicts with the
        # already-open implicit transaction.
        with runtime_engine.connect() as conn, conn.begin() as trans:
            _set_context(conn, CUSTOMER_A, BRANCH_A)
            with pytest.raises(Exception, match="row-level security"):
                conn.execute(text(sql), {"c": CUSTOMER_B, "b": BRANCH_B})
            # the failed INSERT left the transaction aborted; roll it back
            # explicitly rather than letting the with-block try to commit
            # an aborted transaction on clean exit.
            trans.rollback()


def test_cross_tenant_update_is_denied_on_ingest_key_ids(owner_engine, runtime_engine):
    with owner_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, algorithm, status)
            VALUES ('ced68da9-90c0-4d06-91fb-c6ffebf31b93', :c, :b, 'hmac-sha256-v1', 'active')
        """), {"c": CUSTOMER_B, "b": BRANCH_B})

    with runtime_engine.connect() as conn, conn.begin():
        _set_context(conn, CUSTOMER_A, BRANCH_A)
        result = conn.execute(text(
            "UPDATE ingest_key_ids SET status = 'retired' WHERE key_id = 'ced68da9-90c0-4d06-91fb-c6ffebf31b93'"
        ))
        # RLS's USING clause filters the target row out entirely for a
        # session in tenant A's context -- zero rows matched, not an error.
        assert result.rowcount == 0

    with owner_engine.connect() as conn:
        status = conn.execute(text(
            "SELECT status FROM ingest_key_ids WHERE key_id = 'ced68da9-90c0-4d06-91fb-c6ffebf31b93'"
        )).scalar()
    assert status == "active"  # untouched


def test_immutable_event_tables_deny_update_even_with_matching_context(owner_engine, runtime_engine):
    with owner_engine.begin() as conn:
        _insert_checkin(conn, CUSTOMER_A, BRANCH_A, "CANARY-IMMUTABLE")

    with runtime_engine.connect() as conn, conn.begin() as trans:
        _set_context(conn, CUSTOMER_A, BRANCH_A)
        with pytest.raises(Exception, match="permission denied|policy"):
            # No UPDATE grant AND no UPDATE policy -- fails at the grant
            # layer before RLS is even reached, which is itself the
            # point: this table is immutable by construction, twice over.
            conn.execute(text("UPDATE checkins SET title = 'changed' WHERE barcode = 'CANARY-IMMUTABLE'"))
        trans.rollback()


# --- fail-closed context handling --------------------------------------------

def test_no_context_set_fails_closed(owner_engine, runtime_engine):
    with owner_engine.begin() as conn:
        _insert_checkin(conn, CUSTOMER_A, BRANCH_A, "CANARY-NOCTX")

    with runtime_engine.connect() as conn:
        # no set_config call at all in this session
        rows = conn.execute(text("SELECT * FROM checkins")).fetchall()
    assert rows == []


def test_empty_string_context_fails_closed_not_a_cast_error(owner_engine, runtime_engine):
    with owner_engine.begin() as conn:
        _insert_checkin(conn, CUSTOMER_A, BRANCH_A, "CANARY-EMPTYCTX")

    with runtime_engine.connect() as conn:
        # simulates a reused session where the GUC was set then cleared to
        # '' rather than truly unset -- must resolve to NULL via NULLIF,
        # not raise an int-cast error
        conn.execute(text("SELECT set_config('app.operational_customer_id', '', true)"))
        conn.execute(text("SELECT set_config('app.operational_branch_id', '', true)"))
        rows = conn.execute(text("SELECT * FROM checkins")).fetchall()
    assert rows == []


# --- pooled-connection non-leak ----------------------------------------------

def test_pooled_connection_does_not_leak_context_to_the_next_checkout(owner_engine, runtime_engine):
    with owner_engine.begin() as conn:
        _insert_checkin(conn, CUSTOMER_A, BRANCH_A, "CANARY-POOL")

    with runtime_engine.connect() as conn:
        _set_context(conn, CUSTOMER_A, BRANCH_A)
        assert conn.execute(text("SELECT COUNT(*) FROM checkins")).scalar() == 1
        # set_config(..., true) is transaction-local; this connection never
        # opened an explicit transaction, so autocommit-per-statement means
        # each statement is already its own transaction -- the context set
        # above should NOT still be visible on a fresh checkout below.

    with runtime_engine.connect() as second_conn:
        rows = second_conn.execute(text("SELECT COUNT(*) FROM checkins")).scalar()
    assert rows == 0


# --- v1 / v2 endpoints, end to end, as the runtime role ----------------------

@pytest.fixture
def api(runtime_engine, monkeypatch):
    monkeypatch.setattr(main, "engine", runtime_engine)
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", True)
    assert _HASH_EXPR in main._AGENT_TOKEN_LOOKUP_SQL
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR))
    return TestClient(main.app, raise_server_exceptions=False)


def test_v1_upload_works_under_rls_as_the_runtime_role(owner_engine, api):
    resp = api.post(
        "/upload",
        json={
            "checkins": [{
                "customer_id": CUSTOMER_A, "branch_id": BRANCH_A, "event_time": datetime.now(UTC).isoformat(),
                "title": "t", "barcode": "CANARY-V1-BC", "destination": "Main", "bin": "1", "source_file": "f.csv",
            }],
            "rejects": [], "acs": [],
        },
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )
    assert resp.status_code == 200, resp.text

    with owner_engine.connect() as conn:
        row = conn.execute(text("SELECT customer_id, branch_id FROM checkins WHERE barcode = 'CANARY-V1-BC'")).first()
    assert row == (CUSTOMER_A, BRANCH_A)


def test_v2_upload_works_under_rls_as_the_runtime_role(owner_engine, api):
    with owner_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, algorithm, status)
            VALUES ('9d08ac7a-9217-4620-a6e0-89cb1113fc45', :c, :b, 'hmac-sha256-v1', 'active')
        """), {"c": CUSTOMER_A, "b": BRANCH_A})

    resp = api.post(
        "/v2/upload",
        json={
            "contract_version": 2, "key_id": "9d08ac7a-9217-4620-a6e0-89cb1113fc45",
            "checkins": [{
                "event_key": hashlib.sha256(b"v2-canary").hexdigest(), "event_time": datetime.now(UTC).isoformat(),
                "item_key": hashlib.sha256(b"v2-canary-item").hexdigest(), "destination": "westside", "bin": "3",
            }],
        },
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )
    assert resp.status_code == 200, resp.text

    with owner_engine.connect() as conn:
        count = conn.execute(text(
            "SELECT COUNT(*) FROM checkin_events WHERE customer_id = :c AND branch_id = :b"
        ), {"c": CUSTOMER_A, "b": BRANCH_A}).scalar()
    assert count == 1


def test_v2_status_works_under_rls_as_the_runtime_role(owner_engine, api):
    with owner_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, algorithm, status)
            VALUES ('8eff6f2e-6d25-4f47-8a2a-f3d8febc8689', :c, :b, 'hmac-sha256-v1', 'active')
        """), {"c": CUSTOMER_A, "b": BRANCH_A})

    resp = api.post(
        "/v2/status",
        json={"contract_version": 2, "key_id": "8eff6f2e-6d25-4f47-8a2a-f3d8febc8689", "status": "healthy"},
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )
    assert resp.status_code == 200, resp.text

    with owner_engine.connect() as conn:
        health = conn.execute(text(
            "SELECT health_status FROM ingest_key_ids WHERE key_id = '8eff6f2e-6d25-4f47-8a2a-f3d8febc8689'"
        )).scalar()
    assert health == "healthy"


def test_v2_status_with_collector_diagnostics_works_as_the_runtime_role_with_no_new_grant(owner_engine, api):
    # The four diagnostic columns (migration c8d5f2a47e91) are written by the same UPDATE, under the same row level
    # security UPDATE policy and the same table-level grant _create_runtime_role already gives: nothing was added to
    # the role for them, and tenant B's key is out of tenant A's reach exactly as before.
    key_a, key_b = "5a1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d", "6b2d3e4f-5061-4b7c-9d8e-0f1a2b3c4d5e"
    with owner_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, algorithm, status)
            VALUES (:a, :ca, :ba, 'hmac-sha256-v1', 'active'), (:b, :cb, :bb, 'hmac-sha256-v1', 'active')
        """), {"a": key_a, "ca": CUSTOMER_A, "ba": BRANCH_A, "b": key_b, "cb": CUSTOMER_B, "bb": BRANCH_B})
    next_run = (datetime.now(UTC) + timedelta(minutes=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"contract_version": 2, "status": "healthy", "collector_next_run_at": next_run,
            "collector_run_duration_ms": 3240, "collector_schedule_status": "healthy"}

    accepted = api.post("/v2/status", json={**body, "key_id": key_a}, headers={"Authorization": f"Bearer {TOKEN_A}"})
    foreign = api.post("/v2/status", json={**body, "key_id": key_b}, headers={"Authorization": f"Bearer {TOKEN_A}"})

    assert accepted.status_code == 200, accepted.text
    assert foreign.status_code == 403
    with owner_engine.connect() as conn:
        stored = dict(conn.execute(text(
            "SELECT key_id, collector_schedule_status FROM ingest_key_ids WHERE key_id IN (:a, :b)"
        ), {"a": key_a, "b": key_b}).all())
        duration = conn.execute(text(
            "SELECT collector_run_duration_ms FROM ingest_key_ids WHERE key_id = :a"), {"a": key_a}).scalar()
    assert stored == {key_a: "healthy", key_b: None} and duration == 3240


# --- trigger path -------------------------------------------------------------

def test_checkins_and_rejects_trigger_sync_still_fires_under_rls(owner_engine, runtime_engine):
    with runtime_engine.connect() as conn, conn.begin():
        _set_context(conn, CUSTOMER_A, BRANCH_A)
        _insert_checkin(conn, CUSTOMER_A, BRANCH_A, "CANARY-TRIGGER-CHECKIN")
        conn.execute(text("""
            INSERT INTO rejects (customer_id, branch_id, event_time, barcode, error_message, source_file)
            VALUES (:c, :b, now(), 'CANARY-TRIGGER-REJECT', 'jam', 'rls_test.csv')
        """), {"c": CUSTOMER_A, "b": BRANCH_A})

    with owner_engine.connect() as conn:
        clean_checkin = conn.execute(text(
            "SELECT barcode FROM checkins_clean WHERE barcode = 'CANARY-TRIGGER-CHECKIN'"
        )).scalar()
        clean_reject = conn.execute(text(
            "SELECT barcode FROM rejects_clean WHERE barcode = 'CANARY-TRIGGER-REJECT'"
        )).scalar()
    assert clean_checkin == "CANARY-TRIGGER-CHECKIN"
    assert clean_reject == "CANARY-TRIGGER-REJECT"


# --- dashboard loader behavior, through the real refactored _read_table -----

def test_dashboard_loader_is_tenant_isolated_under_rls(owner_engine, runtime_engine, monkeypatch):
    import data_loader as dl

    with owner_engine.begin() as conn:
        _insert_checkin(conn, CUSTOMER_A, BRANCH_A, "CANARY-DASH-A")
        _insert_checkin(conn, CUSTOMER_B, BRANCH_B, "CANARY-DASH-B")

    monkeypatch.setattr(dl, "get_engine", lambda: runtime_engine)

    frame_a = dl._load_checkins_history_from_db(CUSTOMER_A, BRANCH_A)
    frame_b = dl._load_checkins_history_from_db(CUSTOMER_B, BRANCH_B)

    assert list(frame_a["barcode"]) == ["CANARY-DASH-A"]
    assert list(frame_b["barcode"]) == ["CANARY-DASH-B"]


# --- information_schema path is unaffected by the optional-context change ---

def test_validate_tenant_schema_is_unaffected_by_the_context_change(owner_engine, runtime_engine, monkeypatch):
    import data_loader as dl

    monkeypatch.setattr(dl, "get_engine", lambda: runtime_engine)

    errors = dl.validate_tenant_schema()

    assert errors == []


# --- tenant_db seam (src/tenant_db.py), as the runtime role ------------------

def test_tenant_connection_isolates_an_unscoped_select(owner_engine, runtime_engine):
    with owner_engine.begin() as conn:
        _insert_checkin(conn, CUSTOMER_A, BRANCH_A, "CANARY-SEAM-A")
        _insert_checkin(conn, CUSTOMER_B, BRANCH_B, "CANARY-SEAM-B")

    # deliberately unscoped -- only the context tenant_connection applied
    # on this same connection can be what restricts the rows
    with tenant_connection(runtime_engine, CUSTOMER_A, BRANCH_A) as conn:
        rows_a = conn.execute(text("SELECT barcode FROM checkins")).fetchall()
    with tenant_connection(runtime_engine, CUSTOMER_B, BRANCH_B) as conn:
        rows_b = conn.execute(text("SELECT barcode FROM checkins")).fetchall()

    assert [r[0] for r in rows_a] == ["CANARY-SEAM-A"]
    assert [r[0] for r in rows_b] == ["CANARY-SEAM-B"]


def test_apply_tenant_context_sets_both_settings_and_enforces_insert_check(owner_engine, runtime_engine):
    # conn.begin() before any execute() -- see test_cross_tenant_insert_is_denied_on_every_table
    with runtime_engine.connect() as conn, conn.begin() as trans:
        apply_tenant_context(conn, CUSTOMER_A, BRANCH_A)
        settings = conn.execute(text(
            "SELECT current_setting('app.operational_customer_id', true), "
            "current_setting('app.operational_branch_id', true)"
        )).one()
        assert tuple(settings) == (str(CUSTOMER_A), str(BRANCH_A))

        _insert_checkin(conn, CUSTOMER_A, BRANCH_A, "CANARY-SEAM-OWN")  # own tenant: WITH CHECK passes
        with pytest.raises(Exception, match="row-level security"):
            _insert_checkin(conn, CUSTOMER_B, BRANCH_B, "CANARY-SEAM-OTHER")
        # the failed INSERT aborted the transaction; roll it back explicitly
        trans.rollback()


def test_tenant_connection_context_ends_with_the_block(owner_engine, runtime_engine):
    with owner_engine.begin() as conn:
        _insert_checkin(conn, CUSTOMER_A, BRANCH_A, "CANARY-SEAM-POOL")

    # A one-connection pool on the same runtime role makes the reuse
    # deterministic: the second checkout must be the same server backend.
    single = create_engine(runtime_engine.url, pool_size=1, max_overflow=0, hide_parameters=True)
    try:
        with tenant_connection(single, CUSTOMER_A, BRANCH_A) as conn:
            pid_inside = conn.execute(text("SELECT pg_backend_pid()")).scalar()
            assert conn.execute(text("SELECT COUNT(*) FROM checkins")).scalar() == 1

        with single.connect() as conn:
            assert conn.execute(text("SELECT pg_backend_pid()")).scalar() == pid_inside
            settings = conn.execute(text(
                "SELECT current_setting('app.operational_customer_id', true), "
                "current_setting('app.operational_branch_id', true)"
            )).one()
            # never set in this session -> NULL; set then ended -> '' (both fail closed)
            assert all(value in (None, "") for value in settings)
            assert conn.execute(text("SELECT COUNT(*) FROM checkins")).scalar() == 0
    finally:
        single.dispose()


# --- operational tenant resolver (services/tenant_resolution_service.py), as the runtime role ---
#
# The resolver turns (user, organization slug, branch slug) into the
# operational ids that tenant_connection then applies as the RLS context.
# These tests run it against the real schema -- with its real UNIQUE / CHECK /
# FK constraints -- as the non-owning runtime role, and then use the ids it
# returns to read an RLS-protected table.

USER_A, USER_B = 9001, 9002
BRANCH_B_NORTH = 23  # a second branch, in tenant B only


def _seed_members(owner_engine, runtime_engine) -> None:
    """Adds what the resolver reads on top of _seed's two tenants: one user
    per organization, and a second branch ("north") in tenant B. Both tenants
    already have a branch whose slug is "main"."""
    role = runtime_engine.url.username
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE app_users RESTART IDENTITY CASCADE"))
        for user_id, org_id in ((USER_A, 1), (USER_B, 2)):
            conn.execute(
                text("INSERT INTO app_users (id, email, is_active) VALUES (:u, :e, TRUE)"),
                {"u": user_id, "e": f"user-{user_id}@example.invalid"},
            )
            conn.execute(
                text("INSERT INTO memberships (organization_id, user_id, role) VALUES (:o, :u, 'admin')"),
                {"o": org_id, "u": user_id},
            )
        conn.execute(text(
            "INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) "
            "VALUES (:b, 2, 'north', 'North', 'active', :b)"
        ), {"b": BRANCH_B_NORTH})
        # Production sortview_app holds SELECT on both (scripts/runtime_role_privileges.py);
        # the phase 1 role above was only given what the ingestion endpoints need.
        conn.execute(text(f"GRANT SELECT ON TABLE public.memberships, public.app_users TO {role}"))  # nosec B608
        # R9C: every customer report reads the organization's plan (its history window; transit routing for the
        # routing reads). Production sortview_app holds SELECT on these three as well. Both tenants are on a plan
        # with every feature and no history limit, so what these tests show is unchanged by the plan.
        conn.execute(text(f"GRANT SELECT ON TABLE public.plans, public.subscriptions, public.feature_entitlements TO {role}"))  # nosec B608
        conn.execute(text("INSERT INTO plans (code, name) VALUES ('test-every-feature', 'Every feature') ON CONFLICT (code) DO NOTHING"))
        conn.execute(text(
            "INSERT INTO feature_entitlements (plan_id, feature_key, enabled, limit_value) "
            "SELECT p.id, f.feature_key, TRUE, NULL FROM plans p, "
            "(VALUES ('transits'), ('history_days'), ('internal_workflow')) AS f(feature_key) "
            "WHERE p.code = 'test-every-feature' ON CONFLICT (plan_id, feature_key) DO NOTHING"
        ))
        conn.execute(text("DELETE FROM subscriptions WHERE organization_id IN (1, 2)"))
        conn.execute(text(
            "INSERT INTO subscriptions (organization_id, plan_id, status) "
            "SELECT o.id, p.id, 'active' FROM plans p, (VALUES (1), (2)) AS o(id) WHERE p.code = 'test-every-feature'"
        ))


@pytest.fixture
def resolver(owner_engine, runtime_engine, monkeypatch):
    _seed_members(owner_engine, runtime_engine)
    monkeypatch.setattr(tenant_resolution_service, "get_engine", lambda: runtime_engine)
    return tenant_resolution_service.resolve_operational_tenant


def _barcodes_visible_to(runtime_engine, resolved) -> list[str]:
    # deliberately unscoped: only the RLS context built from the resolved ids restricts the rows
    with tenant_connection(runtime_engine, resolved.operational_customer_id, resolved.operational_branch_id) as conn:
        return sorted(row[0] for row in conn.execute(text("SELECT barcode FROM checkins")).fetchall())


def test_resolved_ids_scope_an_rls_read_to_exactly_that_tenant(owner_engine, runtime_engine, resolver):
    with owner_engine.begin() as conn:
        _insert_checkin(conn, CUSTOMER_A, BRANCH_A, "CANARY-RESOLVE-A")
        _insert_checkin(conn, CUSTOMER_B, BRANCH_B, "CANARY-RESOLVE-B-MAIN")
        _insert_checkin(conn, CUSTOMER_B, BRANCH_B_NORTH, "CANARY-RESOLVE-B-NORTH")

    resolved_a = resolver(USER_A, "tenant-a", "main")
    resolved_b_main = resolver(USER_B, "tenant-b", "main")
    resolved_b_north = resolver(USER_B, "tenant-b", "north")

    assert (resolved_a.operational_customer_id, resolved_a.operational_branch_id) == (CUSTOMER_A, BRANCH_A)
    assert (resolved_b_main.operational_customer_id, resolved_b_main.operational_branch_id) == (CUSTOMER_B, BRANCH_B)
    assert resolved_b_north.operational_branch_id == BRANCH_B_NORTH
    assert resolved_a.access_mode == "full"
    assert _barcodes_visible_to(runtime_engine, resolved_a) == ["CANARY-RESOLVE-A"]
    assert _barcodes_visible_to(runtime_engine, resolved_b_main) == ["CANARY-RESOLVE-B-MAIN"]
    assert _barcodes_visible_to(runtime_engine, resolved_b_north) == ["CANARY-RESOLVE-B-NORTH"]


def test_a_user_cannot_resolve_another_organization_or_its_branch(resolver):
    # Not a member of tenant B at all.
    assert resolver(USER_A, "tenant-b", "main") is None
    assert resolver(USER_A, "tenant-b", "north") is None
    # A member of tenant A, naming a branch that exists only in tenant B.
    assert resolver(USER_A, "tenant-a", "north") is None
    # Both tenants have a "main": the user's own organization decides which one.
    resolved = resolver(USER_A, "tenant-a", "main")
    assert (resolved.operational_customer_id, resolved.operational_branch_id) == (CUSTOMER_A, BRANCH_A)
    assert resolver(USER_B, "tenant-a", "main") is None


def test_resolution_follows_organization_and_branch_state_on_the_real_schema(owner_engine, resolver):
    def set_state(sql: str) -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(sql))

    set_state("UPDATE organizations SET status = 'suspended' WHERE slug = 'tenant-a'")
    assert resolver(USER_A, "tenant-a", "main").access_mode == "read_only"

    set_state("UPDATE organizations SET status = 'cancelled' WHERE slug = 'tenant-a'")
    assert resolver(USER_A, "tenant-a", "main") is None

    set_state("UPDATE organizations SET status = 'active', operational_customer_id = NULL WHERE slug = 'tenant-a'")
    assert resolver(USER_A, "tenant-a", "main") is None

    set_state("UPDATE branches SET status = 'inactive' WHERE id = 22")
    assert resolver(USER_B, "tenant-b", "main") is None
    assert resolver(USER_B, "tenant-b", "north") is not None  # no substitution, in either direction

    set_state("UPDATE branches SET status = 'active', operational_branch_id = NULL WHERE id = 22")
    assert resolver(USER_B, "tenant-b", "main") is None

    set_state("UPDATE app_users SET is_active = FALSE WHERE id = 9002")
    assert resolver(USER_B, "tenant-b", "north") is None


def test_the_resolver_needs_no_tenant_context_and_leaves_none_behind(runtime_engine, resolver):
    # It reads SaaS tables, which are outside operational RLS, and sets nothing.
    assert resolver(USER_A, "tenant-a", "main") is not None

    with runtime_engine.connect() as conn:
        settings = conn.execute(text(
            "SELECT current_setting('app.operational_customer_id', true), "
            "current_setting('app.operational_branch_id', true)"
        )).one()
        assert all(value in (None, "") for value in settings)
        assert conn.execute(text("SELECT COUNT(*) FROM checkins")).scalar() == 0


# --- GET /api/organizations/{org}/branches/{branch}/ingest-status, end to end, as the runtime role ---
#
# The first customer route that reads an RLS-protected table. Only the
# authenticated user is stubbed (the runtime role in this harness has no
# grant on auth_sessions). Everything after authentication is real: the
# tenant-scope dependency, the tenant resolver, tenant_connection, the
# context read-back, the read service's SQL, the RLS policy on
# ingest_key_ids, and the response serialization.

INGEST_STATUS_PATH = "/api/organizations/{org}/branches/{branch}/ingest-status"
INGEST_STATUS_FIELDS = {
    "health_status", "last_error_class", "pending_outbox_count", "quarantined_count", "oldest_pending_event_at",
    "last_success_at", "watcher_last_active_at", "last_heartbeat_at", "collector_last_run_at",
    "collector_next_run_at", "collector_run_duration_ms", "collector_schedule_status",
}
TENANT_NOT_FOUND = {"code": "tenant_not_found", "message": "Organization or branch not found."}

SESSION_A, SESSION_B = "synthetic-session-a", "synthetic-session-b"
_SESSION_USERS = {
    SESSION_A: {"id": USER_A, "email": f"user-{USER_A}@example.invalid", "full_name": ""},
    SESSION_B: {"id": USER_B, "email": f"user-{USER_B}@example.invalid", "full_name": ""},
}
KEY_A = "3db44444-931c-43cc-af3c-b1001443e761"
KEY_B_MAIN = "6b0c9b37-aba4-4c93-b09b-c562977ff157"
KEY_B_NORTH = "ced68da9-90c0-4d06-91fb-c6ffebf31b93"


def _insert_ingest_status(conn, customer_id, branch_id, key_id, health_status, minutes_ago) -> None:
    conn.execute(text("""
        INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, algorithm, status, health_status, last_heartbeat_at)
        VALUES (:k, :c, :b, 'hmac-sha256-v1', 'active', :h, now() - make_interval(mins => :m))
    """), {"k": key_id, "c": customer_id, "b": branch_id, "h": health_status, "m": minutes_ago})


@pytest.fixture
def customer_api(owner_engine, runtime_engine, monkeypatch):
    """The real customer API with the runtime role as its database engine.

    Tenant A's status is the OLDEST of the three: any read that was not
    scoped to its own tenant would pick one of tenant B's newer rows."""
    _seed_members(owner_engine, runtime_engine)
    with owner_engine.begin() as conn:
        _insert_ingest_status(conn, CUSTOMER_A, BRANCH_A, KEY_A, "healthy", 30)
        _insert_ingest_status(conn, CUSTOMER_B, BRANCH_B, KEY_B_MAIN, "error", 5)
        _insert_ingest_status(conn, CUSTOMER_B, BRANCH_B_NORTH, KEY_B_NORTH, "degraded", 1)

    # The one flat database engine both the resolver and the tenant scope use.
    monkeypatch.setattr(database, "_engine", runtime_engine)
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: _SESSION_USERS.get(raw_token))
    monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    return TestClient(main.app, raise_server_exceptions=False)


def _ingest_status(client, session, org, branch):
    return client.get(
        INGEST_STATUS_PATH.format(org=org, branch=branch),
        headers={"Cookie": f"__Host-sortview_api_session={session}"},
    )


def test_ingest_status_route_returns_each_user_only_their_own_branch_status(customer_api):
    a = _ingest_status(customer_api, SESSION_A, "tenant-a", "main")
    b_main = _ingest_status(customer_api, SESSION_B, "tenant-b", "main")
    b_north = _ingest_status(customer_api, SESSION_B, "tenant-b", "north")

    assert (a.status_code, b_main.status_code, b_north.status_code) == (200, 200, 200)
    # Tenant B's rows are both newer than tenant A's; neither may win for user A.
    assert a.json()["status"]["health_status"] == "healthy"
    assert b_main.json()["status"]["health_status"] == "error"
    assert b_north.json()["status"]["health_status"] == "degraded"

    assert set(a.json()) == {"status"}
    assert set(a.json()["status"]) == INGEST_STATUS_FIELDS
    assert datetime.fromisoformat(a.json()["status"]["last_heartbeat_at"]).tzinfo is not None
    for key_id in (KEY_A, KEY_B_MAIN, KEY_B_NORTH):
        assert key_id not in a.text + b_main.text + b_north.text


def test_ingest_status_route_refuses_another_tenants_organization_or_branch(customer_api):
    refused = [
        _ingest_status(customer_api, SESSION_A, "tenant-b", "main"),    # not a member of tenant B
        _ingest_status(customer_api, SESSION_A, "tenant-b", "north"),
        _ingest_status(customer_api, SESSION_A, "tenant-a", "north"),   # tenant B's branch slug under tenant A
        _ingest_status(customer_api, SESSION_B, "tenant-a", "main"),
    ]

    for response in refused:
        assert response.status_code == 404
        assert response.json() == TENANT_NOT_FOUND
        assert "degraded" not in response.text and "error" not in response.text.replace("not_found", "")


def test_ingest_status_route_reports_no_status_when_only_other_tenants_have_an_active_key(owner_engine, customer_api):
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE ingest_key_ids SET status = 'retired', retired_at = now() WHERE customer_id = :c"),
            {"c": CUSTOMER_A},
        )

    response = _ingest_status(customer_api, SESSION_A, "tenant-a", "main")

    # Tenant B still has two active keys. A valid tenant with none of its own gets "no status", never B's.
    assert response.status_code == 200
    assert response.json() == {"status": None}
    assert _ingest_status(customer_api, SESSION_B, "tenant-b", "main").json()["status"]["health_status"] == "error"


def test_ingest_status_route_requires_a_session(customer_api):
    response = customer_api.get(INGEST_STATUS_PATH.format(org="tenant-a", branch="main"))

    assert response.status_code == 401


def test_the_production_tenant_connection_is_verified_and_scoped_by_rls(customer_api, runtime_engine):
    resolved_a = tenant_resolution_service.resolve_operational_tenant(USER_A, "tenant-a", "main")
    resolved_b = tenant_resolution_service.resolve_operational_tenant(USER_B, "tenant-b", "main")

    with tenant_scope.open_customer_tenant_connection(resolved_a) as conn:
        # The context read-back passed against a real server, and these are the values it saw.
        settings = conn.execute(text(
            "SELECT current_setting('app.operational_customer_id', true), "
            "current_setting('app.operational_branch_id', true)"
        )).one()
        assert tuple(settings) == (str(CUSTOMER_A), str(BRANCH_A))

        # deliberately unscoped: no WHERE clause, so only RLS can be restricting this to tenant A's row
        assert conn.execute(text("SELECT health_status FROM ingest_key_ids")).scalars().all() == ["healthy"]

        # The real read-back refuses this very connection for any other tenant.
        with pytest.raises(tenant_scope.TenantContextError):
            tenant_scope._verify_tenant_context(conn, resolved_b)

    # ...and refuses a connection that carries no tenant context at all.
    with runtime_engine.connect() as conn, pytest.raises(tenant_scope.TenantContextError):
        tenant_scope._verify_tenant_context(conn, resolved_a)


def test_sequential_requests_on_one_pooled_connection_never_see_each_others_tenant(
    customer_api, runtime_engine, monkeypatch
):
    # A one-connection pool makes the reuse deterministic: every request below
    # -- resolver query and tenant-scoped read alike -- runs on the same server backend.
    single = create_engine(runtime_engine.url, pool_size=1, max_overflow=0, hide_parameters=True)
    monkeypatch.setattr(database, "_engine", single)
    try:
        seen = [
            _ingest_status(customer_api, session, org, branch).json()["status"]["health_status"]
            for session, org, branch in (
                (SESSION_A, "tenant-a", "main"),
                (SESSION_B, "tenant-b", "main"),
                (SESSION_A, "tenant-a", "main"),
                (SESSION_B, "tenant-b", "north"),
                (SESSION_A, "tenant-a", "main"),
            )
        ]
        assert seen == ["healthy", "error", "healthy", "degraded", "healthy"]
        assert _ingest_status(customer_api, SESSION_A, "tenant-b", "main").status_code == 404

        # Nothing of any request's tenant context is left on the pooled connection.
        with single.connect() as conn:
            settings = conn.execute(text(
                "SELECT current_setting('app.operational_customer_id', true), "
                "current_setting('app.operational_branch_id', true)"
            )).one()
            assert all(value in (None, "") for value in settings)
            assert conn.execute(text("SELECT COUNT(*) FROM ingest_key_ids")).scalar() == 0
    finally:
        single.dispose()


# --- GET .../checkins/count?date=YYYY-MM-DD, end to end, as the runtime role ---
#
# The first real-server proof of the two time domains behind the check-in
# count: checkins.event_time is a real TIMESTAMP (naive local wall clock),
# checkin_events.event_time and v2_cutovers.cutover_at are real TIMESTAMPTZ.
# As above, only the authenticated user is stubbed. The tenant-scope
# dependency, the resolver, tenant_connection, the context read-back, the
# cutover lookup, both COUNT statements, RLS on both tables and the response
# are all real. The product zone is the default, America/Chicago.

CHECKIN_COUNT_PATH = "/api/organizations/{org}/branches/{branch}/checkins/count"


def _v1_checkin(conn, customer_id, branch_id, local_wall_clock: str, barcode: str) -> None:
    """A legacy row. `local_wall_clock` has no offset: it is stored as given."""
    conn.execute(text("""
        INSERT INTO checkins (customer_id, branch_id, event_time, title, barcode, destination, bin, source_file)
        VALUES (:c, :b, CAST(:t AS timestamp), 'title', :barcode, 'Main', 'bin1', 'rls_test.csv')
    """), {"c": customer_id, "b": branch_id, "t": local_wall_clock, "barcode": barcode})


def _v2_checkin(conn, customer_id, branch_id, instant: str) -> None:
    """A Contract v2 row. `instant` carries an explicit offset."""
    event_key = hashlib.sha256(f"{customer_id}:{branch_id}:{instant}:{secrets.token_hex(8)}".encode()).hexdigest()
    conn.execute(text("""
        INSERT INTO checkin_events (customer_id, branch_id, key_id, event_key, event_time, destination, bin)
        VALUES (:c, :b, :k, :ek, CAST(:t AS timestamptz), 'unknown', 'unknown')
    """), {"c": customer_id, "b": branch_id, "k": KEY_A, "ek": event_key, "t": instant})


def _set_cutover(conn, customer_id, branch_id, instant: str) -> None:
    conn.execute(text("""
        INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by)
        VALUES (:c, :b, CAST(:t AS timestamptz), 'rls-test')
    """), {"c": customer_id, "b": branch_id, "t": instant})


@pytest.fixture
def checkin_api(customer_api, monkeypatch):
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    return customer_api


def _checkin_count(client, session, org, branch, day: str):
    return client.get(
        CHECKIN_COUNT_PATH.format(org=org, branch=branch),
        params={"date": day},
        headers={"Cookie": f"__Host-sortview_api_session={session}"},
    )


def _count_of(client, session, org, branch, day: str) -> int:
    response = _checkin_count(client, session, org, branch, day)
    assert response.status_code == 200, response.text
    assert response.json()["date"] == day
    assert response.json()["timezone"] == "America/Chicago"
    return response.json()["checkin_count"]


def _seed_mixed_era_days(owner_engine) -> None:
    """Tenant A is cut over at 12:00 local on 10 June 2026 (17:00Z).
        9 June:   2 legacy rows, at 00:30 and 23:30 local                      -> 2
        10 June:  1 legacy row before noon; 3 v2 rows from the cutover on      -> 4
        11 June:  1 v2 row, at local midnight                                  -> 1
    Tenant B -- both of its branches -- was cut over on 1 June and is busier in BOTH tables on 10 June."""
    with owner_engine.begin() as conn:
        _v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-09 00:30:00", "a-0609-early")
        _v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-09 23:30:00", "a-0609-late")
        _v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 09:00:00", "a-0610-before-cutover")   # counts
        _v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 12:00:00", "a-0610-at-cutover")       # v1 is strictly before
        _v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 15:00:00", "a-0610-after-cutover")    # does not count
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 17:00:00+00")
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 16:59:59+00")   # before the cutover: does not count
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 17:00:00+00")   # exactly at it: counts
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 22:00:00+00")   # counts
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 04:59:59+00")   # 23:59:59 local on the 10th: counts
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 05:00:00+00")   # local midnight: the 11th

        _set_cutover(conn, CUSTOMER_B, BRANCH_B, "2026-06-01 05:00:00+00")
        _set_cutover(conn, CUSTOMER_B, BRANCH_B_NORTH, "2026-06-01 05:00:00+00")   # a cutover is per branch
        for hour in (8, 9, 10):
            _v1_checkin(conn, CUSTOMER_B, BRANCH_B, f"2026-06-10 {hour:02d}:00:00", f"b-0610-{hour}")
        for hour in (6, 9, 12, 15, 18, 23):
            _v2_checkin(conn, CUSTOMER_B, BRANCH_B, f"2026-06-10 {hour:02d}:00:00+00")
        for hour in (14, 20):
            _v2_checkin(conn, CUSTOMER_B, BRANCH_B_NORTH, f"2026-06-10 {hour:02d}:30:00+00")


def test_the_three_time_columns_have_the_types_the_count_relies_on(owner_engine):
    with owner_engine.connect() as conn:
        types = dict(conn.execute(text("""
            SELECT table_name || '.' || column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND (table_name, column_name) IN (
                  ('checkins', 'event_time'), ('checkin_events', 'event_time'), ('v2_cutovers', 'cutover_at'))
        """)).fetchall())

    assert types == {
        "checkins.event_time": "timestamp without time zone",
        "checkin_events.event_time": "timestamp with time zone",
        "v2_cutovers.cutover_at": "timestamp with time zone",
    }


def test_checkin_count_for_a_v1_only_branch_counts_its_own_local_day(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        for stamped, barcode in (("2026-06-09 23:59:59", "a1"), ("2026-06-10 00:00:00", "a2"),
                                 ("2026-06-10 12:00:00", "a3"), ("2026-06-10 23:59:59", "a4"),
                                 ("2026-06-11 00:00:00", "a5")):
            _v1_checkin(conn, CUSTOMER_A, BRANCH_A, stamped, barcode)
        # v2 rows for tenant A on that day, but NO cutover: they are not part of its history.
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 15:00:00+00")
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 18:00:00+00")
        for hour in range(8, 13):
            _v1_checkin(conn, CUSTOMER_B, BRANCH_B, f"2026-06-10 {hour:02d}:00:00", f"b{hour}")

    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 3
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-09") == 1
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == 1
    assert _count_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == 5


def test_checkin_count_across_a_real_cutover_inside_the_day(owner_engine, checkin_api):
    _seed_mixed_era_days(owner_engine)

    response = _checkin_count(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10")

    assert response.status_code == 200
    assert response.json() == {"date": "2026-06-10", "timezone": "America/Chicago", "checkin_count": 4}
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-09") == 2
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == 1
    # Every one of tenant A's countable rows lands on exactly one day: 2 + 4 + 1.


def test_checkin_count_is_isolated_from_another_tenant_in_both_tables(owner_engine, checkin_api):
    _seed_mixed_era_days(owner_engine)

    # Tenant B has 3 legacy rows and 6 + 2 v2 rows on 10 June. None reaches tenant A's 4...
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 4
    # ...and B's own counts are its own: its legacy rows fall after ITS cutover, so only v2 counts.
    assert _count_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == 6
    assert _count_of(checkin_api, SESSION_B, "tenant-b", "north", "2026-06-10") == 2

    for session, org, branch in ((SESSION_A, "tenant-b", "main"), (SESSION_A, "tenant-a", "north"),
                                 (SESSION_B, "tenant-a", "main")):
        refused = _checkin_count(checkin_api, session, org, branch, "2026-06-10")
        assert refused.status_code == 404 and refused.json() == TENANT_NOT_FOUND


def test_checkin_count_request_validation_through_the_real_app(checkin_api):
    assert checkin_api.get(CHECKIN_COUNT_PATH.format(org="tenant-a", branch="main"), params={"date": "2026-06-10"}).status_code == 401
    assert _checkin_count(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10T00:00:00").status_code == 422
    assert _checkin_count(checkin_api, SESSION_A, "tenant-a", "main", "2026-02-30").status_code == 422
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2099-01-01") == 0


@pytest.mark.parametrize("session_time_zone", ["UTC", "America/Chicago", "America/New_York", "Asia/Tokyo"])
def test_checkin_count_does_not_depend_on_the_database_session_time_zone(
    owner_engine, checkin_api, runtime_engine, monkeypatch, session_time_zone
):
    _seed_mixed_era_days(owner_engine)
    engine = create_engine(
        runtime_engine.url, connect_args={"options": f"-c timezone={session_time_zone}"}, hide_parameters=True
    )
    monkeypatch.setattr(database, "_engine", engine)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SHOW timezone")).scalar() == session_time_zone

        # 9 June is all legacy rows, at 00:30 and 23:30 local -- the two a session-dependent date cast misplaces.
        assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-09") == 2
        # 10 June mixes both tables around the cutover, with a v2 row at 23:59:59 local.
        assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 4
        assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == 1
    finally:
        engine.dispose()


def test_control_a_date_cast_on_the_legacy_column_does_depend_on_the_session_time_zone(owner_engine, runtime_engine):
    """Control for the test above, so it is known to be testing something.
    This is the SHAPE of expression the customer API deliberately does not
    use: converting the naive column and casting to a date. Its answer for
    the same rows changes with the session time zone."""
    # This test goes straight to the database, without the customer_api
    # fixture every other test here uses -- so it must create tenant B's
    # second branch itself (_seed_members) before _seed_mixed_era_days
    # records a cutover and v2 rows for it.
    _seed_members(owner_engine, runtime_engine)
    _seed_mixed_era_days(owner_engine)
    answers = {}
    for session_time_zone in ("UTC", "America/Chicago"):
        engine = create_engine(
            runtime_engine.url, connect_args={"options": f"-c timezone={session_time_zone}"}, hide_parameters=True
        )
        try:
            with tenant_connection(engine, CUSTOMER_A, BRANCH_A) as conn:
                answers[session_time_zone] = conn.execute(text(
                    "SELECT COUNT(*) FROM checkins "
                    "WHERE (event_time AT TIME ZONE 'America/Chicago')::date = DATE '2026-06-09'"
                )).scalar()
        finally:
            engine.dispose()

    assert answers == {"America/Chicago": 2, "UTC": 1}   # under UTC the 23:30 row is cast onto 10 June


def test_checkin_count_on_the_spring_forward_date(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        # Tenant A, cut over well before: 8 March 2026 is [06:00Z, 05:00Z next day) -- 23 hours.
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-03-01 06:00:00+00")
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 05:59:59+00")   # 23:59:59 CST on the 7th
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 06:00:00+00")   # 00:00 CST on the 8th
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-03-09 04:59:59+00")   # 23:59:59 CDT on the 8th
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-03-09 05:00:00+00")   # 00:00 CDT on the 9th
        # Tenant B, never cut over: the same date as naive local wall-clock rows.
        for stamped, barcode in (("2026-03-07 23:59:59", "b1"), ("2026-03-08 00:00:00", "b2"),
                                 ("2026-03-08 01:59:00", "b3"), ("2026-03-08 03:00:00", "b4"),
                                 ("2026-03-08 23:59:59", "b5"), ("2026-03-09 00:00:00", "b6")):
            _v1_checkin(conn, CUSTOMER_B, BRANCH_B, stamped, barcode)

    assert [_count_of(checkin_api, SESSION_A, "tenant-a", "main", day)
            for day in ("2026-03-07", "2026-03-08", "2026-03-09")] == [1, 2, 1]
    assert [_count_of(checkin_api, SESSION_B, "tenant-b", "main", day)
            for day in ("2026-03-07", "2026-03-08", "2026-03-09")] == [1, 4, 1]


def test_checkin_count_on_the_fall_back_date(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        # Tenant A, cut over well before: 1 November 2026 is [05:00Z, 06:00Z next day) -- 25 hours.
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-10-01 05:00:00+00")
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 04:59:59+00")   # 23:59:59 CDT on 31 October
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 06:30:00+00")   # 01:30 CDT -- the first 01:30
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 07:30:00+00")   # 01:30 CST -- the second, an hour later
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-02 05:59:59+00")   # 23:59:59 CST on the 1st
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-02 06:00:00+00")   # 00:00 CST on the 2nd
        # Tenant B, never cut over: two legacy rows both stamped 01:30 are simply two rows on 1 November.
        _v1_checkin(conn, CUSTOMER_B, BRANCH_B, "2026-11-01 01:30:00", "b-first-pass")
        _v1_checkin(conn, CUSTOMER_B, BRANCH_B, "2026-11-01 01:30:00", "b-second-pass")
        _v1_checkin(conn, CUSTOMER_B, BRANCH_B, "2026-11-01 23:59:59", "b-late")

    # Both real 01:30 instants belong to 1 November and are counted once each.
    assert [_count_of(checkin_api, SESSION_A, "tenant-a", "main", day)
            for day in ("2026-10-31", "2026-11-01", "2026-11-02")] == [1, 3, 1]
    assert _count_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-11-01") == 3


def test_checkin_counts_on_one_pooled_connection_never_see_each_others_tenant(
    owner_engine, checkin_api, runtime_engine, monkeypatch
):
    _seed_mixed_era_days(owner_engine)
    # One connection: every statement of every request below runs on the same server backend.
    single = create_engine(runtime_engine.url, pool_size=1, max_overflow=0, hide_parameters=True)
    monkeypatch.setattr(database, "_engine", single)
    requests = (
        (SESSION_A, "tenant-a", "main", 4),
        (SESSION_B, "tenant-b", "main", 6),
        (SESSION_A, "tenant-a", "main", 4),
        (SESSION_B, "tenant-b", "north", 2),
        (SESSION_A, "tenant-a", "main", 4),
    )
    try:
        for session, org, branch, expected in requests:
            assert _count_of(checkin_api, session, org, branch, "2026-06-10") == expected

        # Change the pooled connection's SESSION time zone for good, then ask again.
        with single.connect() as conn:
            conn.execute(text("SET TIME ZONE 'Asia/Tokyo'"))
            conn.commit()
        with single.connect() as conn:
            assert conn.execute(text("SHOW timezone")).scalar() == "Asia/Tokyo"

        for session, org, branch, expected in requests:
            assert _count_of(checkin_api, session, org, branch, "2026-06-10") == expected
        assert _checkin_count(checkin_api, SESSION_A, "tenant-b", "main", "2026-06-10").status_code == 404

        # Nothing of any request's tenant context is left on the pooled connection.
        with single.connect() as conn:
            settings = conn.execute(text(
                "SELECT current_setting('app.operational_customer_id', true), "
                "current_setting('app.operational_branch_id', true)"
            )).one()
            assert all(value in (None, "") for value in settings)
            assert conn.execute(text("SELECT COUNT(*) FROM checkins")).scalar() == 0
            assert conn.execute(text("SELECT COUNT(*) FROM checkin_events")).scalar() == 0
    finally:
        single.dispose()


# --- GET .../checkins/by-hour?date=YYYY-MM-DD, end to end, as the runtime role ---
#
# The same real pieces as the count tests above, with the hourly statements:
# 24 conditional counts per era, their boundaries bound as real TIMESTAMP
# values for checkins and real TIMESTAMPTZ values for checkin_events. Every
# answer is also checked against the count endpoint for the same day, on the
# same server: the 24 hours must add up to it.

CHECKINS_BY_HOUR_PATH = "/api/organizations/{org}/branches/{branch}/checkins/by-hour"


def _checkins_by_hour(client, session, org, branch, day: str):
    return client.get(
        CHECKINS_BY_HOUR_PATH.format(org=org, branch=branch),
        params={"date": day},
        headers={"Cookie": f"__Host-sortview_api_session={session}"},
    )


def _hours_of(client, session, org, branch, day: str) -> dict[int, int]:
    """The hours of `day` that are not zero, as {hour: count} -- after
    checking the response is the approved shape, has all 24 hours in order,
    and adds up to what the count endpoint says for the same day."""
    response = _checkins_by_hour(client, session, org, branch, day)
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == ["date", "timezone", "hours"]
    assert (body["date"], body["timezone"]) == (day, "America/Chicago")
    assert [entry["hour"] for entry in body["hours"]] == list(range(24))
    assert all(list(entry) == ["hour", "checkin_count"] for entry in body["hours"])
    assert sum(entry["checkin_count"] for entry in body["hours"]) == _count_of(client, session, org, branch, day)
    return {entry["hour"]: entry["checkin_count"] for entry in body["hours"] if entry["checkin_count"]}


def test_checkins_by_hour_for_a_v1_only_branch_buckets_its_own_local_day(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        for stamped, barcode in (("2026-06-09 23:59:59", "a1"), ("2026-06-10 00:00:00", "a2"),
                                 ("2026-06-10 09:05:00", "a3"), ("2026-06-10 09:55:00", "a4"),
                                 ("2026-06-10 10:00:00", "a5"), ("2026-06-10 14:30:00", "a6"),
                                 ("2026-06-10 23:59:59", "a7"), ("2026-06-11 00:00:00", "a8")):
            _v1_checkin(conn, CUSTOMER_A, BRANCH_A, stamped, barcode)
        # v2 rows for tenant A on that day, but NO cutover: they are not part of its history.
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 15:00:00+00")
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 18:00:00+00")
        for hour in range(8, 13):
            _v1_checkin(conn, CUSTOMER_B, BRANCH_B, f"2026-06-10 {hour:02d}:00:00", f"b{hour}")

    response = _checkins_by_hour(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    expected = {0: 1, 9: 2, 10: 1, 14: 1, 23: 1}
    assert response.json() == {
        "date": "2026-06-10",
        "timezone": "America/Chicago",
        "hours": [{"hour": hour, "checkin_count": expected.get(hour, 0)} for hour in range(24)],
    }
    assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == expected   # adds up to 6
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 6
    assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-09") == {23: 1}
    assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == {0: 1}
    assert _hours_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == {8: 1, 9: 1, 10: 1, 11: 1, 12: 1}


def test_checkins_by_hour_splits_the_hour_a_real_cutover_falls_in(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 19:30:00+00")   # 14:30 local, inside hour 14
        # Legacy rows, by wall clock: v1 owns everything strictly before 14:30.
        _v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 13:50:00", "v1-1350")            # hour 13
        _v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 14:10:00", "v1-1410")            # hour 14
        _v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 14:29:59", "v1-142959")          # hour 14, the last second
        _v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 14:30:00", "v1-at-cutover")      # not v1's
        _v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 14:45:00", "v1-after-cutover")   # not v1's
        # Contract v2 rows, as instants: v2 owns everything from 19:30Z on.
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 19:10:00+00")   # 14:10 local, before the cutover: not v2's
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 19:29:59+00")   # one second before: not v2's
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 19:30:00+00")   # exactly at the cutover: hour 14, v2's
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 19:40:00+00")   # hour 14
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 19:59:59+00")   # hour 14, its last second
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 20:00:00+00")   # hour 15

    # Hour 14 holds 2 legacy rows from before the cutover and 3 v2 rows from it on: nothing twice, nothing lost.
    assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == {13: 1, 14: 5, 15: 1}
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 7

    response = _checkins_by_hour(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10")
    for leaked in ("v1", "v2", "cutover", "era", "19:30", str(CUSTOMER_A), "customer_id", "branch_id"):
        assert leaked not in response.text, leaked


def test_checkins_by_hour_is_isolated_from_another_tenant_in_both_tables(owner_engine, checkin_api):
    _seed_mixed_era_days(owner_engine)

    # Tenant A on 10 June: one legacy row at 09:00, then v2 rows at 12:00, 17:00 and 23:59:59 local.
    assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == {9: 1, 12: 1, 17: 1, 23: 1}
    # Tenant B has legacy rows at 08:00, 09:00 and 10:00 and v2 rows in six other hours that day. None of them is
    # in tenant A's hours above (its 09:00 would be 2) -- and B's own hours are only its own v2 rows, at their
    # local hours: 06, 09, 12, 15, 18 and 23Z are 01, 04, 07, 10, 13 and 18 CDT.
    assert _hours_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == {
        1: 1, 4: 1, 7: 1, 10: 1, 13: 1, 18: 1,
    }
    assert _hours_of(checkin_api, SESSION_B, "tenant-b", "north", "2026-06-10") == {9: 1, 15: 1}

    for session, org, branch in ((SESSION_A, "tenant-b", "main"), (SESSION_A, "tenant-a", "north"),
                                 (SESSION_B, "tenant-a", "main")):
        refused = _checkins_by_hour(checkin_api, session, org, branch, "2026-06-10")
        assert refused.status_code == 404 and refused.json() == TENANT_NOT_FOUND


def test_checkins_by_hour_request_validation_through_the_real_app(checkin_api):
    path = CHECKINS_BY_HOUR_PATH.format(org="tenant-a", branch="main")

    assert checkin_api.get(path, params={"date": "2026-06-10"}).status_code == 401
    assert checkin_api.get(path, headers={"Cookie": f"__Host-sortview_api_session={SESSION_A}"}).status_code == 422
    assert _checkins_by_hour(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10T00:00:00").status_code == 422
    assert _checkins_by_hour(checkin_api, SESSION_A, "tenant-a", "main", "2026-02-30").status_code == 422
    assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2099-01-01") == {}   # 24 zeros


@pytest.mark.parametrize("session_time_zone", ["UTC", "America/Chicago", "America/New_York", "Asia/Tokyo"])
def test_checkins_by_hour_does_not_depend_on_the_database_session_time_zone(
    owner_engine, checkin_api, runtime_engine, monkeypatch, session_time_zone
):
    _seed_mixed_era_days(owner_engine)
    engine = create_engine(
        runtime_engine.url, connect_args={"options": f"-c timezone={session_time_zone}"}, hide_parameters=True
    )
    monkeypatch.setattr(database, "_engine", engine)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SHOW timezone")).scalar() == session_time_zone

        # 9 June is all legacy rows, at 00:30 and 23:30 local: the first and last hours of the day.
        assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-09") == {0: 1, 23: 1}
        # 10 June mixes both tables around the cutover; its v2 hours are local hours whatever the session says.
        assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == {9: 1, 12: 1, 17: 1, 23: 1}
        assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == {0: 1}
        assert _hours_of(checkin_api, SESSION_B, "tenant-b", "north", "2026-06-10") == {9: 1, 15: 1}
    finally:
        engine.dispose()


def test_checkins_by_hour_on_the_spring_forward_date(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        # Tenant A, cut over well before: 8 March 2026 is [06:00Z, 05:00Z next day) -- 23 hours.
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-03-01 06:00:00+00")
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 05:59:59+00")   # 23:59:59 CST on the 7th
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 06:00:00+00")   # 00:00 CST            -> hour 0
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 07:59:59+00")   # 01:59:59 CST         -> hour 1
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 08:00:00+00")   # the next second is 03:00 CDT -> hour 3
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 08:30:00+00")   # 03:30 CDT            -> hour 3
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-03-09 04:59:59+00")   # 23:59:59 CDT         -> hour 23
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-03-09 05:00:00+00")   # 00:00 CDT on the 9th
        # Tenant B, never cut over: naive wall-clock rows, one of them stamped in the hour that did not exist.
        for stamped, barcode in (("2026-03-08 01:59:00", "b1"), ("2026-03-08 02:30:00", "b2"),
                                 ("2026-03-08 03:00:00", "b3")):
            _v1_checkin(conn, CUSTOMER_B, BRANCH_B, stamped, barcode)

    response = _checkins_by_hour(checkin_api, SESSION_A, "tenant-a", "main", "2026-03-08")
    assert len(response.json()["hours"]) == 24                                   # 24 buckets on a 23-hour day
    assert response.json()["hours"][2] == {"hour": 2, "checkin_count": 0}        # no instant is 02:xx that day

    assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-03-08") == {0: 1, 1: 1, 3: 2, 23: 1}
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-03-08") == 5
    assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-03-07") == {23: 1}
    assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-03-09") == {0: 1}
    # The legacy row stamped 02:30 is counted in the hour it names.
    assert _hours_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-03-08") == {1: 1, 2: 1, 3: 1}


def test_checkins_by_hour_on_the_fall_back_date(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        # Tenant A, cut over well before: 1 November 2026 is [05:00Z, 06:00Z next day) -- 25 hours.
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-10-01 05:00:00+00")
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 04:59:59+00")   # 23:59:59 CDT on 31 October
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 05:59:59+00")   # 00:59:59 CDT          -> hour 0
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 06:00:00+00")   # 01:00 CDT, first pass -> hour 1
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 06:30:00+00")   # 01:30 CDT, first pass -> hour 1
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 07:30:00+00")   # 01:30 CST, second pass -> hour 1
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 07:59:59+00")   # 01:59:59 CST          -> hour 1
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 08:00:00+00")   # 02:00 CST             -> hour 2
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-02 05:59:59+00")   # 23:59:59 CST          -> hour 23
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-11-02 06:00:00+00")   # 00:00 CST on the 2nd
        # Tenant B, never cut over: two legacy rows both stamped 01:30 share the one bucket that reading names.
        _v1_checkin(conn, CUSTOMER_B, BRANCH_B, "2026-11-01 01:30:00", "b-first-pass")
        _v1_checkin(conn, CUSTOMER_B, BRANCH_B, "2026-11-01 01:30:00", "b-second-pass")
        _v1_checkin(conn, CUSTOMER_B, BRANCH_B, "2026-11-01 23:59:59", "b-late")

    response = _checkins_by_hour(checkin_api, SESSION_A, "tenant-a", "main", "2026-11-01")
    assert len(response.json()["hours"]) == 24                                   # 24 buckets on a 25-hour day

    # Both passes through 01:xx -- four real instants across two real hours -- are in hour 1, once each.
    assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-11-01") == {0: 1, 1: 4, 2: 1, 23: 1}
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-11-01") == 7
    assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-10-31") == {23: 1}
    assert _hours_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-11-02") == {0: 1}
    assert _hours_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-11-01") == {1: 2, 23: 1}


def test_checkins_by_hour_on_one_pooled_connection_never_see_each_others_tenant(
    owner_engine, checkin_api, runtime_engine, monkeypatch
):
    _seed_mixed_era_days(owner_engine)
    # One connection: every statement of every request below runs on the same server backend.
    single = create_engine(runtime_engine.url, pool_size=1, max_overflow=0, hide_parameters=True)
    monkeypatch.setattr(database, "_engine", single)
    tenant_a = {9: 1, 12: 1, 17: 1, 23: 1}
    requests = (
        (SESSION_A, "tenant-a", "main", tenant_a),
        (SESSION_B, "tenant-b", "main", {1: 1, 4: 1, 7: 1, 10: 1, 13: 1, 18: 1}),
        (SESSION_A, "tenant-a", "main", tenant_a),
        (SESSION_B, "tenant-b", "north", {9: 1, 15: 1}),
        (SESSION_A, "tenant-a", "main", tenant_a),
    )
    try:
        for session, org, branch, expected in requests:
            assert _hours_of(checkin_api, session, org, branch, "2026-06-10") == expected

        # Change the pooled connection's SESSION time zone for good, then ask again.
        with single.connect() as conn:
            conn.execute(text("SET TIME ZONE 'Asia/Tokyo'"))
            conn.commit()
        with single.connect() as conn:
            assert conn.execute(text("SHOW timezone")).scalar() == "Asia/Tokyo"

        for session, org, branch, expected in requests:
            assert _hours_of(checkin_api, session, org, branch, "2026-06-10") == expected
        assert _checkins_by_hour(checkin_api, SESSION_A, "tenant-b", "main", "2026-06-10").status_code == 404

        # Nothing of any request's tenant context is left on the pooled connection.
        with single.connect() as conn:
            settings = conn.execute(text(
                "SELECT current_setting('app.operational_customer_id', true), "
                "current_setting('app.operational_branch_id', true)"
            )).one()
            assert all(value in (None, "") for value in settings)
            assert conn.execute(text("SELECT COUNT(*) FROM checkins")).scalar() == 0
            assert conn.execute(text("SELECT COUNT(*) FROM checkin_events")).scalar() == 0
    finally:
        single.dispose()


# --- GET .../rejects/count?date=YYYY-MM-DD, end to end, as the runtime role ---
#
# The reject tables keep time as the check-in tables do -- rejects.event_time
# is a real TIMESTAMP (naive local wall clock), reject_events.event_time a real
# TIMESTAMPTZ -- and the branch's one cutover divides them. As above, only the
# authenticated user is stubbed: the tenant-scope dependency, the resolver,
# tenant_connection, the context read-back, the cutover lookup, both COUNT
# statements, RLS on both reject tables and the response are all real.

REJECT_COUNT_PATH = "/api/organizations/{org}/branches/{branch}/rejects/count"


def _v1_reject(conn, customer_id, branch_id, local_wall_clock: str, barcode, error_message="Item not found") -> None:
    """A legacy reject row. `local_wall_clock` has no offset: it is stored as given."""
    conn.execute(text("""
        INSERT INTO rejects (customer_id, branch_id, event_time, barcode, error_message, source_file)
        VALUES (:c, :b, CAST(:t AS timestamp), :barcode, :e, 'rls_test.csv')
    """), {"c": customer_id, "b": branch_id, "t": local_wall_clock, "barcode": barcode, "e": error_message})


def _v2_reject(conn, customer_id, branch_id, instant: str, error_class="item_not_found", item_key=None) -> None:
    """A Contract v2 reject row. `instant` carries an explicit offset."""
    event_key = hashlib.sha256(f"{customer_id}:{branch_id}:{instant}:{secrets.token_hex(8)}".encode()).hexdigest()
    conn.execute(text("""
        INSERT INTO reject_events (customer_id, branch_id, key_id, event_key, event_time, error_class, item_key)
        VALUES (:c, :b, :k, :ek, CAST(:t AS timestamptz), :e, :ik)
    """), {"c": customer_id, "b": branch_id, "k": KEY_A, "ek": event_key, "t": instant, "e": error_class,
           "ik": item_key})


def _reject_count(client, session, org, branch, day: str):
    return client.get(
        REJECT_COUNT_PATH.format(org=org, branch=branch),
        params={"date": day},
        headers={"Cookie": f"__Host-sortview_api_session={session}"},
    )


def _rejects_of(client, session, org, branch, day: str) -> int:
    response = _reject_count(client, session, org, branch, day)
    assert response.status_code == 200, response.text
    assert list(response.json()) == ["date", "timezone", "reject_count"]
    assert response.json()["date"] == day
    assert response.json()["timezone"] == "America/Chicago"
    return response.json()["reject_count"]


ONE_ITEM = "c" * 64   # one item's v2 key, rejected more than once


def _seed_mixed_era_reject_days(owner_engine) -> None:
    """Tenant A is cut over at 12:00 local on 10 June 2026 (17:00Z).
        9 June:   2 legacy rejects, at 00:30 and 23:30 local                          -> 2
        10 June:  2 legacy rejects before noon; 4 v2 rejects from the cutover on      -> 6
        11 June:  1 v2 reject, at local midnight                                      -> 1
    Tenant A also has check-ins on 10 June, in both tables, which are not rejects.
    Tenant B -- both of its branches -- was cut over on 1 June and has its own rejects in BOTH tables on 10 June."""
    with owner_engine.begin() as conn:
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-09 00:30:00", "a-0609-early")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-09 23:30:00", "a-0609-late")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 09:00:00", "a-0610-morning")                      # counts
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 11:59:59", "a-0610-one-second-before", "ACS timeout")  # counts
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 12:00:00", "a-0610-at-cutover")       # v1 is strictly before
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 15:00:00", "a-0610-after-cutover")    # does not count
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 17:00:00+00")
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 16:59:59+00")                         # before the cutover: no
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 17:00:00+00")                         # exactly at it: counts
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 22:00:00+00", "rfid_collision", ONE_ITEM)   # counts
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 22:00:30+00", "other", ONE_ITEM)      # the same item again: counts
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 04:59:59+00", "unknown")              # 23:59:59 local: counts
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 05:00:00+00")                         # local midnight: the 11th

        _v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 08:00:00", "a-checkin-v1")
        _v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 18:00:00+00")

        _set_cutover(conn, CUSTOMER_B, BRANCH_B, "2026-06-01 05:00:00+00")
        _set_cutover(conn, CUSTOMER_B, BRANCH_B_NORTH, "2026-06-01 05:00:00+00")   # a cutover is per branch
        for hour in (8, 9, 10):
            _v1_reject(conn, CUSTOMER_B, BRANCH_B, f"2026-06-10 {hour:02d}:00:00", f"b-0610-{hour}")
        for hour in (6, 9, 12, 15, 18):
            _v2_reject(conn, CUSTOMER_B, BRANCH_B, f"2026-06-10 {hour:02d}:00:00+00")
        for hour in (14, 20):
            _v2_reject(conn, CUSTOMER_B, BRANCH_B_NORTH, f"2026-06-10 {hour:02d}:30:00+00")


def test_the_two_reject_time_columns_have_the_types_the_count_relies_on(owner_engine):
    with owner_engine.connect() as conn:
        types = dict(conn.execute(text("""
            SELECT table_name || '.' || column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND (table_name, column_name) IN (('rejects', 'event_time'), ('reject_events', 'event_time'))
        """)).fetchall())

    assert types == {
        "rejects.event_time": "timestamp without time zone",
        "reject_events.event_time": "timestamp with time zone",
    }


def test_reject_count_for_a_v1_only_branch_counts_every_stored_row_of_its_local_day(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-09 23:59:59", "a1")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 00:00:00", "a2")
        # One item, rejected three times that day -- twice in the same second, for two different reasons.
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 09:00:00", "a3", "Item not found")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 09:00:00", "a3", "Multiple RFID tags detected")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 14:00:00", "a3", "Something uncategorized")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 15:00:00", None, "ACS timeout")   # no barcode at all
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 23:59:59", "a4", None)            # no message at all
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 00:00:00", "a5")
        # v2 rejects for tenant A on that day, but NO cutover: they are not part of its history.
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 15:00:00+00")
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 18:00:00+00")
        # Check-ins for tenant A on that day: not rejects.
        for hour in range(8, 18):
            _v1_checkin(conn, CUSTOMER_A, BRANCH_A, f"2026-06-10 {hour:02d}:00:00", f"checkin-{hour}")
        for hour in range(8, 13):
            _v1_reject(conn, CUSTOMER_B, BRANCH_B, f"2026-06-10 {hour:02d}:00:00", f"b{hour}")

    response = _reject_count(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10")

    assert response.status_code == 200
    assert response.json() == {"date": "2026-06-10", "timezone": "America/Chicago", "reject_count": 6}
    assert response.headers["cache-control"] == "no-store"
    for leaked in ("total", "v1_count", "v2_count", "customer_id", "branch_id", "cutover", "era", "reason",
                   "error_message", "error_class", "barcode", "item_key", "reject_rate", str(CUSTOMER_A),
                   "RFID", "ACS", "a3"):
        assert leaked not in response.text, leaked

    assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-09") == 1
    assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == 1
    assert _rejects_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == 5
    # The check-in count for the same day is its own number, untouched by any reject row.
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 10


def test_reject_count_across_a_real_cutover_inside_the_day(owner_engine, checkin_api):
    _seed_mixed_era_reject_days(owner_engine)

    response = _reject_count(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10")

    # 2 legacy rows before 12:00 (one of them at 11:59:59) + 4 v2 rows from 17:00:00Z on (one of them exactly
    # at it). The legacy row AT 12:00 and the v2 row one second BEFORE 17:00Z are on the other era's side.
    assert response.status_code == 200
    assert response.json() == {"date": "2026-06-10", "timezone": "America/Chicago", "reject_count": 6}
    for leaked in ("total", "v1", "v2", "cutover", "era", "17:00", "error", "rfid", "item_key", str(CUSTOMER_A)):
        assert leaked not in response.text, leaked

    assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-09") == 2
    assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == 1
    # Every one of tenant A's countable rejects lands on exactly one day: 2 + 6 + 1, of the 12 rows stored
    # (the other three are the legacy rows at/after the cutover and the v2 row before it).
    with owner_engine.connect() as conn:
        stored = conn.execute(text(
            "SELECT (SELECT COUNT(*) FROM rejects WHERE customer_id = :c) + "
            "(SELECT COUNT(*) FROM reject_events WHERE customer_id = :c)"
        ), {"c": CUSTOMER_A}).scalar()
    assert stored == 12

    # Tenant A's check-ins that day (one per era) are counted by their own endpoint, and only there.
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 2


def test_reject_count_is_isolated_from_another_tenant_in_both_tables(owner_engine, checkin_api):
    _seed_mixed_era_reject_days(owner_engine)

    # Tenant B has 3 legacy and 5 + 2 v2 rejects on 10 June. None reaches tenant A's 6...
    assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 6
    # ...and B's own counts are its own: its legacy rows fall after ITS cutover, so only v2 counts,
    # and each of its two branches sees only its own rows.
    assert _rejects_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == 5
    assert _rejects_of(checkin_api, SESSION_B, "tenant-b", "north", "2026-06-10") == 2

    for session, org, branch in ((SESSION_A, "tenant-b", "main"), (SESSION_A, "tenant-a", "north"),
                                 (SESSION_B, "tenant-a", "main")):
        refused = _reject_count(checkin_api, session, org, branch, "2026-06-10")
        assert refused.status_code == 404 and refused.json() == TENANT_NOT_FOUND


def test_row_level_security_alone_hides_another_tenants_rejects(owner_engine, runtime_engine):
    """The count statements also filter the tenant themselves. This shows the
    second line of defence on its own: a deliberately unscoped count on a
    tenant-scoped connection sees only that tenant's reject rows."""
    _seed_members(owner_engine, runtime_engine)
    _seed_mixed_era_reject_days(owner_engine)

    seen = {}
    for name, customer_id, branch_id in (("a", CUSTOMER_A, BRANCH_A), ("b-main", CUSTOMER_B, BRANCH_B),
                                         ("b-north", CUSTOMER_B, BRANCH_B_NORTH)):
        with tenant_connection(runtime_engine, customer_id, branch_id) as conn:
            seen[name] = (
                conn.execute(text("SELECT COUNT(*) FROM rejects")).scalar(),
                conn.execute(text("SELECT COUNT(*) FROM reject_events")).scalar(),
            )

    assert seen == {"a": (6, 6), "b-main": (3, 5), "b-north": (0, 2)}


def test_reject_count_request_validation_through_the_real_app(checkin_api):
    path = REJECT_COUNT_PATH.format(org="tenant-a", branch="main")

    assert checkin_api.get(path, params={"date": "2026-06-10"}).status_code == 401
    assert checkin_api.get(path, headers={"Cookie": f"__Host-sortview_api_session={SESSION_A}"}).status_code == 422
    assert _reject_count(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10T00:00:00").status_code == 422
    assert _reject_count(checkin_api, SESSION_A, "tenant-a", "main", "2026-02-30").status_code == 422
    assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2099-01-01") == 0


@pytest.mark.parametrize("session_time_zone", ["UTC", "America/Chicago", "America/New_York", "Asia/Tokyo"])
def test_reject_count_does_not_depend_on_the_database_session_time_zone(
    owner_engine, checkin_api, runtime_engine, monkeypatch, session_time_zone
):
    _seed_mixed_era_reject_days(owner_engine)
    engine = create_engine(
        runtime_engine.url, connect_args={"options": f"-c timezone={session_time_zone}"}, hide_parameters=True
    )
    monkeypatch.setattr(database, "_engine", engine)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SHOW timezone")).scalar() == session_time_zone

        # 9 June is all legacy rows, at 00:30 and 23:30 local -- the two a session-dependent date cast misplaces.
        assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-09") == 2
        # 10 June mixes both tables around the cutover, with a v2 row at 23:59:59 local.
        assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 6
        assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == 1
        assert _rejects_of(checkin_api, SESSION_B, "tenant-b", "north", "2026-06-10") == 2
    finally:
        engine.dispose()


def test_reject_count_on_the_spring_forward_date(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        # Tenant A, cut over well before: 8 March 2026 is [06:00Z, 05:00Z next day) -- 23 hours.
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-03-01 06:00:00+00")
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 05:59:59+00")   # 23:59:59 CST on the 7th
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 06:00:00+00")   # 00:00 CST on the 8th
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 07:59:59+00")   # 01:59:59 CST, the last second before the jump
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 08:00:00+00")   # the next second: 03:00 CDT
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-03-09 04:59:59+00")   # 23:59:59 CDT on the 8th
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-03-09 05:00:00+00")   # 00:00 CDT on the 9th
        # Tenant B, never cut over: naive wall-clock rows, one stamped in the hour that did not exist.
        for stamped, barcode in (("2026-03-07 23:59:59", "b1"), ("2026-03-08 00:00:00", "b2"),
                                 ("2026-03-08 02:30:00", "b3"), ("2026-03-08 03:00:00", "b4"),
                                 ("2026-03-08 23:59:59", "b5"), ("2026-03-09 00:00:00", "b6")):
            _v1_reject(conn, CUSTOMER_B, BRANCH_B, stamped, barcode)

    assert [_rejects_of(checkin_api, SESSION_A, "tenant-a", "main", day)
            for day in ("2026-03-07", "2026-03-08", "2026-03-09")] == [1, 4, 1]
    # The legacy row stamped 02:30 is a stored row of 8 March and counts there.
    assert [_rejects_of(checkin_api, SESSION_B, "tenant-b", "main", day)
            for day in ("2026-03-07", "2026-03-08", "2026-03-09")] == [1, 4, 1]


def test_reject_count_on_the_fall_back_date(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        # Tenant A, cut over well before: 1 November 2026 is [05:00Z, 06:00Z next day) -- 25 hours.
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-10-01 05:00:00+00")
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 04:59:59+00")   # 23:59:59 CDT on 31 October
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 06:30:00+00")   # 01:30 CDT -- the first 01:30
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 07:30:00+00")   # 01:30 CST -- the second, an hour later
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-11-02 05:59:59+00")   # 23:59:59 CST on the 1st
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-11-02 06:00:00+00")   # 00:00 CST on the 2nd
        # Tenant B, never cut over: two legacy rows both stamped 01:30 are simply two stored rows on 1 November.
        _v1_reject(conn, CUSTOMER_B, BRANCH_B, "2026-11-01 01:30:00", "b-first-pass")
        _v1_reject(conn, CUSTOMER_B, BRANCH_B, "2026-11-01 01:30:00", "b-second-pass")
        _v1_reject(conn, CUSTOMER_B, BRANCH_B, "2026-11-01 23:59:59", "b-late")

    # Both real 01:30 instants belong to 1 November and are counted once each.
    assert [_rejects_of(checkin_api, SESSION_A, "tenant-a", "main", day)
            for day in ("2026-10-31", "2026-11-01", "2026-11-02")] == [1, 3, 1]
    assert _rejects_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-11-01") == 3


def test_reject_count_after_a_rollback_is_v1_only_again(owner_engine, checkin_api):
    _seed_mixed_era_reject_days(owner_engine)
    assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 6

    # A later v2_cutovers row with no cutover_at is a recorded rollback for that branch.
    with owner_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_at, set_by)
            VALUES (:c, :b, NULL, now() + interval '1 minute', 'rls-test')
        """), {"c": CUSTOMER_A, "b": BRANCH_A})

    # All four legacy rows of 10 June now count -- including those at and after the old cutover -- and no v2 row.
    assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 4
    assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == 0
    # Tenant B's branches have their own cutovers and are unaffected.
    assert _rejects_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == 5


def test_reject_counts_on_one_pooled_connection_never_see_each_others_tenant(
    owner_engine, checkin_api, runtime_engine, monkeypatch
):
    _seed_mixed_era_reject_days(owner_engine)
    # One connection: every statement of every request below runs on the same server backend.
    single = create_engine(runtime_engine.url, pool_size=1, max_overflow=0, hide_parameters=True)
    monkeypatch.setattr(database, "_engine", single)
    requests = (
        (SESSION_A, "tenant-a", "main", 6),
        (SESSION_B, "tenant-b", "main", 5),
        (SESSION_A, "tenant-a", "main", 6),
        (SESSION_B, "tenant-b", "north", 2),
        (SESSION_A, "tenant-a", "main", 6),
    )
    try:
        for session, org, branch, expected in requests:
            assert _rejects_of(checkin_api, session, org, branch, "2026-06-10") == expected
        # A check-in request for another tenant in between, on the same connection, changes nothing.
        assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 2
        assert _rejects_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == 5

        # Change the pooled connection's SESSION time zone for good, then ask again.
        with single.connect() as conn:
            conn.execute(text("SET TIME ZONE 'Asia/Tokyo'"))
            conn.commit()
        with single.connect() as conn:
            assert conn.execute(text("SHOW timezone")).scalar() == "Asia/Tokyo"

        for session, org, branch, expected in requests:
            assert _rejects_of(checkin_api, session, org, branch, "2026-06-10") == expected
        assert _reject_count(checkin_api, SESSION_A, "tenant-b", "main", "2026-06-10").status_code == 404

        # Nothing of any request's tenant context is left on the pooled connection.
        with single.connect() as conn:
            settings = conn.execute(text(
                "SELECT current_setting('app.operational_customer_id', true), "
                "current_setting('app.operational_branch_id', true)"
            )).one()
            assert all(value in (None, "") for value in settings)
            assert conn.execute(text("SELECT COUNT(*) FROM rejects")).scalar() == 0
            assert conn.execute(text("SELECT COUNT(*) FROM reject_events")).scalar() == 0
    finally:
        single.dispose()


# --- GET .../rejects/by-reason?date=YYYY-MM-DD, end to end, as the runtime role ---
#
# The same rows as the reject count above, sorted by reason. As there, only the
# authenticated user is stubbed: the tenant-scope dependency, the resolver,
# tenant_connection, the context read-back, the cutover lookup, both GROUP BY
# statements, RLS on both reject tables, the reason classifier and the
# response are all real.
#
# What only a real server can show here: a real NULL in rejects.error_message
# forms its own group; reject_events.error_class is held to a PATTERN by the
# database, not to the eight reason codes, so a class such as 'jam' really can
# be stored; and neither grouped statement depends on the session time zone.

REJECTS_BY_REASON_PATH = "/api/organizations/{org}/branches/{branch}/rejects/by-reason"
REASON_CODES = ["item_not_found", "ils_acs_failure", "rfid_collision", "configuration_error", "routing_error",
                "communication_error", "other", "unknown"]
REASON_CANARY = "CANARY-31234000123456 Smith, Pat"


def _rejects_by_reason(client, session, org, branch, day: str):
    return client.get(
        REJECTS_BY_REASON_PATH.format(org=org, branch=branch),
        params={"date": day},
        headers={"Cookie": f"__Host-sortview_api_session={session}"},
    )


def _reasons_of(client, session, org, branch, day: str) -> dict[str, int]:
    """The reasons that have a count, by code -- after checking the whole
    contract: the three keys, all eight codes in their fixed order, and that
    the counts add up to /rejects/count for the same day and data state."""
    response = _rejects_by_reason(client, session, org, branch, day)
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == ["date", "timezone", "reasons"]
    assert body["date"] == day
    assert body["timezone"] == "America/Chicago"
    assert [entry["reason"] for entry in body["reasons"]] == REASON_CODES
    assert all(list(entry) == ["reason", "reject_count"] for entry in body["reasons"])
    assert response.headers["cache-control"] == "no-store"
    assert sum(entry["reject_count"] for entry in body["reasons"]) == _rejects_of(client, session, org, branch, day)
    return {entry["reason"]: entry["reject_count"] for entry in body["reasons"] if entry["reject_count"]}


def _seed_mixed_era_reject_reasons(owner_engine) -> None:
    """Tenant A is cut over at 12:00 local on 10 June 2026 (17:00Z).
        9 June:   2 legacy rejects, at 00:30 and 23:30 local       -> rfid_collision 1, configuration_error 1
        10 June:  2 legacy rejects before noon                     -> item_not_found 1, ils_acs_failure 1
                  4 v2 rejects from the cutover on                 -> rfid_collision 2, communication_error 1, unknown 1
        11 June:  1 v2 reject, at local midnight                   -> other 1
    Tenant B -- both of its branches -- was cut over on 1 June and has its own rejects in BOTH tables on 10 June,
    two of them with a stored class that is not a reason code."""
    with owner_engine.begin() as conn:
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-09 00:30:00", "a-0609-early", "Multiple tags in the field")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-09 23:30:00", "a-0609-late", "Collection code missing")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 09:00:00", "a-0610-morning", "Item not found")       # counts
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 11:59:59", "a-0610-one-second-before", "ACS timeout")  # counts
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 12:00:00", "a-0610-at-cutover", "Library not found")   # v1 is strictly before
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 15:00:00", "a-0610-after-cutover", "Library not found")  # does not count
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 17:00:00+00")
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 16:59:59+00", "configuration_error")   # before the cutover: no
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 17:00:00+00", "rfid_collision")        # exactly at it: counts
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 22:00:00+00", "rfid_collision", ONE_ITEM)       # counts
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 22:00:30+00", "communication_error", ONE_ITEM)  # the same item again
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 04:59:59+00", "unknown")               # 23:59:59 local: counts
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 05:00:00+00", "other")                 # local midnight: the 11th

        _set_cutover(conn, CUSTOMER_B, BRANCH_B, "2026-06-01 05:00:00+00")
        _set_cutover(conn, CUSTOMER_B, BRANCH_B_NORTH, "2026-06-01 05:00:00+00")   # a cutover is per branch
        for hour in (8, 9, 10):
            _v1_reject(conn, CUSTOMER_B, BRANCH_B, f"2026-06-10 {hour:02d}:00:00", f"b-0610-{hour}", "Library not found")
        for hour in (6, 9, 12):
            _v2_reject(conn, CUSTOMER_B, BRANCH_B, f"2026-06-10 {hour:02d}:00:00+00", "configuration_error")
        for hour in (15, 18):
            _v2_reject(conn, CUSTOMER_B, BRANCH_B, f"2026-06-10 {hour:02d}:00:00+00", "jam")
        for hour in (14, 20):
            _v2_reject(conn, CUSTOMER_B, BRANCH_B_NORTH, f"2026-06-10 {hour:02d}:30:00+00", "unknown")


A_0610 = {"item_not_found": 1, "ils_acs_failure": 1, "rfid_collision": 2, "communication_error": 1, "unknown": 1}
B_MAIN_0610 = {"configuration_error": 3, "other": 2}
B_NORTH_0610 = {"unknown": 2}


def test_the_reason_columns_are_what_the_grouping_relies_on(owner_engine):
    with owner_engine.connect() as conn:
        columns = {row[0]: (row[1], row[2]) for row in conn.execute(text("""
            SELECT table_name || '.' || column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND (table_name, column_name) IN (('rejects', 'error_message'), ('reject_events', 'error_class'))
        """)).fetchall()}
        class_check = conn.execute(text("""
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conname = 'reject_events_error_class_format_chk'
        """)).scalar()

    # A legacy message is free text and may be missing altogether; a v2 class is always present...
    assert columns == {"rejects.error_message": ("text", "YES"), "reject_events.error_class": ("text", "NO")}
    # ...and is held to a slug pattern only -- the database does not know the eight reason codes.
    assert "~" in class_check
    for reason in REASON_CODES:
        assert reason not in class_check, reason


def test_rejects_by_reason_for_a_v1_only_branch_classifies_every_stored_row_of_its_local_day(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-09 23:59:59", "a1", "Library not found")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 00:00:00", "a2", "Item not found in database")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 08:00:00", "a3", "No item found for this tag")
        # One item, rejected twice in the same second, for two different reasons.
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 09:00:00", "a4", "ITEM NOT FOUND")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 09:00:00", "a4", "Multiple RFID tags detected")
        # The same message on two different items: two rows, one group, a count of two.
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 10:00:00", "a5", "ACS timeout")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 10:00:00", "a6", "ACS timeout")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 11:00:00", "a7", "Collection code mismatch")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 12:00:00", "a8", "Library not found")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 13:00:00", "a9", "Something uncategorized")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 14:00:00", "a10", REASON_CANARY)
        # No text to classify, four ways: a real NULL, an empty string, a blank one and the literal "nan".
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 15:00:00", None, None)
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 16:00:00", "a12", "")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 17:00:00", "a13", "   ")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 23:59:59", "a14", "nan")
        _v1_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 00:00:00", "a15", "Library not found")
        # v2 rejects for tenant A on that day, but NO cutover: they are not part of its history.
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 15:00:00+00", "communication_error")
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 18:00:00+00", "jam")
        # Check-ins for tenant A on that day: not rejects.
        for hour in range(8, 18):
            _v1_checkin(conn, CUSTOMER_A, BRANCH_A, f"2026-06-10 {hour:02d}:00:00", f"checkin-{hour}")
        for hour in range(8, 13):
            _v1_reject(conn, CUSTOMER_B, BRANCH_B, f"2026-06-10 {hour:02d}:00:00", f"b{hour}", "Library not found")

    response = _rejects_by_reason(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10")

    assert response.status_code == 200
    assert response.json() == {
        "date": "2026-06-10",
        "timezone": "America/Chicago",
        "reasons": [
            {"reason": "item_not_found", "reject_count": 3},
            {"reason": "ils_acs_failure", "reject_count": 2},
            {"reason": "rfid_collision", "reject_count": 1},
            {"reason": "configuration_error", "reject_count": 1},
            {"reason": "routing_error", "reject_count": 1},
            {"reason": "communication_error", "reject_count": 0},
            {"reason": "other", "reject_count": 2},
            {"reason": "unknown", "reject_count": 4},
        ],
    }
    assert response.headers["cache-control"] == "no-store"
    for leaked in ("total", "v1", "v2", "unexpected", "customer_id", "branch_id", "cutover", "era", "error_message",
                   "error_class", "barcode", "item_key", "event_key", "key_id", "reject_rate", str(CUSTOMER_A),
                   "CANARY", "Smith", "31234000123456", "RFID", "ACS", "timeout", "uncategorized", "a4", "jam",
                   "Item Not Found", "label"):
        assert leaked not in response.text, leaked

    # The fourteen stored rows of the day, each under exactly one reason: the same number /rejects/count gives.
    assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 14
    assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == {
        "item_not_found": 3, "ils_acs_failure": 2, "rfid_collision": 1, "configuration_error": 1, "routing_error": 1,
        "other": 2, "unknown": 4,
    }
    assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-09") == {"routing_error": 1}
    assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == {"routing_error": 1}
    assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-12") == {}
    assert _reasons_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == {"routing_error": 5}
    # The check-in count for the same day is its own number, untouched by any reject row.
    assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 10


def test_rejects_by_reason_across_a_real_cutover_inside_the_day(owner_engine, checkin_api):
    _seed_mixed_era_reject_reasons(owner_engine)

    response = _rejects_by_reason(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10")

    # 2 legacy rows before 12:00 (one of them at 11:59:59), each classified from its message, + 4 v2 rows from
    # 17:00:00Z on (one of them exactly at it), each under its stored class. The legacy rows AT and after 12:00
    # ("Library not found") and the v2 row one second BEFORE 17:00Z (configuration_error) are on the other
    # era's side: neither routing_error nor configuration_error has a count.
    assert response.status_code == 200
    assert response.json()["reasons"] == [
        {"reason": "item_not_found", "reject_count": 1},
        {"reason": "ils_acs_failure", "reject_count": 1},
        {"reason": "rfid_collision", "reject_count": 2},
        {"reason": "configuration_error", "reject_count": 0},
        {"reason": "routing_error", "reject_count": 0},
        {"reason": "communication_error", "reject_count": 1},
        {"reason": "other", "reject_count": 0},
        {"reason": "unknown", "reject_count": 1},
    ]
    for leaked in ("total", "v1", "v2", "cutover", "era", "17:00", "error_", "item_key", ONE_ITEM, str(CUSTOMER_A)):
        assert leaked not in response.text, leaked

    assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-09") == {
        "rfid_collision": 1, "configuration_error": 1,
    }
    assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == A_0610
    assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == {"other": 1}


def test_rejects_by_reason_after_a_rollback_is_v1_only_again(owner_engine, checkin_api):
    _seed_mixed_era_reject_reasons(owner_engine)
    assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == A_0610

    # A later v2_cutovers row with no cutover_at is a recorded rollback for that branch.
    with owner_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_at, set_by)
            VALUES (:c, :b, NULL, now() + interval '1 minute', 'rls-test')
        """), {"c": CUSTOMER_A, "b": BRANCH_A})

    # All four legacy rows of 10 June now count -- including the two at and after the old cutover -- and no v2 row.
    assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == {
        "item_not_found": 1, "ils_acs_failure": 1, "routing_error": 2,
    }
    assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == {}
    # Tenant B's branches have their own cutovers and are unaffected.
    assert _reasons_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == B_MAIN_0610


def test_an_unexpected_stored_class_is_folded_into_other_and_never_returned(owner_engine, checkin_api, caplog):
    with owner_engine.begin() as conn:
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-06-01 05:00:00+00")
        # 'jam' and 'sensor_fault' satisfy the database's pattern check: the INSERT itself proves they can be stored.
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 15:00:00+00", "jam")
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 15:00:01+00", "jam")
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 16:00:00+00", "sensor_fault")
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 17:00:00+00", "other")            # a real `other`
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 18:00:00+00", "item_not_found")
        stored = conn.execute(text(
            "SELECT COUNT(*) FROM reject_events WHERE customer_id = :c AND error_class IN ('jam', 'sensor_fault')"
        ), {"c": CUSTOMER_A}).scalar()
    assert stored == 3

    with caplog.at_level("DEBUG"):
        response = _rejects_by_reason(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10")

    assert response.status_code == 200          # a stray class does not take the endpoint down
    assert response.json()["reasons"] == [
        {"reason": "item_not_found", "reject_count": 1},
        {"reason": "ils_acs_failure", "reject_count": 0},
        {"reason": "rfid_collision", "reject_count": 0},
        {"reason": "configuration_error", "reject_count": 0},
        {"reason": "routing_error", "reject_count": 0},
        {"reason": "communication_error", "reject_count": 0},
        {"reason": "other", "reject_count": 4},
        {"reason": "unknown", "reject_count": 0},
    ]
    for leaked in ("jam", "sensor_fault", "unexpected"):
        assert leaked not in response.text, leaked
    # Nothing is dropped from the day: the five stored rows are the five /rejects/count reports.
    assert _rejects_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 5
    # The warning carries the number of rows and nothing else.
    warnings = [record for record in caplog.records if record.name == "sortview.operational_metrics"]
    assert warnings and all(record.args == (3,) for record in warnings)
    assert "jam" not in caplog.text and "sensor_fault" not in caplog.text


def test_rejects_by_reason_is_isolated_from_another_tenant_in_both_tables(owner_engine, checkin_api):
    _seed_mixed_era_reject_reasons(owner_engine)

    # Tenant B has 3 legacy ("Library not found") and 5 + 2 v2 rejects on 10 June. None reaches tenant A:
    # A has no routing_error, no configuration_error and no `other` that day...
    assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == A_0610
    # ...and B's own reasons are its own: its legacy rows fall after ITS cutover, so only v2 counts,
    # and each of its two branches sees only its own rows.
    assert _reasons_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == B_MAIN_0610
    assert _reasons_of(checkin_api, SESSION_B, "tenant-b", "north", "2026-06-10") == B_NORTH_0610

    for session, org, branch in ((SESSION_A, "tenant-b", "main"), (SESSION_A, "tenant-a", "north"),
                                 (SESSION_B, "tenant-a", "main")):
        refused = _rejects_by_reason(checkin_api, session, org, branch, "2026-06-10")
        assert refused.status_code == 404 and refused.json() == TENANT_NOT_FOUND
        same = _reject_count(checkin_api, session, org, branch, "2026-06-10")
        assert (refused.status_code, refused.content) == (same.status_code, same.content)


def test_row_level_security_alone_hides_another_tenants_reject_reasons(owner_engine, runtime_engine):
    """The grouped statements also filter the tenant themselves. This shows
    the second line of defence on its own: the same two groupings, deliberately
    WITHOUT their tenant filter, on a tenant-scoped connection, return only
    that tenant's groups -- no other tenant's message or class, and no count
    that includes another tenant's rows."""
    _seed_members(owner_engine, runtime_engine)
    _seed_mixed_era_reject_reasons(owner_engine)

    seen = {}
    for name, customer_id, branch_id in (("a", CUSTOMER_A, BRANCH_A), ("b-main", CUSTOMER_B, BRANCH_B),
                                         ("b-north", CUSTOMER_B, BRANCH_B_NORTH)):
        with tenant_connection(runtime_engine, customer_id, branch_id) as conn:
            seen[name] = (
                dict(conn.execute(text("SELECT error_message, COUNT(*) FROM rejects GROUP BY error_message")).fetchall()),
                dict(conn.execute(text("SELECT error_class, COUNT(*) FROM reject_events GROUP BY error_class")).fetchall()),
            )

    assert seen == {
        "a": (
            {"Multiple tags in the field": 1, "Collection code missing": 1, "Item not found": 1, "ACS timeout": 1,
             "Library not found": 2},
            {"configuration_error": 1, "rfid_collision": 2, "communication_error": 1, "unknown": 1, "other": 1},
        ),
        "b-main": ({"Library not found": 3}, {"configuration_error": 3, "jam": 2}),
        "b-north": ({}, {"unknown": 2}),
    }

    # With no tenant context at all, the same groupings see nothing.
    with runtime_engine.connect() as conn:
        assert conn.execute(text("SELECT error_message, COUNT(*) FROM rejects GROUP BY error_message")).fetchall() == []
        assert conn.execute(text("SELECT error_class, COUNT(*) FROM reject_events GROUP BY error_class")).fetchall() == []


def test_rejects_by_reason_request_validation_through_the_real_app(checkin_api):
    path = REJECTS_BY_REASON_PATH.format(org="tenant-a", branch="main")

    assert checkin_api.get(path, params={"date": "2026-06-10"}).status_code == 401
    assert checkin_api.get(path, headers={"Cookie": f"__Host-sortview_api_session={SESSION_A}"}).status_code == 422
    assert _rejects_by_reason(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10T00:00:00").status_code == 422
    assert _rejects_by_reason(checkin_api, SESSION_A, "tenant-a", "main", "2026-02-30").status_code == 422
    assert checkin_api.post(path, params={"date": "2026-06-10"},
                            headers={"Cookie": f"__Host-sortview_api_session={SESSION_A}"}).status_code == 405
    assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2099-01-01") == {}   # eight zeros, still 200


@pytest.mark.parametrize("session_time_zone", ["UTC", "America/Chicago", "America/New_York", "Asia/Tokyo"])
def test_rejects_by_reason_does_not_depend_on_the_database_session_time_zone(
    owner_engine, checkin_api, runtime_engine, monkeypatch, session_time_zone
):
    _seed_mixed_era_reject_reasons(owner_engine)
    engine = create_engine(
        runtime_engine.url, connect_args={"options": f"-c timezone={session_time_zone}"}, hide_parameters=True
    )
    monkeypatch.setattr(database, "_engine", engine)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SHOW timezone")).scalar() == session_time_zone

        # 9 June is all legacy rows, at 00:30 and 23:30 local -- the two a session-dependent date cast misplaces.
        assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-09") == {
            "rfid_collision": 1, "configuration_error": 1,
        }
        # 10 June mixes both tables around the cutover, with a v2 row at 23:59:59 local.
        assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == A_0610
        assert _reasons_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-11") == {"other": 1}
        assert _reasons_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == B_MAIN_0610
        assert _reasons_of(checkin_api, SESSION_B, "tenant-b", "north", "2026-06-10") == B_NORTH_0610
        # The whole response, byte for byte, is one fixed answer whatever the session's zone.
        assert _rejects_by_reason(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10").json() == {
            "date": "2026-06-10",
            "timezone": "America/Chicago",
            "reasons": [{"reason": reason, "reject_count": A_0610.get(reason, 0)} for reason in REASON_CODES],
        }
    finally:
        engine.dispose()


def test_rejects_by_reason_on_the_spring_forward_date(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        # Tenant A, cut over well before: 8 March 2026 is [06:00Z, 05:00Z next day) -- 23 hours.
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-03-01 06:00:00+00")
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 05:59:59+00", "other")            # 23:59:59 CST on the 7th
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 06:00:00+00", "item_not_found")   # 00:00 CST on the 8th
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 07:59:59+00", "ils_acs_failure")  # 01:59:59 CST, before the jump
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-03-08 08:00:00+00", "ils_acs_failure")  # the next second: 03:00 CDT
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-03-09 04:59:59+00", "unknown")          # 23:59:59 CDT on the 8th
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-03-09 05:00:00+00", "other")            # 00:00 CDT on the 9th
        # Tenant B, never cut over: naive wall-clock rows, one stamped in the hour that did not exist.
        for stamped, barcode, message in (
            ("2026-03-07 23:59:59", "b1", "Library not found"), ("2026-03-08 00:00:00", "b2", "Item not found"),
            ("2026-03-08 02:30:00", "b3", "ACS timeout"), ("2026-03-08 03:00:00", "b4", "Multiple tags"),
            ("2026-03-08 23:59:59", "b5", ""), ("2026-03-09 00:00:00", "b6", "Library not found"),
        ):
            _v1_reject(conn, CUSTOMER_B, BRANCH_B, stamped, barcode, message)

    assert [_reasons_of(checkin_api, SESSION_A, "tenant-a", "main", day)
            for day in ("2026-03-07", "2026-03-08", "2026-03-09")] == [
        {"other": 1}, {"item_not_found": 1, "ils_acs_failure": 2, "unknown": 1}, {"other": 1},
    ]
    # The legacy row stamped 02:30 is a stored row of 8 March and has its reason there.
    assert [_reasons_of(checkin_api, SESSION_B, "tenant-b", "main", day)
            for day in ("2026-03-07", "2026-03-08", "2026-03-09")] == [
        {"routing_error": 1}, {"item_not_found": 1, "ils_acs_failure": 1, "rfid_collision": 1, "unknown": 1},
        {"routing_error": 1},
    ]


def test_rejects_by_reason_on_the_fall_back_date(owner_engine, checkin_api):
    with owner_engine.begin() as conn:
        # Tenant A, cut over well before: 1 November 2026 is [05:00Z, 06:00Z next day) -- 25 hours.
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-10-01 05:00:00+00")
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 04:59:59+00", "other")            # 23:59:59 CDT on 31 October
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 06:30:00+00", "rfid_collision")   # 01:30 CDT -- the first 01:30
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-11-01 07:30:00+00", "routing_error")    # 01:30 CST -- the second
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-11-02 05:59:59+00", "rfid_collision")   # 23:59:59 CST on the 1st
        _v2_reject(conn, CUSTOMER_A, BRANCH_A, "2026-11-02 06:00:00+00", "other")            # 00:00 CST on the 2nd
        # Tenant B, never cut over: two legacy rows both stamped 01:30 are simply two stored rows on 1 November.
        _v1_reject(conn, CUSTOMER_B, BRANCH_B, "2026-11-01 01:30:00", "b-first-pass", "ACS timeout")
        _v1_reject(conn, CUSTOMER_B, BRANCH_B, "2026-11-01 01:30:00", "b-second-pass", "ACS timeout")
        _v1_reject(conn, CUSTOMER_B, BRANCH_B, "2026-11-01 23:59:59", "b-late", None)

    # Both real 01:30 instants belong to 1 November and are counted once each, each under its own reason.
    assert [_reasons_of(checkin_api, SESSION_A, "tenant-a", "main", day)
            for day in ("2026-10-31", "2026-11-01", "2026-11-02")] == [
        {"other": 1}, {"rfid_collision": 2, "routing_error": 1}, {"other": 1},
    ]
    assert _reasons_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-11-01") == {"ils_acs_failure": 2, "unknown": 1}


def test_rejects_by_reason_on_one_pooled_connection_never_sees_another_tenant(
    owner_engine, checkin_api, runtime_engine, monkeypatch
):
    _seed_mixed_era_reject_reasons(owner_engine)
    # One connection: every statement of every request below runs on the same server backend.
    single = create_engine(runtime_engine.url, pool_size=1, max_overflow=0, hide_parameters=True)
    monkeypatch.setattr(database, "_engine", single)
    requests = (
        (SESSION_A, "tenant-a", "main", A_0610),
        (SESSION_B, "tenant-b", "main", B_MAIN_0610),
        (SESSION_A, "tenant-a", "main", A_0610),
        (SESSION_B, "tenant-b", "north", B_NORTH_0610),
        (SESSION_A, "tenant-a", "main", A_0610),
    )
    try:
        # _reasons_of also asks /rejects/count each time, so the two reject routes alternate on the connection.
        for session, org, branch, expected in requests:
            assert _reasons_of(checkin_api, session, org, branch, "2026-06-10") == expected
        # A check-in request for another tenant in between, on the same connection, changes nothing.
        assert _count_of(checkin_api, SESSION_A, "tenant-a", "main", "2026-06-10") == 0
        assert _reasons_of(checkin_api, SESSION_B, "tenant-b", "main", "2026-06-10") == B_MAIN_0610

        # Change the pooled connection's SESSION time zone for good, then ask again.
        with single.connect() as conn:
            conn.execute(text("SET TIME ZONE 'Asia/Tokyo'"))
            conn.commit()
        with single.connect() as conn:
            assert conn.execute(text("SHOW timezone")).scalar() == "Asia/Tokyo"

        for session, org, branch, expected in requests:
            assert _reasons_of(checkin_api, session, org, branch, "2026-06-10") == expected
        assert _rejects_by_reason(checkin_api, SESSION_A, "tenant-b", "main", "2026-06-10").status_code == 404

        # Nothing of any request's tenant context is left on the pooled connection.
        with single.connect() as conn:
            settings = conn.execute(text(
                "SELECT current_setting('app.operational_customer_id', true), "
                "current_setting('app.operational_branch_id', true)"
            )).one()
            assert all(value in (None, "") for value in settings)
            assert conn.execute(text("SELECT COUNT(*) FROM rejects")).scalar() == 0
            assert conn.execute(text("SELECT COUNT(*) FROM reject_events")).scalar() == 0
    finally:
        single.dispose()


def test_the_reasons_always_add_up_to_the_reject_count_for_the_same_day_and_data(owner_engine, checkin_api):
    _seed_mixed_era_reject_reasons(owner_engine)

    totals = {}
    for session, org, branch in ((SESSION_A, "tenant-a", "main"), (SESSION_B, "tenant-b", "main"),
                                 (SESSION_B, "tenant-b", "north")):
        for day in ("2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12"):
            reasons = _reasons_of(checkin_api, session, org, branch, day)       # asserts the sum itself
            totals[(org, branch, day)] = sum(reasons.values())
            assert totals[(org, branch, day)] == _rejects_of(checkin_api, session, org, branch, day)

    assert totals[("tenant-a", "main", "2026-06-09")] == 2
    assert totals[("tenant-a", "main", "2026-06-10")] == 6
    assert totals[("tenant-a", "main", "2026-06-11")] == 1
    assert totals[("tenant-b", "main", "2026-06-10")] == 5       # two of them stored as 'jam', counted as `other`
    assert totals[("tenant-b", "north", "2026-06-10")] == 2
    assert totals[("tenant-a", "main", "2026-06-08")] == totals[("tenant-a", "main", "2026-06-12")] == 0


# --- GET .../pipeline-status, end to end, as the runtime role ---
#
# What a branch's collection pipeline last reported, and when the server received it. As above, only the
# authenticated user is stubbed: the tenant-scope dependency, the resolver, tenant_connection, the context read-back,
# the cutover lookup, the one status read, the state mapping and the response are all real.
#
# This is the one customer route that reads a table with NO row level security: pipeline_status. So besides the
# route's own behaviour, these prove where its isolation actually comes from -- the service statement's explicit
# customer_id AND branch_id filter -- and that it holds on a real server, as the runtime role.

PIPELINE_STATUS_PATH = "/api/organizations/{org}/branches/{branch}/pipeline-status"
A, B_MAIN_SCOPE, B_NORTH_SCOPE = (CUSTOMER_A, BRANCH_A), (CUSTOMER_B, BRANCH_B), (CUSTOMER_B, BRANCH_B_NORTH)
# Operational pairs no organization maps to: reachable through no route, but storable in pipeline_status (which has
# no foreign key). The same branch id as tenant A's under tenant B's customer, and the reverse.
B_WITH_AS_BRANCH_ID, A_WITH_BS_BRANCH_ID = (CUSTOMER_B, BRANCH_A), (CUSTOMER_A, BRANCH_B)
LONG_PAST_CUTOVER, FAR_FUTURE_CUTOVER = "2026-01-01 06:00:00+00", "2099-01-01 06:00:00+00"


@pytest.fixture
def pipeline_api(customer_api, owner_engine):
    """The customer API, with pipeline_status and v2_cutovers emptied first and pipeline_status emptied again after:
    it has no foreign key, so the per-test TRUNCATE ... CASCADE of the tenant tables never reaches it."""
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE pipeline_status, v2_cutovers"))
    yield customer_api
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE pipeline_status"))


def _legacy_status(conn, scope, *, status=None, health_status=None, status_mins=None, health_mins=None) -> None:
    """A pipeline_status row whose two server stamps are that many minutes before the server's clock (None = NULL).
    One statement, so two equal minute values give two IDENTICAL instants."""
    conn.execute(text("""
        INSERT INTO pipeline_status (customer_id, branch_id, status, health_status, last_error, checkins_rows,
                                     last_run, last_attempt, status_reported_at, health_status_reported_at)
        VALUES (:c, :b, :s, :h, 'CANARY-raw-error-text', 4001, '1988-01-01 00:00:00', '1988-01-01 00:00:00',
                now() - make_interval(mins => :sm), now() - make_interval(mins => :hm))
    """), {"c": scope[0], "b": scope[1], "s": status, "h": health_status, "sm": status_mins, "hm": health_mins})


def _pipeline_status(client, session, org, branch):
    return client.get(PIPELINE_STATUS_PATH.format(org=org, branch=branch),
                      headers={"Cookie": f"__Host-sortview_api_session={session}"})


def _server_now(engine) -> datetime:
    with engine.connect() as conn:
        return conn.execute(text("SELECT clock_timestamp()")).scalar().astimezone(UTC)


def _status_of(client, session, org, branch) -> tuple[str, datetime | None]:
    """(state, last_reported_at as an aware UTC datetime or None) -- after checking the whole public contract."""
    response = _pipeline_status(client, session, org, branch)
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == ["timezone", "state", "last_reported_at"]
    assert body["timezone"] == "America/Chicago"
    assert body["state"] in ("ok", "degraded", "failed", "unknown")
    assert response.headers["cache-control"] == "no-store"
    # Exactly three keys, a known zone and one of four states: with the time set aside, nothing else is in the body.
    stamp = body["last_reported_at"]
    rest = response.text.replace(stamp, "") if stamp else response.text
    for leaked in ("customer_id", "branch_id", "cutover", "v1", "v2", "era", "source", "health", "schedule", "error",
                   "CANARY", "1988", "4001", "key_id", str(CUSTOMER_A), str(CUSTOMER_B)):
        assert leaked not in rest, leaked
    if stamp is None:
        assert body["state"] == "unknown"            # a state is never returned without a time
        return body["state"], None
    assert stamp.endswith("Z") and "+" not in stamp  # UTC, whatever the session's zone
    return body["state"], datetime.fromisoformat(stamp)


def _minutes_ago(engine, moment: datetime) -> float:
    return (_server_now(engine) - moment).total_seconds() / 60


def _about(minutes: float, expected: float) -> bool:
    return abs(minutes - expected) < 0.25           # within fifteen seconds of the seeded age


# --- a legacy branch: the later of the two stamps is the last report ---------------------------------------------------

def test_pipeline_status_of_a_legacy_branch_with_only_a_run_report(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:
        _legacy_status(conn, A, status="completed", health_status="degraded", status_mins=7)   # health has no stamp

    state, reported = _status_of(pipeline_api, SESSION_A, "tenant-a", "main")

    assert state == "ok" and _about(_minutes_ago(owner_engine, reported), 7)


def test_pipeline_status_of_a_legacy_branch_with_only_a_heartbeat(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:
        _legacy_status(conn, A, status="failed_upload", health_status="degraded", health_mins=3)   # status has no stamp

    state, reported = _status_of(pipeline_api, SESSION_A, "tenant-a", "main")

    assert state == "degraded" and _about(_minutes_ago(owner_engine, reported), 3)


def test_pipeline_status_of_a_legacy_branch_is_whichever_signal_was_reported_last(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:
        _legacy_status(conn, A, status="failed_upload", health_status="healthy", status_mins=2, health_mins=9)
        _legacy_status(conn, B_MAIN_SCOPE, status="failed_upload", health_status="healthy", status_mins=9, health_mins=2)

    a_state, a_reported = _status_of(pipeline_api, SESSION_A, "tenant-a", "main")
    b_state, b_reported = _status_of(pipeline_api, SESSION_B, "tenant-b", "main")

    assert a_state == "failed" and _about(_minutes_ago(owner_engine, a_reported), 2)    # the run report was last
    assert b_state == "ok" and _about(_minutes_ago(owner_engine, b_reported), 2)        # the heartbeat was last


def test_pipeline_status_on_an_exact_tie_takes_the_heartbeat(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:
        _legacy_status(conn, A, status="failed_upload", health_status="degraded", status_mins=4, health_mins=4)
        equal = conn.execute(text("SELECT status_reported_at = health_status_reported_at FROM pipeline_status "
                                  "WHERE customer_id = :c"), {"c": CUSTOMER_A}).scalar()
    assert equal is True                             # really the same instant, to the microsecond

    state, reported = _status_of(pipeline_api, SESSION_A, "tenant-a", "main")

    assert state == "degraded" and _about(_minutes_ago(owner_engine, reported), 4)


def test_pipeline_status_of_an_install_probe_is_unknown_with_the_time_it_was_received(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:
        _legacy_status(conn, A, status="preflight_check", health_status="healthy", status_mins=1, health_mins=30)

    state, reported = _status_of(pipeline_api, SESSION_A, "tenant-a", "main")

    assert state == "unknown" and _about(_minutes_ago(owner_engine, reported), 1)


# --- nothing reported ----------------------------------------------------------------------------------------------------

def test_pipeline_status_of_a_branch_that_never_reported_is_unknown_and_null_and_still_200(pipeline_api):
    response = _pipeline_status(pipeline_api, SESSION_A, "tenant-a", "main")

    assert response.status_code == 200
    assert response.json() == {"timezone": "America/Chicago", "state": "unknown", "last_reported_at": None}


def test_pipeline_status_of_a_row_written_before_the_server_stamped_reports_is_unknown_and_null(owner_engine, pipeline_api):
    # As every row is straight after migration 16b41d730e15: a status, three naive timestamps, and no stamp.
    with owner_engine.begin() as conn:
        conn.execute(text("INSERT INTO pipeline_status (customer_id, branch_id, status, health_status, last_run, "
                          "last_attempt, updated_at) VALUES (:c, :b, 'completed', 'healthy', now(), now(), now())"),
                     {"c": CUSTOMER_A, "b": BRANCH_A})

    assert _status_of(pipeline_api, SESSION_A, "tenant-a", "main") == ("unknown", None)


# --- which source: the effective cutover -----------------------------------------------------------------------------------

def test_pipeline_status_with_an_active_key_but_no_cutover_is_still_the_legacy_report(owner_engine, pipeline_api):
    # The customer_api fixture gives every branch an active ingest key. Without a cutover that changes nothing.
    with owner_engine.begin() as conn:
        _legacy_status(conn, A, status="failed_upload", status_mins=6)
        assert conn.execute(text("SELECT COUNT(*) FROM ingest_key_ids WHERE customer_id = :c AND status = 'active'"),
                            {"c": CUSTOMER_A}).scalar() == 1

    state, reported = _status_of(pipeline_api, SESSION_A, "tenant-a", "main")

    assert state == "failed" and _about(_minutes_ago(owner_engine, reported), 6)


def test_pipeline_status_with_a_future_cutover_is_still_the_legacy_report(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:
        _legacy_status(conn, A, status="failed_upload", status_mins=6)
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, FAR_FUTURE_CUTOVER)

    assert _status_of(pipeline_api, SESSION_A, "tenant-a", "main")[0] == "failed"


def test_pipeline_status_after_the_cutover_is_the_current_report(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:
        _legacy_status(conn, A, status="failed_upload", status_mins=6)
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, LONG_PAST_CUTOVER)

    state, reported = _status_of(pipeline_api, SESSION_A, "tenant-a", "main")

    # Tenant A's key: healthy, heartbeat thirty minutes ago (the customer_api fixture). The legacy row is not read.
    assert state == "ok" and _about(_minutes_ago(owner_engine, reported), 30)


def test_pipeline_status_after_the_cutover_shows_a_reported_schedule_fault(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, LONG_PAST_CUTOVER)
        _set_cutover(conn, CUSTOMER_B, BRANCH_B_NORTH, LONG_PAST_CUTOVER)
        conn.execute(text("UPDATE ingest_key_ids SET collector_schedule_status = 'task_disabled' WHERE key_id = :k"),
                     {"k": KEY_A})                                   # healthy heartbeat, disabled task
        conn.execute(text("UPDATE ingest_key_ids SET collector_schedule_status = 'query_failed' WHERE key_id = :k"),
                     {"k": KEY_B_NORTH})                             # degraded heartbeat, unchecked schedule

    assert _status_of(pipeline_api, SESSION_A, "tenant-a", "main")[0] == "failed"
    assert _status_of(pipeline_api, SESSION_B, "tenant-b", "north")[0] == "degraded"
    for session, org, branch in ((SESSION_A, "tenant-a", "main"), (SESSION_B, "tenant-b", "north")):
        assert "task_disabled" not in _pipeline_status(pipeline_api, session, org, branch).text
        assert "query_failed" not in _pipeline_status(pipeline_api, session, org, branch).text


def test_pipeline_status_after_a_rollback_is_the_legacy_report_again(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:
        _legacy_status(conn, A, status="failed_upload", status_mins=6)
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, LONG_PAST_CUTOVER)
    assert _status_of(pipeline_api, SESSION_A, "tenant-a", "main")[0] == "ok"

    # A later v2_cutovers row with no cutover_at is a recorded rollback for that branch.
    with owner_engine.begin() as conn:
        conn.execute(text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_at, set_by) "
                          "VALUES (:c, :b, NULL, now() + interval '1 minute', 'rls-test')"), {"c": CUSTOMER_A, "b": BRANCH_A})

    assert _status_of(pipeline_api, SESSION_A, "tenant-a", "main")[0] == "failed"


def test_pipeline_status_source_is_decided_per_branch(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:
        _legacy_status(conn, A, status="completed", status_mins=6)
        _legacy_status(conn, B_MAIN_SCOPE, status="completed", status_mins=6)
        _legacy_status(conn, B_NORTH_SCOPE, status="completed", status_mins=6)
        _set_cutover(conn, CUSTOMER_B, BRANCH_B, LONG_PAST_CUTOVER)          # only tenant B's main branch is current

    assert _status_of(pipeline_api, SESSION_A, "tenant-a", "main")[0] == "ok"        # legacy: completed
    assert _status_of(pipeline_api, SESSION_B, "tenant-b", "main")[0] == "failed"    # current: its key reports error
    assert _status_of(pipeline_api, SESSION_B, "tenant-b", "north")[0] == "ok"       # legacy: completed


# --- tenant isolation on a table with no row level security ----------------------------------------------------------------

def _seed_isolation_rows(owner_engine) -> None:
    with owner_engine.begin() as conn:
        _legacy_status(conn, A, status="completed", status_mins=5)
        _legacy_status(conn, B_MAIN_SCOPE, status="failed_upload", status_mins=10)
        _legacy_status(conn, B_NORTH_SCOPE, health_status="degraded", health_mins=15)
        _legacy_status(conn, B_WITH_AS_BRANCH_ID, status="failed_upload", health_status="auth_failure",
                       status_mins=1, health_mins=1)                 # tenant A's branch id, under tenant B's customer
        _legacy_status(conn, A_WITH_BS_BRANCH_ID, status="failed_upload", health_status="auth_failure",
                       status_mins=1, health_mins=1)                 # tenant B's branch id, under tenant A's customer


def test_pipeline_status_is_not_protected_by_row_level_security(owner_engine, runtime_engine):
    """The premise of everything below. If this fails because pipeline_status has gained row level security, that
    is welcome -- and the explicit filter is then no longer the only protection. Until then, it is."""
    _seed_members(owner_engine, runtime_engine)
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE pipeline_status"))
        assert conn.execute(text("SELECT relrowsecurity FROM pg_class WHERE relname = 'pipeline_status'")).scalar() is False
    _seed_isolation_rows(owner_engine)
    try:
        with tenant_connection(runtime_engine, CUSTOMER_A, BRANCH_A) as conn:
            # Deliberately unscoped, as the runtime role, on tenant A's connection: every tenant's row comes back.
            seen = {tuple(r) for r in conn.execute(text("SELECT customer_id, branch_id FROM pipeline_status"))}
            raw_errors = conn.execute(text("SELECT COUNT(*) FROM pipeline_status WHERE last_error IS NOT NULL")).scalar()

        assert seen == {A, B_MAIN_SCOPE, B_NORTH_SCOPE, B_WITH_AS_BRANCH_ID, A_WITH_BS_BRANCH_ID}
        assert raw_errors == 5
    finally:
        with owner_engine.begin() as conn:
            conn.execute(text("TRUNCATE pipeline_status"))


def test_pipeline_status_gives_each_user_only_their_own_branchs_report(owner_engine, pipeline_api):
    _seed_isolation_rows(owner_engine)

    a = _status_of(pipeline_api, SESSION_A, "tenant-a", "main")
    b_main = _status_of(pipeline_api, SESSION_B, "tenant-b", "main")
    b_north = _status_of(pipeline_api, SESSION_B, "tenant-b", "north")

    # Each answer is its own row's state AND its own row's age: three different rows were read.
    assert a[0] == "ok" and _about(_minutes_ago(owner_engine, a[1]), 5)
    assert b_main[0] == "failed" and _about(_minutes_ago(owner_engine, b_main[1]), 10)
    assert b_north[0] == "degraded" and _about(_minutes_ago(owner_engine, b_north[1]), 15)


def test_pipeline_status_never_leaks_the_same_branch_id_under_another_customer(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:      # ONLY the look-alike rows exist: a one-minute-old auth failure each
        _legacy_status(conn, B_WITH_AS_BRANCH_ID, health_status="auth_failure", health_mins=1)
        _legacy_status(conn, A_WITH_BS_BRANCH_ID, health_status="auth_failure", health_mins=1)

    # Tenant A (101, 11) has no row. Neither (202, 11) nor (101, 22) is borrowed for it -- nor for tenant B (202, 22).
    assert _status_of(pipeline_api, SESSION_A, "tenant-a", "main") == ("unknown", None)
    assert _status_of(pipeline_api, SESSION_B, "tenant-b", "main") == ("unknown", None)


def test_pipeline_status_never_leaks_another_branch_of_the_same_customer(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:
        _legacy_status(conn, B_NORTH_SCOPE, status="failed_upload", status_mins=1)

    assert _status_of(pipeline_api, SESSION_B, "tenant-b", "main") == ("unknown", None)
    assert _status_of(pipeline_api, SESSION_B, "tenant-b", "north")[0] == "failed"


def test_pipeline_status_refuses_a_cross_tenant_request_exactly_as_the_sibling_routes_do(owner_engine, pipeline_api):
    _seed_isolation_rows(owner_engine)

    for session, org, branch in ((SESSION_A, "tenant-b", "main"), (SESSION_A, "tenant-b", "north"),
                                 (SESSION_A, "tenant-a", "north"), (SESSION_B, "tenant-a", "main")):
        refused = _pipeline_status(pipeline_api, session, org, branch)
        sibling = _ingest_status(pipeline_api, session, org, branch)

        assert refused.status_code == 404 and refused.json() == TENANT_NOT_FOUND
        assert (refused.status_code, refused.content) == (sibling.status_code, sibling.content)


def test_pipeline_status_of_an_active_branch_with_no_operational_mapping_is_the_existing_404(owner_engine, pipeline_api):
    with owner_engine.begin() as conn:
        conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) "
                          "VALUES (31, 1, 'unmapped', 'Unmapped', 'active', NULL)"))
        _legacy_status(conn, (CUSTOMER_A, 31), status="completed", status_mins=1)   # a row its id WOULD match, if mapped

    refused = _pipeline_status(pipeline_api, SESSION_A, "tenant-a", "unmapped")
    sibling = _ingest_status(pipeline_api, SESSION_A, "tenant-a", "unmapped")

    assert refused.status_code == 404 and refused.json() == TENANT_NOT_FOUND
    assert refused.content == sibling.content


def test_pipeline_status_request_handling_through_the_real_app(pipeline_api):
    path = PIPELINE_STATUS_PATH.format(org="tenant-a", branch="main")
    cookie = {"Cookie": f"__Host-sortview_api_session={SESSION_A}"}

    assert pipeline_api.get(path).status_code == 401                                   # no session
    assert pipeline_api.post(path, headers=cookie).status_code == 405                  # read-only
    ignored = pipeline_api.get(path, headers=cookie, params={"customer_id": CUSTOMER_B, "branch_id": BRANCH_B,
                                                             "now": "2099-01-01T00:00:00Z", "source": "current"})
    assert ignored.status_code == 200 and ignored.json()["state"] == "unknown"         # hints change nothing


# --- one pooled connection, alternating tenants and sources ----------------------------------------------------------------

def test_pipeline_status_on_one_pooled_connection_never_sees_another_tenant(
    owner_engine, pipeline_api, runtime_engine, monkeypatch
):
    _seed_isolation_rows(owner_engine)
    with owner_engine.begin() as conn:
        _set_cutover(conn, CUSTOMER_B, BRANCH_B, LONG_PAST_CUTOVER)      # tenant B main answers from its key: error
    # One connection: every statement of every request below runs on the same server backend.
    single = create_engine(runtime_engine.url, pool_size=1, max_overflow=0, hide_parameters=True)
    monkeypatch.setattr(database, "_engine", single)
    requests = (
        (SESSION_A, "tenant-a", "main", "ok"),             # legacy
        (SESSION_B, "tenant-b", "main", "failed"),         # current
        (SESSION_A, "tenant-a", "main", "ok"),
        (SESSION_B, "tenant-b", "north", "degraded"),      # legacy
        (SESSION_B, "tenant-b", "main", "failed"),
        (SESSION_A, "tenant-a", "main", "ok"),
    )
    try:
        first = {(org, branch): _status_of(pipeline_api, session, org, branch) for session, org, branch, _ in requests}
        for session, org, branch, expected in requests:
            answer = _status_of(pipeline_api, session, org, branch)
            assert answer[0] == expected and answer == first[(org, branch)], (org, branch)
        # Another operational route for another tenant in between, on the same connection, changes nothing.
        assert _ingest_status(pipeline_api, SESSION_B, "tenant-b", "north").json()["status"]["health_status"] == "degraded"
        assert _status_of(pipeline_api, SESSION_A, "tenant-a", "main") == first[("tenant-a", "main")]

        # Change the pooled connection's SESSION time zone for good, then ask again: the same UTC answers.
        with single.connect() as conn:
            conn.execute(text("SET TIME ZONE 'Asia/Tokyo'"))
            conn.commit()
        with single.connect() as conn:
            assert conn.execute(text("SHOW timezone")).scalar() == "Asia/Tokyo"
        for session, org, branch, _expected in requests:
            assert _status_of(pipeline_api, session, org, branch) == first[(org, branch)], (org, branch)
        assert _pipeline_status(pipeline_api, SESSION_A, "tenant-b", "main").status_code == 404

        # Nothing of any request's tenant context is left on the pooled connection.
        with single.connect() as conn:
            settings = conn.execute(text(
                "SELECT current_setting('app.operational_customer_id', true), "
                "current_setting('app.operational_branch_id', true)"
            )).one()
            assert all(value in (None, "") for value in settings)
    finally:
        single.dispose()


# --- the database session's time zone ----------------------------------------------------------------------------------------

def test_pipeline_status_is_the_same_utc_instant_whatever_the_database_session_time_zone(
    owner_engine, pipeline_api, runtime_engine, monkeypatch
):
    _seed_isolation_rows(owner_engine)
    with owner_engine.begin() as conn:
        _set_cutover(conn, CUSTOMER_B, BRANCH_B, LONG_PAST_CUTOVER)
    bodies, raw_offsets = {}, {}
    for zone in ("UTC", "America/Chicago", "America/New_York", "Asia/Tokyo", "Asia/Kolkata"):
        engine = create_engine(runtime_engine.url, connect_args={"options": f"-c timezone={zone}"}, hide_parameters=True)
        monkeypatch.setattr(database, "_engine", engine)
        try:
            with engine.connect() as conn:
                assert conn.execute(text("SHOW timezone")).scalar() == zone
                raw_offsets[zone] = conn.execute(text(
                    "SELECT status_reported_at FROM pipeline_status WHERE customer_id = :c AND branch_id = :b"
                ), {"c": CUSTOMER_A, "b": BRANCH_A}).scalar().utcoffset()
            bodies[zone] = (
                _pipeline_status(pipeline_api, SESSION_A, "tenant-a", "main").content,       # legacy
                _pipeline_status(pipeline_api, SESSION_B, "tenant-b", "main").content,       # current
                _pipeline_status(pipeline_api, SESSION_B, "tenant-b", "north").content,      # legacy, heartbeat
            )
        finally:
            engine.dispose()

    # The driver handed the stored instant back in five different offsets...
    assert len(set(raw_offsets.values())) == 5 and raw_offsets["UTC"] == timedelta(0)
    # ...and the three responses are byte-for-byte the same in every one of them.
    assert len(set(bodies.values())) == 1
    for body in next(iter(bodies.values())):
        assert body.endswith(b'Z"}') and b"+" not in body


# --- end to end: a report posted by a collector is what the customer route then answers with ----------------------------

def test_pipeline_status_answers_with_what_the_real_report_endpoint_just_stored(owner_engine, pipeline_api, runtime_engine, monkeypatch):
    monkeypatch.setattr(main, "engine", runtime_engine)               # the collector API writes as the runtime role too
    assert _HASH_EXPR in main._AGENT_TOKEN_LOOKUP_SQL
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR))

    def report(body: dict) -> None:
        response = pipeline_api.post("/upload-pipeline-status", headers={"Authorization": f"Bearer {TOKEN_A}"},
                                     json={"customer_id": CUSTOMER_A, "branch_id": BRANCH_A, **body})
        assert response.status_code == 200, response.text

    def pause() -> None:
        with owner_engine.connect() as conn:
            conn.execute(text("SELECT pg_sleep(0.05)"))

    assert _status_of(pipeline_api, SESSION_A, "tenant-a", "main") == ("unknown", None)

    before = _server_now(owner_engine)
    report({"status": "completed", "checkins_rows": 3})
    first = _status_of(pipeline_api, SESSION_A, "tenant-a", "main")
    assert first[0] == "ok" and before - timedelta(seconds=1) <= first[1] <= _server_now(owner_engine) + timedelta(seconds=1)

    pause()
    report({"health_status": "degraded", "pending_outbox_count": 2})
    second = _status_of(pipeline_api, SESSION_A, "tenant-a", "main")
    assert second[0] == "degraded" and second[1] > first[1]           # the heartbeat is now the last report

    pause()
    report({"status": "failed_upload", "last_error": "CANARY-raw-error-text"})
    third = _status_of(pipeline_api, SESSION_A, "tenant-a", "main")
    assert third[0] == "failed" and third[1] > second[1]              # then the failed run is

    pause()
    report({"checkins_rows": 9})                                      # carries neither signal: the answer does not move
    assert _status_of(pipeline_api, SESSION_A, "tenant-a", "main") == third

    # Tenant B posted nothing and sees nothing of it.
    assert _status_of(pipeline_api, SESSION_B, "tenant-b", "main") == ("unknown", None)


# --- GET .../checkins/by-destination?date=YYYY-MM-DD, end to end, as the runtime role ---
#
# The same real pieces as the count tests above, plus the two that are new:
# the settings read (organization_settings / branch_settings hold real JSONB,
# and neither is under row level security, so the statement's own filter is
# what scopes it) and the grouped statements over a real TEXT destination in
# each check-in table. Every answer is also checked against the count
# endpoint for the same day, on the same server.
#
# The runtime role is given SELECT on the two settings tables here. That is
# the production role's observed baseline (scripts/runtime_role_privileges.py)
# and nothing this endpoint adds: it is granted in this section only because
# no earlier test in this file read those tables.

CHECKINS_BY_DESTINATION_PATH = "/api/organizations/{org}/branches/{branch}/checkins/by-destination"


def _routed_v1_checkin(conn, customer_id, branch_id, local_wall_clock: str, barcode: str, destination) -> None:
    conn.execute(text("""
        INSERT INTO checkins (customer_id, branch_id, event_time, title, barcode, destination, bin, source_file)
        VALUES (:c, :b, CAST(:t AS timestamp), 'title', :barcode, :d, 'bin1', 'rls_test.csv')
    """), {"c": customer_id, "b": branch_id, "t": local_wall_clock, "barcode": barcode, "d": destination})


def _routed_v2_checkin(conn, customer_id, branch_id, instant: str, destination: str) -> None:
    event_key = hashlib.sha256(f"{customer_id}:{branch_id}:{instant}:{secrets.token_hex(8)}".encode()).hexdigest()
    conn.execute(text("""
        INSERT INTO checkin_events (customer_id, branch_id, key_id, event_key, event_time, destination, bin)
        VALUES (:c, :b, :k, :ek, CAST(:t AS timestamptz), :d, 'unknown')
    """), {"c": customer_id, "b": branch_id, "k": KEY_A, "ek": event_key, "t": instant, "d": destination})


def _transit_settings(home: str, *labels: str) -> str:
    import json

    return json.dumps({
        "security": {"admin_lock_hash": "CANARY-settings-secret"},
        "transit": {
            "home_branch_label": home,
            # The free-form keys are deliberately useless: a destination is matched by its label.
            "destinations": [{"key": f"branch_{n}", "label": label, "enabled": True} for n, label in enumerate(labels, 1)],
        },
    })


@pytest.fixture
def routing_api(checkin_api, owner_engine, runtime_engine):
    """Tenant A routes to Westside and Library Express; tenant B's organization routes to Westside only, and its
    north site overrides that with a destination of its own."""
    role = runtime_engine.url.username
    with owner_engine.begin() as conn:
        conn.execute(text(f"GRANT SELECT ON TABLE public.organization_settings, public.branch_settings TO {role}"))  # nosec B608
        conn.execute(text("TRUNCATE organization_settings, branch_settings RESTART IDENTITY"))
        conn.execute(text("TRUNCATE v2_cutovers RESTART IDENTITY"))
        for org_id, document in ((1, _transit_settings("Main", "Westside", "Library Express")),
                                 (2, _transit_settings("Main", "Westside"))):
            conn.execute(
                text("INSERT INTO organization_settings (organization_id, settings_json) VALUES (:o, CAST(:s AS jsonb))"),
                {"o": org_id, "s": document},
            )
        conn.execute(
            text("INSERT INTO branch_settings (branch_id, settings_json) VALUES (:b, CAST(:s AS jsonb))"),
            {"b": BRANCH_B_NORTH, "s": _transit_settings("North", "Harbor Depot")},
        )
    return checkin_api


def _checkins_by_destination(client, session, org, branch, day: str):
    return client.get(
        CHECKINS_BY_DESTINATION_PATH.format(org=org, branch=branch),
        params={"date": day},
        headers={"Cookie": f"__Host-sortview_api_session={session}"},
    )


def _routing_of(client, session, org, branch, day: str) -> dict:
    """The answer for `day`, after checking it is the approved shape, that every check-in is in exactly one place,
    and that its total is what the count endpoint says for the same day."""
    response = _checkins_by_destination(client, session, org, branch, day)
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == ["date", "timezone", "checkin_count", "home", "transit", "transit_count", "other_count"]
    assert (body["date"], body["timezone"]) == (day, "America/Chicago")
    assert all(list(entry) == ["key", "label", "checkin_count"] for entry in body["transit"])
    assert body["transit_count"] == sum(entry["checkin_count"] for entry in body["transit"])
    assert body["home"]["checkin_count"] + body["transit_count"] + body["other_count"] == body["checkin_count"]
    assert body["checkin_count"] == _count_of(client, session, org, branch, day)
    return body


def _transit_of(body: dict) -> dict[str, int]:
    return {entry["key"]: entry["checkin_count"] for entry in body["transit"]}


def test_checkins_by_destination_for_a_v1_only_site_classifies_its_stored_labels(owner_engine, routing_api):
    with owner_engine.begin() as conn:
        for number, destination in enumerate(
            ["Main", "Main", "1", "LOCAL", "Westside", "WESTSIDE", "Library Express", "No Agency Destination",
             "Northgate Annex", "", None]
        ):
            _routed_v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 09:00:00", f"a-{number}", destination)
        _routed_v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-09 23:59:59", "a-day-before", "Westside")
        _routed_v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 00:00:00", "a-day-after", "Westside")
        # v2 rows for tenant A on that day, but NO cutover: they are not part of its history.
        _routed_v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 15:00:00+00", "westside")
        for number in range(7):
            _routed_v1_checkin(conn, CUSTOMER_B, BRANCH_B, "2026-06-10 09:00:00", f"b-{number}", "Westside")

    body = _routing_of(routing_api, SESSION_A, "tenant-a", "main", "2026-06-10")

    assert body["checkin_count"] == 11
    assert body["home"] == {"label": "Main", "checkin_count": 4}
    assert body["transit"] == [
        {"key": "westside", "label": "Westside", "checkin_count": 2},
        {"key": "library_express", "label": "Library Express", "checkin_count": 1},
    ]
    assert body["other_count"] == 4


def test_checkins_by_destination_across_a_cutover_keeps_one_destination_one_destination(owner_engine, routing_api):
    with owner_engine.begin() as conn:
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 17:00:00+00")          # 12:00 local
        _routed_v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 09:00:00", "a1", "Library Express")      # counts
        _routed_v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 11:59:59", "a2", "Main")                 # counts
        _routed_v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 12:00:00", "a3", "Library Express")      # v2 owns it
        _routed_v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 20:30:00", "a4", "Westside")             # v2 owns it
        _routed_v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 16:59:59+00", "library_express")         # before
        _routed_v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 17:00:00+00", "library_express")         # counts
        _routed_v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 12:30:00-05", "westside")                # counts
        _routed_v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 04:59:59+00", "main")                    # counts
        _routed_v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 04:59:59+00", "unknown")                 # counts
        _routed_v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 05:00:00+00", "westside")                # next day

    body = _routing_of(routing_api, SESSION_A, "tenant-a", "main", "2026-06-10")

    assert body["checkin_count"] == 6
    assert body["home"]["checkin_count"] == 2
    assert _transit_of(body) == {"westside": 1, "library_express": 2}
    assert body["other_count"] == 1
    # The days either side are each read from one era alone.
    assert _routing_of(routing_api, SESSION_A, "tenant-a", "main", "2026-06-09")["checkin_count"] == 0
    after = _routing_of(routing_api, SESSION_A, "tenant-a", "main", "2026-06-11")
    assert (after["checkin_count"], _transit_of(after)) == (1, {"westside": 1, "library_express": 0})


def test_checkins_by_destination_does_not_depend_on_the_database_session_time_zone(owner_engine, routing_api, runtime_engine, monkeypatch):
    with owner_engine.begin() as conn:
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 17:00:00+00")
        _routed_v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 00:30:00", "a1", "Westside")
        _routed_v2_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-11 04:30:00+00", "westside")

    answers = []
    for zone in ("UTC", "Asia/Tokyo", "America/Los_Angeles"):
        shifted = create_engine(runtime_engine.url, hide_parameters=True, connect_args={"options": f"-c timezone={zone}"})
        monkeypatch.setattr(database, "_engine", shifted)
        try:
            answers.append(_transit_of(_routing_of(routing_api, SESSION_A, "tenant-a", "main", "2026-06-10")))
        finally:
            shifted.dispose()

    assert answers == [{"westside": 2, "library_express": 0}] * 3


def test_checkins_by_destination_is_isolated_by_tenant_and_by_site_with_each_sites_own_settings(owner_engine, routing_api):
    with owner_engine.begin() as conn:
        _routed_v1_checkin(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 09:00:00", "a1", "Westside")
        for number in range(3):
            _routed_v1_checkin(conn, CUSTOMER_B, BRANCH_B, "2026-06-10 09:00:00", f"b-main-{number}", "Westside")
        _routed_v1_checkin(conn, CUSTOMER_B, BRANCH_B, "2026-06-10 09:00:00", "b-main-le", "Library Express")
        for number in range(5):
            _routed_v1_checkin(conn, CUSTOMER_B, BRANCH_B_NORTH, "2026-06-10 09:00:00", f"b-north-{number}", "Harbor Depot")
        _routed_v1_checkin(conn, CUSTOMER_B, BRANCH_B_NORTH, "2026-06-10 09:00:00", "b-north-home", "North")
        _routed_v1_checkin(conn, CUSTOMER_B, BRANCH_B_NORTH, "2026-06-10 09:00:00", "b-north-ws", "Westside")

    tenant_a = _routing_of(routing_api, SESSION_A, "tenant-a", "main", "2026-06-10")
    b_main = _routing_of(routing_api, SESSION_B, "tenant-b", "main", "2026-06-10")
    b_north = _routing_of(routing_api, SESSION_B, "tenant-b", "north", "2026-06-10")

    assert (tenant_a["checkin_count"], _transit_of(tenant_a)) == (1, {"westside": 1, "library_express": 0})
    # Tenant B's organization has one destination: Library Express is not one of its, so it is "other".
    assert (b_main["checkin_count"], _transit_of(b_main), b_main["other_count"]) == (4, {"westside": 3}, 1)
    # Its north site's own settings replace the organization's.
    assert b_north["home"] == {"label": "North", "checkin_count": 1}
    assert b_north["transit"] == [{"key": "harbor_depot", "label": "Harbor Depot", "checkin_count": 5}]
    assert b_north["other_count"] == 1

    # A member of one tenant cannot read the other's, and learns nothing from asking.
    crossed = _checkins_by_destination(routing_api, SESSION_A, "tenant-b", "north", "2026-06-10")
    assert crossed.status_code == 404
    assert crossed.json() == {"code": "tenant_not_found", "message": "Organization or branch not found."}
    assert _checkins_by_destination(routing_api, "no-such-session", "tenant-a", "main", "2026-06-10").status_code == 401

    for response in (tenant_a, b_main, b_north):
        assert "CANARY" not in str(response) and "branch_1" not in str(response)


# --- An organization's sorter sites, read as the runtime role ---
#
# services.sorter_inventory_service against real tables: real foreign keys
# from collector_installations to organizations and branches, a real BOOLEAN
# is_primary to order by, and the runtime role's own SELECT on
# collector_installations (granted above, as in production's baseline).
# The table is not under row level security: the statement's own filter on
# the organization's slug is what scopes the read.

def _install_collector(conn, organization_id: int, branch_id: int, name: str, status: str = "active") -> None:
    conn.execute(text("""
        INSERT INTO collector_installations (organization_id, branch_id, name, hostname, collector_version, status)
        VALUES (:o, :b, :n, 'CANARY-HOST-01', '9.9.9-canary', :s)
    """), {"o": organization_id, "b": branch_id, "n": name, "s": status})


def test_sorter_sites_are_one_per_host_branch_isolated_by_organization_and_carry_no_machine_detail(
    owner_engine, runtime_engine, monkeypatch
):
    from services import sorter_inventory_service
    from services.sorter_inventory_service import SorterSite, list_sorter_sites

    _seed_members(owner_engine, runtime_engine)     # adds tenant B's second branch, "north"
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE collector_installations RESTART IDENTITY CASCADE"))
        _install_collector(conn, 1, BRANCH_A, "Tenant A Main AMH")
        _install_collector(conn, 1, BRANCH_A, "Tenant A retired unit", status="retired")
        # Tenant B: two collectors at its main branch (one site), and one being set up at north.
        _install_collector(conn, 2, BRANCH_B, "Tenant B AMH 1")
        _install_collector(conn, 2, BRANCH_B, "Tenant B AMH 2")
        _install_collector(conn, 2, BRANCH_B_NORTH, "Tenant B North AMH", status="provisioning")
    monkeypatch.setattr(sorter_inventory_service, "get_engine", lambda: runtime_engine)

    tenant_a = list_sorter_sites("tenant-a")
    tenant_b = list_sorter_sites("tenant-b")

    assert tenant_a == [
        SorterSite(slug="main", name="Tenant A Main AMH", host_branch_slug="main", host_branch_name="Main",
                   status="active", collector_count=1),
    ]
    assert sorted(tenant_b, key=lambda site: site.slug) == [
        SorterSite(slug="main", name="Tenant B AMH 1", host_branch_slug="main", host_branch_name="Main",
                   status="active", collector_count=2),
        SorterSite(slug="north", name="Tenant B North AMH", host_branch_slug="north", host_branch_name="North",
                   status="provisioning", collector_count=1),
    ]
    assert list_sorter_sites("no-such-tenant") == []
    assert "CANARY" not in repr(tenant_a + tenant_b) and "9.9.9" not in repr(tenant_a + tenant_b)


# --- GET .../reports/{overview,volume,routing,reliability}?from=&to=, end to end, as the runtime role ---
#
# The range reports against real tables: real TIMESTAMP bounds for the legacy
# tables, real TIMESTAMPTZ bounds for the current ones, many FILTER columns in
# one statement, real row level security. What is proved here and nowhere
# else: that every day of a range is what the single-day endpoints answer for
# it ON A REAL SERVER, and that the database session's time zone cannot move
# a single row between days, hours or eras.

REPORT_PATH = "/api/organizations/{org}/branches/{branch}/reports/{report}"
SITE_PATH = "/api/organizations/{org}/branches/{branch}/{endpoint}"


@pytest.fixture
def reports_api(routing_api, owner_engine, monkeypatch):
    """The routing fixture's tenants and settings, on 20 June 2026, with tenant A cut over at 12:00 local on
    10 June (17:00Z) and rows in BOTH tables on both sides of the cutover."""
    from controlled_clock import ControlledClock

    from customer_api import report_routes

    # The report routes read this clock, not the real one: 20 June 2026, whatever today is.
    stand_in = ControlledClock(datetime(2026, 6, 20, 18, 0, tzinfo=UTC)).datetime_class()
    monkeypatch.setattr(report_routes, "datetime", stand_in)
    with owner_engine.begin() as conn:
        _set_cutover(conn, CUSTOMER_A, BRANCH_A, "2026-06-10 17:00:00+00")
        for day in range(7, 14):
            for number, (clock, destination) in enumerate(
                (("00:00:00", "Main"), ("09:30:00", "Westside"), ("11:59:59", "Library Express"),
                 ("12:00:00", "Northgate Annex"), ("23:59:59", "1"))
            ):
                _routed_v1_checkin(conn, CUSTOMER_A, BRANCH_A, f"2026-06-{day:02d} {clock}", f"a-{day}-{number}", destination)
            for clock, destination in (("05:00:00", "main"), ("16:59:59", "westside"), ("17:00:00", "library_express"),
                                       ("23:30:00", "unknown")):
                _routed_v2_checkin(conn, CUSTOMER_A, BRANCH_A, f"2026-06-{day:02d} {clock}+00", destination)
            _v1_reject(conn, CUSTOMER_A, BRANCH_A, f"2026-06-{day:02d} 09:30:00", f"ar-{day}-1", "Item not found")
            _v1_reject(conn, CUSTOMER_A, BRANCH_A, f"2026-06-{day:02d} 23:59:59", f"ar-{day}-2", "Multiple RFID tags")
            _v2_reject(conn, CUSTOMER_A, BRANCH_A, f"2026-06-{day:02d} 17:00:00+00", "routing_error")
            _v2_reject(conn, CUSTOMER_A, BRANCH_A, f"2026-06-{day:02d} 23:30:00+00", "ils_acs_failure")
            # Tenant B is busier on every day, in the legacy tables only.
            for number in range(9):
                _routed_v1_checkin(conn, CUSTOMER_B, BRANCH_B, f"2026-06-{day:02d} 10:00:00", f"b-{day}-{number}", "Westside")
            _v1_reject(conn, CUSTOMER_B, BRANCH_B, f"2026-06-{day:02d} 10:00:00", f"br-{day}", "Item not found")
    return routing_api


def _report_of(client, session, org, branch, report: str, first: str, last: str):
    return client.get(
        REPORT_PATH.format(org=org, branch=branch, report=report),
        params={"from": first, "to": last},
        headers={"Cookie": f"__Host-sortview_api_session={session}"},
    )


def _site_read(client, session, org, branch, endpoint: str, day: str) -> dict:
    response = client.get(
        SITE_PATH.format(org=org, branch=branch, endpoint=endpoint),
        params={"date": day},
        headers={"Cookie": f"__Host-sortview_api_session={session}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _assert_reports_reconcile_with_the_single_day_reads(client, session, org, branch, days: list[str]) -> dict:
    """Reads the four reports over `days` and checks every date of each against the single-day endpoints.
    Returns the overview, for the caller's own assertions."""
    reports = {}
    for report in ("overview", "volume", "routing", "reliability"):
        response = _report_of(client, session, org, branch, report, days[0], days[-1])
        assert response.status_code == 200, response.text
        reports[report] = response.json()
        assert reports[report]["range"] == {
            "from": days[0], "to": days[-1], "days": len(days), "timezone": "America/Chicago", "includes_today": False,
        }
        assert [entry["date"] for entry in reports[report]["days"]] == days

    hours_total = [0] * 24
    reasons_total: dict[str, int] = {}
    for index, day in enumerate(days):
        checkins = _site_read(client, session, org, branch, "checkins/count", day)["checkin_count"]
        rejects = _site_read(client, session, org, branch, "rejects/count", day)["reject_count"]
        by_destination = _site_read(client, session, org, branch, "checkins/by-destination", day)
        for hour in _site_read(client, session, org, branch, "checkins/by-hour", day)["hours"]:
            hours_total[hour["hour"]] += hour["checkin_count"]
        for reason in _site_read(client, session, org, branch, "rejects/by-reason", day)["reasons"]:
            reasons_total[reason["reason"]] = reasons_total.get(reason["reason"], 0) + reason["reject_count"]

        assert reports["overview"]["days"][index] == {"date": day, "checkin_count": checkins, "reject_count": rejects}
        assert reports["volume"]["days"][index] == {"date": day, "checkin_count": checkins}
        assert reports["reliability"]["days"][index] == {"date": day, "checkin_count": checkins, "reject_count": rejects}
        assert reports["routing"]["days"][index] == {
            "date": day,
            "checkin_count": checkins,
            "home_count": by_destination["home"]["checkin_count"],
            "transit_counts": [entry["checkin_count"] for entry in by_destination["transit"]],
            "other_count": by_destination["other_count"],
        }

    assert [entry["checkin_count"] for entry in reports["volume"]["hours"]] == hours_total
    assert {entry["reason"]: entry["reject_count"] for entry in reports["reliability"]["reasons"]} == reasons_total
    overview, routing = reports["overview"], reports["routing"]
    assert overview["home_count"] + overview["transit_count"] + overview["other_count"] == overview["checkin_count"]
    assert routing["checkin_count"] == reports["volume"]["checkin_count"] == overview["checkin_count"]
    assert (overview["home_count"], overview["transit_count"], overview["other_count"]) == (
        routing["home"]["checkin_count"], routing["transit_count"], routing["other_count"])
    return overview


JUNE_8_TO_12 = ["2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12"]


def test_range_reports_across_a_cutover_reconcile_with_the_single_day_reads_on_a_real_server(reports_api):
    overview = _assert_reports_reconcile_with_the_single_day_reads(reports_api, SESSION_A, "tenant-a", "main", JUNE_8_TO_12)

    # 8, 9 June: legacy only, five check-ins and two rejects a day.
    # 10 June: three legacy check-ins before noon (the 12:00:00 row is at the cutover: v2 owns that instant),
    #          then the two current rows from 17:00Z on.
    # 11, 12 June: current only. A row at 05:00Z is 00:00 local on that date; one at 23:30Z is 18:30 local.
    assert [day["checkin_count"] for day in overview["days"]] == [5, 5, 5, 4, 4]
    assert [day["reject_count"] for day in overview["days"]] == [2, 2, 3, 2, 2]
    assert overview["active_days"] == 5


def test_range_reports_do_not_depend_on_the_database_session_time_zone(reports_api, runtime_engine, monkeypatch):
    answers = []
    for zone in ("UTC", "Asia/Tokyo", "America/Los_Angeles", "Asia/Kolkata"):
        shifted = create_engine(runtime_engine.url, hide_parameters=True, connect_args={"options": f"-c timezone={zone}"})
        monkeypatch.setattr(database, "_engine", shifted)
        try:
            overview = _assert_reports_reconcile_with_the_single_day_reads(
                reports_api, SESSION_A, "tenant-a", "main", JUNE_8_TO_12)
            volume = _report_of(reports_api, SESSION_A, "tenant-a", "main", "volume", "2026-06-08", "2026-06-12").json()
            answers.append((overview, volume))
        finally:
            shifted.dispose()

    assert all(answer == answers[0] for answer in answers)
    assert answers[0][0]["checkin_count"] == 23


def test_range_reports_are_isolated_by_tenant_and_refuse_what_the_single_day_reads_refuse(reports_api):
    tenant_a = _report_of(reports_api, SESSION_A, "tenant-a", "main", "overview", "2026-06-08", "2026-06-12").json()
    tenant_b = _report_of(reports_api, SESSION_B, "tenant-b", "main", "overview", "2026-06-08", "2026-06-12").json()

    assert (tenant_a["checkin_count"], tenant_a["reject_count"]) == (23, 11)
    assert (tenant_b["checkin_count"], tenant_b["reject_count"]) == (45, 5)
    # Tenant B's organization has one destination, Westside; nothing of tenant A's is in its answer.
    assert (tenant_b["home_count"], tenant_b["transit_count"], tenant_b["other_count"]) == (0, 45, 0)

    for report in ("overview", "volume", "routing", "reliability"):
        crossed = _report_of(reports_api, SESSION_A, "tenant-b", "main", report, "2026-06-08", "2026-06-12")
        assert crossed.status_code == 404
        assert crossed.json() == {"code": "tenant_not_found", "message": "Organization or branch not found."}
        assert _report_of(reports_api, "no-such-session", "tenant-a", "main", report, "2026-06-08", "2026-06-12").status_code == 401
        assert _report_of(reports_api, SESSION_A, "tenant-a", "main", report, "2026-06-12", "2026-06-08").status_code == 422
        assert _report_of(reports_api, SESSION_A, "tenant-a", "main", report, "2026-06-19", "2026-06-21").status_code == 422


# --- GET /api/organizations/{org}/reports/{overview,routing-network,reliability}, end to end, as the runtime role ---
#
# The organization reports against real tables and real row level security.
# What is proved here and nowhere else: that an organization's report is read
# ONE SORTER SITE AT A TIME, each on a connection whose policies show that
# site's rows and no other's -- so even a statement with no WHERE clause, run
# while a report is being built, sees one site -- and that the sum of those
# reads is exactly the sorter-site reports added up, whatever the database
# session's time zone.

ORGANIZATION_REPORT_PATH = "/api/organizations/{org}/reports/{report}"
ORGANIZATION_REPORTS = ("overview", "routing-network", "reliability")


@pytest.fixture
def organization_reports_api(reports_api, owner_engine):
    """The reports fixture's rows, plus what makes tenant B an organization with TWO sorter sites:

        tenant A   one sorter, at main (crosses its cutover on 10 June)
        tenant B   main: two collectors, one site. Legacy only: nine check-ins to Westside and one reject a day.
                   north: one collector. Cut over at 12:00 local on 10 June, with rows in BOTH tables on every
                   day, so the rows on the wrong side of the cutover must count for nothing:

                       check-ins by day (8-12 June)   3, 3, 5, 2, 2  = 15     home 6, Harbor Depot 6, other 3
                       rejects by day                 1, 1, 2, 1, 1  = 6
    """
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE collector_installations RESTART IDENTITY CASCADE"))
        _install_collector(conn, 1, BRANCH_A, "Tenant A Main AMH")
        _install_collector(conn, 2, BRANCH_B, "Tenant B AMH 1")
        _install_collector(conn, 2, BRANCH_B, "Tenant B AMH 2")
        _install_collector(conn, 2, BRANCH_B_NORTH, "Tenant B North AMH")
        _set_cutover(conn, CUSTOMER_B, BRANCH_B_NORTH, "2026-06-10 17:00:00+00")
        for day in range(8, 13):
            for number, (clock, destination) in enumerate(
                (("09:00:00", "North"), ("09:00:01", "North"), ("09:30:00", "Harbor Depot"))
            ):
                _routed_v1_checkin(conn, CUSTOMER_B, BRANCH_B_NORTH, f"2026-06-{day:02d} {clock}", f"bn-{day}-{number}", destination)
            _routed_v2_checkin(conn, CUSTOMER_B, BRANCH_B_NORTH, f"2026-06-{day:02d} 18:00:00+00", "harbor_depot")
            _routed_v2_checkin(conn, CUSTOMER_B, BRANCH_B_NORTH, f"2026-06-{day:02d} 19:00:00+00", "westside")
            _v1_reject(conn, CUSTOMER_B, BRANCH_B_NORTH, f"2026-06-{day:02d} 09:00:00", f"bnr-{day}", "Item not found")
            _v2_reject(conn, CUSTOMER_B, BRANCH_B_NORTH, f"2026-06-{day:02d} 18:00:00+00", "rfid_collision")
    return reports_api


def _organization_report_of(client, session, org, report: str, first: str = "2026-06-08", last: str = "2026-06-12"):
    return client.get(
        ORGANIZATION_REPORT_PATH.format(org=org, report=report),
        params={"from": first, "to": last},
        headers={"Cookie": f"__Host-sortview_api_session={session}"},
    )


def _organization_reports(client, session, org) -> dict:
    reports = {}
    for report in ORGANIZATION_REPORTS:
        response = _organization_report_of(client, session, org, report)
        assert response.status_code == 200, response.text
        reports[report] = response.json()
        assert reports[report]["range"] == {
            "from": "2026-06-08", "to": "2026-06-12", "days": 5, "timezone": "America/Chicago", "includes_today": False,
        }
    return reports


def _assert_organization_reports_are_its_site_reports_added_up(client, session, org, branches: list[str]) -> dict:
    """Reads the three organization reports and checks every figure against the sorter-site reports of
    `branches`, read through their own endpoints on the same server. Returns the organization reports."""
    reports = _organization_reports(client, session, org)
    sites: dict[str, dict] = {}
    for branch in branches:
        sites[branch] = {}
        for report in ("overview", "routing", "reliability"):
            response = _report_of(client, session, org, branch, report, "2026-06-08", "2026-06-12")
            assert response.status_code == 200, response.text
            sites[branch][report] = response.json()

    overview, network, reliability = (reports[report] for report in ORGANIZATION_REPORTS)

    assert sorted(sorter["slug"] for sorter in overview["sorters"]) == sorted(branches)
    for sorter in overview["sorters"]:
        site = sites[sorter["slug"]]["overview"]
        assert sorter["available"] is True
        for figure in ("checkin_count", "active_days", "transit_count", "reject_count"):
            assert sorter[figure] == site[figure], (sorter["slug"], figure)
    for total in ("checkin_count", "home_count", "transit_count", "other_count", "reject_count"):
        assert overview["totals"][total] == sum(site["overview"][total] for site in sites.values()), total
    for index, day in enumerate(overview["days"]):
        assert day["checkin_count"] == sum(site["overview"]["days"][index]["checkin_count"] for site in sites.values())
        assert day["reject_count"] == sum(site["overview"]["days"][index]["reject_count"] for site in sites.values())
    totals = overview["totals"]
    assert totals["home_count"] + totals["transit_count"] + totals["other_count"] == totals["checkin_count"]

    assert sorted(source["sorter"]["slug"] for source in network["sources"]) == sorted(branches)
    for source in network["sources"]:
        site = sites[source["sorter"]["slug"]]["routing"]
        for figure in ("checkin_count", "home", "transit", "transit_count", "other_count"):
            assert source[figure] == site[figure], (source["sorter"]["slug"], figure)
    assert network["totals"] == {"checkin_count": totals["checkin_count"], "transit_count": totals["transit_count"]}
    assert sum(destination["checkin_count"] for destination in network["destinations"]) == totals["transit_count"]

    for sorter in reliability["sorters"]:
        site = sites[sorter["sorter"]["slug"]]["reliability"]
        for figure in ("checkin_count", "reject_count", "reasons"):
            assert sorter[figure] == site[figure], (sorter["sorter"]["slug"], figure)
    for index, reason in enumerate(reliability["totals"]["reasons"]):
        assert reason["reject_count"] == sum(site["reliability"]["reasons"][index]["reject_count"] for site in sites.values())
    assert sum(reason["reject_count"] for reason in reliability["totals"]["reasons"]) == reliability["totals"]["reject_count"]
    assert reliability["days"] == overview["days"]
    return reports


# Every field the three organization reports may have, at any depth. None of them holds an identifier.
_ORGANIZATION_REPORT_FIELDS = frozenset({
    "range", "from", "to", "days", "timezone", "includes_today", "totals", "sorters", "sources", "destinations",
    "sorter", "slug", "name", "host_branch", "status", "collector_count", "available", "active_days",
    "checkin_count", "home_count", "transit_count", "other_count", "reject_count", "source_count",
    "home", "transit", "key", "label", "reasons", "reason", "date",
})
# The fields that say what something is called or when it was. Everything else is a count or a flag.
_ORGANIZATION_REPORT_TEXT_FIELDS = frozenset({"from", "to", "timezone", "slug", "name", "status", "key", "label", "reason", "date"})


def _fields_and_values(value, field: str = ""):
    """(field, value) for every scalar anywhere in a JSON value, under the name of the field that holds it,
    and (field, None) for every field that holds an object or a list."""
    if isinstance(value, dict):
        for name, item in value.items():
            if isinstance(item, (dict, list)):
                yield name, None
            yield from _fields_and_values(item, name)
    elif isinstance(value, list):
        for item in value:
            yield from _fields_and_values(item, field)
    else:
        yield field, value


def _assert_no_operational_id_is_exposed(answer: dict) -> None:
    """No operational id of either tenant is anywhere in `answer`.

    An id is a short number -- 101, 202, 11, 22, 23 -- so looking for its digits in the serialized answer
    proves nothing: "202" is in every date of 2026. The answer is walked instead:

      * it has no field but the approved ones, and none of those is an identifier;
      * no text value IS an id (a slug, name, key or label equal to one);
      * no number sits anywhere but in a count of rows, days or collectors -- so a number that happened
        to equal an id could only ever be a count of rows, never an id that was returned.
    """
    ids = {CUSTOMER_A, CUSTOMER_B, BRANCH_A, BRANCH_B, BRANCH_B_NORTH}
    seen = list(_fields_and_values(answer))
    assert seen, "the answer is empty"

    for field, value in seen:
        assert field in _ORGANIZATION_REPORT_FIELDS, field
        for forbidden in ("customer", "tenant", "organization", "branch_id", "_id", "installation"):
            assert forbidden not in field, field
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, str):
            assert field in _ORGANIZATION_REPORT_TEXT_FIELDS, (field, value)
            assert value.strip() not in {str(identifier) for identifier in ids}, (field, value)
        else:
            assert isinstance(value, int), (field, value)
            assert field in ("days", "active_days") or field.endswith("_count"), (field, value)
    # The two customer ids in particular are larger than any count this fixture can produce, so here they
    # can be ruled out as exact values too.
    numbers = {value for _field, value in seen if isinstance(value, int) and not isinstance(value, bool)}
    assert not numbers & {CUSTOMER_A, CUSTOMER_B}, numbers & {CUSTOMER_A, CUSTOMER_B}


def test_organization_reports_are_the_site_reports_added_up_on_a_real_server(organization_reports_api):
    one_sorter = _assert_organization_reports_are_its_site_reports_added_up(
        organization_reports_api, SESSION_A, "tenant-a", ["main"])
    two_sorters = _assert_organization_reports_are_its_site_reports_added_up(
        organization_reports_api, SESSION_B, "tenant-b", ["main", "north"])

    # Tenant A: its one sorter's own answer (see the range-report tests above).
    assert (one_sorter["overview"]["totals"]["checkin_count"], one_sorter["overview"]["totals"]["reject_count"]) == (23, 11)

    # Tenant B: main's 45 and north's 15. Two collectors at main are one sorter, counted once.
    overview = two_sorters["overview"]
    assert overview["totals"] == {"checkin_count": 60, "home_count": 6, "transit_count": 51, "other_count": 3, "reject_count": 11}
    by_slug = {sorter["slug"]: sorter for sorter in overview["sorters"]}
    assert (by_slug["main"]["checkin_count"], by_slug["main"]["collector_count"], by_slug["main"]["name"]) == (45, 2, "Tenant B AMH 1")
    assert (by_slug["north"]["checkin_count"], by_slug["north"]["reject_count"], by_slug["north"]["active_days"]) == (15, 6, 5)
    # North, either side of its cutover: no row counted twice, and none moved to another day.
    assert [day["checkin_count"] for day in overview["days"]] == [9 + 3, 9 + 3, 9 + 5, 9 + 2, 9 + 2]
    assert [day["reject_count"] for day in overview["days"]] == [1 + 1, 1 + 1, 1 + 2, 1 + 1, 1 + 1]

    # Westside is configured at main only. North's rows stored as "westside" are north's "other": equal text
    # on another site's rows is not that site's destination.
    destinations = {entry["key"]: entry for entry in two_sorters["routing-network"]["destinations"]}
    assert destinations["westside"] == {"key": "westside", "label": "Westside", "checkin_count": 45, "source_count": 1}
    assert destinations["harbor_depot"] == {"key": "harbor_depot", "label": "Harbor Depot", "checkin_count": 6, "source_count": 1}
    assert set(destinations) == {"westside", "harbor_depot"}

    for answer in (*one_sorter.values(), *two_sorters.values()):
        # Distinctive text: nowhere in the answer, in any form.
        for leaked in ("CANARY", "branch_1", "Item not found", "9.9.9"):
            assert leaked not in str(answer), leaked
        _assert_no_operational_id_is_exposed(answer)


def test_organization_reports_read_one_site_at_a_time_and_rls_shows_each_read_only_that_site(
    organization_reports_api, monkeypatch
):
    from contextlib import contextmanager

    from customer_api import organization_report_routes

    real_open = tenant_scope.open_customer_tenant_connection
    seen = []

    @contextmanager
    def probing_open(tenant):
        with real_open(tenant) as conn:
            # Deliberately unscoped: no WHERE clause at all. Only the policies decide what these see.
            seen.append((
                tenant.org_slug, tenant.branch_slug,
                conn.execute(text("SELECT COUNT(*) FROM checkins")).scalar_one(),
                conn.execute(text("SELECT COUNT(*) FROM checkin_events")).scalar_one(),
                conn.execute(text("SELECT COUNT(DISTINCT (customer_id, branch_id)) FROM checkins")).scalar_one(),
            ))
            yield conn

    monkeypatch.setattr(organization_report_routes, "open_customer_tenant_connection", probing_open)

    for report in ORGANIZATION_REPORTS:
        seen.clear()
        assert _organization_report_of(organization_reports_api, SESSION_B, "tenant-b", report).status_code == 200

        # Two sites, opened one after the other. Each connection holds exactly one (customer, branch) and
        # exactly that site's rows: main's 63 legacy rows (9 a day, 7-13 June), north's 15 legacy and 10 current.
        # Never the rows the sites of the two tenants hold together, and never tenant A's.
        assert sorted(seen) == [("tenant-b", "main", 63, 0, 1), ("tenant-b", "north", 15, 10, 1)], report

    seen.clear()
    assert _organization_report_of(organization_reports_api, SESSION_A, "tenant-a", "overview").status_code == 200
    assert seen == [("tenant-a", "main", 35, 28, 1)]


def test_organization_reports_are_isolated_by_organization_and_refuse_what_the_organization_detail_refuses(
    organization_reports_api,
):
    not_found = {"code": "organization_not_found", "message": "Organization not found."}

    for report in ORGANIZATION_REPORTS:
        for session, org in ((SESSION_A, "tenant-b"), (SESSION_B, "tenant-a"), (SESSION_A, "no-such-tenant")):
            crossed = _organization_report_of(organization_reports_api, session, org, report)
            assert (crossed.status_code, crossed.json()) == (404, not_found)
        assert _organization_report_of(organization_reports_api, "no-such-session", "tenant-a", report).status_code == 401
        assert _organization_report_of(
            organization_reports_api, SESSION_A, "tenant-a", report, "2026-06-12", "2026-06-08").status_code == 422
        assert _organization_report_of(
            organization_reports_api, SESSION_A, "tenant-a", report, "2026-06-19", "2026-06-21").status_code == 422

    tenant_a = _organization_report_of(organization_reports_api, SESSION_A, "tenant-a", "overview").json()
    tenant_b = _organization_report_of(organization_reports_api, SESSION_B, "tenant-b", "overview").json()
    assert tenant_a["totals"]["checkin_count"] == 23 and tenant_b["totals"]["checkin_count"] == 60


def test_a_sorter_at_an_unmapped_branch_is_not_available_and_a_cancelled_organization_is_not_found(
    organization_reports_api, owner_engine
):
    with owner_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) "
            "VALUES (24, 2, 'annex', 'Annex', 'active', NULL)"
        ))
        _install_collector(conn, 2, 24, "Tenant B Annex AMH", status="provisioning")

    overview = _organization_report_of(organization_reports_api, SESSION_B, "tenant-b", "overview").json()
    annex = next(sorter for sorter in overview["sorters"] if sorter["slug"] == "annex")

    assert annex["available"] is False
    assert (annex["checkin_count"], annex["active_days"], annex["transit_count"], annex["reject_count"]) == (0, 0, 0, 0)
    assert overview["totals"]["checkin_count"] == 60
    network = _organization_report_of(organization_reports_api, SESSION_B, "tenant-b", "routing-network").json()
    assert sorted(source["sorter"]["slug"] for source in network["sources"]) == ["main", "north"]

    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE id = 2"))
    suspended = _organization_report_of(organization_reports_api, SESSION_B, "tenant-b", "overview")
    assert suspended.json()["totals"]["checkin_count"] == 60

    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'cancelled' WHERE id = 2"))
    for report in ORGANIZATION_REPORTS:
        assert _organization_report_of(organization_reports_api, SESSION_B, "tenant-b", report).status_code == 404


def test_organization_reports_do_not_depend_on_the_database_session_time_zone(
    organization_reports_api, runtime_engine, monkeypatch
):
    answers = []
    for zone in ("UTC", "Asia/Tokyo", "America/Los_Angeles", "Asia/Kolkata"):
        shifted = create_engine(runtime_engine.url, hide_parameters=True, connect_args={"options": f"-c timezone={zone}"})
        monkeypatch.setattr(database, "_engine", shifted)
        try:
            answers.append(_assert_organization_reports_are_its_site_reports_added_up(
                organization_reports_api, SESSION_B, "tenant-b", ["main", "north"]))
        finally:
            shifted.dispose()

    assert all(answer == answers[0] for answer in answers)
    assert answers[0]["overview"]["totals"]["checkin_count"] == 60
    assert [day["checkin_count"] for day in answers[0]["overview"]["days"]] == [12, 12, 14, 11, 11]


# --- GET .../reports/bins?from=&to=, end to end, as the runtime role ---
#
# Bin Volume against real tables: a real nullable TEXT `bin` on the legacy
# table and a real NOT NULL one on the current table, grouped by the server,
# with many FILTER columns a statement, under real row level security. What
# is proved here and nowhere else: that the report's total is EXACTLY the
# overview's and the volume report's on a real server, that one bin stored
# two ways is one bin across a real cutover, and that the database session's
# time zone cannot move a row between hours, days or eras.

def _binned_v1_checkin(conn, customer_id, branch_id, local_wall_clock: str, barcode: str, stored_bin) -> None:
    conn.execute(text("""
        INSERT INTO checkins (customer_id, branch_id, event_time, title, barcode, destination, bin, source_file)
        VALUES (:c, :b, CAST(:t AS timestamp), 'title', :barcode, 'Main', :bin, 'rls_test.csv')
    """), {"c": customer_id, "b": branch_id, "t": local_wall_clock, "barcode": barcode, "bin": stored_bin})


def _binned_v2_checkin(conn, customer_id, branch_id, instant: str, stored_bin: str) -> None:
    event_key = hashlib.sha256(f"{customer_id}:{branch_id}:{instant}:{secrets.token_hex(8)}".encode()).hexdigest()
    conn.execute(text("""
        INSERT INTO checkin_events (customer_id, branch_id, key_id, event_key, event_time, destination, bin)
        VALUES (:c, :b, :k, :ek, CAST(:t AS timestamptz), 'main', :bin)
    """), {"c": customer_id, "b": branch_id, "k": KEY_A, "ek": event_key, "t": instant, "bin": stored_bin})


@pytest.fixture
def bins_api(reports_api, owner_engine):
    """The reports fixture's rows -- every one of which has no usable bin ("bin1" in the legacy table, "unknown"
    in the current one) -- plus, on each of 8-12 June, rows that do. Tenant A is cut over at 12:00 local on
    10 June, so a row counts only on its own table's side of that instant:

        legacy   "04" 09:15 · "0" 10:00 · NULL 10:30     counted on the 8th, 9th and 10th
                 "10" 13:00                              counted on the 8th and 9th (on the 10th it is after the cutover)
        current  "2" 15:00Z (10:00 local)                counted on the 11th and 12th (on the 10th it is before it)
                 "4" 18:00Z (13:00 local) · "unknown" 19:00Z     counted on the 10th, 11th and 12th

        bin 0: 3 (10:00) · bin 2: 2 (10:00) · bin 4: 3 (09:00) + 3 (13:00) · bin 10: 2 (13:00)     known 13
        unknown: 3 + 3 of these, and all 23 of the reports fixture's                                unknown 29

    Tenant B's main site (no cutover) gets a bin of its own, and tenant A's bin numbers on far more rows.
    """
    with owner_engine.begin() as conn:
        for day in range(8, 13):
            stamp = f"2026-06-{day:02d}"
            _binned_v1_checkin(conn, CUSTOMER_A, BRANCH_A, f"{stamp} 09:15:00", f"bin-a-{day}-1", "04")
            _binned_v1_checkin(conn, CUSTOMER_A, BRANCH_A, f"{stamp} 10:00:00", f"bin-a-{day}-2", "0")
            _binned_v1_checkin(conn, CUSTOMER_A, BRANCH_A, f"{stamp} 10:30:00", f"bin-a-{day}-3", None)
            _binned_v1_checkin(conn, CUSTOMER_A, BRANCH_A, f"{stamp} 13:00:00", f"bin-a-{day}-4", "10")
            _binned_v2_checkin(conn, CUSTOMER_A, BRANCH_A, f"{stamp} 15:00:00+00", "2")
            _binned_v2_checkin(conn, CUSTOMER_A, BRANCH_A, f"{stamp} 18:00:00+00", "4")
            _binned_v2_checkin(conn, CUSTOMER_A, BRANCH_A, f"{stamp} 19:00:00+00", "unknown")
            _binned_v1_checkin(conn, CUSTOMER_B, BRANCH_B, f"{stamp} 09:00:00", f"bin-b-{day}-1", "99")
            for number in range(6):
                _binned_v1_checkin(conn, CUSTOMER_B, BRANCH_B, f"{stamp} 10:00:00", f"bin-b-{day}-4-{number}", "4")
    return reports_api


def _bins_of(client, session, org, branch, first: str = "2026-06-08", last: str = "2026-06-12"):
    return _report_of(client, session, org, branch, "bins", first, last)


def _bin_hours(**counts: int) -> list[int]:
    hours = [0] * 24
    for name, count in counts.items():
        hours[int(name[1:])] = count
    return hours


def _checked_bins(client, session, org, branch) -> dict:
    """The answer for 8-12 June, after checking its shape, its invariants, and that its total is exactly the
    overview's and the volume report's for the same range on the same server."""
    response = _bins_of(client, session, org, branch)
    assert response.status_code == 200, response.text
    body = response.json()

    assert list(body) == ["range", "checkin_count", "known_bin_count", "unknown_bin_count", "bins"]
    assert body["range"] == {
        "from": "2026-06-08", "to": "2026-06-12", "days": 5, "timezone": "America/Chicago", "includes_today": False,
    }
    assert body["known_bin_count"] + body["unknown_bin_count"] == body["checkin_count"]
    assert sum(entry["checkin_count"] for entry in body["bins"]) == body["known_bin_count"]
    for entry in body["bins"]:
        assert list(entry) == ["key", "checkin_count", "hours"]
        assert len(entry["hours"]) == 24 and sum(entry["hours"]) == entry["checkin_count"] > 0
    keys = [entry["key"] for entry in body["bins"]]
    assert keys == sorted(keys, key=int) and len(set(keys)) == len(keys)

    overview = _report_of(client, session, org, branch, "overview", "2026-06-08", "2026-06-12").json()
    volume = _report_of(client, session, org, branch, "volume", "2026-06-08", "2026-06-12").json()
    assert body["checkin_count"] == overview["checkin_count"] == volume["checkin_count"]
    assert body["range"] == overview["range"] == volume["range"]
    return body


def test_bin_volume_across_a_cutover_on_a_real_server_matches_the_overview_and_volume_reports(bins_api):
    body = _checked_bins(bins_api, SESSION_A, "tenant-a", "main")

    assert (body["checkin_count"], body["known_bin_count"], body["unknown_bin_count"]) == (42, 13, 29)
    # "04" in the legacy table and "4" in the current one are bin 4; NULL and "unknown" are both unknown.
    assert body["bins"] == [
        {"key": "0", "checkin_count": 3, "hours": _bin_hours(h10=3)},
        {"key": "2", "checkin_count": 2, "hours": _bin_hours(h10=2)},
        {"key": "4", "checkin_count": 6, "hours": _bin_hours(h9=3, h13=3)},
        {"key": "10", "checkin_count": 2, "hours": _bin_hours(h13=2)},
    ]
    for leaked in ("CANARY", "bin1", "rls_test", "title"):
        assert leaked not in str(body["bins"]), leaked


def test_bin_volume_is_isolated_by_tenant_and_refuses_what_the_other_reports_refuse(bins_api):
    tenant_a = _checked_bins(bins_api, SESSION_A, "tenant-a", "main")
    tenant_b = _checked_bins(bins_api, SESSION_B, "tenant-b", "main")

    # Tenant B logged thirty check-ins in ITS bin 4. None of them is in tenant A's bin 4, and tenant A's
    # bins 0, 2 and 10 are not in tenant B's answer.
    assert {entry["key"]: entry["checkin_count"] for entry in tenant_a["bins"]} == {"0": 3, "2": 2, "4": 6, "10": 2}
    assert {entry["key"]: entry["checkin_count"] for entry in tenant_b["bins"]} == {"4": 30, "99": 5}
    assert (tenant_b["checkin_count"], tenant_b["unknown_bin_count"]) == (80, 45)

    crossed = _bins_of(bins_api, SESSION_A, "tenant-b", "main")
    assert (crossed.status_code, crossed.json()) == (404, TENANT_NOT_FOUND)
    assert _bins_of(bins_api, SESSION_B, "tenant-a", "main").status_code == 404
    assert _bins_of(bins_api, "no-such-session", "tenant-a", "main").status_code == 401
    assert _bins_of(bins_api, SESSION_A, "tenant-a", "main", "2026-06-12", "2026-06-08").status_code == 422
    assert _bins_of(bins_api, SESSION_A, "tenant-a", "main", "2026-06-19", "2026-06-21").status_code == 422


def test_bin_volume_does_not_depend_on_the_database_session_time_zone(bins_api, runtime_engine, monkeypatch):
    answers = []
    for zone in ("UTC", "Asia/Tokyo", "America/Los_Angeles", "Asia/Kolkata"):
        shifted = create_engine(runtime_engine.url, hide_parameters=True, connect_args={"options": f"-c timezone={zone}"})
        monkeypatch.setattr(database, "_engine", shifted)
        try:
            answers.append(_checked_bins(bins_api, SESSION_A, "tenant-a", "main"))
        finally:
            shifted.dispose()

    assert all(answer == answers[0] for answer in answers)
    assert (answers[0]["checkin_count"], answers[0]["known_bin_count"]) == (42, 13)
    assert [entry["key"] for entry in answers[0]["bins"]] == ["0", "2", "4", "10"]


def test_bin_volume_for_a_range_with_no_check_ins_is_zeros_and_no_bins(bins_api):
    response = _bins_of(bins_api, SESSION_A, "tenant-a", "main", "2026-05-01", "2026-05-07")

    assert response.status_code == 200
    body = response.json()
    assert (body["checkin_count"], body["known_bin_count"], body["unknown_bin_count"], body["bins"]) == (0, 0, 0, [])
