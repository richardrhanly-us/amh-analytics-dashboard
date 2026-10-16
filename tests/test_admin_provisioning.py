"""A2 item 7: provisioning a library, and assigning an existing library's operational identity, in the administration
API.

    GET  /api/admin/plans
    POST /api/admin/libraries
    POST /api/admin/libraries/{org_slug}/operational-identity

Driven through TestClient(main.app) with the services.tenant_service functions the routes use replaced: who may call,
the exact shapes, every refusal, and that a refused value is never echoed. The validation rules themselves are tested
at the bottom against services.tenant_service.validate_provisioning, which the route calls unreplaced. What the
services really write -- one transaction, the rows, the audit, rollback -- is in tests/test_admin_provisioning_postgres.py.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import main
from services import platform_admin_service, session_service, tenant_service
from services.tenant_service import AdminProvisioningError

COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 7, "email": "admin@example.invalid", "full_name": "Test Admin"}
ADMIN_ORIGIN = "https://admin.sortview.example"
CUSTOMER_ORIGIN = "https://app.sortview.example"
LIBRARIES = "/api/admin/libraries"
PLANS = "/api/admin/plans"
IDENTITY = "/api/admin/libraries/acme/operational-identity"

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have access to SortView administration."}
ORIGIN_NOT_ALLOWED = {"code": "origin_not_allowed", "message": "Request origin is not allowed."}

BODY = {"name": "Acme Library", "slug": "acme", "branch_name": "Main Branch", "branch_slug": "main", "plan_code": "starter"}
CREATED = {
    "library": {"slug": "acme", "name": "Acme Library", "status": "active"},
    "primary_branch": {"slug": "main", "name": "Main Branch", "status": "active"},
    "subscription": {"plan_code": "starter", "plan_name": "Starter", "status": "trial"},
    "operational_identity": "assigned",
}
FORBIDDEN_KEYS = {"id", "organization_id", "branch_id", "customer_id", "operational_customer_id", "operational_branch_id",
                  "plan_id", "settings", "settings_json", "contact_name", "contact_email", "notes", "entitlements"}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", CUSTOMER_ORIGIN)
    monkeypatch.setenv("SORTVIEW_ADMIN_ALLOWED_ORIGINS", ADMIN_ORIGIN)
    monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


class World:
    """The session, the platform flag and the three service calls, recording every write. `refuse` makes the next
    write refuse with that code."""

    def __init__(self, monkeypatch, *, admin=True, user=USER, changed=True):
        self.calls: list[tuple] = []
        self.refuse: str | None = None
        self.changed = changed
        monkeypatch.setattr(session_service, "validate_session", lambda token: None if user is None else dict(user))
        monkeypatch.setattr(platform_admin_service, "is_platform_admin", lambda user_id: admin)
        monkeypatch.setattr(tenant_service, "list_active_plans",
                            lambda: [{"code": "starter", "name": "Starter"}, {"code": "pro", "name": "Pro"}])
        monkeypatch.setattr(tenant_service, "provision_library", self._provision)
        monkeypatch.setattr(tenant_service, "assign_library_operational_identity", self._assign)

    def _provision(self, *, actor, **values):
        if self.refuse:
            raise AdminProvisioningError(self.refuse)
        self.calls.append(("provision", values, actor["id"]))
        return {**CREATED, "library": {**CREATED["library"], "slug": values["slug"], "name": values["name"]}}

    def _assign(self, org_slug, *, actor):
        if self.refuse:
            raise AdminProvisioningError(self.refuse)
        self.calls.append(("assign", org_slug, actor["id"]))
        return {"changed": self.changed, "created_customer": self.changed}


WRITES = [(LIBRARIES, BODY), (IDENTITY, None)]


def _post(api, path, body=None, *, origin=ADMIN_ORIGIN):
    headers = dict(COOKIE)
    if origin is not None:
        headers["Origin"] = origin
    return api.post(path, json=body, headers=headers) if body is not None else api.post(path, headers=headers)


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

    assert (api.get(PLANS, headers=COOKIE).status_code, api.get(PLANS, headers=COOKIE).json()) == (401, NOT_AUTHENTICATED)
    for path, body in WRITES:
        response = _post(api, path, body)
        assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED), path
        assert response.headers["cache-control"] == "no-store"
    assert world.calls == []


def test_an_organization_owner_who_is_not_a_platform_admin_is_403_everywhere(api, monkeypatch):
    world = World(monkeypatch, admin=False)

    assert (api.get(PLANS, headers=COOKIE).status_code, api.get(PLANS, headers=COOKIE).json()) == (403, FORBIDDEN)
    for path, body in WRITES:
        response = _post(api, path, body)
        assert (response.status_code, response.json()) == (403, FORBIDDEN), path
    assert world.calls == []


@pytest.mark.parametrize("origin", [None, "https://evil.example", CUSTOMER_ORIGIN])
@pytest.mark.parametrize(("path", "body"), WRITES)
def test_a_write_without_the_admin_origin_is_refused_before_anything_is_written(api, monkeypatch, path, body, origin):
    world = World(monkeypatch)

    response = _post(api, path, body, origin=origin)

    assert (response.status_code, response.json()) == (403, ORIGIN_NOT_ALLOWED)
    assert world.calls == []


@pytest.mark.parametrize(("method", "path"), [
    ("put", LIBRARIES), ("patch", LIBRARIES), ("delete", LIBRARIES),
    ("post", PLANS), ("put", PLANS), ("delete", PLANS),
    ("get", IDENTITY), ("put", IDENTITY), ("delete", IDENTITY),
])
def test_only_these_operations_are_routes(api, monkeypatch, method, path):
    world = World(monkeypatch)

    response = getattr(api, method)(path, headers={**COOKIE, "Origin": ADMIN_ORIGIN})

    assert response.status_code == 405
    assert world.calls == []


# =====================================================================================================================
# plans
# =====================================================================================================================

def test_the_plans_are_codes_and_names_only(api, monkeypatch):
    World(monkeypatch)

    response = api.get(PLANS, headers=COOKIE)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"plans": [{"code": "starter", "name": "Starter"}, {"code": "pro", "name": "Pro"}]}


def test_a_plans_row_carrying_more_is_reduced_to_its_code_and_name(api, monkeypatch):
    World(monkeypatch)
    monkeypatch.setattr(tenant_service, "list_active_plans",
                        lambda: [{"code": "starter", "name": "Starter", "id": 90417, "entitlements": {"x": 1}}])

    response = api.get(PLANS, headers=COOKIE)

    assert response.json() == {"plans": [{"code": "starter", "name": "Starter"}]}
    assert "90417" not in response.text


# =====================================================================================================================
# provision
# =====================================================================================================================

def test_provisioning_answers_201_with_slugs_names_and_states_only(api, monkeypatch):
    world = World(monkeypatch)

    response = _post(api, LIBRARIES, {**BODY, "name": "  Acme Library  ", "branch_name": " Main Branch "})

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == CREATED
    assert not _keys(response.json()) & FORBIDDEN_KEYS
    # Names trimmed; slugs and plan exactly as sent.
    assert world.calls == [("provision", {"name": "Acme Library", "slug": "acme", "branch_name": "Main Branch",
                                          "branch_slug": "main", "plan_code": "starter"}, 7)]


@pytest.mark.parametrize("extra", [
    {"operational_customer_id": 5}, {"customer_id": 5}, {"status": "trial"}, {"contact_email": "x@example.invalid"},
    {"notes": "x"}, {"contact_name": "x"}, {"branches": []}, {"installation": {"name": "x"}}, {"owner_email": "x"},
    {"system_name": "x"}, {"library_name": "x"}, {"id": 1},
])
def test_any_other_field_is_422_and_nothing_is_written(api, monkeypatch, extra):
    world = World(monkeypatch)

    response = _post(api, LIBRARIES, {**BODY, **extra})

    assert response.status_code == 422
    assert world.calls == []


@pytest.mark.parametrize("field", ["name", "slug", "branch_name", "branch_slug", "plan_code"])
@pytest.mark.parametrize("bad", [None, 5, ["acme"], {"x": 1}])
def test_a_missing_or_non_string_field_is_422(api, monkeypatch, field, bad):
    world = World(monkeypatch)
    body = {**BODY, field: bad} if bad is not None else {k: v for k, v in BODY.items() if k != field}

    assert _post(api, LIBRARIES, body).status_code == 422
    assert world.calls == []


@pytest.mark.parametrize(("field", "value", "code"), [
    ("name", "", "required"), ("name", "   ", "required"), ("name", "N" * 121, "too_long"),
    ("branch_name", "", "required"), ("branch_name", "  ", "required"), ("branch_name", "B" * 121, "too_long"),
    ("slug", "", "required"), ("slug", "s" * 64, "too_long"), ("slug", "Acme", "invalid_slug"),
    ("slug", "-acme", "invalid_slug"), ("slug", "acme-", "invalid_slug"), ("slug", "ac--me", "invalid_slug"),
    ("slug", "ac_me", "invalid_slug"), ("slug", " acme", "invalid_slug"), ("slug", "acme library", "invalid_slug"),
    ("slug", "acmé", "invalid_slug"),
    ("branch_slug", "", "required"), ("branch_slug", "m" * 64, "too_long"), ("branch_slug", "Main", "invalid_slug"),
    ("plan_code", "", "required"),
])
def test_a_refused_value_is_422_naming_the_field_and_never_the_value(api, monkeypatch, caplog, field, value, code):
    world = World(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        response = _post(api, LIBRARIES, {**BODY, field: value})

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": "invalid_library", "message": "Library details are invalid.",
                               "problems": [{"field": field, "code": code}]}
    assert world.calls == []
    if value.strip():
        assert value not in response.text


def test_every_problem_is_reported_at_once(api, monkeypatch):
    World(monkeypatch)

    response = _post(api, LIBRARIES, {"name": "", "slug": "Bad", "branch_name": "", "branch_slug": "", "plan_code": ""})

    assert [problem["field"] for problem in response.json()["problems"]] == [
        "name", "branch_name", "slug", "branch_slug", "plan_code"]


def test_exactly_120_character_names_and_63_character_slugs_are_accepted(api, monkeypatch):
    World(monkeypatch)
    body = {**BODY, "name": "N" * 120, "branch_name": "B" * 120, "slug": "a" * 63, "branch_slug": "b" * 63}

    assert _post(api, LIBRARIES, body).status_code == 201


@pytest.mark.parametrize(("code", "message"), [
    ("library_slug_taken", "A library with this slug already exists."),
    ("plan_not_available", "This plan is not available for a new library."),
])
def test_a_provisioning_refusal_is_409_with_its_fixed_message(api, monkeypatch, code, message):
    world = World(monkeypatch)
    world.refuse = code

    response = _post(api, LIBRARIES, BODY)

    assert (response.status_code, response.json()) == (409, {"code": code, "message": message})
    assert response.headers["cache-control"] == "no-store"


def test_a_provisioning_log_line_names_the_slug_and_the_user_only(api, monkeypatch, caplog):
    World(monkeypatch)

    with caplog.at_level(logging.INFO, logger="sortview.customer_api"):
        _post(api, LIBRARIES, BODY)

    lines = [record.getMessage() for record in caplog.records if "Admin library provisioned" in record.getMessage()]
    assert lines == ["Admin library provisioned | org=acme user_id=7 action=provision"]
    assert "Acme Library" not in caplog.text and USER["email"] not in caplog.text


# =====================================================================================================================
# operational identity
# =====================================================================================================================

@pytest.mark.parametrize("changed", [True, False])
def test_assigning_an_identity_answers_assigned_and_whether_it_changed(api, monkeypatch, changed):
    world = World(monkeypatch, changed=changed)

    response = _post(api, IDENTITY)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"operational_identity": "assigned", "changed": changed}
    assert world.calls == [("assign", "acme", 7)]


@pytest.mark.parametrize(("code", "status", "message"), [
    ("library_not_found", 404, "Library not found."),
    ("library_cancelled", 409, "This library is cancelled; its operational identity cannot be changed."),
    ("no_primary_branch", 409, "This library has no single primary branch to assign an operational identity to."),
    ("operational_identity_inconsistent", 409,
     "This library's existing operational identity is inconsistent. It was not changed; contact SortView engineering."),
])
def test_an_identity_refusal_is_its_fixed_answer(api, monkeypatch, code, status, message):
    world = World(monkeypatch)
    world.refuse = code

    response = _post(api, IDENTITY)

    assert (response.status_code, response.json()) == (status, {"code": code, "message": message})
    assert response.headers["cache-control"] == "no-store"


def test_the_identity_action_takes_no_body_and_returns_no_id(api, monkeypatch):
    world = World(monkeypatch)

    response = _post(api, IDENTITY, {"operational_customer_id": 5, "branch_id": 9})

    assert response.status_code == 200
    assert set(response.json()) == {"operational_identity", "changed"}
    assert world.calls == [("assign", "acme", 7)]


# =====================================================================================================================
# the validation rules (services.tenant_service.validate_provisioning)
# =====================================================================================================================

def test_validation_trims_names_and_leaves_slugs_and_plan_exactly_as_given():
    values, problems = tenant_service.validate_provisioning(
        name="  Acme  ", slug="acme", branch_name="\tMain\n", branch_slug="main", plan_code="pro")

    assert problems == []
    assert values == {"name": "Acme", "branch_name": "Main", "slug": "acme", "branch_slug": "main", "plan_code": "pro"}


@pytest.mark.parametrize("slug", ["a", "0", "acme", "acme-2", "a-b-c", "nbpl", "new", "provision", "a" * 63])
def test_these_are_slugs(slug):
    assert tenant_service.validate_provisioning(
        name="A", slug=slug, branch_name="B", branch_slug=slug, plan_code="p")[1] == []
