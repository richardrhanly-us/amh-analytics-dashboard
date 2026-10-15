"""A2 item 1: the administration API's boundary.

    GET /api/admin/session

The routes are driven through TestClient(main.app), with the services replaced at the FLAT module identity the code
uses (services.session_service, services.platform_admin_service), as in tests/test_customer_api_auth.py. Cookies are
sent as an explicit Cookie header for the same reason given there.

The Origin guard is proved on a probe POST route added to a freshly built admin router -- which is also the proof that
a route added to the router later inherits both guards without naming them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

import main
from admin_api.dependencies import require_admin_origin, require_platform_admin
from admin_api.router import ADMIN_API_PREFIX, create_admin_router
from services import (
    access_service,
    entitlement_service,
    platform_admin_service,
    session_service,
)

ADMIN_API = Path(__file__).resolve().parents[1] / "src" / "admin_api"

ADMIN_ORIGIN = "https://admin.example.test"
CUSTOMER_ORIGIN = "https://app.example.test"
SECURE_COOKIE = "__Host-sortview_api_session"
COOKIE = {"Cookie": f"{SECURE_COOKIE}=synthetic-opaque-session-token"}

USER = {"id": 7, "email": "admin@example.invalid", "full_name": "Test Admin"}
NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have access to SortView administration."}
ORIGIN_NOT_ALLOWED = {"code": "origin_not_allowed", "message": "Request origin is not allowed."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", CUSTOMER_ORIGIN)
    monkeypatch.setenv("SORTVIEW_ADMIN_ALLOWED_ORIGINS", ADMIN_ORIGIN)
    monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


class Services:
    """Stand-ins for the session lookup and the platform-administrator flag. `admin` is a list read one value per
    call, so a test can change the answer between requests; the last value repeats."""

    def __init__(self, monkeypatch, *, user=USER, admin=(True,)):
        self.user = user
        self.admin = list(admin)
        self.admin_calls: list[int] = []
        self.raise_on_admin: Exception | None = None
        monkeypatch.setattr(session_service, "validate_session", self._validate_session)
        monkeypatch.setattr(platform_admin_service, "is_platform_admin", self._is_platform_admin)

    def _validate_session(self, token):
        return None if self.user is None else dict(self.user)

    def _is_platform_admin(self, user_id):
        self.admin_calls.append(user_id)
        if self.raise_on_admin is not None:
            raise self.raise_on_admin
        return self.admin.pop(0) if len(self.admin) > 1 else self.admin[0]


def _session(api):
    return api.get(f"{ADMIN_API_PREFIX}/session", headers=COOKIE)


# --- who may read the admin session ------------------------------------------------------------------------------

def test_no_session_cookie_is_401_and_the_flag_is_never_read(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.get(f"{ADMIN_API_PREFIX}/session")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert response.headers["cache-control"] == "no-store"
    assert services.admin_calls == []


def test_a_session_that_does_not_validate_is_401_and_the_cookie_is_cleared(api, monkeypatch):
    services = Services(monkeypatch, user=None)

    response = _session(api)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert f"{SECURE_COOKIE}=" in response.headers["set-cookie"]
    assert services.admin_calls == []


def test_a_signed_in_user_who_is_not_a_platform_admin_is_403(api, monkeypatch):
    services = Services(monkeypatch, admin=(False,))

    response = _session(api)

    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert response.headers["cache-control"] == "no-store"
    assert services.admin_calls == [USER["id"]]


def test_an_organization_owner_who_is_not_a_platform_admin_is_403_and_no_role_is_consulted(api, monkeypatch):
    # Owning or administering an organization grants nothing here: the decision is the platform flag alone.
    services = Services(monkeypatch, admin=(False,))
    consulted: list[str] = []
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", lambda **_: consulted.append("role") or "owner")
    monkeypatch.setattr(access_service, "get_user_memberships", lambda *_: consulted.append("memberships") or [])

    response = _session(api)

    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert consulted == []
    assert services.admin_calls == [USER["id"]]


def test_a_platform_admin_gets_exactly_the_current_user_fields(api, monkeypatch):
    services = Services(monkeypatch, user={**USER, "is_platform_admin": True, "organizations": ["acme"]})

    response = _session(api)

    assert response.status_code == 200
    assert response.json() == USER
    assert set(response.json()) == {"id", "email", "full_name"}
    assert response.headers["cache-control"] == "no-store"
    # One lookup per request, even though the route and the router both depend on it.
    assert services.admin_calls == [USER["id"]]


def test_the_flag_is_read_again_on_every_request(api, monkeypatch):
    services = Services(monkeypatch, admin=(True, False))

    first = _session(api)
    second = _session(api)

    assert first.status_code == 200
    assert second.status_code == 403
    assert second.json() == FORBIDDEN
    assert services.admin_calls == [USER["id"], USER["id"]]


def test_a_failing_flag_lookup_is_a_server_error_never_an_authorization_answer(api, monkeypatch):
    services = Services(monkeypatch)
    services.raise_on_admin = RuntimeError("synthetic database failure")

    response = _session(api)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert "synthetic" not in response.text


# --- the customer API is unchanged --------------------------------------------------------------------------------

def test_the_customer_session_of_a_platform_admin_is_unchanged_and_never_reads_the_flag(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.get("/api/auth/session", headers=COOKIE)

    assert response.status_code == 200
    assert response.json() == USER
    assert services.admin_calls == []


def test_no_customer_route_depends_on_the_platform_admin_check():
    for route in main.customer_router.routes:
        assert not route.path.startswith(ADMIN_API_PREFIX), route.path
        assert require_platform_admin not in _dependency_calls(route), route.path


# --- every admin route is guarded ----------------------------------------------------------------------------------

def _dependency_calls(route: APIRoute) -> set:
    found = set()
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        found.add(dependant.call)
        pending.extend(dependant.dependencies)
    return found


def test_every_admin_route_requires_a_platform_admin_and_the_admin_origin_guard():
    admin_routes = [route for route in main.app.routes if route.path.startswith(f"{ADMIN_API_PREFIX}/")]

    assert {(method, route.path) for route in admin_routes for method in route.methods} == {
        ("GET", f"{ADMIN_API_PREFIX}/session"),
        # A2 item 2: read-only libraries (admin_api.library_routes).
        ("GET", f"{ADMIN_API_PREFIX}/libraries"),
        ("GET", f"{ADMIN_API_PREFIX}/libraries/{{org_slug}}"),
        # A2 item 3: a sorter site's diagnostics (admin_api.diagnostics_routes).
        ("GET", f"{ADMIN_API_PREFIX}/libraries/{{org_slug}}/sorters/{{sorter_slug}}/diagnostics"),
    }
    for route in admin_routes:
        router_level = {dependency.dependency for dependency in route.dependencies}
        assert {require_admin_origin, require_platform_admin} <= router_level, route.path


def test_admin_api_code_never_imports_a_second_module_identity_main_or_streamlit():
    files = sorted(ADMIN_API.rglob("*.py"))
    assert {"__init__.py", "router.py", "dependencies.py", "settings.py"} <= {f.name for f in files}

    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                assert top not in {"src", "main", "streamlit"}, f"{path.name} imports {module!r}"
                assert not module.startswith("services.streamlit_"), f"{path.name} imports {module!r}"


# --- the admin Origin guard, on a write added to the router later ------------------------------------------------

@pytest.fixture
def probe(monkeypatch):
    """A fresh admin router with one POST added after it was built, as a later admin route would be."""
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", CUSTOMER_ORIGIN)
    monkeypatch.setenv("SORTVIEW_ADMIN_ALLOWED_ORIGINS", ADMIN_ORIGIN)
    monkeypatch.delenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", raising=False)
    router = create_admin_router()

    @router.post("/probe")
    def probe_write() -> JSONResponse:
        return JSONResponse({"written": True})

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _post(client, *origins):
    headers = list(COOKIE.items()) + [("Origin", origin) for origin in origins]
    return client.post(f"{ADMIN_API_PREFIX}/probe", headers=headers)


def test_a_get_needs_no_origin(api, monkeypatch):
    Services(monkeypatch)

    assert _session(api).status_code == 200


def test_a_write_with_no_origin_is_refused_before_the_flag_is_read(probe, monkeypatch):
    services = Services(monkeypatch)

    response = _post(probe)

    assert response.status_code == 403
    assert response.json() == ORIGIN_NOT_ALLOWED
    assert services.admin_calls == []


@pytest.mark.parametrize(
    "origins",
    [
        (CUSTOMER_ORIGIN,),                # allowed for the customer API, not for administration
        ("https://evil.example.test",),
        ("null",),
        (f"{ADMIN_ORIGIN}/",),             # not an origin at all
        (ADMIN_ORIGIN, ADMIN_ORIGIN),      # repeated
    ],
)
def test_a_write_from_any_other_origin_is_refused(probe, monkeypatch, origins):
    services = Services(monkeypatch)

    response = _post(probe, *origins)

    assert response.status_code == 403
    assert response.json() == ORIGIN_NOT_ALLOWED
    assert services.admin_calls == []


def test_a_write_from_the_admin_origin_still_needs_a_platform_admin(probe, monkeypatch):
    Services(monkeypatch, admin=(False,))

    response = _post(probe, ADMIN_ORIGIN)

    assert response.status_code == 403
    assert response.json() == FORBIDDEN


def test_a_write_from_the_admin_origin_by_a_platform_admin_passes(probe, monkeypatch):
    Services(monkeypatch)

    response = _post(probe, ADMIN_ORIGIN.upper().replace("HTTPS", "https"))

    assert response.status_code == 200
    assert response.json() == {"written": True}


def test_no_admin_origin_configured_refuses_every_write(probe, monkeypatch):
    Services(monkeypatch)
    monkeypatch.setenv("SORTVIEW_ADMIN_ALLOWED_ORIGINS", "")

    response = _post(probe, ADMIN_ORIGIN)

    assert response.status_code == 403
    assert response.json() == ORIGIN_NOT_ALLOWED
