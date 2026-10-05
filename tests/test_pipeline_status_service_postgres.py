"""Block 9d on a REAL PostgreSQL: the pipeline status of one resolved tenant.

tests/test_pipeline_status_service.py runs the service's real statements on SQLite. What only a real server can
show is here, with every read made as a NON-OWNING, NOBYPASSRLS runtime role (the kind production runs as):

  * pipeline_status has no row level security -- a deliberately unscoped SELECT on a tenant-scoped connection sees
    EVERY tenant's row -- so the legacy statement's own customer_id AND branch_id filter is the only thing scoping
    it, and it is enough: each tenant gets its own row, including where two customers share a branch id;
  * ingest_key_ids IS under row level security, and the current statement is scoped by both;
  * a TIMESTAMPTZ comes back in the SESSION's offset, which differs per session time zone, and is returned as the
    same aware UTC instant every time;
  * a pooled connection reused across tenants never carries one tenant's answer to the next;
  * the stamps the service reads are the ones the real endpoint writes (Block 9b), end to end.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_rls_phase1_postgres.py. They run only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server the tests may create and drop
databases and roles on, e.g.

    SORTVIEW_TEST_POSTGRES_URL=postgresql://postgres:@127.0.0.1:5432/postgres

The host must be local (localhost / 127.0.0.1 / ::1) unless SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also set, so a
production URL left in an environment variable cannot be used. The database and the role are created for this module
and dropped afterwards.

Every value is SYNTHETIC.
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

import main
from services.pipeline_status_service import PipelineStatus, get_pipeline_status
from services.tenant_resolution_service import ResolvedOperationalTenant
from tenant_db import tenant_connection

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL tests)"
)

_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
_BUILTIN_SHA256_EXPR = "encode(sha256(convert_to(:token, 'UTF8')), 'hex')"  # pgcrypto is not on every throwaway server
RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only

# Operational (customer_id, branch_id) pairs. A and B share branch id 1 under different customers -- possible in
# pipeline_status, which has no foreign key -- and A also has a second branch.
CUSTOMER_A, CUSTOMER_B = 10, 11
A_MAIN, A_NORTH, B_SAME_BRANCH_ID, B_MAIN = (10, 1), (10, 3), (11, 1), (11, 2)
TOKEN_A = "CANARY-PIPELINE-STATUS-TOKEN-A-9201"
NOTHING_REPORTED = PipelineStatus(state="unknown", last_reported_at=None)

client = TestClient(main.app, raise_server_exceptions=False)


def tenant(scope: tuple[int, int]) -> ResolvedOperationalTenant:
    return ResolvedOperationalTenant(org_slug="synthetic", branch_slug="synthetic", access_mode="full",
                                     operational_customer_id=scope[0], operational_branch_id=scope[1])


def key_id(n: int) -> str:
    return f"{n:08x}-4d5a-4b6c-8d7e-9f0a1b2c3d4e"


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


# --- a throwaway database and a throwaway runtime role ----------------------------------------------------------------

def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


def _create_runtime_role(owner_engine, role_name: str) -> None:
    """The production runtime role's attributes, and its table-level grants on what these tests touch (the
    baseline in scripts/runtime_role_privileges.py)."""
    with owner_engine.begin() as conn:
        conn.execute(text(f"""
            CREATE ROLE {role_name}
                LOGIN
                NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT
                PASSWORD '{RUNTIME_ROLE_PASSWORD}'
        """))  # nosec B608 - role_name is generated here, the password is a fresh local secret
        conn.execute(text(f"GRANT CONNECT ON DATABASE {conn.engine.url.database} TO {role_name}"))  # nosec B608
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role_name}"))  # nosec B608
        for grant in (
            "SELECT, INSERT, UPDATE ON TABLE public.pipeline_status",
            "SELECT, INSERT, UPDATE ON TABLE public.ingest_key_ids",
            "SELECT ON TABLE public.v2_cutovers",
            "SELECT, UPDATE ON TABLE public.agent_tokens",
            "SELECT ON TABLE public.organizations, public.branches, public.customers, public.collector_installations",
        ):
            conn.execute(text(f"GRANT {grant} TO {role_name}"))  # nosec B608


@pytest.fixture(scope="module")
def cluster():
    admin = make_url(ADMIN_URL)
    _guard(admin)
    name = f"sortview_pipeline_status_test_{secrets.token_hex(4)}"
    role_name = f"sortview_pipeline_status_role_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    url = admin.set(database=name)
    owner_engine = create_engine(url, hide_parameters=True)
    role_created = False
    try:
        env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
        migrated = subprocess.run(  # nosec B603
            [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, capture_output=True, text=True, check=False,
        )
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        _create_runtime_role(owner_engine, role_name)
        role_created = True
        yield owner_engine, url.set(username=role_name, password=RUNTIME_ROLE_PASSWORD)
    finally:
        if role_created:
            with owner_engine.begin() as conn:     # roles are cluster-wide: never left behind
                conn.execute(text(f"DROP OWNED BY {role_name}"))  # nosec B608
                conn.execute(text(f"DROP ROLE IF EXISTS {role_name}"))  # nosec B608
        owner_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


@pytest.fixture
def owner(cluster):
    """The table owner, for seeding only. Never used to call the service: row level security does not bind it."""
    owner_engine, _ = cluster
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE v2_cutovers, ingest_key_ids, pipeline_status, agent_tokens, collector_installations, "
                          "branches, organizations, customers RESTART IDENTITY CASCADE"))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Lib A'), (11, 'Lib B')"))
        conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES "
                          "(1, 'lib-a', 'Lib A', 'active', 10), (2, 'lib-b', 'Lib B', 'active', 11)"))
        conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) VALUES "
                          "(1, 1, 'main', 'Main', 'active', 1), (3, 1, 'north', 'North', 'active', 3), "
                          "(2, 2, 'main', 'Main', 'active', 2)"))
        conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active) "
                          "VALUES (:h, 10, 1, 't', TRUE)"), {"h": hashlib.sha256(TOKEN_A.encode()).hexdigest()})
    return owner_engine


@pytest.fixture
def runtime(cluster, owner):
    """An engine connected AS the runtime role."""
    _, runtime_url = cluster
    engine = create_engine(runtime_url, hide_parameters=True)
    with engine.connect() as conn:
        role = conn.execute(text("SELECT current_user, (SELECT rolbypassrls OR rolsuper FROM pg_roles "
                                 "WHERE rolname = current_user)")).one()
        assert role[0].startswith("sortview_pipeline_status_role_") and role[1] is False
    yield engine
    engine.dispose()


def server_now(engine) -> datetime:
    with engine.connect() as conn:
        return conn.execute(text("SELECT clock_timestamp()")).scalar().astimezone(UTC)


def legacy_row(owner, scope, *, status=None, health_status=None, status_ago=None, health_ago=None) -> None:
    """A pipeline_status row whose stamps are `..._ago` before the server's current time (a timedelta, or None)."""
    with owner.begin() as conn:
        conn.execute(text(
            "INSERT INTO pipeline_status (customer_id, branch_id, status, health_status, last_error, checkins_rows, "
            "status_reported_at, health_status_reported_at) VALUES (:c, :b, :s, :h, 'CANARY-raw-error', 4001, "
            "now() - CAST(:sa AS interval), now() - CAST(:ha AS interval))"
        ), {"c": scope[0], "b": scope[1], "s": status, "h": health_status,
            "sa": None if status_ago is None else f"{status_ago.total_seconds()} seconds",
            "ha": None if health_ago is None else f"{health_ago.total_seconds()} seconds"})


