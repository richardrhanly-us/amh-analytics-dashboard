"""A2 item 4: a library's collector configuration in the administration API.

    GET/PUT /api/admin/libraries/{org_slug}/collector-settings
    GET/PUT /api/admin/libraries/{org_slug}/internal-routing

Driven through TestClient(main.app), with the tenant service replaced at the flat identity the routes use: who may
call, what may reach the browser, what a refusal says, and that no Internal Routing entry is ever echoed or logged.
The validator is tested in tests/test_internal_routing.py; the real statements, the audit rows and the v1/v2 effects
on a real server in tests/test_admin_collector_config_postgres.py. The service's audit record is checked here too,
on a recording connection.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

import main
from services import (
    collector_rules,
    internal_routing,
    platform_admin_service,
    session_service,
    tenant_service,
)

COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 7, "email": "admin@example.invalid", "full_name": "Test Admin"}
ADMIN_ORIGIN = "https://admin.sortview.example"
CUSTOMER_ORIGIN = "https://app.sortview.example"
SETTINGS_PATH = "/api/admin/libraries/acme/collector-settings"
ROUTING_PATH = "/api/admin/libraries/acme/internal-routing"
PATHS = (SETTINGS_PATH, ROUTING_PATH)

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have access to SortView administration."}
ORIGIN_NOT_ALLOWED = {"code": "origin_not_allowed", "message": "Request origin is not allowed."}
LIBRARY_NOT_FOUND = {"code": "library_not_found", "message": "Library not found."}
LIBRARY_CANCELLED = {"code": "library_cancelled", "message": "This library is cancelled; its settings cannot be changed."}

# A distinctive entry: whatever a test does with it, it must never come back in an error, an audit record or a log.
CANARY = "CANARY-ROUTING-7Q4Z"
ORGANIZATION_ID = 90417  # distinctive, so a leak is caught by value
ACME = {"id": ORGANIZATION_ID, "name": "Acme Library", "slug": "acme", "status": "active"}
MARKER = {"reviewed_at": "2026-10-11T15:04:05+00:00", "reviewed_by_user_id": 7}
SITE = {
    "collector_timezone": "America/Chicago",
    "collector_rules": MARKER,
    "home_branch_label": "Central",
    "transit_destination_labels": ["North Branch", "Bookmobile"],
    "internal_routing_counts": {"branch_services_names": 2, "collection_services_names": 1,
                                "branch_services_da_patterns": 0, "collection_services_da_patterns": 3},
}
LISTS = {
    "branch_services_names": ["Branch Programs", CANARY],
    "collection_services_names": ["Cataloging"],
    "branch_services_da_patterns": [],
    "collection_services_da_patterns": ["DAONE", "DATWO", "DATHREE"],
}
GOOD_ROUTING = {name: [] for name in internal_routing.LISTS}
FORBIDDEN_KEYS = {"id", "organization_id", "customer_id", "branch_id", "key_id", "installation_id", "hostname",
                  "collector_version", "settings", "settings_json", "internal_routing", "reviewed_by_user_id",
                  "transit", "destinations"}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", CUSTOMER_ORIGIN)
    monkeypatch.setenv("SORTVIEW_ADMIN_ALLOWED_ORIGINS", ADMIN_ORIGIN)
    monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


class World:
    """The session, the platform flag and the tenant service, recording every write."""

    def __init__(self, monkeypatch, *, admin=True, user=USER, organizations=None):
        self.organizations = {"acme": ACME} if organizations is None else organizations
        self.site = dict(SITE)
        self.lists = {name: list(values) for name, values in LISTS.items()}
        self.cancelled = False
        self.writes: list[tuple[str, dict]] = []
        monkeypatch.setattr(session_service, "validate_session", lambda token: None if user is None else dict(user))
        monkeypatch.setattr(platform_admin_service, "is_platform_admin", lambda user_id: admin)
        monkeypatch.setattr(tenant_service, "get_organization_by_slug", lambda slug: self.organizations.get(slug))
        monkeypatch.setattr(tenant_service, "get_collector_site_settings", self._site)
        monkeypatch.setattr(tenant_service, "get_internal_routing", self._lists)
        monkeypatch.setattr(tenant_service, "set_collector_enrollment_settings", self._set_settings)
        monkeypatch.setattr(tenant_service, "replace_internal_routing", self._replace)

    def _site(self, organization_id):
        assert organization_id == ORGANIZATION_ID
        return dict(self.site)

    def _lists(self, organization_id):
        assert organization_id == ORGANIZATION_ID
        return {name: list(values) for name, values in self.lists.items()}

    def _set_settings(self, organization_id, *, timezone, target_org_slug, actor, reviewed_at=None):
        zone = collector_rules.collector_timezone({"collector_timezone": timezone})  # the real check
        if self.cancelled:
            raise tenant_service.OrganizationCancelledError("cancelled")
        self.writes.append(("settings", {"organization_id": organization_id, "timezone": zone,
                                         "target_org_slug": target_org_slug, "actor": actor}))
        self.site["collector_timezone"] = zone
        return {}

    def _replace(self, organization_id, lists, *, target_org_slug, actor):
        if self.cancelled:
            raise tenant_service.OrganizationCancelledError("cancelled")
        self.writes.append(("routing", {"organization_id": organization_id, "lists": lists,
                                        "target_org_slug": target_org_slug, "actor": actor}))
        self.lists = {name: list(values) for name, values in lists.items()}
        return lists


def _put(api, path, body, *, origin=ADMIN_ORIGIN):
    headers = dict(COOKIE)
    if origin is not None:
        headers["Origin"] = origin
    return api.put(path, json=body, headers=headers)


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


SETTINGS_BODY = {"collector_timezone": "America/New_York", "rules_reviewed": True}


# =====================================================================================================================
# who may call
# =====================================================================================================================

@pytest.mark.parametrize("path", PATHS)
def test_no_session_is_401_for_every_route_and_nothing_is_read_or_written(api, monkeypatch, path):
    world = World(monkeypatch, user=None)

    assert api.get(path, headers=COOKIE).json() == NOT_AUTHENTICATED
    response = _put(api, path, SETTINGS_BODY if path == SETTINGS_PATH else GOOD_ROUTING)
    assert response.status_code == 401 and response.json() == NOT_AUTHENTICATED
    assert world.writes == []


@pytest.mark.parametrize("path", PATHS)
def test_a_member_or_an_organization_owner_who_is_not_a_platform_admin_is_403(api, monkeypatch, path):
    # The decision is the platform flag alone: an owner of "acme" is refused like anyone else.
    world = World(monkeypatch, admin=False)

    get = api.get(path, headers=COOKIE)
    put = _put(api, path, SETTINGS_BODY if path == SETTINGS_PATH else GOOD_ROUTING)

    assert (get.status_code, get.json()) == (403, FORBIDDEN)
    assert (put.status_code, put.json()) == (403, FORBIDDEN)
    assert world.writes == []


@pytest.mark.parametrize("origin", [None, "https://evil.example", CUSTOMER_ORIGIN])
@pytest.mark.parametrize("path", PATHS)
def test_a_write_without_the_admin_origin_is_refused_before_anything_is_written(api, monkeypatch, path, origin):
    world = World(monkeypatch)

    response = _put(api, path, SETTINGS_BODY if path == SETTINGS_PATH else GOOD_ROUTING, origin=origin)

    assert (response.status_code, response.json()) == (403, ORIGIN_NOT_ALLOWED)
    assert world.writes == []


@pytest.mark.parametrize("path", PATHS)
def test_an_unknown_library_is_404_for_every_route(api, monkeypatch, path):
    world = World(monkeypatch, organizations={})

    get = api.get(path, headers=COOKIE)
    put = _put(api, path, SETTINGS_BODY if path == SETTINGS_PATH else GOOD_ROUTING)

    assert (get.status_code, get.json()) == (404, LIBRARY_NOT_FOUND)
    assert (put.status_code, put.json()) == (404, LIBRARY_NOT_FOUND)
    assert world.writes == []


@pytest.mark.parametrize("path", PATHS)
def test_a_cancelled_librarys_settings_are_409_to_change_and_still_readable(api, monkeypatch, path):
    world = World(monkeypatch)
    world.cancelled = True

    assert api.get(path, headers=COOKIE).status_code == 200
    response = _put(api, path, SETTINGS_BODY if path == SETTINGS_PATH else GOOD_ROUTING)

    assert (response.status_code, response.json()) == (409, LIBRARY_CANCELLED)
    assert response.headers["cache-control"] == "no-store"
    assert world.writes == []


@pytest.mark.parametrize("method", ["post", "patch", "delete"])
@pytest.mark.parametrize("path", PATHS)
def test_only_get_and_put_are_routes(api, monkeypatch, path, method):
    World(monkeypatch)

    response = getattr(api, method)(path, headers={**COOKIE, "Origin": ADMIN_ORIGIN})

    assert response.status_code == 405


# =====================================================================================================================
# collector settings
# =====================================================================================================================

def test_the_collector_settings_are_exactly_these_fields(api, monkeypatch):
    World(monkeypatch)

    response = api.get(SETTINGS_PATH, headers=COOKIE)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "library": {"slug": "acme", "name": "Acme Library"},
        "collector_timezone": "America/Chicago",
        "rules_reviewed_at": "2026-10-11T15:04:05Z",
        "home_branch_label": "Central",
        "transit_destination_labels": ["North Branch", "Bookmobile"],
        "internal_routing_counts": SITE["internal_routing_counts"],
        "enrollment_problems": [],
    }
    assert not _keys(response.json()) & FORBIDDEN_KEYS


@pytest.mark.parametrize(("zone", "marker", "problems"), [
    (None, None, ["timezone_not_configured", "rules_not_reviewed"]),
    ("  ", MARKER, ["timezone_not_configured"]),
    ("Mars/Olympus_Mons", MARKER, ["timezone_invalid"]),
    ("America/Chicago", {"reviewed_at": "2026-10-11T15:04:05", "reviewed_by_user_id": 7}, ["rules_not_reviewed"]),
])
def test_the_enrollment_problems_are_what_the_enrollment_itself_would_refuse_for(api, monkeypatch, zone, marker, problems):
    world = World(monkeypatch)
    world.site.update({"collector_timezone": zone, "collector_rules": marker})

    body = api.get(SETTINGS_PATH, headers=COOKIE).json()

    assert body["enrollment_problems"] == problems
    if "rules_not_reviewed" in problems:
        assert body["rules_reviewed_at"] is None


def test_a_put_records_the_time_zone_and_the_review_by_this_admin_and_answers_with_the_new_settings(api, monkeypatch):
    world = World(monkeypatch)

    response = _put(api, SETTINGS_PATH, SETTINGS_BODY)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["collector_timezone"] == "America/New_York"
    assert world.writes == [("settings", {"organization_id": ORGANIZATION_ID, "timezone": "America/New_York",
                                          "target_org_slug": "acme",
                                          "actor": {"id": 7, "email": "admin@example.invalid"}})]


@pytest.mark.parametrize(("zone", "code"), [
    ("", "required"),
    ("   ", "required"),
    ("Mars/Olympus_Mons", "timezone_invalid"),
    ("America/" + "X" * 60, "timezone_invalid"),
])
def test_a_time_zone_that_is_blank_unknown_or_too_long_is_422_naming_the_field(api, monkeypatch, zone, code):
    world = World(monkeypatch)

    response = _put(api, SETTINGS_PATH, {"collector_timezone": zone, "rules_reviewed": True})

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_collector_settings", "message": "The collector settings are not valid.",
                               "problems": [{"field": "collector_timezone", "code": code}]}
    assert world.writes == []


@pytest.mark.parametrize("body", [
    {"collector_timezone": "America/Chicago", "rules_reviewed": False},
    {"collector_timezone": "America/Chicago"},
    {"collector_timezone": 5, "rules_reviewed": True},
    {"collector_timezone": "America/Chicago", "rules_reviewed": True, "transit": {"destinations": []}},
    {"collector_timezone": "America/Chicago", "rules_reviewed": True, "internal_routing": {}},
])
def test_a_put_without_the_confirmation_or_with_any_other_field_is_422_and_writes_nothing(api, monkeypatch, body):
    world = World(monkeypatch)

    response = _put(api, SETTINGS_PATH, body)

    assert response.status_code == 422
    assert world.writes == []


# =====================================================================================================================
# Internal Routing
# =====================================================================================================================

def test_the_internal_routing_answer_is_exactly_the_library_and_the_four_lists(api, monkeypatch):
    World(monkeypatch)

    response = api.get(ROUTING_PATH, headers=COOKIE)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"library": {"slug": "acme", "name": "Acme Library"}, **LISTS}


def test_a_put_stores_the_normalized_lists_and_answers_with_them(api, monkeypatch):
    world = World(monkeypatch)
    body = {
        "branch_services_names": ["  Branch Programs ", "", "branch programs", "Outreach", "   "],
        "collection_services_names": ["Cataloging", "CATALOGING", "Mending"],
        "branch_services_da_patterns": [],
        "collection_services_da_patterns": ["DaOne", "DATWO"],
    }

    response = _put(api, ROUTING_PATH, body)

    expected = {
        "branch_services_names": ["Branch Programs", "Outreach"],
        "collection_services_names": ["Cataloging", "Mending"],
        "branch_services_da_patterns": [],
        "collection_services_da_patterns": ["DaOne", "DATWO"],
    }
    assert response.status_code == 200
    assert response.json() == {"library": {"slug": "acme", "name": "Acme Library"}, **expected}
    assert world.writes == [("routing", {"organization_id": ORGANIZATION_ID, "lists": expected, "target_org_slug": "acme",
                                         "actor": {"id": 7, "email": "admin@example.invalid"}})]


@pytest.mark.parametrize(("entries", "field", "code"), [
    (["fine", CANARY + "x" * 200], "branch_services_names.1", "too_long"),
    ([CANARY + "\x07"], "branch_services_names.0", "control_character"),
    ([CANARY + "|DA"], "branch_services_names.0", "separator_character"),
    (["fine", 42], "branch_services_names.1", "not_text"),
    (["x"] * 5001, "branch_services_names", "too_many"),
])
def test_a_refused_entry_is_422_with_its_field_and_code_and_is_never_echoed_or_logged(api, monkeypatch, caplog, entries, field, code):
    world = World(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        response = _put(api, ROUTING_PATH, {**GOOD_ROUTING, "branch_services_names": entries})

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": "invalid_internal_routing", "message": "The Internal Routing lists are not valid.",
                               "problems": [{"field": field, "code": code}]}
    assert CANARY not in response.text and CANARY not in caplog.text
    assert world.writes == []


@pytest.mark.parametrize("body", [
    {name: [] for name in internal_routing.LISTS[:3]},
    {**GOOD_ROUTING, "transit": {}},
    {**GOOD_ROUTING, "branch_services_names": "not a list"},
])
def test_a_put_missing_a_list_or_carrying_another_field_is_422_and_writes_nothing(api, monkeypatch, body):
    world = World(monkeypatch)

    response = _put(api, ROUTING_PATH, body)

    assert response.status_code == 422
    assert world.writes == []


def test_a_successful_put_logs_counts_only(api, monkeypatch, caplog):
    World(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        response = _put(api, ROUTING_PATH, LISTS)

    assert response.status_code == 200
    assert CANARY not in caplog.text and "Branch Programs" not in caplog.text
    assert "Admin Internal Routing replaced | org=acme user_id=7" in caplog.text


# =====================================================================================================================
# the service's audit record: counts only, under target_org_slug, in the writing transaction
# =====================================================================================================================

class RecordingEngine:
    """Stands in for the database: the organization's stored document is read, and every statement is recorded."""

    def __init__(self, document, status="active"):
        self.document, self.status, self.statements = document, status, []

    @contextmanager
    def begin(self):
        yield self

    def execute(self, statement, parameters):
        self.statements.append((str(statement), parameters))
        engine = self

        class Result:
            def mappings(self):
                return self

            def first(self):
                if "SELECT o.status" in str(statement):
                    return {"organization_status": engine.status, "settings_json": engine.document}
                return {"organization_id": ORGANIZATION_ID}

        return Result()


