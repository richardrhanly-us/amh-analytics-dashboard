"""A2 item 2: the administration API's read-only libraries.

    GET /api/admin/libraries
    GET /api/admin/libraries/{org_slug}

Driven through TestClient(main.app), with every service replaced at the FLAT module identity the routes use. Which
stored source speaks for a site (Contract v2 heartbeat or legacy report) is services.pipeline_status_service's own
decision, tested in tests/test_pipeline_status_service.py and, end to end through these routes, in
tests/test_admin_library_status_postgres.py. Here: which sites are read, under which tenant, and what is returned.
"""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import main
from customer_api import tenant_scope
from services import (
    admin_library_service,
    pipeline_status_service,
    platform_admin_service,
    session_service,
    sorter_inventory_service,
)
from services.admin_library_service import LibraryRow
from services.pipeline_status_service import PipelineStatus
from services.sorter_inventory_service import SorterSite
from services.tenant_resolution_service import ResolvedOperationalTenant

COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 7, "email": "admin@example.invalid", "full_name": "Test Admin"}
FORBIDDEN = {"code": "forbidden", "message": "You do not have access to SortView administration."}
NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
LIBRARY_NOT_FOUND = {"code": "library_not_found", "message": "Library not found."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

# Operational ids are distinctive so a leak of any of them into a response is caught by value, not only by key.
NBPL_CUSTOMER, NBPL_BRANCH = 90417, 90523
HEARD_AT = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)

NBPL = LibraryRow(
    slug="nbpl",
    name="New Braunfels Public Library",
    status="active",
    primary_branch_slug="main",
    primary_branch_name="Main Branch",
    primary_branch_status="active",
    operational_identity="assigned",
    plan_code="enterprise",
    plan_name="Enterprise",
    subscription_status="trial",
)
PENDING = LibraryRow(
    slug="pending",
    name="Pending Library",
    status="trial",
    primary_branch_slug="central",
    primary_branch_name="Central",
    primary_branch_status="active",
    operational_identity="missing",
    plan_code=None,
    plan_name=None,
    subscription_status=None,
)


def _site(slug, name, *, status="active", collectors=1, onboarding="ready"):
    return SorterSite(
        slug=slug,
        name=name,
        host_branch_slug=slug,
        host_branch_name=slug.title(),
        status=status,
        collector_count=collectors,
        onboarding_status=onboarding,
    )


@pytest.fixture
def api(monkeypatch):
    monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


class World:
    """The libraries, their sites, their tenants and what each tenant last reported."""

    def __init__(self, monkeypatch, *, admin=True):
        self.rows = [NBPL]
        self.sites = {"nbpl": [_site("main", "Main AMH")]}
        self.tenants = {("nbpl", "main"): (NBPL_CUSTOMER, NBPL_BRANCH)}
        self.statuses = {(NBPL_CUSTOMER, NBPL_BRANCH): PipelineStatus(state="degraded", last_reported_at=HEARD_AT)}
        self.status_reads: list[tuple[int, int]] = []
        self.open_scopes: list[tuple[int, int]] = []
        monkeypatch.setattr(session_service, "validate_session", lambda token: dict(USER))
        monkeypatch.setattr(platform_admin_service, "is_platform_admin", lambda user_id: admin)
        monkeypatch.setattr(admin_library_service, "list_library_rows", lambda: list(self.rows))
        monkeypatch.setattr(
            admin_library_service, "get_library_row", lambda slug: next((r for r in self.rows if r.slug == slug), None)
        )
        monkeypatch.setattr(admin_library_service, "site_tenant", self._site_tenant)
        monkeypatch.setattr(sorter_inventory_service, "list_sorter_sites", lambda slug: list(self.sites.get(slug, [])))
        monkeypatch.setattr(tenant_scope, "open_customer_tenant_connection", self._open)
        monkeypatch.setattr(pipeline_status_service, "get_pipeline_status", self._status)

    def _site_tenant(self, org_slug, branch_slug):
        ids = self.tenants.get((org_slug, branch_slug))
        if ids is None:
            return None
        return ResolvedOperationalTenant(
            org_slug=org_slug, branch_slug=branch_slug, access_mode="full",
            operational_customer_id=ids[0], operational_branch_id=ids[1],
        )

    @contextmanager
    def _open(self, tenant):
        scope = (tenant.operational_customer_id, tenant.operational_branch_id)
        self.open_scopes.append(scope)
        yield scope

    def _status(self, conn, tenant, *, now):
        scope = (tenant.operational_customer_id, tenant.operational_branch_id)
        # The status is read on the connection scoped to that very tenant, with an aware "now".
        assert conn == scope
        assert now.tzinfo is not None
        self.status_reads.append(scope)
        return self.statuses.get(scope, PipelineStatus(state="unknown", last_reported_at=None))


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