def ingest_key(owner, scope, n, *, health_status="healthy", schedule="healthy", ago=timedelta(minutes=5), status="active") -> None:
    with owner.begin() as conn:
        conn.execute(text(
            "INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, status, retired_at, last_heartbeat_at, health_status, "
            "collector_schedule_status, last_error_class) VALUES (:k, :c, :b, :st, CASE WHEN :st = 'retired' THEN now() END, "
            "now() - CAST(:ago AS interval), :h, :sched, 'retryable_infra')"
        ), {"k": key_id(n), "c": scope[0], "b": scope[1], "st": status, "h": health_status, "sched": schedule,
            "ago": None if ago is None else f"{ago.total_seconds()} seconds"})


def cutover(owner, scope, *, at_offset: timedelta | None, set_ago=timedelta(days=1)) -> None:
    """A v2_cutovers row: the cutover is `at_offset` from the server's current time (None = a rollback row)."""
    with owner.begin() as conn:
        conn.execute(text(
            "INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, set_at) VALUES (:c, :b, "
            "now() + CAST(:at AS interval), 'pipeline-status-test', now() - CAST(:set AS interval))"
        ), {"c": scope[0], "b": scope[1], "at": None if at_offset is None else f"{at_offset.total_seconds()} seconds",
            "set": f"{set_ago.total_seconds()} seconds"})


