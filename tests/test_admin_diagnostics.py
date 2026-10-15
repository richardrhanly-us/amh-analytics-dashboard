"""A2 item 3: a sorter site's diagnostics in the administration API.

    GET /api/admin/libraries/{org_slug}/sorters/{sorter_slug}/diagnostics

Driven through TestClient(main.app), with every service replaced at the FLAT module identity the route uses. What each
read returns is tested where it lives (operational_read_service, admin_diagnostics_service, pipeline_status_service)
and, end to end on a real server, in tests/test_admin_diagnostics_postgres.py. Here: which site, under which tenant,
which era is read, and what may reach the browser.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import main
from customer_api import tenant_scope
from services import (
    admin_diagnostics_service,
    admin_library_service,
    operational_metrics_service,
    operational_read_service,
    pipeline_status_service,
    platform_admin_service,
    session_service,
    sorter_inventory_service,
)
from services.admin_diagnostics_service import LegacyDiagnostics
from services.admin_library_service import LibraryRow
from services.operational_read_service import IngestStatus
from services.pipeline_status_service import PipelineStatus
from services.sorter_inventory_service import SorterSite
from services.tenant_resolution_service import ResolvedOperationalTenant

COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 7, "email": "admin@example.invalid", "full_name": "Test Admin"}
PATH = "/api/admin/libraries/nbpl/sorters/main/diagnostics"
SORTER_NOT_FOUND = {"code": "sorter_not_found", "message": "Sorter not found."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have access to SortView administration."}
NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

# Distinctive operational ids, so any leak is caught by value as well as by key.
CUSTOMER, BRANCH = 90417, 90523
HEARD = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)

NBPL = LibraryRow(
    slug="nbpl", name="New Braunfels Public Library", status="active", primary_branch_slug="main",
    primary_branch_name="Main Branch", primary_branch_status="active", operational_identity="assigned",
    plan_code="enterprise", plan_name="Enterprise", subscription_status="trial",
)
MAIN = SorterSite(slug="main", name="Main AMH", host_branch_slug="main", host_branch_name="Main Branch",
                  status="active", collector_count=1, onboarding_status="ready")

HEARTBEAT = IngestStatus(
    health_status="degraded", last_error_class="retryable_infra", pending_outbox_count=12, quarantined_count=3,
    oldest_pending_event_at=HEARD - timedelta(minutes=40), last_success_at=HEARD - timedelta(minutes=20),
    watcher_last_active_at=HEARD - timedelta(minutes=1), last_heartbeat_at=HEARD,
    collector_last_run_at=HEARD - timedelta(minutes=2), collector_next_run_at=HEARD + timedelta(minutes=13),
    collector_run_duration_ms=4500, collector_schedule_status="healthy",
)
LEGACY = LegacyDiagnostics(
    run_status="completed", run_reported_at=HEARD, health_status=None, health_reported_at=None,
    pending_outbox_count=None, quarantined_count=None, checkins_rows=120, rejects_rows=3,
    uploaded_checkins_rows=118, uploaded_rejects_rows=3, checkins_bad_datetime_rows=2, rejects_bad_datetime_rows=0,
)

V2_FIELDS = {
    "last_heartbeat_at", "health_status", "last_error_class", "collector_schedule_status", "collector_last_run_at",
    "collector_next_run_at", "collector_run_duration_ms", "pending_outbox_count", "quarantined_count",
    "oldest_pending_event_at", "last_success_at", "watcher_last_active_at",
}
LEGACY_FIELDS = {
    "run_status", "run_reported_at", "health_status", "health_reported_at", "pending_outbox_count",
    "quarantined_count", "checkins_rows", "rejects_rows", "uploaded_checkins_rows", "uploaded_rejects_rows",
    "checkins_bad_datetime_rows", "rejects_bad_datetime_rows",
}
FORBIDDEN_KEYS = {
    "id", "key_id", "installation_id", "hostname", "collector_version", "algorithm", "last_error",
    "last_failure_category", "destination_breakdown", "transit_items", "problem_items", "last_run", "last_attempt",
    "updated_at", "settings", "notes", "is_platform_admin",
}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


class World:
    def __init__(self, monkeypatch, *, admin=True, cutover=HEARD - timedelta(days=20)):
        self.rows = {"nbpl": NBPL}
        self.sites = {"nbpl": [MAIN]}
        self.tenants = {("nbpl", "main"): (CUSTOMER, BRANCH)}
        self.cutover = cutover
        self.heartbeat: IngestStatus | None = HEARTBEAT
        self.legacy: LegacyDiagnostics | None = LEGACY
        self.reads: list[str] = []
        monkeypatch.setattr(session_service, "validate_session", lambda token: dict(USER))
        monkeypatch.setattr(platform_admin_service, "is_platform_admin", lambda user_id: admin)
        monkeypatch.setattr(admin_library_service, "get_library_row", lambda slug: self.rows.get(slug))
        monkeypatch.setattr(admin_library_service, "site_tenant", self._site_tenant)
        monkeypatch.setattr(sorter_inventory_service, "list_sorter_sites", lambda slug: list(self.sites.get(slug, [])))
        monkeypatch.setattr(tenant_scope, "open_customer_tenant_connection", self._open)
        monkeypatch.setattr(pipeline_status_service, "get_pipeline_status", self._pipeline)
        monkeypatch.setattr(operational_metrics_service, "get_effective_cutover", self._cutover)
        monkeypatch.setattr(operational_read_service, "get_latest_ingest_status", self._heartbeat)
        monkeypatch.setattr(admin_diagnostics_service, "get_legacy_diagnostics", self._legacy)

    def _site_tenant(self, org_slug, branch_slug):
        ids = self.tenants.get((org_slug, branch_slug))
        if ids is None:
            return None
        return ResolvedOperationalTenant(org_slug=org_slug, branch_slug=branch_slug, access_mode="full",
                                         operational_customer_id=ids[0], operational_branch_id=ids[1])

    @contextmanager
    def _open(self, tenant):
        yield (tenant.operational_customer_id, tenant.operational_branch_id)

    def _scoped(self, conn, tenant, name):
        # Every read runs on the connection scoped to that very tenant.
        assert conn == (tenant.operational_customer_id, tenant.operational_branch_id) == (CUSTOMER, BRANCH)
        self.reads.append(name)

    def _pipeline(self, conn, tenant, *, now):
        self._scoped(conn, tenant, "pipeline")
        assert now.tzinfo is not None
        return PipelineStatus(state="degraded", last_reported_at=HEARD)

    def _cutover(self, conn, tenant):
        self._scoped(conn, tenant, "cutover")
        return self.cutover

    def _heartbeat(self, conn, tenant):
        self._scoped(conn, tenant, "v2")
        return self.heartbeat

    def _legacy(self, conn, tenant):
        self._scoped(conn, tenant, "legacy")
        return self.legacy


def _keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)
    return found


def _assert_nothing_internal(response):
    keys = _keys(response.json())
    assert not keys & FORBIDDEN_KEYS, keys & FORBIDDEN_KEYS
    assert not [key for key in keys if key.endswith("_id")]
    assert str(CUSTOMER) not in response.text and str(BRANCH) not in response.text


# --- who -----------------------------------------------------------------------------------------------------------

def test_a_signed_in_user_who_is_not_a_platform_admin_is_403_and_nothing_is_read(api, monkeypatch):
    world = World(monkeypatch, admin=False)

    response = api.get(PATH, headers=COOKIE)

    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert world.reads == []


def test_no_session_is_401(api, monkeypatch):
    World(monkeypatch)

    response = api.get(PATH)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_the_route_is_read_only(api, monkeypatch, method):
    World(monkeypatch)

    response = getattr(api, method)(PATH, headers=COOKIE)

    assert response.status_code in (403, 405)    # the admin Origin guard, or no such method: never a write
    assert response.status_code != 200


# --- the v2 era ----------------------------------------------------------------------------------------------------

def test_a_cut_over_site_gives_its_v2_heartbeat_and_no_legacy_row(api, monkeypatch):
    world = World(monkeypatch)

    response = api.get(PATH, headers=COOKIE)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["timezone"] == "America/Chicago"
    assert body["library"] == {"slug": "nbpl", "name": "New Braunfels Public Library"}
    assert body["sorter"] == {
        "slug": "main", "name": "Main AMH", "host_branch": {"slug": "main", "name": "Main Branch"},
        "status": "active", "collector_count": 1, "onboarding_status": "ready",
    }
    assert body["pipeline"] == {"state": "degraded", "last_reported_at": "2026-10-14T15:30:00Z"}
    assert body["contract"] == "v2"
    assert body["cutover_at"] == "2026-09-24T15:30:00Z"
    assert body["legacy"] is None
    status = body["v2"]["status"]
    assert set(status) == V2_FIELDS
    assert status["last_error_class"] == "retryable_infra"
    assert (status["pending_outbox_count"], status["quarantined_count"]) == (12, 3)
    assert status["collector_run_duration_ms"] == 4500
    assert status["collector_next_run_at"] == "2026-10-14T15:43:00Z"
    assert world.reads == ["pipeline", "cutover", "v2"]           # the legacy row of a cut-over site is never read
    _assert_nothing_internal(response)


def test_a_cut_over_site_with_no_active_key_says_so(api, monkeypatch):
    world = World(monkeypatch)
    world.heartbeat = None

    body = api.get(PATH, headers=COOKIE).json()

    assert body["contract"] == "v2"
    assert body["v2"] == {"status": None}


# --- the legacy era ----------------------------------------------------------------------------------------------

def test_a_legacy_site_gives_its_legacy_report_and_no_v2_heartbeat(api, monkeypatch):
    world = World(monkeypatch, cutover=None)

    response = api.get(PATH, headers=COOKIE)

    body = response.json()
    assert body["contract"] == "legacy"
    assert body["cutover_at"] is None
    assert body["v2"] is None
    assert set(body["legacy"]["status"]) == LEGACY_FIELDS
    assert body["legacy"]["status"]["run_status"] == "completed"
    assert body["legacy"]["status"]["checkins_rows"] == 120
    assert world.reads == ["pipeline", "cutover", "legacy"]
    _assert_nothing_internal(response)


def test_a_future_cutover_is_shown_but_the_site_is_still_legacy(api, monkeypatch):
    future = datetime.now(UTC) + timedelta(days=3)
    World(monkeypatch, cutover=future)

    body = api.get(PATH, headers=COOKIE).json()

    assert body["contract"] == "legacy"
    assert body["cutover_at"] is not None
    assert body["v2"] is None


def test_a_legacy_site_without_a_row_says_so(api, monkeypatch):
    world = World(monkeypatch, cutover=None)
    world.legacy = None

    body = api.get(PATH, headers=COOKIE).json()

    assert body["legacy"] == {"status": None}


# --- no identity, not found, failures ------------------------------------------------------------------------------

def test_a_site_with_no_operational_identity_reads_nothing(api, monkeypatch):
    world = World(monkeypatch)
    world.tenants = {}

    body = api.get(PATH, headers=COOKIE).json()

    assert body["contract"] == "unknown"
    assert body["pipeline"] == {"state": "unknown", "last_reported_at": None}
    assert (body["cutover_at"], body["v2"], body["legacy"]) == (None, None, None)
    assert world.reads == []


@pytest.mark.parametrize(
    "path", ["/api/admin/libraries/nobody/sorters/main/diagnostics", "/api/admin/libraries/nbpl/sorters/annex/diagnostics"]
)
def test_an_unknown_library_or_sorter_is_the_same_404_and_nothing_is_read(api, monkeypatch, path):
    world = World(monkeypatch)

    response = api.get(path, headers=COOKIE)

    assert response.status_code == 404
    assert response.json() == SORTER_NOT_FOUND
    assert response.headers["cache-control"] == "no-store"
    assert world.reads == []


def test_a_failing_read_is_the_generic_server_error(api, monkeypatch):
    World(monkeypatch)

    def failing(conn, tenant):
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(operational_read_service, "get_latest_ingest_status", failing)

    response = api.get(PATH, headers=COOKIE)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert "synthetic" not in response.text


# --- the deep facts are served here and nowhere a customer can reach -----------------------------------------------

def test_no_customer_response_model_carries_a_heartbeat_or_legacy_diagnostic_field():
    import importlib
    import inspect
    import pkgutil

    from pydantic import BaseModel

    import customer_api

    models = []
    for module_info in pkgutil.iter_modules(customer_api.__path__):
        if module_info.name.endswith("_schemas"):
            module = importlib.import_module(f"customer_api.{module_info.name}")
            models += [obj for _, obj in inspect.getmembers(module, inspect.isclass)
                       if issubclass(obj, BaseModel) and obj.__module__ == module.__name__]
    assert len(models) > 20                                     # the scan really sees the customer schemas

    for model in models:
        assert not set(model.model_fields) & (V2_FIELDS | LEGACY_FIELDS), model.__name__


def test_no_customer_route_serves_ingest_status_or_diagnostics():
    paths = {route.path for route in main.app.routes if not route.path.startswith("/api/admin/")}

    assert not [path for path in paths if "ingest-status" in path or "diagnostic" in path]
