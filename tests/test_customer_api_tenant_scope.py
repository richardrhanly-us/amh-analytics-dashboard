"""Block 4b: the tenant scope of a customer request (customer_api.tenant_scope).

    require_resolved_tenant            user + path slugs -> resolved tenant, or 404
    open_customer_tenant_connection    resolved tenant -> RLS-scoped, VERIFIED connection

No production route uses these yet. To exercise them through the real
customer error contract (the route class, the session dependency, the
{"code", "message"} bodies), this file mounts them on a throwaway route in a
throwaway FastAPI app that exists only inside these tests.

Three kinds of stand-in are used, each where it proves the most:

- the REAL tenant resolver against in-memory SQLite, for which organization /
  branch pairs resolve;
- the REAL tenant_db.tenant_connection against a recording fake engine, for
  what is set on the connection, what is read back and when it is closed
  (SQLite has no set_config, so a fake connection plays PostgreSQL's part);
- small recording fakes for everything else.

The PostgreSQL end-to-end test of the whole path belongs to the first route
that uses it.
"""

from __future__ import annotations

import ast
import inspect
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import database
import tenant_db
from customer_api import tenant_scope
from customer_api.errors import CustomerApiRoute
from customer_api.tenant_scope import (
    TenantContextError,
    open_customer_tenant_connection,
    require_resolved_tenant,
)
from services import session_service, tenant_resolution_service
from services.tenant_resolution_service import ResolvedOperationalTenant

COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 1, "email": "alice@example.invalid", "full_name": "Alice"}

CUSTOMER, BRANCH = 8101, 11
TENANT = ResolvedOperationalTenant(
    org_slug="acme", branch_slug="main", access_mode="full",
    operational_customer_id=CUSTOMER, operational_branch_id=BRANCH,
)

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
TENANT_NOT_FOUND = {"code": "tenant_not_found", "message": "Organization or branch not found."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}


# =====================================================================================================================
# A throwaway app: one route that resolves the tenant and one that also opens the scoped connection
# =====================================================================================================================

def _build_app() -> FastAPI:
    router = APIRouter(route_class=CustomerApiRoute)

    @router.get("/probe/{org_slug}/{branch_slug}/resolved")
    def resolved(tenant: ResolvedOperationalTenant = Depends(require_resolved_tenant)):  # noqa: B008
        return {"org_slug": tenant.org_slug, "branch_slug": tenant.branch_slug, "access_mode": tenant.access_mode}

    @router.get("/probe/{org_slug}/{branch_slug}/connected")
    def connected(tenant: ResolvedOperationalTenant = Depends(require_resolved_tenant)):  # noqa: B008
        with open_customer_tenant_connection(tenant) as conn:
            value = conn.execute(text("SELECT 'downstream query'")).scalar()
        return {"value": value}

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: dict(USER))
    return TestClient(_build_app())


# --- the real resolver, on SQLite ------------------------------------------------------------------------------------

_RESOLVER_DDL = (
    "CREATE TABLE app_users (id INTEGER PRIMARY KEY, email TEXT, is_active BOOLEAN)",
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT, status TEXT, operational_customer_id INTEGER)",
    (
        "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, slug TEXT, status TEXT, "
        "operational_branch_id INTEGER)"
    ),
    "CREATE TABLE memberships (id INTEGER PRIMARY KEY, organization_id INTEGER, user_id INTEGER, role TEXT)",
    "INSERT INTO app_users (id, email, is_active) VALUES (1, 'alice@example.invalid', 1)",
    (
        f"INSERT INTO organizations (id, slug, status, operational_customer_id) VALUES "
        f"(1, 'acme', 'active', {CUSTOMER}), (2, 'beta', 'active', 8202), (3, 'unmapped-org', 'active', NULL), "
        f"(4, 'closed', 'cancelled', 8404)"
    ),
    (
        f"INSERT INTO branches (id, organization_id, slug, status, operational_branch_id) VALUES "
        f"({BRANCH}, 1, 'main', 'active', {BRANCH}), (12, 1, 'shut', 'inactive', 12), (13, 1, 'unmapped', 'active', NULL), "
        f"(21, 2, 'north', 'active', 21), (31, 3, 'main', 'active', 31), (41, 4, 'main', 'active', 41)"
    ),
    # Alice belongs to acme, unmapped-org and closed -- not to beta.
    "INSERT INTO memberships (organization_id, user_id, role) VALUES (1, 1, 'admin'), (3, 1, 'admin'), (4, 1, 'admin')",
)


