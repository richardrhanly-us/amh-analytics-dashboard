"""Block 3a: the API import seam and the customer router's place in the app.

Root main.py runs from the repository root and imports its collector modules
as `src.services.*`. The framework-neutral services the customer API will use
import each other "flat", with src/ as the import root, so main.py appends
src/ to sys.path once. These tests pin three things:

1. From the API's own launch context -- a fresh process, repository root as
   the import root, no PYTHONPATH -- every customer-core module imports under
   its FLAT name, exactly one module object each, and without Streamlit.
2. The customer router is built by the flat customer_api package, is included
   in main.app, and contributes exactly the customer routes under /api.
3. The collector's route table, middleware and exception handlers are exactly
   what they were before the router was included.

Block 3b added the first customer routes (tests/test_customer_api_auth.py), so
the checks that once said "no route" now pin the exact customer route set.

The fresh-process checks run in a subprocess because this test process already
has src/ on sys.path (tests/conftest.py), which would hide a broken seam.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from slowapi.errors import RateLimitExceeded

import main

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CUSTOMER_API = SRC / "customer_api"

# The flat identities the customer API uses (Block 3 decision: Option A).
CUSTOMER_CORE_MODULES = [
    "services.auth_service",
    "services.session_service",
    "services.access_service",
    "services.entitlement_service",
    "services.user_admin_service",
    "services.tenant_resolution_service",
    "database",
    "tenant_db",
]
# The same files under their OTHER name. Loading any of these next to the flat
# one would give the process two module objects for one file.
SECOND_IDENTITIES = [f"src.{name}" for name in CUSTOMER_CORE_MODULES]

COLLECTOR_ROUTES = {
    ("GET", "/"),
    ("POST", "/upload"),
    ("POST", "/upload-pipeline-status"),
    ("POST", "/collector/enroll"),
    ("POST", "/v2/upload"),
    ("POST", "/v2/status"),
}
CUSTOMER_ROUTES = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/auth/session"),
    ("GET", "/api/organizations"),
    ("GET", "/api/organizations/{org_slug}"),
    ("GET", "/api/organizations/{org_slug}/branches/{branch_slug}/ingest-status"),
    ("GET", "/api/organizations/{org_slug}/branches/{branch_slug}/checkins/count"),
    ("GET", "/api/organizations/{org_slug}/branches/{branch_slug}/checkins/by-hour"),
    ("GET", "/api/organizations/{org_slug}/branches/{branch_slug}/rejects/count"),
    ("GET", "/api/organizations/{org_slug}/branches/{branch_slug}/rejects/by-reason"),
}

_PROBE = """
import importlib
import json
import sys

root, mode = sys.argv[1], sys.argv[2]
if mode == "other-cwd":
    # The repository root is importable, but it is NOT the working directory.
    sys.path.insert(0, root)

import main

for name in {modules!r}:
    importlib.import_module(name)

print("RESULT_JSON=" + json.dumps({{
    "src_dir": main._SRC_DIR,
    "src_entries": sys.path.count(main._SRC_DIR),
    "flat_loaded": sorted(name for name in {modules!r} if name in sys.modules),
    "second_identities": sorted(name for name in {second!r} if name in sys.modules),
    "streamlit": sorted(name for name in sys.modules if name == "streamlit" or name.startswith("streamlit.")),
    "router_is_flat": sys.modules["customer_api.router"].create_customer_router is main.create_customer_router,
    "src_customer_api": sorted(name for name in sys.modules if name.startswith("src.customer_api")),
    "routes": sorted([method, route.path] for route in main.app.routes for method in getattr(route, "methods", [])),
}}))
""".strip()


def _probe(mode: str, *, cwd: Path, pythonpath: str | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    # main.py needs a DATABASE_URL to import; nothing connects (engines are lazy).
    env["DATABASE_URL"] = "postgresql://test:test@localhost/test"
    env["SENTRY_DSN"] = ""
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath

    code = _PROBE.format(modules=CUSTOMER_CORE_MODULES, second=SECOND_IDENTITIES)
    completed = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(code), str(ROOT), mode],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT_JSON=")]
    assert len(lines) == 1, f"probe failed:\n{completed.stdout}\n{completed.stderr[-2000:]}"
    return json.loads(lines[0][len("RESULT_JSON="):])


@pytest.fixture(scope="module")
def api_process() -> dict:
    """The API's own launch context: started in the repository root, no PYTHONPATH."""
    return _probe("repo-root", cwd=ROOT)


# --- the import seam ---------------------------------------------------------------------------------------------

def test_every_customer_core_module_imports_flat_from_the_api_process(api_process):
    assert api_process["flat_loaded"] == sorted(CUSTOMER_CORE_MODULES)


def test_the_api_process_holds_one_module_object_per_customer_core_module(api_process):
    assert api_process["second_identities"] == []


def test_the_customer_core_modules_do_not_import_streamlit(api_process):
    assert api_process["streamlit"] == []


def test_src_is_on_the_path_exactly_once_and_comes_from_the_location_of_main(api_process):
    assert api_process["src_dir"] == str(SRC)
    assert api_process["src_entries"] == 1


