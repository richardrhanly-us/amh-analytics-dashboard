"""Migration 67d06f4ccd24 -- checkins_clean/rejects_clean sync-trigger
security, on a REAL PostgreSQL server.

BACKGROUND. sync_checkins_to_clean()/sync_rejects_to_clean() are AFTER
INSERT triggers on checkins/rejects that copy each new row into
checkins_clean/rejects_clean via INSERT ... ON CONFLICT (id) DO NOTHING.
Before this migration, both functions were SECURITY INVOKER (the default)
-- confirmed empirically, not assumed: a role granted INSERT only on
checkins_clean succeeds on a plain INSERT but fails with "permission
denied" on the identical row via ON CONFLICT (id) DO NOTHING, because
PostgreSQL requires SELECT on the target table for ANY ON CONFLICT clause
(DO NOTHING included) to perform conflict detection. The already-deployed
production sortview_app grant script gave these two tables INSERT only --
insufficient on its own, and NOT fixed by adding SELECT, since these two
tables are outside the RLS tranche (0acba192bf69) and carry the same
patron-checkout-adjacent columns (title, barcode, message, source_file) as
checkins/rejects -- a bare SELECT grant would be a genuine, unprotected
cross-tenant read path, not a redundant convenience.

THE FIX (this migration): both trigger functions become SECURITY DEFINER
with a pinned search_path, and sortview_app's INSERT grant on both tables
is explicitly REVOKEd. The desired end state, proven below: sortview_app
has NO privilege of any kind -- not SELECT, not INSERT -- on
checkins_clean or rejects_clean, and the trigger still works because it
now executes with the function OWNER's privileges, not the calling role's.

Every test here that verifies a clean-table row was actually created by
the trigger does so through the OWNER connection, never the runtime role
-- the runtime role intentionally cannot SELECT these tables after this
migration, so using it to verify would either fail (defeating the test) or
require weakening the very privilege state this migration establishes.

OPT-IN AND SAFE BY CONSTRUCTION -- same convention as
tests/test_ingest_v2_postgres.py and tests/test_rls_phase1_postgres.py:
runs only when SORTVIEW_TEST_POSTGRES_URL points at a maintenance database
on a NON-PRODUCTION, local server. Each test creates its own throwaway
database and (where needed) its own throwaway runtime role, and drops both
afterward. Production is never touched.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL trigger-security tests)"
)

CUSTOMER, BRANCH = 301, 31
BEFORE_REVISION = "e5a2c7b93d14"   # immediately before this migration
FIX_REVISION = "67d06f4ccd24"      # this migration
FUNCTIONS = ("sync_checkins_to_clean", "sync_rejects_to_clean")
CLEAN_TABLES = ("checkins_clean", "rejects_clean")


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
    """A brand-new database, migrated to a given revision, dropped on exit."""

    def __init__(self, revision: str | None):
        self.revision = revision
        admin = make_url(ADMIN_URL)
        _guard(admin)
        self.admin = admin
        self.name = f"sortview_trigsec_test_{secrets.token_hex(4)}"
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


def _seed_tenant(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO customers (id, name) VALUES (:c, 'trigsec-tenant')"), {"c": CUSTOMER})
        conn.execute(text(
            "INSERT INTO organizations (id, slug, name, status, operational_customer_id) "
            "VALUES (1, 'trigsec', 'trigsec', 'active', :c)"
        ), {"c": CUSTOMER})
        conn.execute(text(
            "INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) "
            "VALUES (:b, 1, 'main', 'Main', 'active', :b)"
        ), {"b": BRANCH})


def _function_state(engine) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT p.proname, p.prosecdef, p.proconfig
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = ANY(:names)
        """), {"names": list(FUNCTIONS)}).fetchall()
        return {r[0]: {"security_definer": r[1], "proconfig": r[2]} for r in rows}