def read(engine, scope, *, now=None) -> PipelineStatus:
    """The service, on a connection carrying `scope`'s tenant context -- exactly as the customer API will call it."""
    with tenant_connection(engine, scope[0], scope[1]) as conn:
        return get_pipeline_status(conn, tenant(scope), now=now or server_now(engine))


def _seed_four_legacy_tenants(owner) -> None:
    legacy_row(owner, A_MAIN, status="completed", status_ago=timedelta(minutes=5))
    legacy_row(owner, A_NORTH, status="failed_upload", status_ago=timedelta(minutes=10))
    legacy_row(owner, B_SAME_BRANCH_ID, health_status="degraded", health_ago=timedelta(minutes=15))
    legacy_row(owner, B_MAIN, status="started", status_ago=timedelta(minutes=20))


FOUR_STATES = {A_MAIN: "ok", A_NORTH: "failed", B_SAME_BRANCH_ID: "degraded", B_MAIN: "unknown"}


# =====================================================================================================================
# The premise: row level security does NOT protect pipeline_status
# =====================================================================================================================

def test_pipeline_status_is_not_under_row_level_security_and_ingest_key_ids_is(owner):
    with owner.connect() as conn:
        rls = dict(conn.execute(text("SELECT relname, relrowsecurity FROM pg_class WHERE relname IN "
                                     "('pipeline_status', 'ingest_key_ids')")).all())

    assert rls == {"pipeline_status": False, "ingest_key_ids": True}


def test_an_unscoped_select_on_a_tenant_scoped_connection_sees_every_tenants_pipeline_status_row(owner, runtime):
    """If this ever fails because pipeline_status has gained row level security, that is good news -- and the
    statement's explicit filter is then no longer the only protection. Until then, it is."""
    _seed_four_legacy_tenants(owner)
    for scope in (A_MAIN, B_MAIN):
        ingest_key(owner, scope, scope[0] * 100 + scope[1])

    with tenant_connection(runtime, *A_MAIN) as conn:
        every_status_row = {tuple(r) for r in conn.execute(text("SELECT customer_id, branch_id FROM pipeline_status"))}
        visible_keys = {tuple(r) for r in conn.execute(text("SELECT customer_id, branch_id FROM ingest_key_ids"))}

    assert every_status_row == {A_MAIN, A_NORTH, B_SAME_BRANCH_ID, B_MAIN}   # all four tenants: RLS is not the protection
    assert visible_keys == {A_MAIN}                                         # where RLS exists, it does restrict


# =====================================================================================================================
# The explicit filter is enough: each tenant gets exactly its own row
# =====================================================================================================================