@pytest.fixture
def saas_db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in _RESOLVER_DDL:
            conn.execute(text(statement))
    monkeypatch.setattr(tenant_resolution_service, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


# --- a recording fake of a PostgreSQL connection ---------------------------------------------------------------------

class FakePgConnection:
    """Plays the part of a PostgreSQL connection: remembers what set_config
    was given, answers current_setting from it, and records its lifecycle."""

    def __init__(self, log: list, *, read_back=None, fail_on: str | None = None):
        self.log = log
        self.settings: dict[str, str] = {}
        self.read_back = read_back
        self.fail_on = fail_on

    def __enter__(self):
        self.log.append("open")
        return self

    def __exit__(self, *_exc):
        self.log.append("close")
        return False

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        if self.fail_on and self.fail_on in sql:
            self.log.append(f"fail:{self.fail_on}")
            raise RuntimeError("synthetic database failure")

        if "set_config('app.operational_customer_id'" in sql:
            self.settings["customer_id"] = parameters["v"]
            self.log.append(("set customer", parameters["v"]))
            return _Result(None)
        if "set_config('app.operational_branch_id'" in sql:
            self.settings["branch_id"] = parameters["v"]
            self.log.append(("set branch", parameters["v"]))
            return _Result(None)
        if "current_setting('app.operational_customer_id', true)" in sql:
            assert "current_setting('app.operational_branch_id', true)" in sql
            self.log.append("read back")
            row = dict(self.settings) if self.read_back is None else self.read_back(dict(self.settings))
            return _Result(row)

        self.log.append("downstream query")
        return _Result({"value": "downstream query"})


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row

    def scalar(self):
        return None if self._row is None else next(iter(self._row.values()))


class FakeEngine:
    def __init__(self, log: list, **connection_options):
        self.log = log
        self.connection_options = connection_options
        self.connections: list[FakePgConnection] = []

    def connect(self):
        connection = FakePgConnection(self.log, **self.connection_options)
        self.connections.append(connection)
        return connection


@pytest.fixture
def pg(monkeypatch):
    """Installs a fake engine as the flat database engine and returns a
    factory for it, so a test can choose how the connection misbehaves."""
    log: list = []

    def install(**connection_options) -> FakeEngine:
        engine = FakeEngine(log, **connection_options)
        monkeypatch.setattr(tenant_scope, "get_engine", lambda: engine)
        return engine

    install.log = log
    return install


def _resolves_to(monkeypatch, tenant):
    calls = []

    def fake_resolve(user_id, org_slug, branch_slug):
        calls.append((user_id, org_slug, branch_slug))
        if isinstance(tenant, Exception):
            raise tenant
        return tenant

    monkeypatch.setattr(tenant_scope, "resolve_operational_tenant", fake_resolve)
    return calls


# =====================================================================================================================
# Resolution
# =====================================================================================================================

def test_a_member_resolves_an_active_branch(api, saas_db):
    response = api.get("/probe/acme/main/resolved", headers=COOKIE)

    assert response.status_code == 200
    assert response.json() == {"org_slug": "acme", "branch_slug": "main", "access_mode": "full"}


def test_a_suspended_organization_resolves_for_reading(api, saas_db):
    with saas_db.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE slug = 'acme'"))

    response = api.get("/probe/acme/main/resolved", headers=COOKIE)

    assert response.status_code == 200
    assert response.json()["access_mode"] == "read_only"


UNREACHABLE = {
    "unknown organization": "/probe/no-such-org/main/resolved",
    "organization the user is not a member of": "/probe/beta/north/resolved",
    "cancelled organization": "/probe/closed/main/resolved",
    "organization with no operational customer id": "/probe/unmapped-org/main/resolved",
    "unknown branch": "/probe/acme/no-such-branch/resolved",
    "branch of another organization": "/probe/acme/north/resolved",
    "inactive branch": "/probe/acme/shut/resolved",
    "branch with no operational branch id": "/probe/acme/unmapped/resolved",
}


@pytest.mark.parametrize("path", UNREACHABLE.values(), ids=UNREACHABLE.keys())
def test_every_unreachable_tenant_gets_the_same_404(api, saas_db, path):
    response = api.get(path, headers=COOKIE)

    assert response.status_code == 404
    assert response.json() == TENANT_NOT_FOUND
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers  # an unreachable tenant does not end the session


def test_the_unreachable_responses_are_byte_for_byte_identical(api, saas_db):
    seen = {
        (r.status_code, r.content, json.dumps(sorted(r.headers.items())))
        for r in (api.get(path, headers=COOKIE) for path in UNREACHABLE.values())
    }

    assert len(seen) == 1


def test_resolution_uses_the_session_user_and_the_path_slugs_only(api, monkeypatch):
    calls = _resolves_to(monkeypatch, TENANT)

    response = api.get(
        "/probe/acme/main/resolved",
        headers=COOKIE,
        params={"user_id": 999, "customer_id": 1, "branch_id": 2, "org_slug": "beta", "branch_slug": "north",
                "operational_customer_id": 3, "operational_branch_id": 4},
    )

    assert response.status_code == 200
    assert calls == [(1, "acme", "main")]


def test_the_dependency_declares_only_the_two_path_slugs():
    app = _build_app()
    route = next(r for r in app.routes if getattr(r, "path", "").endswith("/resolved"))

    # Flattened: the slugs are declared by the dependency, not by the route function itself.
    flat = get_flat_dependant(route.dependant)

    assert sorted(p.name for p in flat.path_params) == ["branch_slug", "org_slug"]
    assert flat.query_params == []
    assert flat.body_params == []
    assert flat.header_params == []
    assert flat.cookie_params == []
    assert list(inspect.signature(require_resolved_tenant).parameters) == ["org_slug", "branch_slug", "user"]


def test_a_resolver_failure_is_a_generic_500_not_a_404(api, monkeypatch, caplog):
    _resolves_to(monkeypatch, RuntimeError(f"synthetic database failure for customer {CUSTOMER}"))

    response = api.get("/probe/acme/main/resolved", headers=COOKIE)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert str(CUSTOMER) not in response.text
    assert "synthetic database failure" not in caplog.text  # logged as a safe summary, never the exception's text


def test_resolution_requires_a_session(monkeypatch):
    calls = _resolves_to(monkeypatch, TENANT)

    response = TestClient(_build_app()).get("/probe/acme/main/resolved")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert calls == []


def test_a_session_that_does_not_validate_is_401_before_any_resolution(monkeypatch):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: None)
    calls = _resolves_to(monkeypatch, TENANT)

    response = TestClient(_build_app()).get("/probe/acme/main/resolved", headers=COOKIE)

    assert response.status_code == 401
    assert calls == []


