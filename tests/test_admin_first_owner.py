"""G6: assigning a library's first owner in the administration API.

    POST /api/admin/libraries/{org_slug}/first-owner

Driven through TestClient(main.app) with services.user_admin_service.assign_first_owner replaced: who may call, the
confirmation check, every refusal, that no password is taken or returned, and that the address is never echoed or
logged. What the service really does -- the lock, the owner rule, the account, the record -- is in
tests/test_user_admin_service.py and tests/test_user_admin_postgres.py.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import main
from services import platform_admin_service, session_service, user_admin_service

COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 7, "email": "admin@example.invalid", "full_name": "Test Admin"}
ADMIN_ORIGIN = "https://admin.sortview.example"
CUSTOMER_ORIGIN = "https://app.sortview.example"
PATH = "/api/admin/libraries/fresh/first-owner"
BODY = {"email": "Olive.Owner@Example.invalid", "email_confirmation": " olive.owner@example.invalid ", "full_name": " Olive Owner "}

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have access to SortView administration."}
ORIGIN_NOT_ALLOWED = {"code": "origin_not_allowed", "message": "Request origin is not allowed."}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", CUSTOMER_ORIGIN)
    monkeypatch.setenv("SORTVIEW_ADMIN_ALLOWED_ORIGINS", ADMIN_ORIGIN)
    monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


class World:
    """The session, the platform flag and the service, recording every call. `answer` is what the service returns."""

    def __init__(self, monkeypatch, *, admin=True, user=USER):
        self.calls: list[dict] = []
        self.answer: dict = {"ok": True, "message": "The first owner was assigned."}
        monkeypatch.setattr(session_service, "validate_session", lambda token: None if user is None else dict(user))
        monkeypatch.setattr(platform_admin_service, "is_platform_admin", lambda user_id: admin)
        monkeypatch.setattr(user_admin_service, "assign_first_owner", self._assign)

    def _assign(self, org_slug, email, password, full_name, *, actor_user_id):
        self.calls.append({"org_slug": org_slug, "email": email, "password": password, "full_name": full_name,
                           "actor_user_id": actor_user_id})
        return self.answer


def _post(api, body=None, *, origin=ADMIN_ORIGIN, path=PATH):
    headers = dict(COOKIE)
    if origin is not None:
        headers["Origin"] = origin
    return api.post(path, json=BODY if body is None else body, headers=headers)


def test_no_session_is_401_and_nothing_is_called(api, monkeypatch):
    world = World(monkeypatch, user=None)

    response = _post(api)

    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert world.calls == []


def test_an_organization_owner_who_is_not_a_platform_admin_is_403(api, monkeypatch):
    world = World(monkeypatch, admin=False)

    response = _post(api)

    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert world.calls == []


@pytest.mark.parametrize("origin", [None, CUSTOMER_ORIGIN, "https://evil.example"], ids=["none", "customer-app", "other"])
def test_a_write_without_the_admin_origin_is_refused_before_anything_is_called(api, monkeypatch, origin):
    world = World(monkeypatch)

    response = _post(api, origin=origin)

    assert (response.status_code, response.json()) == (403, ORIGIN_NOT_ALLOWED)
    assert world.calls == []


def test_a_first_owner_is_assigned_with_a_random_password_nobody_sees_and_answers_204(api, monkeypatch):
    world = World(monkeypatch)

    response = _post(api)

    assert response.status_code == 204 and response.content == b""
    assert response.headers["cache-control"] == "no-store"
    [call] = world.calls
    assert (call["org_slug"], call["email"], call["full_name"], call["actor_user_id"]) == (
        "fresh", "olive.owner@example.invalid", "Olive Owner", USER["id"])
    assert len(call["password"]) >= 48 and call["password"] not in response.text


def test_each_request_gets_its_own_random_password(api, monkeypatch):
    world = World(monkeypatch)

    _post(api)
    _post(api)

    assert world.calls[0]["password"] != world.calls[1]["password"]


@pytest.mark.parametrize("extra", ["password", "role", "user_id", "actor_user_id"])
def test_any_other_field_is_refused_and_nothing_is_called(api, monkeypatch, extra):
    world = World(monkeypatch)

    response = _post(api, {**BODY, extra: "owner"})

    assert response.status_code == 422
    assert world.calls == []


@pytest.mark.parametrize(("body", "field", "code"), [
    ({**BODY, "email_confirmation": "someone.else@example.invalid"}, "email_confirmation", "mismatch"),
    ({**BODY, "email": "not-an-address", "email_confirmation": "not-an-address"}, "email", "invalid"),
], ids=["confirmation-differs", "not-an-address"])
def test_a_confirmation_that_differs_or_no_address_is_422_naming_the_field_never_the_value(api, monkeypatch, body, field, code):
    world = World(monkeypatch)

    response = _post(api, body)

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_owner", "message": "The owner's details are not valid.",
                               "problems": [{"field": field, "code": code}]}
    assert "example.invalid" not in response.text and "not-an-address" not in response.text
    assert world.calls == []


@pytest.mark.parametrize("missing", ["email", "email_confirmation"])
def test_both_addresses_are_required(api, monkeypatch, missing):
    world = World(monkeypatch)

    response = _post(api, {key: value for key, value in BODY.items() if key != missing})

    assert response.status_code == 422
    assert world.calls == []


@pytest.mark.parametrize(("code", "status", "api_code", "message"), [
    ("platform_admin_required", 403, "forbidden", "You do not have access to SortView administration."),
    ("organization_not_found", 404, "library_not_found", "Library not found."),
    ("organization_not_editable", 409, "library_not_editable", "This library is suspended or cancelled; its owner cannot be assigned."),
    ("owner_exists", 409, "owner_exists", "This library already has an owner. Its owners add everyone else."),
    ("platform_admin_account", 409, "platform_admin_account", "A platform administrator's account cannot be a library's owner."),
    ("account_inactive", 409, "account_inactive", "That account is switched off, so it cannot be made an owner."),
    ("already_member", 409, "already_member", "That person already belongs to this library."),
])
def test_a_refusal_is_its_fixed_answer(api, monkeypatch, code, status, api_code, message):
    world = World(monkeypatch)
    world.answer = {"ok": False, "code": code, "message": "the service's own words, never relayed"}

    response = _post(api)

    assert (response.status_code, response.json()) == (status, {"code": api_code, "message": message})
    assert response.headers["cache-control"] == "no-store"


def test_the_services_invalid_details_answer_is_the_same_422(api, monkeypatch):
    world = World(monkeypatch)
    world.answer = {"ok": False, "code": "invalid_member_details", "message": "x"}

    response = _post(api)

    assert response.status_code == 422 and response.json()["problems"] == [{"field": "email", "code": "invalid"}]


def test_an_unknown_refusal_is_a_server_error_never_a_guess(api, monkeypatch):
    world = World(monkeypatch)
    world.answer = {"ok": False, "code": "member_not_added", "message": "x"}

    response = _post(api)

    assert response.status_code == 500
    assert response.json() == {"code": "internal_error", "message": "Internal server error."}


def test_the_log_line_names_the_library_and_the_administrator_never_the_address(api, monkeypatch, caplog):
    World(monkeypatch)

    with caplog.at_level(logging.INFO):
        _post(api)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "Admin first owner assigned | org=fresh user_id=7" in logged
    assert "olive" not in logged.lower()