def test_each_tenant_gets_only_its_own_legacy_row(owner, runtime):
    _seed_four_legacy_tenants(owner)
    before = server_now(runtime)

    seen = {scope: read(runtime, scope) for scope in FOUR_STATES}

    assert {scope: result.state for scope, result in seen.items()} == FOUR_STATES
    # Four different rows were read: each time is its own row's, and each is that row's own number of minutes ago.
    for scope, minutes in ((A_MAIN, 5), (A_NORTH, 10), (B_SAME_BRANCH_ID, 15), (B_MAIN, 20)):
        age = before - seen[scope].last_reported_at
        assert timedelta(minutes=minutes) - timedelta(seconds=5) <= age <= timedelta(minutes=minutes) + timedelta(seconds=5), scope


def test_the_same_branch_id_under_another_customer_never_leaks(owner, runtime):
    legacy_row(owner, B_SAME_BRANCH_ID, status="failed_upload", health_status="auth_failure",
               status_ago=timedelta(minutes=1), health_ago=timedelta(minutes=1))

    assert read(runtime, A_MAIN) == NOTHING_REPORTED          # A's branch 1 has no row: B's branch 1 is not borrowed

    legacy_row(owner, A_MAIN, status="completed", status_ago=timedelta(minutes=30))
    assert read(runtime, A_MAIN).state == "ok"
    assert read(runtime, B_SAME_BRANCH_ID).state == "failed"


def test_another_branch_of_the_same_customer_never_leaks(owner, runtime):
    legacy_row(owner, A_NORTH, status="failed_upload", status_ago=timedelta(minutes=1))

    assert read(runtime, A_MAIN) == NOTHING_REPORTED

    legacy_row(owner, A_MAIN, health_status="healthy", health_ago=timedelta(minutes=30))
    assert (read(runtime, A_MAIN).state, read(runtime, A_NORTH).state) == ("ok", "failed")


def test_the_legacy_answer_does_not_depend_on_the_connections_tenant_context(owner, runtime):
    """The context is applied for every read; this shows it is not what scopes THIS table. With no context at all,
    and even with another tenant's context, the statement's own filter returns the resolved tenant's row."""
    _seed_four_legacy_tenants(owner)
    now = server_now(runtime)

    with runtime.connect() as conn:                                      # no tenant context at all
        without_context = get_pipeline_status(conn, tenant(A_NORTH), now=now)
    with tenant_connection(runtime, *B_MAIN) as conn:                    # another tenant's context
        under_another_context = get_pipeline_status(conn, tenant(A_NORTH), now=now)

    assert without_context == under_another_context == read(runtime, A_NORTH, now=now)
    assert without_context.state == "failed"


def test_the_primary_key_guarantees_one_row_per_branch(owner):
    legacy_row(owner, A_MAIN, status="completed", status_ago=timedelta(minutes=1))

    with pytest.raises(Exception, match="duplicate key"):
        legacy_row(owner, A_MAIN, status="failed_upload", status_ago=timedelta(minutes=2))


# =====================================================================================================================
# Which source: the effective cutover, on real TIMESTAMPTZ values
# =====================================================================================================================

def _both_sources(owner, scope=A_MAIN) -> None:
    legacy_row(owner, scope, status="failed_upload", status_ago=timedelta(minutes=30))
    ingest_key(owner, scope, 1, health_status="healthy", ago=timedelta(minutes=5))


def test_with_no_cutover_the_legacy_row_is_the_answer_even_though_an_active_key_exists(owner, runtime):
    _both_sources(owner)

    assert read(runtime, A_MAIN).state == "failed"


def test_a_future_cutover_leaves_the_legacy_row_the_answer_and_a_past_one_makes_the_key_the_answer(owner, runtime):
    _both_sources(owner)
    cutover(owner, A_MAIN, at_offset=timedelta(hours=1))
    assert read(runtime, A_MAIN).state == "failed"

    cutover(owner, A_MAIN, at_offset=timedelta(hours=-1), set_ago=timedelta(minutes=1))    # a newer record: past cutover
    assert read(runtime, A_MAIN).state == "ok"