def test_the_seam_does_not_depend_on_the_working_directory(tmp_path):
    result = _probe("other-cwd", cwd=tmp_path)

    assert result["src_dir"] == str(SRC)
    assert result["src_entries"] == 1
    assert result["flat_loaded"] == sorted(CUSTOMER_CORE_MODULES)
    assert result["second_identities"] == []


def test_the_seam_adds_no_duplicate_when_src_is_already_on_the_path():
    result = _probe("repo-root", cwd=ROOT, pythonpath=str(SRC))

    assert result["src_entries"] == 1
    assert result["second_identities"] == []


# --- the customer package and router -----------------------------------------------------------------------------

def test_the_customer_router_is_imported_under_its_flat_name_only(api_process):
    assert api_process["router_is_flat"] is True
    assert api_process["src_customer_api"] == []


def test_the_customer_router_is_built_by_the_flat_package_under_the_api_prefix():
    from customer_api.router import API_PREFIX, create_customer_router

    assert main.create_customer_router is create_customer_router
    assert API_PREFIX == "/api"
    assert main.customer_router.prefix == "/api"
    assert {(method, route.path) for route in main.customer_router.routes for method in route.methods} == CUSTOMER_ROUTES


def _imports(path: Path) -> list[tuple[str, int]]:
    """(module, relative level) for every import statement in a file."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def test_only_the_tenant_scope_module_calls_the_operational_tenant_resolver():
    # Block 3 ended with the resolver built but with no customer caller. Block
    # 4b gives it exactly one: customer_api/tenant_scope.py. Every other
    # customer module that needs a tenant goes through that module, so there
    # is one place where a request becomes an operational tenant.
    for path in sorted(CUSTOMER_API.rglob("*.py")):
        if path.name == "tenant_scope.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert "tenant_resolution_service" not in source, path.name
        assert "resolve_operational_tenant" not in source, path.name


def test_customer_api_code_never_imports_a_second_module_identity_or_main():
    files = sorted(CUSTOMER_API.rglob("*.py"))
    assert {"__init__.py", "router.py"} <= {f.name for f in files}  # the scan really sees the package

    for path in files:
        for module, _level in _imports(path):
            top = module.split(".")[0]
            assert top != "src", f"{path.name} imports {module!r}: customer API code uses the flat services.* identity"
            assert top != "main", f"{path.name} imports {module!r}: customer API code must not depend on main.py"
            assert top != "streamlit", f"{path.name} imports {module!r}: customer API code is framework-neutral"
            assert not module.startswith("services.streamlit_"), (
                f"{path.name} imports {module!r}: customer API code uses the uncached core services, never a Streamlit adapter"
            )


# --- the collector surface is unchanged --------------------------------------------------------------------------

def test_the_route_table_is_exactly_the_collector_routes_plus_the_customer_routes(api_process):
    assert {(method, path) for method, path in api_process["routes"]} == COLLECTOR_ROUTES | CUSTOMER_ROUTES


def test_every_customer_route_is_under_api_and_every_collector_route_is_not():
    routes = main.app.routes

    assert all(isinstance(route, APIRoute) for route in routes)
    under_api = {(method, route.path) for route in routes for method in route.methods if route.path.startswith("/api/")}
    elsewhere = {(method, route.path) for route in routes for method in route.methods} - under_api
    assert under_api == CUSTOMER_ROUTES
    assert elsewhere == COLLECTOR_ROUTES


def test_collector_routes_do_not_use_the_customer_route_class_or_dependencies():
    from customer_api.errors import CustomerApiRoute

    for route in main.app.routes:
        is_customer = route.path.startswith("/api/")
        assert isinstance(route, CustomerApiRoute) is is_customer, route.path
        if not is_customer:
            assert route.dependencies == [], route.path


def test_no_customer_route_takes_a_tenant_identifier():
    for route in main.customer_router.routes:
        names = {param.name for param in route.dependant.path_params + route.dependant.query_params}
        for body_param in route.dependant.body_params:
            names |= set(body_param.field_info.annotation.model_fields)
        assert names.isdisjoint({"customer_id", "branch_id"}), route.path

    login = next(route for route in main.customer_router.routes if route.path == "/api/auth/login")
    (body,) = login.dependant.body_params
    assert set(body.field_info.annotation.model_fields) == {"email", "password"}  # the check above really sees bodies


def test_the_middleware_stack_and_cors_settings_are_unchanged():
    stack = main.app.user_middleware

    assert [middleware.cls.__name__ for middleware in stack] == [
        "SecurityHeadersMiddleware",
        "MaxBodySizeMiddleware",
        "CORSMiddleware",
    ]
    cors = stack[-1].kwargs
    assert cors["allow_credentials"] is False
    assert cors["allow_methods"] == ["GET", "POST"]
    assert cors["allow_headers"] == ["Authorization", "Content-Type"]
    assert cors["allow_origins"] == main.ALLOWED_ORIGINS


def test_the_exception_handlers_and_limiter_are_unchanged():
    assert main.app.exception_handlers[RequestValidationError] is main.safe_request_validation_handler
    assert RateLimitExceeded in main.app.exception_handlers
    assert main.app.state.limiter is main.limiter


def test_the_docs_stay_off_by_default_with_the_customer_router_included():
    assert (main.app.docs_url, main.app.redoc_url, main.app.openapi_url) == (None, None, None)
