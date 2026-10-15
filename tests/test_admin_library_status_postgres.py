"""A2 item 2 on a REAL PostgreSQL: the admin libraries show each site's CURRENT pipeline state.

The case this exists for is NBPL's: a library cut over to Contract v2 still holds its old legacy pipeline_status row,
frozen at whatever it last said. The Super Admin listing reads that row. The admin API must instead show what the
site's v2 heartbeat says -- and a library still on the legacy contract must keep showing its legacy report.

Everything is read through the real routes (GET /api/admin/libraries and /api/admin/libraries/{slug}) by the real
services, as a NON-OWNING, NOBYPASSRLS runtime role, so ingest_key_ids' row level security really applies: the
heartbeat is visible only because the routes read it on a connection scoped to that site's own tenant. Only the
session lookup and the platform-administrator flag are stubbed -- they are tested in tests/test_admin_api.py.

OPT-IN AND SAFE BY CONSTRUCTION, the same convention as tests/test_pipeline_status_service_postgres.py: runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server the test may create and drop a
database and a role on; the host must be local unless SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1. Every value is SYNTHETIC.
"""

from __future__ import annotations

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

import database
import main
from services import platform_admin_service, session_service

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL tests)")

RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only
COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
ADMIN = {"id": 7, "email": "admin@example.invalid", "full_name": "Test Admin"}


def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


def _create_runtime_role(owner_engine, role_name: str) -> None:
    """The production runtime role's attributes, with SELECT on exactly what the admin libraries read."""
    with owner_engine.begin() as conn:
        conn.execute(text(f"""
            CREATE ROLE {role_name}
                LOGIN
                NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT
                PASSWORD '{RUNTIME_ROLE_PASSWORD}'
        """))  # nosec B608 - role_name is generated here, the password is a fresh local secret
        conn.execute(text(f"GRANT CONNECT ON DATABASE {conn.engine.url.database} TO {role_name}"))  # nosec B608
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role_name}"))  # nosec B608
        conn.execute(text(
            "GRANT SELECT ON TABLE public.organizations, public.branches, public.subscriptions, public.plans, "
            f"public.collector_installations, public.pipeline_status, public.ingest_key_ids, public.v2_cutovers "
            f"TO {role_name}"
        ))  # nosec B608


@pytest.fixture(scope="module")
def cluster():
    admin = make_url(ADMIN_URL)
    _guard(admin)
    name = f"sortview_admin_libraries_test_{secrets.token_hex(4)}"
    role_name = f"sortview_admin_libraries_role_{secrets.token_hex(4)}"
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


def _seed(conn) -> None:
    # The migrations' own plans stay; everything this test writes starts empty.
    conn.execute(text(
        "TRUNCATE v2_cutovers, ingest_key_ids, pipeline_status, collector_installations, subscriptions, branches, "
        "organizations, customers RESTART IDENTITY CASCADE"
    ))
    conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'NBPL'), (11, 'Legacy')"))
    conn.execute(text(
        "INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES "
        "(1, 'nbpl', 'New Braunfels Public Library', 'active', 10), "
        "(2, 'legacy', 'Legacy Library', 'active', 11), "
        "(3, 'waiting', 'Waiting Library', 'trial', NULL)"
    ))
    conn.execute(text(
        "INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id, onboarding_status) "
        "VALUES (1, 1, 'main', 'Main Branch', TRUE, 'active', 1, 'ready'), "
        "(2, 2, 'main', 'Legacy Main', TRUE, 'active', 2, 'ready'), "
        "(3, 3, 'central', 'Central', TRUE, 'active', NULL, 'pending')"
    ))
    conn.execute(text(
        "INSERT INTO subscriptions (organization_id, plan_id, status) "
        "SELECT 1, id, 'trial' FROM plans WHERE code = 'enterprise'"
    ))
    conn.execute(text(
        "INSERT INTO collector_installations (organization_id, branch_id, name, status) VALUES "
        "(1, 1, 'Main AMH', 'active'), (2, 2, 'Legacy AMH', 'active'), (3, 3, 'Waiting AMH', 'provisioning')"
    ))
    # NBPL: an OLD legacy report that says everything is fine -- frozen there since the cutover ...
    conn.execute(text(
        "INSERT INTO pipeline_status (customer_id, branch_id, status, status_reported_at) "
        "VALUES (10, 1, 'completed', now() - interval '30 days')"
    ))
    # ... a cutover in the past, and a recent v2 heartbeat that is degraded.
    conn.execute(text(
        "INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, set_at) "
        "VALUES (10, 1, now() - interval '20 days', 'admin-libraries-test', now() - interval '21 days')"
    ))
    conn.execute(text(
        "INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, status, last_heartbeat_at, health_status, "
        "collector_schedule_status, last_error_class) VALUES "
        "('0000000a-4d5a-4b6c-8d7e-9f0a1b2c3d4e', 10, 1, 'active', now() - interval '5 minutes', 'degraded', "
        "'healthy', 'retryable_infra')"
    ))
    # Legacy: still on the legacy contract, and its latest run failed.
    conn.execute(text(
        "INSERT INTO pipeline_status (customer_id, branch_id, status, status_reported_at) "
        "VALUES (11, 2, 'failed_upload', now() - interval '10 minutes')"
    ))