def test_the_stored_cutover_instant_itself_is_already_current(owner, runtime):
    _both_sources(owner)
    cutover(owner, A_MAIN, at_offset=timedelta(hours=-1))
    with owner.connect() as conn:
        cutover_at = conn.execute(text("SELECT cutover_at FROM v2_cutovers WHERE customer_id = 10")).scalar()

    assert read(runtime, A_MAIN, now=cutover_at).state == "ok"                                   # at the instant
    assert read(runtime, A_MAIN, now=cutover_at - timedelta(microseconds=1)).state == "failed"   # just before it


def test_a_latest_rollback_row_returns_the_branch_to_its_legacy_row(owner, runtime):
    _both_sources(owner)
    cutover(owner, A_MAIN, at_offset=timedelta(days=-10), set_ago=timedelta(days=11))
    assert read(runtime, A_MAIN).state == "ok"

    cutover(owner, A_MAIN, at_offset=None, set_ago=timedelta(minutes=1))     # the latest record: a rollback
    assert read(runtime, A_MAIN).state == "failed"


def test_another_tenants_cutover_does_not_change_this_tenants_source(owner, runtime):
    _both_sources(owner)
    for scope in (A_NORTH, B_SAME_BRANCH_ID, B_MAIN):
        cutover(owner, scope, at_offset=timedelta(days=-1))

    assert read(runtime, A_MAIN).state == "failed"      # still legacy: none of those cutovers is this branch's


# =====================================================================================================================
# The current source, under row level security
# =====================================================================================================================

def test_a_current_branch_gets_the_state_of_its_own_most_recent_active_key(owner, runtime):
    cutover(owner, A_MAIN, at_offset=timedelta(days=-1))
    cutover(owner, B_MAIN, at_offset=timedelta(days=-1))
    ingest_key(owner, A_MAIN, 1, health_status="error", ago=timedelta(hours=2))
    ingest_key(owner, A_MAIN, 2, health_status="healthy", schedule="task_disabled", ago=timedelta(minutes=5))
    ingest_key(owner, A_MAIN, 3, health_status="healthy", ago=None)                       # never reported
    ingest_key(owner, A_MAIN, 4, health_status="healthy", ago=timedelta(seconds=1), status="retired")
    ingest_key(owner, B_MAIN, 5, health_status="degraded", ago=timedelta(minutes=1))
    before = server_now(runtime)

    a, b = read(runtime, A_MAIN), read(runtime, B_MAIN)

    assert a.state == "failed"       # a healthy heartbeat whose scheduled task is disabled: the fault shows
    assert timedelta(minutes=4, seconds=55) <= before - a.last_reported_at <= timedelta(minutes=5, seconds=5)
    assert b.state == "degraded"     # B's own key, not A's


def test_a_current_branch_whose_key_never_reported_is_unknown_and_does_not_fall_back_to_its_legacy_row(owner, runtime):
    cutover(owner, A_MAIN, at_offset=timedelta(days=-1))
    legacy_row(owner, A_MAIN, status="completed", status_ago=timedelta(minutes=1))
    ingest_key(owner, A_MAIN, 1, health_status=None, schedule=None, ago=None)

    assert read(runtime, A_MAIN) == NOTHING_REPORTED


def test_row_level_security_hides_another_tenants_key_from_the_current_read(owner, runtime):
    cutover(owner, A_MAIN, at_offset=timedelta(days=-1))
    ingest_key(owner, A_MAIN, 1, health_status="healthy", ago=timedelta(minutes=5))
    now = server_now(runtime)

    with runtime.connect() as conn:                                    # no tenant context: RLS returns no row
        without_context = get_pipeline_status(conn, tenant(A_MAIN), now=now)
    with tenant_connection(runtime, *B_MAIN) as conn:                  # another tenant's context: still no row
        under_another_context = get_pipeline_status(conn, tenant(A_MAIN), now=now)

    assert without_context == under_another_context == NOTHING_REPORTED
    assert read(runtime, A_MAIN, now=now).state == "ok"                # the right context, and the row is there


