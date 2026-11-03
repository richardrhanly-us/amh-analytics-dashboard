"""scripts/check_schema_compat.py on a REAL PostgreSQL: real migrations, a real alembic_version, real roles and real locks.

OPT-IN AND SAFE BY CONSTRUCTION, the same convention as tests/test_reconcile_ingest_keys_postgres.py: it runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server it may create and drop databases on.
Throwaway databases and roles are created and dropped. The host must be local unless SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is
also set. Neither DATABASE_URL nor SORTVIEW_SCHEMA_CHECK_DATABASE_URL from the environment is ever used.

Nothing here may wait without a limit: every blocking call runs in a thread joined with a timeout, every subprocess has a
timeout, and the tool's own lock_timeout is what a lock test proves.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from scripts import check_schema_compat as tool

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only
HISTORICAL_PRODUCTION = "f7b3d2c81a46"  # production on 2026-11-02, behind code that needed c1e7a94f2b60
JOIN_SECONDS = 30

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL tests)")


def _alembic(url, *arguments):
    done = subprocess.run(  # nosec B603
        [sys.executable, "-m", "alembic", *arguments], cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
        capture_output=True, text=True, check=False, timeout=300,
    )
    assert done.returncode == 0, done.stderr[-2000:]


@pytest.fixture(scope="module")
def cluster():
    admin = make_url(ADMIN_URL)
    host = admin.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(f"refusing to run against non-local PostgreSQL host {host!r}")
    suffix = secrets.token_hex(4)
    names = {kind: f"sortview_schema_check_{kind}_{suffix}" for kind in ("head", "behind", "empty")}
    roles = {kind: f"sortview_schema_check_{kind}_{suffix}" for kind in ("checker", "noaccess", "writer")}
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT", hide_parameters=True)
    owners = {}
    created_roles = False
    try:
        with admin_engine.connect() as conn:
            for name in names.values():
                conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
            for role in roles.values():
                conn.execute(text(f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT PASSWORD '{ROLE_PASSWORD}'"))  # nosec B608
            created_roles = True
        urls = {kind: admin.set(database=name) for kind, name in names.items()}
        _alembic(urls["head"], "upgrade", "head")
        _alembic(urls["behind"], "upgrade", HISTORICAL_PRODUCTION)
        for kind, url in urls.items():
            owners[kind] = create_engine(url, hide_parameters=True)
            with owners[kind].begin() as conn:
                conn.execute(text(f"REVOKE CONNECT ON DATABASE \"{names[kind]}\" FROM PUBLIC"))  # nosec B608
                for role in roles.values():
                    conn.execute(text(f'GRANT CONNECT ON DATABASE "{names[kind]}" TO {role}'))  # nosec B608
                    conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))  # nosec B608
                if kind != "empty":
                    # The documented role: SELECT on alembic_version and nothing else.
                    conn.execute(text(f"GRANT SELECT ON public.alembic_version TO {roles['checker']}"))  # nosec B608
                    # A role that COULD write, to prove the transaction itself is what stops a write.
                    conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.alembic_version TO {roles['writer']}"))  # nosec B608

        def role_url(kind, role):
            return urls[kind].set(username=roles[role], password=ROLE_PASSWORD).render_as_string(hide_password=False)

        yield {"owners": owners, "urls": urls, "role_url": role_url}
    finally:
        for engine in owners.values():
            engine.dispose()
        with admin_engine.connect() as conn:
            for name in names.values():
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
            if created_roles:
                for role in roles.values():
                    conn.execute(text(f"DROP ROLE IF EXISTS {role}"))  # nosec B608
        admin_engine.dispose()


@pytest.fixture
def head_revision(cluster):
    """The head database's alembic_version, restored after a test that rewrites it."""
    owner = cluster["owners"]["head"]
    with owner.connect() as conn:
        original = [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version")).all()]
    assert original == [tool.single_head(tool.load_code_graph())]
    yield owner
    with owner.begin() as conn:
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": original[0]})


def _check(url):
    return tool.check({tool.URL_VARIABLE: url})


def _status(line):
    return line.split(":", 2)[1].strip()


# --- the decision, on real databases ------------------------------------------------------------------------------------------

def test_the_head_passes(cluster):
    code, line = _check(cluster["role_url"]("head", "checker"))
    head = tool.single_head(tool.load_code_graph())
    assert code == tool.EXIT_OK
    assert line == f"SCHEMA CHECK PASSED: equal: the database is at the code's head | code head {head} | database {head}"


def test_the_historical_production_state_is_refused(cluster):
    code, line = _check(cluster["role_url"]("behind", "checker"))
    assert code == tool.EXIT_REFUSED
    assert _status(line) == "ancestor" and line.endswith(f"| database {HISTORICAL_PRODUCTION}")
    with cluster["owners"]["behind"].connect() as conn:  # really the pre-M1 schema the incident ran against
        assert conn.execute(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'ingest_key_ids' AND column_name = 'installation_id'"
        )).scalar() == 0


def test_a_database_ahead_of_the_code_is_unknown_and_refused(cluster, head_revision):
    with head_revision.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
    code, line = _check(cluster["role_url"]("head", "checker"))
    assert code == tool.EXIT_REFUSED and _status(line) == "unknown" and line.endswith("| database ffffffffffff")


def test_no_revision_row_is_refused(cluster, head_revision):
    with head_revision.begin() as conn:
        conn.execute(text("DELETE FROM alembic_version"))
    code, line = _check(cluster["role_url"]("head", "checker"))
    assert code == tool.EXIT_REFUSED and _status(line) == "no_database_revision"