def test_replacing_the_lists_is_audited_with_counts_only_and_never_with_org_slug(monkeypatch):
    engine = RecordingEngine({"internal_routing": LISTS, "transit": {"home_branch_label": "Central"}})
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)
    new = {**GOOD_ROUTING, "branch_services_names": ["Outreach"]}

    tenant_service.replace_internal_routing(ORGANIZATION_ID, new, target_org_slug="acme", actor={"id": 7, "email": "a@x.invalid"})

    audit = [parameters for statement, parameters in engine.statements if "auth_audit_log" in statement]
    assert len(audit) == 1
    record = audit[0]
    assert record["event_type"] == "admin_internal_routing_replaced"
    assert record["message"] == "Internal Routing replaced."
    assert json.loads(record["metadata"]) == {
        "target_org_slug": "acme",
        "previous_counts": {"branch_services_names": 2, "collection_services_names": 1,
                            "branch_services_da_patterns": 0, "collection_services_da_patterns": 3},
        "counts": {"branch_services_names": 1, "collection_services_names": 0,
                   "branch_services_da_patterns": 0, "collection_services_da_patterns": 0},
        "actor_user_id": 7,
    }
    assert CANARY not in json.dumps(record) and "Outreach" not in json.dumps(record)
    # The write is to the internal_routing key alone, after the audit's read, in one transaction.
    writes = [statement for statement, _ in engine.statements if "jsonb_set" in statement]
    assert len(writes) == 1 and "'{internal_routing}'" in writes[0]