# =====================================================================================================================
# Time: a TIMESTAMPTZ in any session offset is the same aware UTC instant
# =====================================================================================================================

def test_the_driver_returns_an_aware_datetime_for_every_stamp_the_service_reads(owner, runtime):
    legacy_row(owner, A_MAIN, status="completed", health_status="healthy", status_ago=timedelta(minutes=5),
               health_ago=timedelta(minutes=6))
    ingest_key(owner, A_MAIN, 1)

    with tenant_connection(runtime, *A_MAIN) as conn:
        values = [*conn.execute(text("SELECT status_reported_at, health_status_reported_at FROM pipeline_status")).one(),
                  conn.execute(text("SELECT last_heartbeat_at FROM ingest_key_ids")).scalar()]

    # So the service's refusal of a value with no offset is a safeguard, not a path production takes.
    assert all(isinstance(value, datetime) and value.utcoffset() is not None for value in values)


@pytest.mark.parametrize("source", ["legacy", "current"])
def test_the_answer_is_the_same_utc_instant_whatever_the_session_time_zone(cluster, owner, source):
    _, runtime_url = cluster
    if source == "legacy":
        legacy_row(owner, A_MAIN, status="completed", status_ago=timedelta(minutes=5))
        column, table = "status_reported_at", "pipeline_status"
    else:
        cutover(owner, A_MAIN, at_offset=timedelta(days=-1))
        ingest_key(owner, A_MAIN, 1, ago=timedelta(minutes=5))
        column, table = "last_heartbeat_at", "ingest_key_ids"

    now = server_now(owner)
    answers, raw_offsets = {}, {}
    for zone in ("UTC", "America/Chicago", "Asia/Tokyo", "Asia/Kolkata", "Pacific/Chatham"):
        engine = create_engine(runtime_url, connect_args={"options": f"-c timezone={zone}"}, hide_parameters=True)
        try:
            with tenant_connection(engine, *A_MAIN) as conn:
                assert conn.execute(text("SHOW timezone")).scalar() == zone
                raw_offsets[zone] = conn.execute(text(f"SELECT {column} FROM {table}")).scalar().utcoffset()  # nosec B608
                answers[zone] = get_pipeline_status(conn, tenant(A_MAIN), now=now)
        finally:
            engine.dispose()

    # The driver really did hand the value back in five different offsets...
    assert len(set(raw_offsets.values())) == 5 and raw_offsets["UTC"] == timedelta(0)
    # ...and the service returned one answer, in UTC.
    assert len(set(answers.values())) == 1
    (answer,) = set(answers.values())
    assert answer.state == "ok" and answer.last_reported_at.tzinfo is UTC
    assert timedelta(minutes=4, seconds=55) <= now - answer.last_reported_at <= timedelta(minutes=5, seconds=5)


# =====================================================================================================================
# A pooled connection reused across tenants
# =====================================================================================================================

def test_one_pooled_connection_never_carries_one_tenants_answer_to_the_next(cluster, owner):
    _, runtime_url = cluster
    _seed_four_legacy_tenants(owner)
    cutover(owner, B_MAIN, at_offset=timedelta(days=-1))                 # B main is current...
    ingest_key(owner, B_MAIN, 1, health_status="degraded", ago=timedelta(minutes=2))
    expected = {**FOUR_STATES, B_MAIN: "degraded"}                       # ...so its answer comes from its key
    single = create_engine(runtime_url, pool_size=1, max_overflow=0, hide_parameters=True)
    try:
        with single.connect() as conn:
            backend = conn.execute(text("SELECT pg_backend_pid()")).scalar()
        now = server_now(single)
        first_pass = {scope: read(single, scope, now=now) for scope in expected}

        order = [A_MAIN, B_MAIN, B_SAME_BRANCH_ID, A_MAIN, A_NORTH, B_MAIN, A_MAIN, B_SAME_BRANCH_ID, A_NORTH]
        for scope in order:
            with tenant_connection(single, scope[0], scope[1]) as conn:
                assert conn.execute(text("SELECT pg_backend_pid()")).scalar() == backend   # really the same backend
                assert get_pipeline_status(conn, tenant(scope), now=now) == first_pass[scope], scope

        # Change the pooled connection's SESSION time zone for good, then ask again: nothing moves.
        with single.connect() as conn:
            conn.execute(text("SET TIME ZONE 'Asia/Tokyo'"))
            conn.commit()
        for scope in order:
            assert read(single, scope, now=now) == first_pass[scope], scope

        assert {scope: result.state for scope, result in first_pass.items()} == expected
        # Nothing of any read's tenant context is left on the pooled connection.
        with single.connect() as conn:
            settings = conn.execute(text("SELECT current_setting('app.operational_customer_id', true), "
                                         "current_setting('app.operational_branch_id', true)")).one()
            assert all(value in (None, "") for value in settings)
    finally:
        single.dispose()