def test_several_revision_rows_are_refused(cluster, head_revision):
    with head_revision.begin() as conn:
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": HISTORICAL_PRODUCTION})
    code, line = _check(cluster["role_url"]("head", "checker"))
    assert code == tool.EXIT_REFUSED and _status(line) == "multiple_database_revisions"


# --- permissions and connections ----------------------------------------------------------------------------------------------

def test_a_role_that_cannot_read_alembic_version_is_refused(cluster):
    code, line = _check(cluster["role_url"]("head", "noaccess"))
    assert (code, _status(line)) == (tool.EXIT_REFUSED, "metadata_unreadable")
    assert line.endswith("| sqlstate 42501") and ROLE_PASSWORD not in line


def test_a_database_without_alembic_version_is_refused(cluster):
    code, line = _check(cluster["role_url"]("empty", "checker"))
    assert (code, _status(line)) == (tool.EXIT_REFUSED, "metadata_missing")
    assert line.endswith("| sqlstate 42P01")


def test_a_wrong_password_is_a_connection_failure_that_prints_nothing_of_the_url(cluster):
    wrong = make_url(cluster["role_url"]("head", "checker")).set(password="not-the-password-x7").render_as_string(hide_password=False)
    code, line = _check(wrong)
    assert (code, _status(line)) == (tool.EXIT_REFUSED, "connection_failed")
    assert "not-the-password-x7" not in line and make_url(wrong).username not in line


def test_nothing_listening_is_a_connection_failure(cluster):
    closed = make_url(cluster["role_url"]("head", "checker")).set(host="127.0.0.1", port=1).render_as_string(hide_password=False)
    started = time.monotonic()
    code, line = _check(closed)
    assert (code, _status(line)) == (tool.EXIT_REFUSED, "connection_failed")
    assert time.monotonic() - started < JOIN_SECONDS and ROLE_PASSWORD not in line


def test_an_unknown_database_is_a_connection_failure(cluster):
    missing = make_url(cluster["role_url"]("head", "checker")).set(database="sortview_no_such_db").render_as_string(hide_password=False)
    code, line = _check(missing)
    assert (code, _status(line)) == (tool.EXIT_REFUSED, "connection_failed")


# --- read only, timeouts, and no residue -----------------------------------------------------------------------------------

def test_the_transaction_is_read_only_even_for_a_role_that_could_write(cluster):
    engine = create_engine(cluster["role_url"]("head", "writer"), hide_parameters=True)
    try:
        with engine.connect() as conn:
            tool.open_read_only(conn)
            assert conn.execute(text("SHOW statement_timeout")).scalar() == "5s"
            assert conn.execute(text("SHOW lock_timeout")).scalar() == "2s"
            with pytest.raises(DBAPIError) as refused:
                conn.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
            assert tool._sqlstate(refused.value) == "25006"  # read_only_sql_transaction
            conn.rollback()
    finally:
        engine.dispose()


def test_a_check_by_a_writable_role_changes_nothing_and_leaves_no_session(cluster):
    owner = cluster["owners"]["head"]
    with owner.connect() as conn:
        before = conn.execute(text("SELECT version_num, xmin::text FROM alembic_version")).all()
    code, _line = _check(cluster["role_url"]("head", "writer"))
    assert code == tool.EXIT_OK
    with owner.connect() as conn:
        assert conn.execute(text("SELECT version_num, xmin::text FROM alembic_version")).all() == before
    deadline = time.monotonic() + 5  # a closed backend leaves pg_stat_activity a moment later
    while True:
        with owner.connect() as conn:
            sessions = conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'sortview_schema_check'"
            )).scalar()
        if sessions == 0 or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert sessions == 0


def test_a_migration_holding_alembic_version_makes_the_check_give_up_instead_of_waiting(cluster):
    owner = cluster["owners"]["head"]
    result: dict[str, object] = {}

    def run():
        result["outcome"] = _check(cluster["role_url"]("head", "checker"))

    with owner.connect() as holder:
        holder.execute(text("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE"))  # what `alembic upgrade` holds
        started = time.monotonic()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=JOIN_SECONDS)
        elapsed = time.monotonic() - started
        holder.rollback()
    assert not thread.is_alive(), "the check waited on the lock instead of timing out"
    code, line = result["outcome"]
    assert (code, _status(line)) == (tool.EXIT_REFUSED, "database_timeout")
    assert line.endswith("| sqlstate 55P03") and elapsed < 15


# --- the command, as the deploy job runs it -----------------------------------------------------------------------------------

def _run(environ):
    env = {key: value for key, value in os.environ.items() if key not in (tool.URL_VARIABLE, "DATABASE_URL")}
    return subprocess.run(  # nosec B603
        [sys.executable, "scripts/check_schema_compat.py"], cwd=ROOT, env={**env, **environ},
        capture_output=True, text=True, check=False, timeout=60,
    )


def test_the_command_reads_only_its_own_variable_and_exits_by_the_decision(cluster):
    head_url = cluster["urls"]["head"].render_as_string(hide_password=False)
    behind = _run({tool.URL_VARIABLE: cluster["role_url"]("behind", "checker"), "DATABASE_URL": head_url})
    assert behind.returncode == tool.EXIT_REFUSED and behind.stdout.startswith("SCHEMA CHECK REFUSED: ancestor:")
    at_head = _run({tool.URL_VARIABLE: cluster["role_url"]("head", "checker")})
    assert at_head.returncode == tool.EXIT_OK and at_head.stdout.startswith("SCHEMA CHECK PASSED: equal:")
    only_app_url = _run({"DATABASE_URL": head_url})
    assert only_app_url.returncode == tool.EXIT_REFUSED and "database_url_required" in only_app_url.stdout
    for done in (behind, at_head, only_app_url):
        assert done.stderr == "" and done.stdout.count("\n") == 1
        assert ROLE_PASSWORD not in done.stdout
