"""Migration 16b41d730e15 (server-stamped report times on pipeline_status), against a REAL PostgreSQL.

The offline tests (tests/test_pipeline_status_report_stamps_migration.py) read the SQL the migration renders. These run
it: on a database migrated to the revision just before it and holding pipeline_status rows, that the upgrade adds
exactly two TIMESTAMP WITH TIME ZONE columns, NULLable and with no default; that every existing row survives with both
of them NULL and every other value untouched; that the primary key, the indexes, the absence of row level security and
the table-level privileges of a runtime-style role are what they were; and that the downgrade takes away the two
columns and nothing else.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_ingest_v2_postgres.py. They run only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server the tests may create and drop
databases on, e.g.

    SORTVIEW_TEST_POSTGRES_URL=postgresql://postgres:@127.0.0.1:5432/postgres

Each test database is brand new, migrated with the project's real Alembic chain (in a subprocess) and dropped
afterwards. The host must be local (localhost / 127.0.0.1 / ::1) unless SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also
set, so a production URL left in an environment variable cannot be used.

The upgrade and downgrade targets are the two named revisions, never "head": a later migration appended above this
one does not change what these tests exercise.

Every value is SYNTHETIC.
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
REVISION = "16b41d730e15"
PREVIOUS_HEAD = "c8d5f2a47e91"
COLUMNS = ("status_reported_at", "health_status_reported_at")

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL migration tests)"
)

# Three rows as the three kinds of writer leave them: a scheduled run's result, a continuous agent's heartbeat on top of
# an older run, and a row that has only ever been inserted. The naive timestamps are exactly the kind this migration
# refuses to turn into instants.
_SEED_ROWS = (
    (
        "INSERT INTO pipeline_status (customer_id, branch_id, last_attempt, last_run, status, checkins_rows, rejects_rows, "
        "uploaded_checkins_rows, destination_breakdown, updated_at) VALUES (10, 1, '2026-06-01 12:00:00', "
        "'2026-06-01 12:00:07', 'completed', 41, 3, 41, CAST('{\"Main\": 40, \"WESTSIDE\": 1}' AS jsonb), '2026-06-01 12:00:08')"
    ),
    (
        "INSERT INTO pipeline_status (customer_id, branch_id, last_attempt, last_run, status, health_status, "
        "pending_outbox_count, quarantined_count, last_success_at, watcher_last_active_at, last_failure_category, last_error, "
        "updated_at) VALUES (20, 2, '2026-05-30 07:15:00', '2026-05-30 07:15:04', 'failed_upload', 'degraded', 4, 1, "
        "'2026-06-01 17:59:00', '2026-06-01 18:00:00', 'retryable_infra', 'synthetic error text', '2026-06-01 18:00:01')"
    ),
    "INSERT INTO pipeline_status (customer_id, branch_id) VALUES (20, 3)",
)


# --- throwaway databases ----------------------------------------------------------------------------------------------

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
    """A brand-new database on the test server, migrated to `revision` and dropped when the context exits."""

    def __init__(self, revision: str):
        self.revision = revision
        admin = make_url(ADMIN_URL)
        _guard(admin)
        self.admin = admin
        self.name = f"sortview_stamps_test_{secrets.token_hex(4)}"
        self.admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")

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


def rows(engine, sql, **params):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params)]


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _seed(engine) -> None:
    with engine.begin() as conn:
        for statement in _SEED_ROWS:
            conn.execute(text(statement))


def _column_names(engine) -> list[str]:
    return [r[0] for r in rows(engine, "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
                                       "AND table_name = 'pipeline_status' ORDER BY ordinal_position")]


def _snapshot(engine) -> dict:
    """Everything about pipeline_status the migration must leave alone, and the one thing it adds to (the columns)."""
    return {
        "columns": rows(engine, "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                                "WHERE table_schema = 'public' AND table_name = 'pipeline_status' ORDER BY ordinal_position"),
        "indexes": rows(engine, "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
                                "AND tablename = 'pipeline_status' ORDER BY indexname"),
        "constraints": rows(engine, "SELECT conname, contype::text, pg_get_constraintdef(oid) FROM pg_constraint "
                                    "WHERE conrelid = 'public.pipeline_status'::regclass ORDER BY conname"),
        "triggers": rows(engine, "SELECT tgname FROM pg_trigger WHERE tgrelid = 'public.pipeline_status'::regclass "
                                 "AND NOT tgisinternal ORDER BY tgname"),
        "row_security": rows(engine, "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                                     "WHERE oid = 'public.pipeline_status'::regclass"),
        "policies": rows(engine, "SELECT policyname, cmd FROM pg_policies WHERE schemaname = 'public' "
                                 "AND tablename = 'pipeline_status' ORDER BY policyname"),
        "table_grants": rows(engine, "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                                     "WHERE table_schema = 'public' AND table_name = 'pipeline_status' ORDER BY 1, 2"),
        "column_grants": rows(engine, "SELECT attname FROM pg_attribute WHERE attrelid = 'public.pipeline_status'::regclass "
                                      "AND attnum > 0 AND NOT attisdropped AND attacl IS NOT NULL ORDER BY attname"),
    }


def _data(engine, columns: list[str]) -> list[tuple]:
    return rows(engine, f"SELECT {', '.join(columns)} FROM pipeline_status ORDER BY customer_id, branch_id")  # nosec B608


@pytest.fixture(scope="module")
def upgraded():
    """A database at the PREVIOUS head, seeded, snapshotted, then upgraded to this migration's revision -- and no
    further. Yields the engine with what was true before the upgrade."""
    with Throwaway(PREVIOUS_HEAD) as db:
        engine = create_engine(db.url)
        try:
            _seed(engine)
            before = {"snapshot": _snapshot(engine), "columns": _column_names(engine)}
            before["data"] = _data(engine, before["columns"])
            assert scalar(engine, "SELECT version_num FROM alembic_version") == PREVIOUS_HEAD

            up = _alembic(db.url, "upgrade", REVISION)
            assert up.returncode == 0, up.stderr[-2000:]

            yield engine, before, db.url
        finally:
            engine.dispose()


# =====================================================================================================================
# The upgrade
# =====================================================================================================================

def test_the_upgrade_from_the_previous_head_succeeds_and_lands_exactly_on_this_revision(upgraded):
    engine, before, _ = upgraded

    assert scalar(engine, "SELECT version_num FROM alembic_version") == REVISION
    assert not set(COLUMNS) & set(before["columns"])            # they really were absent at the previous head
    assert set(COLUMNS) <= set(_column_names(engine))


@pytest.mark.parametrize("column", COLUMNS)
def test_each_new_column_is_a_nullable_timestamp_with_time_zone_and_has_no_default(upgraded, column):
    engine, _, _ = upgraded

    (described,) = rows(engine, "SELECT data_type, udt_name, is_nullable, column_default, is_generated, is_identity "
                                "FROM information_schema.columns WHERE table_schema = 'public' "
                                "AND table_name = 'pipeline_status' AND column_name = :c", c=column)

    assert described == ("timestamp with time zone", "timestamptz", "YES", None, "NEVER", "NO")
    # The catalog agrees: no NOT NULL, no default expression, and the type really is timestamptz.
    assert rows(engine, "SELECT a.attnotnull, a.atthasdef, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                        "WHERE a.attrelid = 'public.pipeline_status'::regclass AND a.attname = :c", c=column) == [
        (False, False, "timestamp with time zone"),
    ]


def test_the_upgrade_adds_exactly_those_two_columns_after_the_existing_ones(upgraded):
    engine, before, _ = upgraded

    assert _column_names(engine) == [*before["columns"], *COLUMNS]
    # Every pre-existing column is described exactly as it was: name, type, nullability and default.
    assert _snapshot(engine)["columns"][:len(before["columns"])] == before["snapshot"]["columns"]


def test_the_naive_timestamp_columns_are_not_changed(upgraded):
    engine, _, _ = upgraded

    described = dict(rows(engine, "SELECT column_name, data_type FROM information_schema.columns "
                                  "WHERE table_schema = 'public' AND table_name = 'pipeline_status' "
                                  "AND column_name IN ('last_attempt', 'last_run', 'updated_at', 'last_success_at', "
                                  "'watcher_last_active_at', 'oldest_pending_event_at')"))

    assert set(described.values()) == {"timestamp without time zone"} and len(described) == 6
    assert scalar(engine, "SELECT column_default FROM information_schema.columns WHERE table_schema = 'public' "
                          "AND table_name = 'pipeline_status' AND column_name = 'updated_at'") == "CURRENT_TIMESTAMP"


def test_every_existing_row_survives_with_both_new_columns_null(upgraded):
    engine, before, _ = upgraded

    assert len(before["data"]) == 3
    assert _data(engine, before["columns"]) == before["data"]   # every pre-existing value, of every row, is identical
    assert rows(engine, "SELECT customer_id, branch_id, status_reported_at, health_status_reported_at "
                        "FROM pipeline_status ORDER BY customer_id, branch_id") == [
        (10, 1, None, None), (20, 2, None, None), (20, 3, None, None),
    ]


def test_no_instant_was_manufactured_from_the_existing_timestamps(upgraded):
    engine, _, _ = upgraded

    # Rows that HAVE a last_attempt, a last_run and an updated_at still have no stamp: nothing was inferred from them.
    assert rows(engine, "SELECT COUNT(*) FROM pipeline_status WHERE last_run IS NOT NULL AND updated_at IS NOT NULL "
                        "AND (status_reported_at IS NOT NULL OR health_status_reported_at IS NOT NULL)") == [(0,)]
    assert rows(engine, "SELECT COUNT(*) FROM pipeline_status WHERE last_run IS NOT NULL AND updated_at IS NOT NULL") == [(2,)]
    assert scalar(engine, "SELECT COUNT(*) FROM pipeline_status WHERE status_reported_at IS NOT NULL "
                          "OR health_status_reported_at IS NOT NULL") == 0


def test_the_primary_key_on_customer_and_branch_is_intact(upgraded):
    engine, before, _ = upgraded

    primary_keys = [c for c in _snapshot(engine)["constraints"] if c[1] == "p"]
    assert [definition for _, _, definition in primary_keys] == ["PRIMARY KEY (customer_id, branch_id)"]
    assert _snapshot(engine)["constraints"] == before["snapshot"]["constraints"]   # and no constraint was added

    # It still does its job: a second row for the same branch is refused.
    with pytest.raises(Exception, match="duplicate key"), engine.begin() as conn:
        conn.execute(text("INSERT INTO pipeline_status (customer_id, branch_id) VALUES (10, 1)"))


def test_no_index_or_trigger_was_added(upgraded):
    engine, before, _ = upgraded
    after = _snapshot(engine)

    assert after["indexes"] == before["snapshot"]["indexes"]
    assert len(after["indexes"]) == 1 and "(customer_id, branch_id)" in after["indexes"][0][1]   # the primary key's own
    assert after["triggers"] == before["snapshot"]["triggers"]


def test_row_level_security_on_pipeline_status_is_unchanged(upgraded):
    engine, before, _ = upgraded
    after = _snapshot(engine)

    assert after["row_security"] == before["snapshot"]["row_security"] == [(False, False)]   # still outside RLS
    assert after["policies"] == before["snapshot"]["policies"] == []
    # ...and the tables that ARE under row level security are the same set as before, pipeline_status not among them.
    assert {r[0] for r in rows(engine, "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                       "WHERE n.nspname = 'public' AND relrowsecurity")} == {
        "checkins", "rejects", "acs_events", "checkin_events", "reject_events", "acs_item_events", "ingest_key_ids",
    }


def test_the_upgrade_grants_nothing_at_table_or_column_level(upgraded):
    engine, before, _ = upgraded
    after = _snapshot(engine)

    assert after["table_grants"] == before["snapshot"]["table_grants"]
    assert after["column_grants"] == before["snapshot"]["column_grants"] == []
    assert rows(engine, "SELECT grantee, column_name, privilege_type FROM information_schema.column_privileges "
                        "WHERE table_schema = 'public' AND table_name = 'pipeline_status' AND grantee = 'PUBLIC'") == []


def test_a_row_inserted_after_the_upgrade_without_naming_the_columns_gets_no_stamp(upgraded):
    engine, _, _ = upgraded

    # What the previous API version does: it names its own columns only. No default fills the new ones in.
    with engine.connect() as conn, conn.begin() as tx:
        conn.execute(text("INSERT INTO pipeline_status (customer_id, branch_id, status, updated_at) "
                          "VALUES (30, 9, 'completed', CURRENT_TIMESTAMP)"))
        conn.execute(text("UPDATE pipeline_status SET status = 'completed_no_new_rows', updated_at = CURRENT_TIMESTAMP "
                          "WHERE customer_id = 30 AND branch_id = 9"))
        assert conn.execute(text("SELECT status, status_reported_at, health_status_reported_at FROM pipeline_status "
                                 "WHERE customer_id = 30 AND branch_id = 9")).one() == ("completed_no_new_rows", None, None)
        tx.rollback()


@pytest.mark.parametrize("column", COLUMNS)
def test_a_stored_value_is_an_instant_whatever_the_session_time_zone(upgraded, column):
    engine, _, _ = upgraded

    with engine.connect() as conn, conn.begin() as tx:
        conn.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))
        conn.execute(text(f"UPDATE pipeline_status SET {column} = TIMESTAMPTZ '2026-10-05 18:45:03+00' "  # nosec B608
                          "WHERE customer_id = 10 AND branch_id = 1"))
        seen = {}
        for zone in ("Asia/Tokyo", "America/Chicago", "UTC"):
            conn.execute(text(f"SET LOCAL TIME ZONE '{zone}'"))  # nosec B608 - a fixed list
            seen[zone] = conn.execute(text(
                f"SELECT EXTRACT(EPOCH FROM {column})::bigint, {column} = TIMESTAMPTZ '2026-10-05T18:45:03Z', "  # nosec B608
                f"({column} AT TIME ZONE 'UTC')::text FROM pipeline_status WHERE customer_id = 10 AND branch_id = 1"
            )).one()
        tx.rollback()

    # One instant, however the session that wrote it or the session that reads it is configured.
    assert set(seen.values()) == {(1791225903, True, "2026-10-05 18:45:03")}


# =====================================================================================================================
# The runtime role: table-level privileges cover the new columns
# =====================================================================================================================

def test_a_role_with_only_the_baseline_table_privileges_can_read_and_write_the_new_columns():
    role = f"sortview_stamps_role_{secrets.token_hex(4)}"
    with Throwaway(PREVIOUS_HEAD) as db:
        engine = create_engine(db.url)
        try:
            _seed(engine)
            with engine.begin() as conn:
                # Granted BEFORE the migration, per table, exactly as scripts/runtime_role_privileges.py records for
                # the runtime role. NOLOGIN: the role is only ever assumed with SET ROLE, so it needs no password.
                conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS'))  # nosec B608 - generated name
                conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))  # nosec B608
                conn.execute(text(f'GRANT SELECT, INSERT, UPDATE ON TABLE public.pipeline_status TO "{role}"'))  # nosec B608

            up = _alembic(db.url, "upgrade", REVISION)
            assert up.returncode == 0, up.stderr[-2000:]

            for column in COLUMNS:
                for privilege in ("SELECT", "INSERT", "UPDATE"):
                    assert scalar(engine, "SELECT has_column_privilege(:r, 'public.pipeline_status', :c, :p)",
                                  r=role, c=column, p=privilege) is True, (column, privilege)
            for privilege, expected in (("SELECT", True), ("INSERT", True), ("UPDATE", True), ("DELETE", False),
                                        ("TRUNCATE", False)):
                assert scalar(engine, "SELECT has_table_privilege(:r, 'public.pipeline_status', :p)",
                              r=role, p=privilege) is expected, privilege
            # Those come from the TABLE grant alone: no column carries a grant of its own.
            assert _snapshot(engine)["column_grants"] == []

            # And in practice, as that role: it stamps an existing row, inserts a stamped one and reads both back.
            with engine.connect() as conn, conn.begin() as tx:
                conn.execute(text(f'SET LOCAL ROLE "{role}"'))  # nosec B608
                assert conn.execute(text("SELECT current_user")).scalar() == role
                conn.execute(text("UPDATE pipeline_status SET status_reported_at = CURRENT_TIMESTAMP "
                                  "WHERE customer_id = 10 AND branch_id = 1"))
                conn.execute(text("INSERT INTO pipeline_status (customer_id, branch_id, health_status, "
                                  "health_status_reported_at) VALUES (30, 9, 'healthy', CURRENT_TIMESTAMP)"))
                stamped = conn.execute(text(
                    "SELECT customer_id, status_reported_at IS NOT NULL, health_status_reported_at IS NOT NULL "
                    "FROM pipeline_status WHERE (customer_id, branch_id) IN ((10, 1), (30, 9)) ORDER BY customer_id"
                )).all()
                assert [tuple(r) for r in stamped] == [(10, True, False), (30, False, True)]
                with pytest.raises(Exception, match="permission denied"):
                    conn.execute(text("DELETE FROM pipeline_status"))
                tx.rollback()
        finally:
            with engine.begin() as conn:     # roles are cluster-wide: never left behind
                if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first() is not None:
                    conn.execute(text(f'DROP OWNED BY "{role}"'))  # nosec B608 - its grants in this database
                    conn.execute(text(f'DROP ROLE "{role}"'))  # nosec B608
            engine.dispose()


# =====================================================================================================================
# The downgrade
# =====================================================================================================================

def test_the_downgrade_removes_only_the_two_columns_and_leaves_everything_else_as_it_was():
    with Throwaway(PREVIOUS_HEAD) as db:
        engine = create_engine(db.url)
        try:
            _seed(engine)
            before = _snapshot(engine)
            columns_before = _column_names(engine)
            data_before = _data(engine, columns_before)

            up = _alembic(db.url, "upgrade", REVISION)
            assert up.returncode == 0, up.stderr[-2000:]
            with engine.begin() as conn:     # values to lose: the downgrade is allowed to discard them, and only them
                conn.execute(text("UPDATE pipeline_status SET status_reported_at = now(), health_status_reported_at = now()"))

            down = _alembic(db.url, "downgrade", PREVIOUS_HEAD)
            assert down.returncode == 0, down.stderr[-2000:]

            assert scalar(engine, "SELECT version_num FROM alembic_version") == PREVIOUS_HEAD
            assert _column_names(engine) == columns_before          # both gone; every other column still there, in order
            assert not set(COLUMNS) & set(_column_names(engine))
            assert _snapshot(engine) == before                      # types, defaults, key, indexes, RLS, grants: identical
            assert _data(engine, columns_before) == data_before     # and not one value of one row changed
        finally:
            engine.dispose()


def test_upgrade_downgrade_upgrade_is_clean_and_never_resurrects_a_stamp():
    with Throwaway(PREVIOUS_HEAD) as db:
        engine = create_engine(db.url)
        try:
            _seed(engine)
            columns_before = _column_names(engine)
            data_before = _data(engine, columns_before)

            first = _alembic(db.url, "upgrade", REVISION)
            assert first.returncode == 0, first.stderr[-2000:]
            after_first = _snapshot(engine)
            with engine.begin() as conn:
                conn.execute(text("UPDATE pipeline_status SET status_reported_at = TIMESTAMPTZ '2026-10-05 18:45:03+00', "
                                  "health_status_reported_at = TIMESTAMPTZ '2026-10-05 18:46:00+00'"))

            down = _alembic(db.url, "downgrade", PREVIOUS_HEAD)
            assert down.returncode == 0, down.stderr[-2000:]
            assert _column_names(engine) == columns_before

            second = _alembic(db.url, "upgrade", REVISION)
            assert second.returncode == 0, second.stderr[-2000:]

            assert scalar(engine, "SELECT version_num FROM alembic_version") == REVISION
            assert _snapshot(engine) == after_first                 # the same schema as after the first upgrade
            assert _column_names(engine) == [*columns_before, *COLUMNS]
            assert _data(engine, columns_before) == data_before     # the rows came through both directions untouched
            assert rows(engine, "SELECT DISTINCT status_reported_at, health_status_reported_at FROM pipeline_status") == [
                (None, None),
            ]   # the old stamps did not come back: a re-added column starts from NULL again
        finally:
            engine.dispose()


def test_the_downgrade_is_safe_to_run_when_the_columns_are_already_gone():
    with Throwaway(REVISION) as db:
        engine = create_engine(db.url)
        try:
            with engine.begin() as conn:     # as if the columns had been removed by hand
                conn.execute(text("ALTER TABLE pipeline_status DROP COLUMN status_reported_at"))
            columns = _column_names(engine)

            down = _alembic(db.url, "downgrade", PREVIOUS_HEAD)

            assert down.returncode == 0, down.stderr[-2000:]
            assert _column_names(engine) == [c for c in columns if c not in COLUMNS]
        finally:
            engine.dispose()


def test_the_whole_chain_still_reaches_one_head_from_an_empty_database():
    with Throwaway("head") as db:
        engine = create_engine(db.url)
        try:
            assert set(COLUMNS) <= set(_column_names(engine))
            assert scalar(engine, "SELECT COUNT(*) FROM alembic_version") == 1
        finally:
            engine.dispose()