# =====================================================================================================================
# The scoped connection: engine, ids, lifecycle
# =====================================================================================================================

def test_the_connection_is_scoped_verified_used_and_then_closed(pg):
    engine = pg()

    with open_customer_tenant_connection(TENANT) as conn:
        assert conn is engine.connections[0]  # the very connection tenant_connection opened
        conn.execute(text("SELECT 1"))

    assert pg.log == [
        "open",
        ("set customer", str(CUSTOMER)),
        ("set branch", str(BRANCH)),
        "read back",
        "downstream query",
        "close",
    ]
    assert len(engine.connections) == 1


def test_tenant_connection_receives_the_flat_engine_and_exactly_the_resolved_ids(monkeypatch):
    engine = object()
    connection = object()
    seen = {}

    @contextmanager
    def fake_tenant_connection(engine_, customer_id, branch_id):
        seen["args"] = (engine_, customer_id, branch_id)
        yield connection

    monkeypatch.setattr(tenant_scope, "get_engine", lambda: engine)
    monkeypatch.setattr(tenant_scope, "tenant_connection", fake_tenant_connection)
    monkeypatch.setattr(tenant_scope, "_verify_tenant_context", lambda conn, tenant: seen.update(verified=(conn, tenant)))

    with open_customer_tenant_connection(TENANT) as conn:
        assert conn is connection

    assert seen["args"] == (engine, CUSTOMER, BRANCH)
    assert seen["verified"] == (connection, TENANT)  # verified on the same connection, for the same tenant


def test_the_engine_is_the_flat_database_engine_shared_with_the_resolver(monkeypatch):
    assert tenant_scope.get_engine is database.get_engine
    assert tenant_resolution_service.get_engine is database.get_engine
    assert tenant_scope.tenant_connection is tenant_db.tenant_connection

    # One patch point -- the flat database module's engine -- reaches both.
    shared = object()
    monkeypatch.setattr(database, "_engine", shared)

    assert tenant_scope.get_engine() is shared
    assert tenant_resolution_service.get_engine() is shared


def test_the_module_creates_no_engine_and_never_touches_main():
    source = inspect.getsource(tenant_scope)
    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "create_engine" not in source
    assert not hasattr(tenant_scope, "create_engine")
    assert not any(name == "main" or name.startswith(("main.", "src")) for name in imported)
    assert "main.engine" not in source


def test_the_connection_is_closed_when_a_downstream_query_raises(pg):
    pg()

    with pytest.raises(ZeroDivisionError), open_customer_tenant_connection(TENANT) as conn:
        conn.execute(text("SELECT 1"))
        raise ZeroDivisionError

    assert pg.log[-2:] == ["downstream query", "close"]