# =====================================================================================================================
# End to end: the stamps the service reads are the ones the real endpoint writes
# =====================================================================================================================

def _post(body: dict):
    response = client.post("/upload-pipeline-status", json={"customer_id": 10, "branch_id": 1, **body},
                           headers={"Authorization": f"Bearer {TOKEN_A}"})
    assert response.status_code == 200, response.text


def _pause(engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_sleep(0.05)"))


def test_reports_posted_to_the_real_endpoint_are_what_the_service_answers_with(owner, runtime, monkeypatch):
    monkeypatch.setattr(main, "engine", runtime)                # the API writes as the runtime role too
    assert _HASH_EXPR in main._AGENT_TOKEN_LOOKUP_SQL
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR))
    assert read(runtime, A_MAIN) == NOTHING_REPORTED

    before = server_now(runtime)
    _post({"status": "completed", "checkins_rows": 3})
    first = read(runtime, A_MAIN)
    assert first.state == "ok" and before - timedelta(seconds=1) <= first.last_reported_at <= server_now(runtime) + timedelta(seconds=1)

    _pause(runtime)
    _post({"health_status": "degraded", "pending_outbox_count": 2})              # the heartbeat is now the last report
    second = read(runtime, A_MAIN)
    assert second.state == "degraded" and second.last_reported_at > first.last_reported_at

    _pause(runtime)
    _post({"status": "failed_upload", "last_error": "CANARY-raw-error-text"})    # then a failed run is
    third = read(runtime, A_MAIN)
    assert third.state == "failed" and third.last_reported_at > second.last_reported_at

    _pause(runtime)
    _post({"status": "preflight_check", "checkins_rows": 0})                     # an install probe: unknown, with its time
    fourth = read(runtime, A_MAIN)
    assert fourth.state == "unknown" and fourth.last_reported_at > third.last_reported_at

    _pause(runtime)
    _post({"status": "completed", "health_status": "healthy"})                   # both at once: a tie, health stands
    fifth = read(runtime, A_MAIN)
    assert fifth.state == "ok" and fifth.last_reported_at > fourth.last_reported_at

    _pause(runtime)
    _post({"checkins_rows": 9})                                                  # carries neither signal: nothing moves
    assert read(runtime, A_MAIN) == fifth

    for result in (first, second, third, fourth, fifth):
        assert result.last_reported_at.tzinfo is UTC
        assert "CANARY" not in repr(result)


def test_a_legacy_row_written_before_the_server_stamped_reports_is_unknown(owner, runtime):
    # As every row is immediately after migration 16b41d730e15: it holds a status and three naive timestamps, and no
    # stamp. Nothing is inferred from them.
    with owner.begin() as conn:
        conn.execute(text("INSERT INTO pipeline_status (customer_id, branch_id, status, health_status, last_run, last_attempt, "
                          "updated_at) VALUES (10, 1, 'completed', 'healthy', now(), now(), now())"))

    assert read(runtime, A_MAIN) == NOTHING_REPORTED