def test_the_collector_settings_are_audited_with_the_time_zones_and_the_review(monkeypatch):
    engine = RecordingEngine({"collector_timezone": "America/Chicago", "internal_routing": LISTS})
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    stored = tenant_service.set_collector_enrollment_settings(
        ORGANIZATION_ID, timezone="America/New_York", target_org_slug="acme", actor={"id": 7, "email": "a@x.invalid"},
        reviewed_at=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
    )

    assert stored == {"collector_timezone": "America/New_York",
                      "collector_rules": {"reviewed_at": "2026-10-15T12:00:00+00:00", "reviewed_by_user_id": 7}}
    (record,) = [parameters for statement, parameters in engine.statements if "auth_audit_log" in statement]
    assert record["event_type"] == "admin_collector_settings_updated"
    assert json.loads(record["metadata"]) == {"target_org_slug": "acme", "previous_collector_timezone": "America/Chicago",
                                              "collector_timezone": "America/New_York", "rules_reviewed": True,
                                              "actor_user_id": 7}
    assert CANARY not in json.dumps(record)
    (write,) = [parameters for statement, parameters in engine.statements if "||" in statement]
    assert set(json.loads(write["keys"])) == {"collector_timezone", "collector_rules"}


def test_a_cancelled_organization_is_refused_before_anything_is_written_or_audited(monkeypatch):
    engine = RecordingEngine({}, status="cancelled")
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(tenant_service.OrganizationCancelledError):
        tenant_service.replace_internal_routing(ORGANIZATION_ID, GOOD_ROUTING, target_org_slug="acme", actor={"id": 7})
    with pytest.raises(tenant_service.OrganizationCancelledError):
        tenant_service.set_collector_enrollment_settings(ORGANIZATION_ID, timezone="UTC", target_org_slug="acme", actor={"id": 7})

    assert all("SELECT o.status" in statement for statement, _ in engine.statements)


