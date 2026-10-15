"""A2 item 6: a library's lifecycle in the administration API.

    POST /api/admin/libraries/{org_slug}/lifecycle/suspend
    POST /api/admin/libraries/{org_slug}/lifecycle/reactivate
    POST /api/admin/libraries/{org_slug}/lifecycle/offboard

Driven through TestClient(main.app) with the two services.platform_admin_service lifecycle functions replaced at the flat
identity the routes use: who may call, the exact shapes, every refusal, that a mismatched confirmation is never echoed,
and that the administrator reaches the service as `admin_actor` (and, for an offboarding, as a non-PII actor label).
What the services really do -- statements, audit rows, gates, rollback -- is in tests/test_admin_lifecycle_postgres.py.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import main
from services import admin_installation_service, platform_admin_service, session_service
from services.admin_installation_service import Library
from services.platform_admin_service import LibraryLifecycleError

COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 7, "email": "admin@example.invalid", "full_name": "Test Admin"}
ADMIN_ORIGIN = "https://admin.sortview.example"
CUSTOMER_ORIGIN = "https://app.sortview.example"
BASE = "/api/admin/libraries/acme/lifecycle"

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have access to SortView administration."}
ORIGIN_NOT_ALLOWED = {"code": "origin_not_allowed", "message": "Request origin is not allowed."}
LIBRARY_NOT_FOUND = {"code": "library_not_found", "message": "Library not found."}
LIBRARY_CANCELLED = {"code": "library_cancelled", "message": "This library is cancelled; its lifecycle cannot be changed."}
CONFIRMATION_MISMATCH = {"code": "confirmation_mismatch", "message": "The confirmation does not match this library's slug."}

ORGANIZATION_ID = 90417  # internal; distinctive so a leak is caught by value
COUNTS = {"agent_tokens_deactivated": 2, "installations_retired": 1, "enrollment_codes_revoked": 1,
          "ingest_keys_retired": 1, "sessions_revoked": 3}
FORBIDDEN_KEYS = {"organization_id", "customer_id", "operational_customer_id", "token_ids", "agent_token_ids",
                  "enrollment_code_ids", "ingest_key_row_ids", "session_ids", "installations", "email", "actor_label"}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", CUSTOMER_ORIGIN)
    monkeypatch.setenv("SORTVIEW_ADMIN_ALLOWED_ORIGINS", ADMIN_ORIGIN)
    monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


class World:
    """The session, the platform flag, the library lookup and the two lifecycle functions, recording every call.
    `refuse` makes the next service call raise LibraryLifecycleError with that code."""

    def __init__(self, monkeypatch, *, admin=True, user=USER, status="active", libraries=None):
        self.libraries = (
            {"acme": Library(id=ORGANIZATION_ID, slug="acme", name="Acme Library", status=status)}
            if libraries is None else libraries
        )
        self.calls: list[tuple] = []
        self.refuse: str | None = None
        monkeypatch.setattr(session_service, "validate_session", lambda token: None if user is None else dict(user))
        monkeypatch.setattr(platform_admin_service, "is_platform_admin", lambda user_id: admin)
        monkeypatch.setattr(admin_installation_service, "get_library", lambda slug: self.libraries.get(slug))
        monkeypatch.setattr(platform_admin_service, "set_library_active_status", self._set_active)
        monkeypatch.setattr(platform_admin_service, "offboard_library", self._offboard)

    def _set_active(self, organization_id, is_active, *, admin_actor):
        if self.refuse:
            raise LibraryLifecycleError(self.refuse, f"internal detail for organization {organization_id}")
        self.calls.append(("set_active", organization_id, is_active, admin_actor["id"]))
        current = self.libraries["acme"].status
        wanted = "active" if is_active else "suspended"
        unchanged = current == wanted or (is_active and current == "trial")
        return {"organization_id": organization_id, "status": current if unchanged else wanted,
                "previous_status": current, "changed": not unchanged}

    def _offboard(self, organization_id, confirm_slug, *, actor_user_id, actor_label, admin_actor):
        if self.refuse:
            raise LibraryLifecycleError(self.refuse, f"internal detail for organization {organization_id}")
        self.calls.append(("offboard", organization_id, confirm_slug, actor_user_id, actor_label, admin_actor["id"]))
        return {"organization_id": organization_id, "status": "cancelled", "changed": True,
                "status_before": self.libraries["acme"].status, "counts": dict(COUNTS)}


ACTIONS = [("suspend", None), ("reactivate", None), ("offboard", {"confirm_slug": "acme"})]


def _post(api, action, body=None, *, origin=ADMIN_ORIGIN, path=None):
    headers = dict(COOKIE)
    if origin is not None:
        headers["Origin"] = origin
    url = path or f"{BASE}/{action}"
    return api.post(url, json=body, headers=headers) if body is not None else api.post(url, headers=headers)


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


# =====================================================================================================================
# who may call
# =====================================================================================================================

@pytest.mark.parametrize(("action", "body"), ACTIONS)
def test_no_session_is_401_and_nothing_is_called(api, monkeypatch, action, body):
    world = World(monkeypatch, user=None)

    response = _post(api, action, body)

    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert response.headers["cache-control"] == "no-store"
    assert world.calls == []


@pytest.mark.parametrize(("action", "body"), ACTIONS)
def test_an_organization_owner_who_is_not_a_platform_admin_is_403(api, monkeypatch, action, body):
    world = World(monkeypatch, admin=False)

    response = _post(api, action, body)

    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert world.calls == []


@pytest.mark.parametrize("origin", [None, "https://evil.example", CUSTOMER_ORIGIN])
@pytest.mark.parametrize(("action", "body"), ACTIONS)
def test_a_write_without_the_admin_origin_is_refused_before_anything_is_called(api, monkeypatch, action, body, origin):
    world = World(monkeypatch)

    response = _post(api, action, body, origin=origin)

    assert (response.status_code, response.json()) == (403, ORIGIN_NOT_ALLOWED)
    assert world.calls == []


@pytest.mark.parametrize(("action", "body"), ACTIONS)
def test_an_unknown_library_is_404(api, monkeypatch, action, body):
    world = World(monkeypatch, libraries={})

    response = _post(api, action, body)

    assert (response.status_code, response.json()) == (404, LIBRARY_NOT_FOUND)
    assert response.headers["cache-control"] == "no-store"
    assert world.calls == []


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
@pytest.mark.parametrize("action", ["suspend", "reactivate", "offboard"])
def test_each_action_is_a_post_only(api, monkeypatch, method, action):
    World(monkeypatch)

    response = getattr(api, method)(f"{BASE}/{action}", headers={**COOKIE, "Origin": ADMIN_ORIGIN})

    assert response.status_code == 405


@pytest.mark.parametrize("path", [BASE, f"{BASE}/status", f"{BASE}/cancel", f"{BASE}/purge", f"{BASE}/restore",
                                  "/api/admin/libraries/acme/status"])
@pytest.mark.parametrize("method", ["post", "put"])
def test_there_is_no_generic_status_write_and_no_purge_or_restore(api, monkeypatch, path, method):
    world = World(monkeypatch)

    response = getattr(api, method)(path, json={"status": "active"}, headers={**COOKIE, "Origin": ADMIN_ORIGIN})

    assert response.status_code in (404, 405)
    assert world.calls == []


# =====================================================================================================================
# suspend / reactivate
# =====================================================================================================================

@pytest.mark.parametrize("status", ["active", "trial"])
def test_suspending_a_usable_library(api, monkeypatch, status):
    world = World(monkeypatch, status=status)

    response = _post(api, "suspend")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"library": {"slug": "acme", "name": "Acme Library", "status": "suspended"},
                               "previous_status": status, "changed": True}
    assert world.calls == [("set_active", ORGANIZATION_ID, False, 7)]


def test_suspending_a_suspended_library_is_200_unchanged(api, monkeypatch):
    World(monkeypatch, status="suspended")

    response = _post(api, "suspend")

    assert response.status_code == 200
    assert response.json() == {"library": {"slug": "acme", "name": "Acme Library", "status": "suspended"},
                               "previous_status": "suspended", "changed": False}


def test_reactivating_a_suspended_library_returns_it_to_active(api, monkeypatch):
    world = World(monkeypatch, status="suspended")

    response = _post(api, "reactivate")

    assert response.status_code == 200
    assert response.json() == {"library": {"slug": "acme", "name": "Acme Library", "status": "active"},
                               "previous_status": "suspended", "changed": True}
    assert world.calls == [("set_active", ORGANIZATION_ID, True, 7)]


@pytest.mark.parametrize("status", ["active", "trial"])
def test_reactivating_a_usable_library_is_200_unchanged_and_keeps_its_label(api, monkeypatch, status):
    World(monkeypatch, status=status)

    response = _post(api, "reactivate")

    assert response.status_code == 200
    assert response.json() == {"library": {"slug": "acme", "name": "Acme Library", "status": status},
                               "previous_status": status, "changed": False}


@pytest.mark.parametrize("action", ["suspend", "reactivate"])
@pytest.mark.parametrize(("code", "status", "body"), [
    ("library_cancelled", 409, LIBRARY_CANCELLED),
    ("invalid_transition", 409, {"code": "invalid_transition",
                                 "message": "This library cannot be changed from its current status."}),
    ("library_not_found", 404, LIBRARY_NOT_FOUND),
])
def test_a_service_refusal_is_its_fixed_answer_and_never_its_internal_text(api, monkeypatch, action, code, status, body):
    world = World(monkeypatch)
    world.refuse = code

    response = _post(api, action)

    assert (response.status_code, response.json()) == (status, body)
    assert response.headers["cache-control"] == "no-store"
    assert str(ORGANIZATION_ID) not in response.text and "internal detail" not in response.text


@pytest.mark.parametrize("action", ["suspend", "reactivate"])
def test_suspend_and_reactivate_take_no_body_fields(api, monkeypatch, action):
    world = World(monkeypatch, status="suspended" if action == "reactivate" else "active")

    response = _post(api, action, {"status": "trial"})

    # No body model: anything sent is ignored, and the action is exactly the one in the path.
    assert response.status_code == 200
    assert response.json()["library"]["status"] in ("active", "suspended")
    assert world.calls[0][2] is (action == "reactivate")


# =====================================================================================================================
# offboard
# =====================================================================================================================

@pytest.mark.parametrize("status", ["active", "trial", "suspended"])
def test_offboarding_answers_counts_only(api, monkeypatch, status):
    World(monkeypatch, status=status)

    response = _post(api, "offboard", {"confirm_slug": "acme"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"library": {"slug": "acme", "name": "Acme Library", "status": "cancelled"},
                               "previous_status": status, "changed": True, "counts": COUNTS}
    assert not _keys(response.json()) & FORBIDDEN_KEYS
    assert str(ORGANIZATION_ID) not in response.text and USER["email"] not in response.text


def test_the_administrator_reaches_the_lifecycle_evidence_by_id_never_by_email(api, monkeypatch):
    world = World(monkeypatch)

    _post(api, "offboard", {"confirm_slug": " acme "})

    assert world.calls == [("offboard", ORGANIZATION_ID, " acme ", 7, "platform_admin:7", 7)]


def test_offboarding_a_cancelled_library_is_409_and_never_reaches_the_service(api, monkeypatch):
    world = World(monkeypatch, status="cancelled")

    response = _post(api, "offboard", {"confirm_slug": "acme"})

    assert (response.status_code, response.json()) == (409, LIBRARY_CANCELLED)
    assert world.calls == []


def test_a_library_cancelled_meanwhile_is_still_409(api, monkeypatch):
    world = World(monkeypatch)
    world.refuse = "library_cancelled"

    response = _post(api, "offboard", {"confirm_slug": "acme"})

    assert (response.status_code, response.json()) == (409, LIBRARY_CANCELLED)


@pytest.mark.parametrize("typed", ["", "   ", "ACME", "acme-library", "Acme Library", "xacme", "other-CANARY-slug"])
def test_a_confirmation_that_is_not_the_slug_is_422_and_never_echoed(api, monkeypatch, caplog, typed):
    world = World(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        response = _post(api, "offboard", {"confirm_slug": typed})

    assert (response.status_code, response.json()) == (422, CONFIRMATION_MISMATCH)
    assert response.headers["cache-control"] == "no-store"
    assert world.calls == []
    if typed.strip():
        assert typed not in response.text
        assert typed not in caplog.text


@pytest.mark.parametrize("body", [{}, {"confirm_slug": None}, {"confirm_slug": 5}, {"confirm_slug": ["acme"]},
                                  {"confirm_slug": "acme", "status": "cancelled"}, {"confirm_slug": "acme", "purge": True}])
def test_a_malformed_offboard_body_is_422_and_nothing_is_called(api, monkeypatch, body):
    world = World(monkeypatch)

    response = _post(api, "offboard", body)

    assert response.status_code == 422
    assert world.calls == []


# =====================================================================================================================
# logging
# =====================================================================================================================

def test_route_logs_name_the_library_the_user_id_the_action_and_counts_only(api, monkeypatch, caplog):
    World(monkeypatch)

    with caplog.at_level(logging.INFO, logger="sortview.customer_api"):
        _post(api, "suspend")
        _post(api, "offboard", {"confirm_slug": "acme"})

    lines = [record.getMessage() for record in caplog.records if "Admin library lifecycle" in record.getMessage()]
    assert lines == [
        "Admin library lifecycle | org=acme user_id=7 action=suspend changed=True",
        ("Admin library lifecycle | org=acme user_id=7 action=offboard changed=True tokens=2 installations=1 codes=1 "
         "keys=1 sessions=3"),
    ]
    assert USER["email"] not in caplog.text and str(ORGANIZATION_ID) not in caplog.text
