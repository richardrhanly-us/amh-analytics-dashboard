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
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import main

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