def _grants_on(engine, table: str, role: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT a.privilege_type
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL aclexplode(c.relacl) AS a
            WHERE n.nspname = 'public' AND c.relname = :table AND a.grantee = CAST(:role AS regrole)
        """), {"table": table, "role": role}).fetchall()
        return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Runtime role fixture: migrated to the FIX revision (head-equivalent for
# this concern), zero grants on the two clean tables -- the desired end
# state -- but the ordinary grants needed for a real checkins/rejects
# INSERT to succeed.
# ---------------------------------------------------------------------------

RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)


def _create_runtime_role(owner_engine, role_name: str) -> None:
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
        conn.execute(text(f"GRANT SELECT, INSERT ON TABLE public.checkins TO {role_name}"))
        conn.execute(text(f"GRANT SELECT, INSERT ON TABLE public.rejects TO {role_name}"))
        conn.execute(text(f"GRANT USAGE ON SEQUENCE public.checkins_id_seq TO {role_name}"))
        conn.execute(text(f"GRANT USAGE ON SEQUENCE public.rejects_id_seq TO {role_name}"))
        # Deliberately NO grant on checkins_clean/rejects_clean -- the
        # property under test.


def _drop_runtime_role(owner_engine, role_name: str) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text(f"DROP OWNED BY {role_name}"))  # nosec B608
        conn.execute(text(f"DROP ROLE IF EXISTS {role_name}"))  # nosec B608


@pytest.fixture(scope="module")
def cluster():
    with Throwaway(FIX_REVISION) as db:
        owner_engine = create_engine(db.url, hide_parameters=True)
        _seed_tenant(owner_engine)
        role_name = f"sortview_trigsec_role_{secrets.token_hex(4)}"
        _create_runtime_role(owner_engine, role_name)
        try:
            runtime_url = db.url.set(username=role_name, password=RUNTIME_ROLE_PASSWORD)
            runtime_engine = create_engine(runtime_url, hide_parameters=True)
            try:
                yield owner_engine, runtime_engine, role_name
            finally:
                runtime_engine.dispose()
        finally:
            _drop_runtime_role(owner_engine, role_name)
            owner_engine.dispose()


@pytest.fixture
def owner_engine(cluster):
    owner_engine, _runtime_engine, _role = cluster
    return owner_engine


@pytest.fixture
def runtime_engine(cluster):
    _owner_engine, runtime_engine, _role = cluster
    return runtime_engine


def _insert_checkin(conn, barcode="TRIGSEC-BC"):
    conn.execute(text("""
        INSERT INTO checkins (customer_id, branch_id, event_time, title, barcode, destination, bin, source_file)
        VALUES (:c, :b, now(), 'title', :barcode, 'Main', 'bin1', 'trigsec_test.csv')
    """), {"c": CUSTOMER, "b": BRANCH, "barcode": barcode})


def _insert_reject(conn, barcode="TRIGSEC-RJ"):
    conn.execute(text("""
        INSERT INTO rejects (customer_id, branch_id, event_time, barcode, error_message, source_file)
        VALUES (:c, :b, now(), :barcode, 'jam', 'trigsec_test.csv')
    """), {"c": CUSTOMER, "b": BRANCH, "barcode": barcode})


# --- 1-2: direct SELECT denied -------------------------------------------

def test_direct_select_from_checkins_clean_is_denied(runtime_engine):
    with runtime_engine.connect() as conn, pytest.raises(Exception, match="permission denied"):
        conn.execute(text("SELECT * FROM checkins_clean"))


def test_direct_select_from_rejects_clean_is_denied(runtime_engine):
    with runtime_engine.connect() as conn, pytest.raises(Exception, match="permission denied"):
        conn.execute(text("SELECT * FROM rejects_clean"))


# --- 3-4: direct INSERT denied --------------------------------------------

def test_direct_insert_into_checkins_clean_is_denied(runtime_engine):
    with runtime_engine.connect() as conn, pytest.raises(Exception, match="permission denied"):
        conn.execute(text("INSERT INTO checkins_clean (id, customer_id, branch_id) VALUES (999001, :c, :b)"),
                     {"c": CUSTOMER, "b": BRANCH})


def test_direct_insert_into_rejects_clean_is_denied(runtime_engine):
    with runtime_engine.connect() as conn, pytest.raises(Exception, match="permission denied"):
        conn.execute(text("INSERT INTO rejects_clean (id, customer_id, branch_id) VALUES (999002, :c, :b)"),
                     {"c": CUSTOMER, "b": BRANCH})


# --- 5-6: the trigger still works despite zero grants ---------------------

def test_checkins_insert_succeeds_and_trigger_creates_the_clean_row(owner_engine, runtime_engine):
    # INSERT as the runtime role -- it has no privilege on checkins_clean
    # at all, so this only works if the trigger executes as the function
    # OWNER (SECURITY DEFINER), not as the calling role.
    with runtime_engine.begin() as conn:
        _insert_checkin(conn, "TRIGSEC-CHECKIN-OK")

    # Verified via the OWNER connection -- the runtime role intentionally
    # cannot SELECT checkins_clean, so using it here would defeat the point.
    with owner_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT customer_id, branch_id FROM checkins_clean WHERE barcode = 'TRIGSEC-CHECKIN-OK'"
        )).first()
    assert row == (CUSTOMER, BRANCH)


def test_rejects_insert_succeeds_and_trigger_creates_the_clean_row(owner_engine, runtime_engine):
    with runtime_engine.begin() as conn:
        _insert_reject(conn, "TRIGSEC-REJECT-OK")

    with owner_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT customer_id, branch_id FROM rejects_clean WHERE barcode = 'TRIGSEC-REJECT-OK'"
        )).first()
    assert row == (CUSTOMER, BRANCH)


# --- 7: ON CONFLICT (id) DO NOTHING still works ---------------------------

def test_on_conflict_do_nothing_still_suppresses_a_duplicate_id(owner_engine, runtime_engine):
    # checkins.id is a real BIGSERIAL primary key, so two checkins rows can
    # never naturally collide on id -- the trigger's own ON CONFLICT (id)
    # DO NOTHING on checkins_clean is a defensive no-op in ordinary
    # operation, not something a normal duplicate upload would exercise
    # (that's caught one level up, by checkins' own unique event index).
    # To prove the ON CONFLICT clause itself still behaves correctly after
    # this migration's ALTER FUNCTION (which changes only the function's
    # security/search_path attributes, never its body), exercise the exact
    # statement shape directly against a manufactured duplicate id, as the
    # owner -- the same privilege context the trigger now runs under.
    with owner_engine.begin() as conn:
        _insert_checkin(conn, "TRIGSEC-CONFLICT-SOURCE")
        real_id = conn.execute(text(
            "SELECT id FROM checkins WHERE barcode = 'TRIGSEC-CONFLICT-SOURCE'"
        )).scalar()
        clean_id = conn.execute(text(
            "SELECT id FROM checkins_clean WHERE barcode = 'TRIGSEC-CONFLICT-SOURCE'"
        )).scalar()
    assert clean_id == real_id  # the trigger copied checkins.id verbatim, as designed

    with owner_engine.begin() as conn:
        # Re-running the identical statement with the SAME id must be a
        # silent no-op, not a duplicate-key error.
        conn.execute(text("""
            INSERT INTO checkins_clean (id, customer_id, branch_id, event_time, barcode, source_file, created_at, ingested_at)
            VALUES (:id, :c, :b, now(), 'TRIGSEC-CONFLICT-SOURCE', 'trigsec_test.csv', now(), now())
            ON CONFLICT (id) DO NOTHING
        """), {"id": real_id, "c": CUSTOMER, "b": BRANCH})

    with owner_engine.connect() as conn:
        count = conn.execute(text(
            "SELECT COUNT(*) FROM checkins_clean WHERE id = :id"
        ), {"id": real_id}).scalar()
    assert count == 1  # still exactly one row -- the "conflict" was suppressed, not duplicated or errored


# --- 8-9: function attributes after upgrade --------------------------------

def test_trigger_functions_are_security_definer_after_upgrade(owner_engine):
    state = _function_state(owner_engine)
    assert state["sync_checkins_to_clean"]["security_definer"] is True
    assert state["sync_rejects_to_clean"]["security_definer"] is True


def test_trigger_functions_have_the_pinned_search_path_after_upgrade(owner_engine):
    state = _function_state(owner_engine)
    assert state["sync_checkins_to_clean"]["proconfig"] == ["search_path=public, pg_temp"]
    assert state["sync_rejects_to_clean"]["proconfig"] == ["search_path=public, pg_temp"]


# --- 10-11: downgrade restores the exact prior state, re-upgrade succeeds --

def test_downgrade_restores_prior_function_state_and_grants_then_reupgrades():
    with Throwaway(BEFORE_REVISION) as db:
        owner_engine = create_engine(db.url, hide_parameters=True)

        # Empirically capture the true pre-migration state (not assumed).
        before_state = _function_state(owner_engine)
        assert before_state["sync_checkins_to_clean"]["security_definer"] is False
        assert before_state["sync_checkins_to_clean"]["proconfig"] is None
        assert before_state["sync_rejects_to_clean"]["security_definer"] is False
        assert before_state["sync_rejects_to_clean"]["proconfig"] is None

        # Replicate the known real production grant this migration must
        # REVOKE: sortview_app (here, a same-named role, since the
        # migration's REVOKE/GRANT target the literal name) has INSERT
        # only on both clean tables prior to the fix. The role is
        # cluster-level (not per-database), so it must be created and torn
        # down inside the same try/finally, or a failure between the two
        # leaks a stale "sortview_app" role that breaks every subsequent run
        # against this cluster.
        role_name = "sortview_app"
        with owner_engine.begin() as conn:
            # Defensive: clear out any stale role a prior failed run left
            # behind (this role is cluster-level, not per-database).
            if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": role_name}).first():
                conn.execute(text(f"DROP OWNED BY {role_name}"))  # nosec B608
                conn.execute(text(f"DROP ROLE IF EXISTS {role_name}"))  # nosec B608
        try:
            with owner_engine.begin() as conn:
                conn.execute(text(f"""
                    CREATE ROLE {role_name}
                        LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT
                        PASSWORD '{RUNTIME_ROLE_PASSWORD}'
                """))  # nosec B608
                for table in CLEAN_TABLES:
                    conn.execute(text(f"GRANT INSERT ON TABLE public.{table} TO {role_name}"))

            before_grants = {t: _grants_on(owner_engine, t, role_name) for t in CLEAN_TABLES}
            assert before_grants["checkins_clean"] == {"INSERT"}
            assert before_grants["rejects_clean"] == {"INSERT"}

            up = _alembic(db.url, "upgrade", FIX_REVISION)
            assert up.returncode == 0, up.stderr[-2000:]

            after_state = _function_state(owner_engine)
            assert after_state["sync_checkins_to_clean"]["security_definer"] is True
            assert after_state["sync_rejects_to_clean"]["security_definer"] is True
            after_grants = {t: _grants_on(owner_engine, t, role_name) for t in CLEAN_TABLES}
            assert after_grants["checkins_clean"] == set()  # REVOKEd
            assert after_grants["rejects_clean"] == set()   # REVOKEd

            # --- 10: downgrade restores the exact prior state -----------
            down = _alembic(db.url, "downgrade", BEFORE_REVISION)
            assert down.returncode == 0, down.stderr[-2000:]

            restored_state = _function_state(owner_engine)
            assert restored_state == before_state  # byte-for-byte the same dict
            restored_grants = {t: _grants_on(owner_engine, t, role_name) for t in CLEAN_TABLES}
            assert restored_grants == before_grants  # INSERT grant is back on both

            # --- 11: re-upgrade succeeds ---------------------------------
            reup = _alembic(db.url, "upgrade", FIX_REVISION)
            assert reup.returncode == 0, reup.stderr[-2000:]
            final_state = _function_state(owner_engine)
            assert final_state["sync_checkins_to_clean"]["security_definer"] is True
            assert final_state["sync_rejects_to_clean"]["security_definer"] is True
        finally:
            with owner_engine.begin() as conn:
                conn.execute(text(f"DROP OWNED BY {role_name}"))  # nosec B608
                conn.execute(text(f"DROP ROLE IF EXISTS {role_name}"))  # nosec B608
            owner_engine.dispose()