FORBIDDEN_KEYS = {
    "id", "settings", "notes", "contact_name", "contact_email", "system_name", "hostname", "collector_version",
    "installation_id", "last_error_class", "pending_outbox_count", "quarantined_count", "oldest_pending_event_at",
    "collector_schedule_status", "collector_next_run_at", "collector_run_duration_ms", "last_attempt",
    "last_heartbeat_at", "watcher_last_active_at", "key_id", "entitlements", "is_platform_admin",
}


def _assert_nothing_internal(response):
    keys = _keys(response.json())
    assert not keys & FORBIDDEN_KEYS, keys & FORBIDDEN_KEYS
    assert not [key for key in keys if key.endswith("_id")]
    assert str(NBPL_CUSTOMER) not in response.text and str(NBPL_BRANCH) not in response.text


# --- who ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/admin/libraries", "/api/admin/libraries/nbpl"])
def test_a_signed_in_user_who_is_not_a_platform_admin_is_403_and_nothing_is_read(api, monkeypatch, path):
    world = World(monkeypatch, admin=False)
    monkeypatch.setattr(admin_library_service, "list_library_rows", lambda: pytest.fail("read for a non-admin"))
    monkeypatch.setattr(admin_library_service, "get_library_row", lambda slug: pytest.fail("read for a non-admin"))

    response = api.get(path, headers=COOKIE)

    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert world.status_reads == []


@pytest.mark.parametrize("path", ["/api/admin/libraries", "/api/admin/libraries/nbpl"])
def test_no_session_is_401(api, monkeypatch, path):
    World(monkeypatch)

    response = api.get(path)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


# --- the list ------------------------------------------------------------------------------------------------------

def test_the_list_gives_each_library_and_its_primary_sorters_current_state(api, monkeypatch):
    world = World(monkeypatch)

    response = api.get("/api/admin/libraries", headers=COOKIE)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "timezone": "America/Chicago",
        "libraries": [
            {
                "slug": "nbpl",
                "name": "New Braunfels Public Library",
                "status": "active",
                "primary_branch": {"slug": "main", "name": "Main Branch", "status": "active"},
                "subscription": {"plan_code": "enterprise", "plan_name": "Enterprise", "status": "trial"},
                "sorter_count": 1,
                "primary_sorter": {
                    "onboarding_status": "ready",
                    "pipeline": {"state": "degraded", "last_reported_at": "2026-10-14T15:30:00Z"},
                },
            }
        ],
    }
    # The state came from the shared service, on the connection scoped to NBPL's own operational tenant.
    assert world.open_scopes == [(NBPL_CUSTOMER, NBPL_BRANCH)]
    assert world.status_reads == [(NBPL_CUSTOMER, NBPL_BRANCH)]
    _assert_nothing_internal(response)


def test_an_empty_platform_lists_nothing(api, monkeypatch):
    world = World(monkeypatch)
    world.rows = []

    response = api.get("/api/admin/libraries", headers=COOKIE)

    assert response.status_code == 200
    assert response.json()["libraries"] == []


def test_a_library_with_no_sorter_has_no_primary_sorter_and_nothing_is_read(api, monkeypatch):
    world = World(monkeypatch)
    world.rows = [PENDING]

    (library,) = api.get("/api/admin/libraries", headers=COOKIE).json()["libraries"]

    assert library["sorter_count"] == 0
    assert library["primary_sorter"] is None
    assert library["subscription"] is None
    assert world.status_reads == []


def test_a_waiting_site_with_no_operational_identity_is_unknown_and_is_not_read(api, monkeypatch):
    world = World(monkeypatch)
    world.rows = [PENDING]
    world.sites = {"pending": [_site("central", "Central AMH", status="provisioning", onboarding="pending")]}

    (library,) = api.get("/api/admin/libraries", headers=COOKIE).json()["libraries"]

    assert library["primary_sorter"] == {
        "onboarding_status": "pending",
        "pipeline": {"state": "unknown", "last_reported_at": None},
    }
    assert world.open_scopes == [] and world.status_reads == []


