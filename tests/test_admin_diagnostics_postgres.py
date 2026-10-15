"""A2 item 3 on a REAL PostgreSQL: a sorter site's diagnostics, through the real admin route.

    GET /api/admin/libraries/{org_slug}/sorters/{sorter_slug}/diagnostics

As a NON-OWNING, NOBYPASSRLS runtime role, so ingest_key_ids' row level security really applies:

  * a site cut over to Contract v2 returns ITS OWN heartbeat -- read on a connection scoped to its tenant -- and never
    its frozen legacy pipeline_status row, nor another tenant's heartbeat;
  * a site still on the legacy contract returns its legacy report with aware report stamps, and none of the row's
    free text, destinations or naive timestamps;
  * no key id, operational id or raw value of a column that is never read reaches the response.

Only the session lookup and the platform-administrator flag are stubbed (tests/test_admin_api.py tests those).

OPT-IN AND SAFE BY CONSTRUCTION, as tests/test_admin_library_status_postgres.py: runs only when
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
NBPL_KEY = "0000000a-4d5a-4b6c-8d7e-9f0a1b2c3d4e"
OTHER_KEY = "0000000b-4d5a-4b6c-8d7e-9f0a1b2c3d4e"


def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


def _create_runtime_role(owner_engine, role_name: str) -> None:
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
    name = f"sortview_admin_diagnostics_test_{secrets.token_hex(4)}"
    role_name = f"sortview_admin_diagnostics_role_{secrets.token_hex(4)}"
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
    conn.execute(text(
        "TRUNCATE v2_cutovers, ingest_key_ids, pipeline_status, collector_installations, subscriptions, branches, "
        "organizations, customers RESTART IDENTITY CASCADE"
    ))
    conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'NBPL'), (11, 'Legacy')"))
    conn.execute(text(
        "INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES "
        "(1, 'nbpl', 'New Braunfels Public Library', 'active', 10), (2, 'legacy', 'Legacy Library', 'active', 11)"
    ))
    conn.execute(text(
        "INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id, onboarding_status) "
        "VALUES (1, 1, 'main', 'Main Branch', TRUE, 'active', 1, 'ready'), "
        "(2, 2, 'main', 'Legacy Main', TRUE, 'active', 2, 'ready')"
    ))
    conn.execute(text(
        "INSERT INTO collector_installations (organization_id, branch_id, name, status, hostname, collector_version) "
        "VALUES (1, 1, 'Main AMH', 'active', 'CANARY-HOST-NBPL', '9.9.9'), "
        "(2, 2, 'Legacy AMH', 'active', 'CANARY-HOST-LEGACY', '9.9.8')"
    ))
    # NBPL: a frozen legacy row full of canaries, a past cutover, and its own current heartbeat.
    conn.execute(text(
        "INSERT INTO pipeline_status (customer_id, branch_id, status, status_reported_at, checkins_rows, last_error) "
        "VALUES (10, 1, 'completed', now() - interval '30 days', 7771, 'CANARY-frozen-error')"
    ))
    conn.execute(text(
        "INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, set_at) "
        "VALUES (10, 1, '2026-09-01T12:00:00+00', 'admin-diagnostics-test', now() - interval '45 days')"
    ))
    conn.execute(text(
        "INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, status, last_heartbeat_at, health_status, "
        "last_error_class, pending_outbox_count, quarantined_count, collector_schedule_status, "
        "collector_run_duration_ms, last_success_at) VALUES "
        "(:nbpl, 10, 1, 'active', '2026-10-14T15:30:00+00', 'degraded', 'retryable_infra', 12, 3, 'healthy', 4500, "
        "'2026-10-14T15:10:00+00'), "
        "(:other, 11, 2, 'active', '2026-10-14T15:00:00+00', 'error', 'auth_failure', 99, 98, 'task_missing', 1, NULL)"
    ), {"nbpl": NBPL_KEY, "other": OTHER_KEY})
    # Legacy: on the legacy contract, with counters, stamps and every column that must never be read.
    conn.execute(text(
        "INSERT INTO pipeline_status (customer_id, branch_id, status, status_reported_at, health_status, "
        "health_status_reported_at, checkins_rows, rejects_rows, uploaded_checkins_rows, uploaded_rejects_rows, "
        "checkins_bad_datetime_rows, rejects_bad_datetime_rows, last_error, destination_breakdown, transit_items, "
        "problem_items, last_run, last_attempt) VALUES "
        "(11, 2, 'failed_upload', '2026-10-14T14:00:00+00', 'degraded', '2026-10-14T14:05:00+00', 120, 3, 0, 0, 2, 0, "
        "'CANARY-legacy-error C:\\path', '{\"Westside\": 4}', 4401, 4402, '2026-10-14 09:00:00', '2026-10-14 09:01:00')"
    ))


@pytest.fixture
def api(cluster, monkeypatch):
    owner_engine, runtime_url = cluster
    with owner_engine.begin() as conn:
        _seed(conn)
    runtime = create_engine(runtime_url, hide_parameters=True)
    with runtime.connect() as conn:
        assert conn.execute(text("SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = current_user")).scalar() is False
    monkeypatch.setattr(database, "_engine", runtime)
    monkeypatch.setattr(session_service, "validate_session", lambda token: dict(ADMIN))
    monkeypatch.setattr(platform_admin_service, "is_platform_admin", lambda user_id: True)
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app, raise_server_exceptions=False)
    main.limiter.reset()
    runtime.dispose()


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def test_a_cut_over_site_returns_its_own_v2_heartbeat_through_rls_and_never_its_frozen_legacy_row(api):
    response = api.get("/api/admin/libraries/nbpl/sorters/main/diagnostics", headers=COOKIE)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract"] == "v2"
    assert _instant(body["cutover_at"]) == datetime(2026, 9, 1, 12, tzinfo=UTC)
    assert body["legacy"] is None
    status = body["v2"]["status"]
    assert (status["health_status"], status["last_error_class"]) == ("degraded", "retryable_infra")
    assert (status["pending_outbox_count"], status["quarantined_count"]) == (12, 3)
    assert _instant(status["last_heartbeat_at"]) == datetime(2026, 10, 14, 15, 30, tzinfo=UTC)
    assert body["pipeline"]["state"] == "degraded"
    # Nothing of the other tenant's heartbeat, the frozen legacy row, the key or the installation.
    for leaked in ("auth_failure", "task_missing", "CANARY", "7771", NBPL_KEY, OTHER_KEY, "9.9.9"):
        assert leaked not in response.text, leaked


def test_a_legacy_site_returns_its_legacy_report_and_nothing_it_must_not(api):
    response = api.get("/api/admin/libraries/legacy/sorters/main/diagnostics", headers=COOKIE)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract"] == "legacy"
    assert body["v2"] is None
    report = body["legacy"]["status"]
    assert (report["run_status"], report["health_status"]) == ("failed_upload", "degraded")
    assert _instant(report["run_reported_at"]) == datetime(2026, 10, 14, 14, tzinfo=UTC)
    assert (report["checkins_rows"], report["checkins_bad_datetime_rows"]) == (120, 2)
    assert body["pipeline"]["state"] == "degraded"       # the later of the two stamps is the health report
    for leaked in ("CANARY", "Westside", "4401", "4402", "09:00:00", "09:01:00", OTHER_KEY, "auth_failure"):
        assert leaked not in response.text, leaked


def test_an_unknown_sorter_is_404(api):
    response = api.get("/api/admin/libraries/nbpl/sorters/annex/diagnostics", headers=COOKIE)

    assert response.status_code == 404
    assert response.json() == {"code": "sorter_not_found", "message": "Sorter not found."}