def test_a_failure_opening_the_engine_propagates(monkeypatch):
    def broken_get_engine():
        raise RuntimeError("synthetic engine failure")

    monkeypatch.setattr(tenant_scope, "get_engine", broken_get_engine)

    with pytest.raises(RuntimeError, match="synthetic engine failure"), open_customer_tenant_connection(TENANT):
        pytest.fail("the block must not run")


def test_a_failure_setting_the_context_propagates_and_closes_the_connection(pg):
    pg(fail_on="set_config('app.operational_branch_id'")

    with pytest.raises(RuntimeError, match="synthetic database failure"), open_customer_tenant_connection(TENANT):
        pytest.fail("the block must not run")

    assert pg.log == ["open", ("set customer", str(CUSTOMER)), "fail:set_config('app.operational_branch_id'", "close"]


# =====================================================================================================================
# Context verification
# =====================================================================================================================

def _tamper(**changes):
    """A read-back that reports the real settings with `changes` applied;
    a value of ... removes the key's value (NULL, as for a never-set setting)."""
    def read_back(settings):
        for key, value in changes.items():
            settings[key] = None if value is ... else value
        return settings

    return read_back


BAD_CONTEXTS = {
    "customer setting missing": _tamper(customer_id=...),
    "branch setting missing": _tamper(branch_id=...),
    "both settings missing": _tamper(customer_id=..., branch_id=...),
    "customer setting blank": _tamper(customer_id=""),
    "branch setting blank": _tamper(branch_id=""),
    "customer setting malformed": _tamper(customer_id="not-a-number"),
    "branch setting malformed": _tamper(branch_id="11; DROP"),
    "customer setting padded": _tamper(customer_id=f" {CUSTOMER}"),
    "branch setting zero-padded": _tamper(branch_id=f"0{BRANCH}"),
    "wrong customer": _tamper(customer_id="8202"),
    "wrong branch": _tamper(branch_id="21"),
    "customer and branch swapped": _tamper(customer_id=str(BRANCH), branch_id=str(CUSTOMER)),
    "integer where text is expected": _tamper(customer_id=CUSTOMER),
    "no row at all": lambda settings: None,
}


@pytest.mark.parametrize("read_back", BAD_CONTEXTS.values(), ids=BAD_CONTEXTS.keys())
def test_a_context_that_is_not_exactly_the_resolved_tenants_is_refused_and_the_connection_closed(pg, read_back):
    pg(read_back=read_back)

    with pytest.raises(TenantContextError), open_customer_tenant_connection(TENANT):
        pytest.fail("a connection with an unverified tenant context must never be handed over")

    assert pg.log == ["open", ("set customer", str(CUSTOMER)), ("set branch", str(BRANCH)), "read back", "close"]


def test_a_database_failure_while_reading_the_context_back_propagates_and_closes_the_connection(pg):
    pg(fail_on="current_setting")

    with pytest.raises(RuntimeError, match="synthetic database failure"), open_customer_tenant_connection(TENANT):
        pytest.fail("the block must not run")

    assert pg.log[-2:] == ["fail:current_setting", "close"]


def test_the_context_error_carries_no_tenant_identifier(pg):
    pg(read_back=_tamper(customer_id="8202"))

    with pytest.raises(TenantContextError) as raised, open_customer_tenant_connection(TENANT):
        pass

    assert str(raised.value) == "The tenant context on the connection does not match the resolved tenant."
    for identifier in (str(CUSTOMER), str(BRANCH), "8202"):
        assert identifier not in str(raised.value)
    assert isinstance(raised.value, RuntimeError)


def test_the_read_back_uses_the_settings_tenant_db_sets():
    read_sql = " ".join(str(tenant_scope._READ_TENANT_CONTEXT_SQL).split())

    for setting in ("app.operational_customer_id", "app.operational_branch_id"):
        assert f"set_config('{setting}', :v, true)" in str(tenant_db._CUSTOMER_CONTEXT_SQL) + str(tenant_db._BRANCH_CONTEXT_SQL)
        assert f"current_setting('{setting}', true)" in read_sql
    assert "FROM" not in read_sql  # it reads settings only, no table


def test_ids_of_zero_verify_like_any_other(pg):
    zero = ResolvedOperationalTenant(
        org_slug="acme", branch_slug="main", access_mode="full", operational_customer_id=0, operational_branch_id=0,
    )
    pg()

    with open_customer_tenant_connection(zero):
        pass

    assert ("set customer", "0") in pg.log and "close" in pg.log


# =====================================================================================================================
# Through the customer error contract
# =====================================================================================================================