def test_several_sites_are_never_merged_the_list_shows_the_primary_sites_state(api, monkeypatch):
    world = World(monkeypatch)
    world.sites = {"nbpl": [_site("main", "Main AMH"), _site("annex", "Annex AMH")]}
    world.tenants[("nbpl", "annex")] = (90418, 90524)
    world.statuses[(90418, 90524)] = PipelineStatus(state="failed", last_reported_at=HEARD_AT)

    (library,) = api.get("/api/admin/libraries", headers=COOKIE).json()["libraries"]

    # The annex has failed; the primary site has not. The list says the primary site's state and how many sites.
    assert library["sorter_count"] == 2
    assert library["primary_sorter"]["pipeline"]["state"] == "degraded"
    assert world.status_reads == [(NBPL_CUSTOMER, NBPL_BRANCH)]


def test_cancelled_and_suspended_libraries_are_listed(api, monkeypatch):
    world = World(monkeypatch)
    world.rows = [
        dataclasses.replace(NBPL, slug="gone", name="Gone", status="cancelled"),
        dataclasses.replace(NBPL, slug="paused", name="Paused", status="suspended"),
    ]

    libraries = api.get("/api/admin/libraries", headers=COOKIE).json()["libraries"]

    assert [(library["slug"], library["status"]) for library in libraries] == [("gone", "cancelled"), ("paused", "suspended")]


# --- one library -----------------------------------------------------------------------------------------------------

def test_the_overview_lists_every_site_with_its_own_state(api, monkeypatch):
    world = World(monkeypatch)
    world.sites = {"nbpl": [_site("main", "Main AMH", collectors=2), _site("annex", "Annex AMH", status="inactive", collectors=0)]}

    response = api.get("/api/admin/libraries/nbpl", headers=COOKIE)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "timezone": "America/Chicago",
        "slug": "nbpl",
        "name": "New Braunfels Public Library",
        "status": "active",
        "primary_branch": {"slug": "main", "name": "Main Branch", "status": "active"},
        "operational_identity": "assigned",
        "subscription": {"plan_code": "enterprise", "plan_name": "Enterprise", "status": "trial"},
        "sorters": [
            {
                "slug": "main",
                "name": "Main AMH",
                "host_branch": {"slug": "main", "name": "Main"},
                "status": "active",
                "collector_count": 2,
                "onboarding_status": "ready",
                "pipeline": {"state": "degraded", "last_reported_at": "2026-10-14T15:30:00Z"},
            },
            {
                "slug": "annex",
                "name": "Annex AMH",
                "host_branch": {"slug": "annex", "name": "Annex"},
                "status": "inactive",
                "collector_count": 0,
                "onboarding_status": "ready",
                "pipeline": {"state": "unknown", "last_reported_at": None},
            },
        ],
    }
    assert world.status_reads == [(NBPL_CUSTOMER, NBPL_BRANCH)]
    _assert_nothing_internal(response)


def test_an_unknown_library_is_404_and_no_site_is_read(api, monkeypatch):
    world = World(monkeypatch)

    response = api.get("/api/admin/libraries/nobody", headers=COOKIE)

    assert response.status_code == 404
    assert response.json() == LIBRARY_NOT_FOUND
    assert response.headers["cache-control"] == "no-store"
    assert world.status_reads == []


def test_the_overview_says_a_missing_operational_identity_in_words(api, monkeypatch):
    world = World(monkeypatch)
    world.rows = [PENDING]

    body = api.get("/api/admin/libraries/pending", headers=COOKIE).json()

    assert body["operational_identity"] == "missing"
    assert body["sorters"] == []


def test_a_failing_status_read_is_the_generic_server_error(api, monkeypatch):
    World(monkeypatch)

    def failing(conn, tenant, *, now):
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(pipeline_status_service, "get_pipeline_status", failing)

    for path in ("/api/admin/libraries", "/api/admin/libraries/nbpl"):
        response = api.get(path, headers=COOKIE)
        assert response.status_code == 500
        assert response.json() == INTERNAL_ERROR
        assert "synthetic" not in response.text


def test_no_customer_route_changed_or_reads_admin_libraries():
    customer_paths = {route.path for route in main.customer_router.routes}

    assert not [path for path in customer_paths if path.startswith("/api/admin")]
