"""A2 item 5: a library's Collector installations and enrollment codes in the administration API.

    GET/POST /api/admin/libraries/{org_slug}/installations
    PUT      /api/admin/libraries/{org_slug}/installations/{installation_id}
    PUT      /api/admin/libraries/{org_slug}/installations/{installation_id}/status
    POST     /api/admin/libraries/{org_slug}/installations/{installation_id}/enrollment-codes

Driven through TestClient(main.app) with services.admin_installation_service replaced at the flat identity the routes
use: who may call, the exact shapes, every refusal, and that a raw enrollment code appears in the one answer that
issues it and nowhere else. The service's own rules (transitions, names, refusal codes) are tested at the bottom; its
statements, audit rows and effects on a real server in tests/test_admin_installations_postgres.py.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

import main
from services import admin_installation_service as service
from services import platform_admin_service, session_service
from services.admin_installation_service import (
    Branch,
    InstallationAdminError,
    Library,
    PrimaryBranch,
)

COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 7, "email": "admin@example.invalid", "full_name": "Test Admin"}
ADMIN_ORIGIN = "https://admin.sortview.example"
CUSTOMER_ORIGIN = "https://app.sortview.example"
BASE = "/api/admin/libraries/acme/installations"

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have access to SortView administration."}
ORIGIN_NOT_ALLOWED = {"code": "origin_not_allowed", "message": "Request origin is not allowed."}
LIBRARY_NOT_FOUND = {"code": "library_not_found", "message": "Library not found."}

# A distinctive code: it may appear in the 201 answer that issues it, and nowhere else at all.
CANARY_CODE = "SV-CNRY-7Q4Z-WXYZ-2345"
ORGANIZATION_ID, BRANCH_ID = 90417, 90523  # internal ids, distinctive so a leak is caught by value

ACME = Library(id=ORGANIZATION_ID, slug="acme", name="Acme Library", status="active")
PRIMARY = PrimaryBranch(id=BRANCH_ID, slug="main", name="Main Branch", status="active")
CREATED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
INSTALLATION = {
    "id": 123,
    "name": "Main AMH Sorter",
    "branch": Branch(slug="main", name="Main Branch", status="active", is_primary=True),
    "status": "active",
    "required_contract": 2,
    "installed_at": CREATED + timedelta(hours=1),
    "last_seen_at": CREATED + timedelta(days=3),
    "created_at": CREATED,
    "pending_code_expires_at": None,
    "last_code_redeemed_at": CREATED + timedelta(minutes=50),
}
INSTALLATION_JSON = {
    "id": 123,
    "name": "Main AMH Sorter",
    "branch": {"slug": "main", "name": "Main Branch", "is_primary": True, "status": "active"},
    "status": "active",
    "required_contract": 2,
    "installed_at": "2026-10-01T13:00:00Z",
    "last_seen_at": "2026-10-04T12:00:00Z",
    "created_at": "2026-10-01T12:00:00Z",
    "pending_code_expires_at": None,
    "last_code_redeemed_at": "2026-10-01T12:50:00Z",
}
FORBIDDEN_KEYS = {"organization_id", "branch_id", "customer_id", "operational_branch_id", "operational_customer_id",
                  "hostname", "collector_version", "key_id", "token", "token_id", "token_hash", "code_hash",
                  "enrollment_code_id", "updated_at"}
FORBIDDEN_VALUES = (str(ORGANIZATION_ID), str(BRANCH_ID), "sorter-pc", "9.9.9")


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", CUSTOMER_ORIGIN)
    monkeypatch.setenv("SORTVIEW_ADMIN_ALLOWED_ORIGINS", ADMIN_ORIGIN)
    monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


class World:
    """The session, the platform flag and the service, recording every call. `refuse` makes the next write refuse
    with that code."""

    def __init__(self, monkeypatch, *, admin=True, user=USER, libraries=None):
        self.libraries = {"acme": ACME} if libraries is None else libraries
        self.calls: list[tuple] = []
        self.refuse: str | None = None
        monkeypatch.setattr(session_service, "validate_session", lambda token: None if user is None else dict(user))
        monkeypatch.setattr(platform_admin_service, "is_platform_admin", lambda user_id: admin)
        monkeypatch.setattr(service, "get_library", lambda slug: self.libraries.get(slug))
        monkeypatch.setattr(service, "get_primary_branch", lambda library: PRIMARY)
        monkeypatch.setattr(service, "add_problems", lambda library, primary: ["collector_settings_not_ready"])
        monkeypatch.setattr(service, "list_installations", lambda library: [dict(INSTALLATION)])
        monkeypatch.setattr(service, "create_installation", self._write("create"))
        monkeypatch.setattr(service, "rename_installation", self._write("rename"))
        monkeypatch.setattr(service, "change_status", self._write("status"))
        monkeypatch.setattr(service, "issue_enrollment_code", self._issue)

    def _write(self, kind):
        def write(library, *args, **kwargs):
            if self.refuse:
                raise InstallationAdminError(self.refuse)
            self.calls.append((kind, library.id, args, {k: v for k, v in kwargs.items() if k != "actor"}, kwargs["actor"]["id"]))
            return {**INSTALLATION, **({"name": kwargs["name"]} if "name" in kwargs else {})}
        return write

    def _issue(self, library, installation_id, *, actor):
        if self.refuse:
            raise InstallationAdminError(self.refuse)
        self.calls.append(("code", library.id, installation_id, actor["id"]))
        return {"installation": {"id": installation_id, "name": "Main AMH Sorter"}, "required_contract": 2,
                "enrollment_code": CANARY_CODE, "expires_at": CREATED + timedelta(minutes=30), "ttl_minutes": 30,
                "revoked_previous_count": 1}


WRITES = [
    ("post", BASE, {"name": "New Sorter", "required_contract": 2}),
    ("put", f"{BASE}/123", {"name": "Renamed"}),
    ("put", f"{BASE}/123/status", {"status": "inactive"}),
    ("post", f"{BASE}/123/enrollment-codes", None),
]


def _send(api, method, path, body, *, origin=ADMIN_ORIGIN):
    headers = dict(COOKIE)
    if origin is not None:
        headers["Origin"] = origin
    return getattr(api, method)(path, json=body, headers=headers) if body is not None else getattr(api, method)(path, headers=headers)


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


# =====================================================================================================================
# who may call
# =====================================================================================================================

def test_no_session_is_401_everywhere_and_nothing_is_written(api, monkeypatch):
    world = World(monkeypatch, user=None)

    assert api.get(BASE, headers=COOKIE).json() == NOT_AUTHENTICATED
    for method, path, body in WRITES:
        response = _send(api, method, path, body)
        assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED), path
    assert world.calls == []


def test_an_organization_owner_who_is_not_a_platform_admin_is_403_everywhere(api, monkeypatch):
    world = World(monkeypatch, admin=False)

    assert (api.get(BASE, headers=COOKIE).status_code, api.get(BASE, headers=COOKIE).json()) == (403, FORBIDDEN)
    for method, path, body in WRITES:
        response = _send(api, method, path, body)
        assert (response.status_code, response.json()) == (403, FORBIDDEN), path
    assert world.calls == []


@pytest.mark.parametrize("origin", [None, "https://evil.example", CUSTOMER_ORIGIN])
@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_a_write_without_the_admin_origin_is_refused_before_anything_is_written(api, monkeypatch, method, path, body, origin):
    world = World(monkeypatch)

    response = _send(api, method, path, body, origin=origin)

    assert (response.status_code, response.json()) == (403, ORIGIN_NOT_ALLOWED)
    assert world.calls == []


def test_an_unknown_library_is_404_everywhere(api, monkeypatch):
    world = World(monkeypatch, libraries={})

    assert (api.get(BASE, headers=COOKIE).status_code, api.get(BASE, headers=COOKIE).json()) == (404, LIBRARY_NOT_FOUND)
    for method, path, body in WRITES:
        response = _send(api, method, path, body)
        assert (response.status_code, response.json()) == (404, LIBRARY_NOT_FOUND), path
    assert world.calls == []


@pytest.mark.parametrize(("method", "path"), [
    ("put", BASE), ("delete", BASE), ("patch", f"{BASE}/123"), ("delete", f"{BASE}/123"), ("post", f"{BASE}/123"),
    ("get", f"{BASE}/123/status"), ("post", f"{BASE}/123/status"), ("get", f"{BASE}/123/enrollment-codes"),
    ("delete", f"{BASE}/123/enrollment-codes"),
])
def test_only_the_five_operations_are_routes(api, monkeypatch, method, path):
    World(monkeypatch)

    response = getattr(api, method)(path, headers={**COOKIE, "Origin": ADMIN_ORIGIN})

    assert response.status_code == 405


# =====================================================================================================================
# shapes
# =====================================================================================================================

def test_the_list_is_exactly_these_fields(api, monkeypatch):
    World(monkeypatch)

    response = api.get(BASE, headers=COOKIE)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "library": {"slug": "acme", "name": "Acme Library", "status": "active"},
        "primary_branch": {"slug": "main", "name": "Main Branch", "status": "active"},
        "add_problems": ["collector_settings_not_ready"],
        "installations": [INSTALLATION_JSON],
    }
    assert not _keys(response.json()) & FORBIDDEN_KEYS
    assert not any(value in response.text for value in FORBIDDEN_VALUES)
    assert CANARY_CODE not in response.text


def test_adding_attaches_nothing_but_the_name_and_contract_and_answers_201(api, monkeypatch):
    world = World(monkeypatch)

    response = _send(api, "post", BASE, {"name": "  New Sorter  ", "required_contract": 1})

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {**INSTALLATION_JSON, "name": "New Sorter"}
    assert world.calls == [("create", ORGANIZATION_ID, (), {"name": "New Sorter", "required_contract": 1}, 7)]


@pytest.mark.parametrize("body", [
    {"name": "Sorter", "required_contract": 2, "branch": "east"},
    {"name": "Sorter", "required_contract": 2, "branch_slug": "east"},
    {"name": "Sorter", "required_contract": 2, "hostname": "sorter-pc"},
    {"name": "Sorter", "required_contract": 2, "status": "active"},
    {"name": "Sorter", "required_contract": 3},
    {"name": "Sorter", "required_contract": "2"},
    {"name": "Sorter"},
    {"name": 5, "required_contract": 2},
])
def test_an_add_with_a_branch_or_any_other_field_or_a_bad_contract_is_422(api, monkeypatch, body):
    world = World(monkeypatch)

    response = _send(api, "post", BASE, body)

    assert response.status_code == 422
    assert world.calls == []


@pytest.mark.parametrize(("name", "code"), [("", "required"), ("    ", "required"), ("x" * 121, "too_long")])
@pytest.mark.parametrize(("method", "path"), [("post", BASE), ("put", f"{BASE}/123")])
def test_a_blank_or_long_name_is_422_naming_the_field(api, monkeypatch, method, path, name, code):
    world = World(monkeypatch)
    body = {"name": name, "required_contract": 2} if method == "post" else {"name": name}

    response = _send(api, method, path, body)

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_installation", "message": "The installation is not valid.",
                               "problems": [{"field": "name", "code": code}]}
    assert world.calls == []


def test_exactly_120_characters_is_a_name(api, monkeypatch):
    World(monkeypatch)
    assert _send(api, "post", BASE, {"name": "x" * 120, "required_contract": 2}).status_code == 201


def test_renaming_sends_the_name_alone(api, monkeypatch):
    world = World(monkeypatch)

    response = _send(api, "put", f"{BASE}/123", {"name": " Renamed "})

    assert response.status_code == 200 and response.json()["name"] == "Renamed"
    assert world.calls == [("rename", ORGANIZATION_ID, (123,), {"name": "Renamed"}, 7)]


@pytest.mark.parametrize("body", [
    {"name": "Renamed", "status": "active"}, {"name": "Renamed", "hostname": "x"}, {"name": "Renamed", "required_contract": 1},
])
def test_a_rename_carrying_anything_else_is_422(api, monkeypatch, body):
    world = World(monkeypatch)
    assert _send(api, "put", f"{BASE}/123", body).status_code == 422
    assert world.calls == []


@pytest.mark.parametrize("status", ["inactive", "provisioning", "retired"])
def test_a_status_change_sends_the_status(api, monkeypatch, status):
    world = World(monkeypatch)

    response = _send(api, "put", f"{BASE}/123/status", {"status": status})

    assert response.status_code == 200
    assert world.calls == [("status", ORGANIZATION_ID, (123,), {"status": status}, 7)]


@pytest.mark.parametrize("status", ["active", "cancelled", "", None])
def test_active_or_any_other_status_cannot_be_asked_for(api, monkeypatch, status):
    world = World(monkeypatch)
    assert _send(api, "put", f"{BASE}/123/status", {"status": status}).status_code == 422
    assert world.calls == []


def test_an_issued_code_is_in_its_own_201_answer_once_and_in_no_log(api, monkeypatch, caplog):
    world = World(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        response = _send(api, "post", f"{BASE}/123/enrollment-codes", None)
        listed = api.get(BASE, headers=COOKIE)

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "installation": {"id": 123, "name": "Main AMH Sorter"},
        "required_contract": 2,
        "enrollment_code": CANARY_CODE,
        "expires_at": "2026-10-01T12:30:00Z",
        "ttl_minutes": 30,
        "revoked_previous_count": 1,
    }
    assert world.calls == [("code", ORGANIZATION_ID, 123, 7)]
    assert CANARY_CODE not in caplog.text
    assert CANARY_CODE not in listed.text
    assert not _keys(response.json()) & FORBIDDEN_KEYS


@pytest.mark.parametrize(("code", "status", "operation"), [
    ("installation_not_found", 404, 1), ("installation_not_found", 404, 2), ("installation_not_found", 404, 3),
    ("library_cancelled", 409, 0), ("library_cancelled", 409, 1), ("library_cancelled", 409, 2),
    ("library_cancelled", 409, 3),
    ("no_active_primary_branch", 409, 0), ("collector_settings_not_ready", 409, 0),
    ("transition_not_allowed", 409, 2),
    ("installation_not_enrollable", 409, 3), ("library_not_enrollable", 409, 3),
    ("operational_identity_incomplete", 409, 3), ("branch_already_enrolled", 409, 3),
])
def test_every_refusal_is_its_fixed_code_and_names_no_internal_reason(api, monkeypatch, caplog, code, status, operation):
    world = World(monkeypatch)
    world.refuse = code
    method, path, body = WRITES[operation]

    with caplog.at_level(logging.DEBUG):
        response = _send(api, method, path, body)

    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == code and set(response.json()) == {"code", "message"}
    assert CANARY_CODE not in response.text and CANARY_CODE not in caplog.text
    for internal in ("installation_status_", "organization_status_", "operational_customer", "mapping"):
        assert internal not in response.text


# =====================================================================================================================
# the service's own rules
# =====================================================================================================================

@pytest.mark.parametrize(("current", "wanted", "allowed"), [
    ("provisioning", "inactive", True), ("active", "inactive", True), ("inactive", "provisioning", True),
    ("provisioning", "retired", True), ("active", "retired", True), ("inactive", "retired", True),
    ("retired", "provisioning", False), ("retired", "inactive", False), ("retired", "active", False),
    ("retired", "retired", False), ("active", "provisioning", False), ("provisioning", "active", False),
    ("inactive", "active", False), ("inactive", "inactive", False), ("provisioning", "provisioning", False),
    ("active", "active", False), ("unknown", "inactive", False),
])
def test_the_transition_table(current, wanted, allowed):
    assert service.transition_allowed(current, wanted) is allowed


@pytest.mark.parametrize(("name", "result"), [
    ("  Main Sorter ", "Main Sorter"), ("x" * 120, "x" * 120), ("", "required"), ("   ", "required"),
    ("x" * 121, "too_long"), (None, "not_text"), (5, "not_text"),
])
def test_names(name, result):
    if result in ("required", "too_long", "not_text"):
        with pytest.raises(ValueError, match=f"^{result}$"):
            service.validate_name(name)
    else:
        assert service.validate_name(name) == result


@pytest.mark.parametrize(("reason", "public"), [
    ("installation_not_found", "installation_not_found"),
    ("installation_organization_mismatch", "installation_not_found"),
    ("installation_status_retired", "installation_not_enrollable"),
    ("installation_status_inactive", "installation_not_enrollable"),
    ("installation_contract_unsupported", "installation_not_enrollable"),
    ("organization_status_suspended", "library_not_enrollable"),
    ("branch_missing", "operational_identity_incomplete"),
    ("branch_status_inactive", "operational_identity_incomplete"),
    ("organization_not_operationally_mapped", "operational_identity_incomplete"),
    ("branch_not_operationally_mapped", "operational_identity_incomplete"),
    ("branch_operational_mapping_inconsistent", "operational_identity_incomplete"),
    ("operational_customer_missing", "operational_identity_incomplete"),
    ("operational_customer_ambiguous", "operational_identity_incomplete"),
    ("branch_belongs_to_another_organization", "operational_identity_incomplete"),
])
def test_each_enrollment_service_reason_becomes_a_fixed_public_code(reason, public):
    assert service._public_refusal(reason) == public


# =====================================================================================================================
# the customer boundary
# =====================================================================================================================

def test_no_customer_model_carries_installation_admin_or_enrollment_fields():
    import importlib
    import inspect
    import pkgutil

    import customer_api

    named = {"enrollment_code", "pending_code_expires_at", "last_code_redeemed_at", "required_contract",
             "installation_id", "add_problems", "revoked_previous_count"}
    found = []
    for module_info in pkgutil.iter_modules(customer_api.__path__):
        module = importlib.import_module(f"customer_api.{module_info.name}")
        for _, model in inspect.getmembers(module, inspect.isclass):
            if issubclass(model, BaseModel) and model.__module__ == module.__name__:
                found += [f"{model.__name__}.{field}" for field in model.model_fields if field in named]
    assert found == []


def test_no_route_outside_the_admin_api_serves_installations_or_enrollment_codes():
    paths = [route.path for route in main.app.routes if hasattr(route, "path")]
    outside = [path for path in paths if not path.startswith("/api/admin/") and path.startswith("/api/")
               and ("installation" in path or "enrollment" in path)]
    assert outside == []
