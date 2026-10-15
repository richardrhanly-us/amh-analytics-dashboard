"""A2 item 3: the customer API no longer has an ingest-status route.

    GET /api/organizations/{org_slug}/branches/{branch_slug}/ingest-status   -- REMOVED

It returned a branch's full Contract v2 heartbeat -- error class, outbox and quarantine counts, schedule, run times --
to any member of the organization, whatever their role. That detail is now served only to platform administrators
(GET /api/admin/libraries/{org_slug}/sorters/{sorter_slug}/diagnostics, tests/test_admin_diagnostics.py). What a
customer sees of the pipeline is /pipeline-status: a state and when it was reported
(tests/test_customer_api_pipeline_status.py).

The heartbeat read itself, services.operational_read_service.get_latest_ingest_status, is unchanged and tested in
tests/test_operational_read_service.py; the admin route uses it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
from customer_api import operational_routes, operational_schemas
from services import (
    access_service,
    entitlement_service,
    operational_read_service,
    session_service,
    tenant_resolution_service,
)

PATH = "/api/organizations/acme/branches/main/ingest-status"
COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
HEARTBEAT_FIELDS = (
    "health_status", "last_error_class", "pending_outbox_count", "quarantined_count", "oldest_pending_event_at",
    "last_success_at", "watcher_last_active_at", "last_heartbeat_at", "collector_last_run_at", "collector_next_run_at",
    "collector_run_duration_ms", "collector_schedule_status",
)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    # A signed-in member of acme, whatever role; nothing about the tenant, and nothing operational, may be read.
    monkeypatch.setattr(session_service, "validate_session", lambda token: {"id": 1, "email": "a@example.invalid", "full_name": ""})
    for module, name in ((tenant_resolution_service, "resolve_operational_tenant"),
                         (access_service, "get_user_memberships"),
                         (entitlement_service, "get_org_role_for_user"),
                         (operational_read_service, "get_latest_ingest_status")):
        monkeypatch.setattr(module, name, lambda *args, _name=name, **kwargs: pytest.fail(f"{_name} was called"))
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


@pytest.mark.parametrize("headers", [COOKIE, {}])
def test_the_old_path_is_not_found_for_anyone_and_says_nothing_of_a_heartbeat(api, headers):
    response = api.get(PATH, headers=headers)

    assert response.status_code == 404
    for field in HEARTBEAT_FIELDS:
        assert field not in response.text, field


def test_no_customer_route_is_an_ingest_status_route():
    assert not [route.path for route in main.app.routes if route.path.endswith("/ingest-status")]
    assert "ingest-status" not in "".join(route.path for route in main.customer_router.routes)


def test_the_customer_api_holds_no_heartbeat_model_and_no_route_reads_the_heartbeat():
    assert not hasattr(operational_schemas, "IngestStatusFields")
    assert not hasattr(operational_schemas, "IngestStatusResponse")
    assert not hasattr(operational_routes, "get_latest_ingest_status")