# =====================================================================================================================
# the customer boundary
# =====================================================================================================================

def test_no_customer_response_or_request_model_has_an_internal_routing_or_collector_setting_field():
    import importlib
    import inspect
    import pkgutil

    import customer_api

    named = set(internal_routing.LISTS) | {"internal_routing", "internal_routing_counts", "collector_timezone",
                                           "collector_rules", "rules_reviewed_at"}
    found = []
    for module_info in pkgutil.iter_modules(customer_api.__path__):
        module = importlib.import_module(f"customer_api.{module_info.name}")
        for _, model in inspect.getmembers(module, inspect.isclass):
            if issubclass(model, BaseModel) and model.__module__ == module.__name__:
                found += [f"{model.__name__}.{field}" for field in model.model_fields if field in named]
    assert found == []


def test_no_route_outside_the_admin_api_serves_collector_settings_or_internal_routing():
    paths = [route.path for route in main.app.routes if hasattr(route, "path")]
    outside = [path for path in paths if not path.startswith("/api/admin/")
               and ("internal-routing" in path or "internal_routing" in path or "collector-settings" in path)]
    assert outside == []
    assert {"/api/admin/libraries/{org_slug}/collector-settings", "/api/admin/libraries/{org_slug}/internal-routing"} <= set(paths)