def test_a_verified_connection_serves_the_request_and_is_closed_before_the_response(api, monkeypatch, pg):
    _resolves_to(monkeypatch, TENANT)
    pg()

    response = api.get("/probe/acme/main/connected", headers=COOKIE)

    assert response.status_code == 200
    assert response.json() == {"value": "downstream query"}
    assert pg.log[-2:] == ["downstream query", "close"]


@pytest.mark.parametrize("read_back", [BAD_CONTEXTS["customer setting missing"], BAD_CONTEXTS["wrong branch"]],
                         ids=["missing", "mismatched"])
def test_a_bad_tenant_context_is_a_generic_500_never_an_empty_answer(api, monkeypatch, pg, caplog, read_back):
    _resolves_to(monkeypatch, TENANT)
    pg(read_back=read_back)

    response = api.get("/probe/acme/main/connected", headers=COOKIE)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert "downstream query" not in pg.log
    assert pg.log[-1] == "close"
    assert "TenantContextError" in caplog.text  # the safe summary names the error type
    for identifier in (str(CUSTOMER), "8202"):
        assert identifier not in response.text


@pytest.mark.parametrize("fail_on", ["set_config('app.operational_customer_id'", "current_setting", "SELECT 'downstream query'"],
                         ids=["setting the context", "reading it back", "the downstream query"])
def test_a_database_failure_anywhere_in_the_scoped_block_is_a_generic_500(api, monkeypatch, pg, fail_on):
    _resolves_to(monkeypatch, TENANT)
    pg(fail_on=fail_on)

    response = api.get("/probe/acme/main/connected", headers=COOKIE)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert pg.log[-1] == "close"


def test_an_engine_failure_is_a_generic_500(api, monkeypatch):
    _resolves_to(monkeypatch, TENANT)

    def broken_get_engine():
        raise RuntimeError("synthetic engine failure")

    monkeypatch.setattr(tenant_scope, "get_engine", broken_get_engine)

    response = api.get("/probe/acme/main/connected", headers=COOKIE)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR


def test_an_unresolved_tenant_never_opens_a_connection(api, monkeypatch, pg):
    _resolves_to(monkeypatch, None)
    pg()

    response = api.get("/probe/acme/main/connected", headers=COOKIE)

    assert response.status_code == 404
    assert response.json() == TENANT_NOT_FOUND
    assert pg.log == []


# =====================================================================================================================
# Module boundaries
# =====================================================================================================================

def _imported_modules(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_the_module_depends_only_on_what_it_is_allowed_to():
    imported = _imported_modules(Path(tenant_scope.__file__))

    assert imported == {
        "__future__", "collections.abc", "contextlib", "typing", "fastapi", "sqlalchemy", "sqlalchemy.engine",
        "customer_api.auth_dependencies", "customer_api.errors", "database",
        "services.tenant_resolution_service", "tenant_db",
    }


def test_the_module_has_no_streamlit_pandas_data_loader_or_collector_dependency():
    source = inspect.getsource(tenant_scope)

    for forbidden in ("streamlit", "pandas", "data_loader", "ingest_v2", "collector_enrollment", "mixed_era",
                      "cache_data", "lru_cache"):
        assert forbidden not in source, forbidden


def test_the_module_holds_no_state_and_caches_nothing():
    for name in ("require_resolved_tenant", "open_customer_tenant_connection"):
        function = getattr(tenant_scope, name)
        assert not hasattr(function, "cache_clear"), name
        assert not hasattr(function, "clear"), name


def test_only_the_operational_and_report_routes_use_the_tenant_scope():
    # Block 4c gave the tenant scope its one production caller. Any other
    # customer module that starts opening tenant connections should be a
    # deliberate change to this list.
    package = Path(tenant_scope.__file__).parent
    users = sorted(
        path.name for path in package.glob("*.py")
        if path.name != "tenant_scope.py" and "tenant_scope" in path.read_text(encoding="utf-8")
    )

    # Reports R2 added the second: the sorter-site range reports, which resolve
    # and scope a request exactly as the single-day reads do.
    # Reports R4 added the third: the organization reports, which resolve and open one sorter site at a time
    # through this module and never a scope of their own (tests/test_customer_api_organization_reports.py).
    # Reports R6C added the fourth: the sorter Efficiency report, which reads its check-ins by day through the
    # same resolved tenant and the same verified connection as the other sorter-site reports
    # (tests/test_customer_api_efficiency_report.py).
    assert users == [
        "efficiency_report_routes.py",
        "operational_routes.py",
        "organization_report_routes.py",
        "report_routes.py",
    ]