@pytest.fixture
def api(cluster, monkeypatch):
    owner_engine, runtime_url = cluster
    with owner_engine.begin() as conn:
        _seed(conn)
    runtime = create_engine(runtime_url, hide_parameters=True)
    with runtime.connect() as conn:
        bypass = conn.execute(text(
            "SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = current_user"
        )).scalar()
        assert bypass is False
    # The one flat database engine every service uses: the runtime role's.
    monkeypatch.setattr(database, "_engine", runtime)
    monkeypatch.setattr(session_service, "validate_session", lambda token: dict(ADMIN))
    monkeypatch.setattr(platform_admin_service, "is_platform_admin", lambda user_id: True)
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app, raise_server_exceptions=False), owner_engine
    main.limiter.reset()
    runtime.dispose()


def _heartbeat_at(owner_engine) -> datetime:
    with owner_engine.connect() as conn:
        heard = conn.execute(text("SELECT last_heartbeat_at FROM ingest_key_ids WHERE customer_id = 10")).scalar()
    return heard.astimezone(UTC)


def test_a_v2_library_shows_its_heartbeat_not_its_frozen_legacy_row_and_a_v1_library_its_legacy_report(api):
    client, owner_engine = api

    response = client.get("/api/admin/libraries", headers=COOKIE)

    assert response.status_code == 200, response.text
    libraries = {library["slug"]: library for library in response.json()["libraries"]}
    assert list(libraries) == ["legacy", "nbpl", "waiting"]

    nbpl = libraries["nbpl"]["primary_sorter"]["pipeline"]
    assert nbpl["state"] == "degraded"          # the v2 heartbeat -- NOT the frozen legacy "completed" (ok)
    assert datetime.fromisoformat(nbpl["last_reported_at"]) == _heartbeat_at(owner_engine)

    assert libraries["legacy"]["primary_sorter"]["pipeline"]["state"] == "failed"
    assert libraries["waiting"]["primary_sorter"] == {
        "onboarding_status": "pending",
        "pipeline": {"state": "unknown", "last_reported_at": None},
    }
    assert libraries["nbpl"]["subscription"] == {"plan_code": "enterprise", "plan_name": "Enterprise", "status": "trial"}
    # Nothing of the heartbeat beyond its state and time, and no operational id.
    assert "retryable_infra" not in response.text and "last_error_class" not in response.text


def test_the_overview_reads_the_same_current_state_and_says_identity_in_words(api):
    client, _ = api

    nbpl = client.get("/api/admin/libraries/nbpl", headers=COOKIE).json()
    waiting = client.get("/api/admin/libraries/waiting", headers=COOKIE).json()

    assert nbpl["operational_identity"] == "assigned"
    assert [(site["name"], site["pipeline"]["state"]) for site in nbpl["sorters"]] == [("Main AMH", "degraded")]
    assert waiting["operational_identity"] == "missing"     # nothing is mapped; nothing could be read
    assert [(site["name"], site["onboarding_status"], site["pipeline"]["state"]) for site in waiting["sorters"]] == [
        ("Waiting AMH", "pending", "unknown")
    ]
