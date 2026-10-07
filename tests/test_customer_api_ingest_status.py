"""Block 4c: the first operational read endpoint.

    GET /api/organizations/{org_slug}/branches/{branch_slug}/ingest-status

These tests drive the real production route through TestClient(main.app):
the session dependency, the tenant-scope dependency, the scoped and verified
connection, the read service and the response schema all run for real. What
stands in for PostgreSQL is:

- an in-memory SQLite database for the SaaS tables, so the REAL tenant
  resolver decides which organization/branch pairs resolve;
- a recording fake engine for the operational connection, which answers the
  set_config / current_setting calls the way PostgreSQL does and returns a
  chosen ingest_key_ids row to the REAL read service.

Row level security itself, and the whole path against a real server, are
covered by the PostgreSQL tests in tests/test_rls_phase1_postgres.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import main
from customer_api import operational_routes, tenant_scope
from services import session_service, tenant_resolution_service

PATH = "/api/organizations/{org}/branches/{branch}/ingest-status"
ACME_MAIN = PATH.format(org="acme", branch="main")
COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 1, "email": "alice@example.invalid", "full_name": "Alice"}

CUSTOMER, BRANCH = 8101, 11
KEY_ID = "3db44444-931c-43cc-af3c-b1001443e761"

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
TENANT_NOT_FOUND = {"code": "tenant_not_found", "message": "Organization or branch not found."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

STATUS_FIELDS = [
    "health_status",
    "last_error_class",
    "pending_outbox_count",
    "quarantined_count",
    "oldest_pending_event_at",
    "last_success_at",
    "watcher_last_active_at",
    "last_heartbeat_at",
    "collector_last_run_at",
    "collector_next_run_at",
    "collector_run_duration_ms",
    "collector_schedule_status",
]

# A moment that is always "a little while ago", so nothing here depends on the date the tests run.
HEARTBEAT = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=2)

STATUS_ROW = {
    "health_status": "degraded",
    "last_error_class": "retryable_infra",
    "pending_outbox_count": 12,
    "quarantined_count": 1,
    "oldest_pending_event_at": HEARTBEAT - timedelta(minutes=40),
    "last_success_at": HEARTBEAT - timedelta(minutes=15),
    "watcher_last_active_at": HEARTBEAT - timedelta(minutes=1),
    "last_heartbeat_at": HEARTBEAT,
    "collector_last_run_at": HEARTBEAT - timedelta(minutes=2),
    "collector_next_run_at": HEARTBEAT + timedelta(minutes=13),
    "collector_run_duration_ms": 4321,
    "collector_schedule_status": "ok",
}


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


EXPECTED_STATUS = {key: (_iso(value) if isinstance(value, datetime) else value) for key, value in STATUS_ROW.items()}


# --- SaaS tables for the real resolver -------------------------------------------------------------------------------

_SAAS = (
    "CREATE TABLE app_users (id INTEGER PRIMARY KEY, email TEXT, is_active BOOLEAN)",
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT, status TEXT, operational_customer_id INTEGER)",
    (
        "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, slug TEXT, status TEXT, "
        "operational_branch_id INTEGER)"
    ),
    "CREATE TABLE memberships (id INTEGER PRIMARY KEY, organization_id INTEGER, user_id INTEGER, role TEXT, removed_at TEXT)",
    "INSERT INTO app_users (id, email, is_active) VALUES (1, 'alice@example.invalid', 1)",
    (
        f"INSERT INTO organizations (id, slug, status, operational_customer_id) VALUES "
        f"(1, 'acme', 'active', {CUSTOMER}), (2, 'beta', 'active', 8202), (3, 'unmapped-org', 'active', NULL), "
        f"(4, 'closed', 'cancelled', 8404)"
    ),
    (
        f"INSERT INTO branches (id, organization_id, slug, status, operational_branch_id) VALUES "
        f"({BRANCH}, 1, 'main', 'active', {BRANCH}), (12, 1, 'shut', 'inactive', 12), "
        f"(13, 1, 'unmapped', 'active', NULL), (21, 2, 'north', 'active', 21), (31, 3, 'main', 'active', 31), "
        f"(41, 4, 'main', 'active', 41)"
    ),
    # Alice belongs to acme, unmapped-org and closed -- not to beta.
    "INSERT INTO memberships (organization_id, user_id, role) VALUES (1, 1, 'viewer'), (3, 1, 'admin'), (4, 1, 'admin')",
)


@pytest.fixture
def saas_db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in _SAAS:
            conn.execute(text(statement))
    monkeypatch.setattr(tenant_resolution_service, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


# --- a recording fake of the operational PostgreSQL connection -------------------------------------------------------

class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeOperationalConnection:
    def __init__(self, owner):
        self.owner = owner
        self.settings: dict[str, str] = {}

    def __enter__(self):
        self.owner.log.append("open")
        return self

    def __exit__(self, *_exc):
        self.owner.log.append("close")
        return False

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        owner = self.owner
        if owner.fail_on and owner.fail_on in sql:
            owner.log.append("fail")
            raise RuntimeError(f"synthetic database failure for customer {CUSTOMER} key {KEY_ID}")

        if "set_config('app.operational_customer_id'" in sql:
            self.settings["customer_id"] = parameters["v"]
            return _Result(None)
        if "set_config('app.operational_branch_id'" in sql:
            self.settings["branch_id"] = parameters["v"]
            return _Result(None)
        if "current_setting(" in sql:
            owner.log.append("verify context")
            return _Result(owner.read_back(dict(self.settings)))
        if "FROM ingest_key_ids" in sql:
            owner.log.append("query")
            owner.queries.append((sql, parameters, dict(self.settings)))
            return _Result(owner.row)

        raise AssertionError(f"unexpected statement: {sql}")


class FakeOperationalDatabase:
    """The flat database engine, as the tenant scope sees it."""

    def __init__(self, row=None):
        self.row = row
        self.fail_on: str | None = None
        self.read_back = lambda settings: settings
        self.log: list[str] = []
        self.queries: list[tuple] = []

    def connect(self):
        return FakeOperationalConnection(self)


@pytest.fixture
def operational(monkeypatch):
    database = FakeOperationalDatabase(row=dict(STATUS_ROW))
    monkeypatch.setattr(tenant_scope, "get_engine", lambda: database)
    return database


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: dict(USER))
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


# =====================================================================================================================
# Success
# =====================================================================================================================

def test_a_member_reads_the_branchs_latest_ingest_status(api, saas_db, operational):
    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.status_code == 200
    assert response.json() == {"status": EXPECTED_STATUS}
    assert response.headers["cache-control"] == "no-store"


def test_the_query_runs_for_the_resolved_tenant_on_a_connection_carrying_its_context(api, saas_db, operational):
    api.get(ACME_MAIN, headers=COOKIE)

    ((sql, parameters, settings),) = operational.queries  # exactly one read
    assert parameters == {"customer_id": CUSTOMER, "branch_id": BRANCH}
    assert settings == {"customer_id": str(CUSTOMER), "branch_id": str(BRANCH)}
    assert "customer_id = :customer_id AND branch_id = :branch_id AND status = 'active'" in sql


def test_timestamps_are_serialized_as_iso_8601(api, saas_db, operational):
    status = api.get(ACME_MAIN, headers=COOKIE).json()["status"]

    assert status["last_heartbeat_at"] == _iso(HEARTBEAT)
    for field in ("oldest_pending_event_at", "last_success_at", "watcher_last_active_at", "last_heartbeat_at",
                  "collector_last_run_at", "collector_next_run_at"):
        assert datetime.fromisoformat(status[field]).tzinfo is not None, field


def test_a_key_that_has_never_reported_is_a_status_of_nulls(api, saas_db, operational):
    operational.row = dict.fromkeys(STATUS_FIELDS)

    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.status_code == 200
    assert response.json() == {"status": dict.fromkeys(STATUS_FIELDS)}


def test_a_branch_with_no_active_ingest_key_is_200_with_a_null_status(api, saas_db, operational):
    operational.row = None

    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.status_code == 200
    assert response.json() == {"status": None}
    assert operational.log == ["open", "verify context", "query", "close"]  # it really looked, in the right scope


def test_a_suspended_organization_reads_exactly_like_a_full_one(api, saas_db, operational):
    full = api.get(ACME_MAIN, headers=COOKIE)
    with saas_db.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE slug = 'acme'"))

    read_only = api.get(ACME_MAIN, headers=COOKIE)

    assert read_only.status_code == full.status_code == 200
    assert read_only.json() == full.json() == {"status": EXPECTED_STATUS}
    assert "access_mode" not in read_only.text


def test_any_role_may_read(api, saas_db, operational):
    # The dashboard shows pipeline health to every member; this route makes no role or entitlement decision.
    for role in ("viewer", "manager", "admin", "owner"):
        with saas_db.begin() as conn:
            conn.execute(text("UPDATE memberships SET role = :r WHERE organization_id = 1"), {"r": role})

        assert api.get(ACME_MAIN, headers=COOKIE).status_code == 200, role


# =====================================================================================================================
# What the response may contain
# =====================================================================================================================

def test_the_response_has_exactly_the_wrapper_and_the_twelve_status_fields(api, saas_db, operational):
    body = api.get(ACME_MAIN, headers=COOKIE).json()

    assert list(body) == ["status"]
    assert list(body["status"]) == STATUS_FIELDS


def test_identifiers_in_the_source_row_never_reach_the_response(api, saas_db, operational):
    # A row that (wrongly) carries every identifier column as well: none of it may pass through.
    operational.row = {
        **STATUS_ROW, "id": 700001, "key_id": KEY_ID, "customer_id": CUSTOMER, "branch_id": BRANCH,
        "algorithm": "hmac-sha256-v1", "status": "active", "key_status": "active",
    }

    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.status_code == 200
    assert _keys(response.json()) == {"status", *STATUS_FIELDS}
    for forbidden in (KEY_ID, "700001", str(CUSTOMER), "hmac-sha256-v1", "key_id", "customer_id", "branch_id",
                      "algorithm", "key_status"):
        assert forbidden not in response.text, forbidden


def test_the_response_schema_declares_only_the_approved_fields():
    from customer_api.operational_schemas import (
        IngestStatusFields,
        IngestStatusResponse,
    )

    assert list(IngestStatusResponse.model_fields) == ["status"]
    assert list(IngestStatusFields.model_fields) == STATUS_FIELDS


# =====================================================================================================================
# Authentication and tenant reachability
# =====================================================================================================================

def test_no_session_is_401_and_nothing_is_resolved_or_read(saas_db, operational):
    main.limiter.reset()

    response = TestClient(main.app).get(ACME_MAIN)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert operational.log == []


def test_a_session_that_does_not_validate_is_401(monkeypatch, saas_db, operational):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: None)

    response = TestClient(main.app).get(ACME_MAIN, headers=COOKIE)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert operational.log == []


UNREACHABLE = {
    "unknown organization": ("no-such-org", "main"),
    "organization the user is not a member of": ("beta", "north"),
    "cancelled organization": ("closed", "main"),
    "organization with no operational customer id": ("unmapped-org", "main"),
    "unknown branch": ("acme", "no-such-branch"),
    "inactive branch": ("acme", "shut"),
    "branch belonging to another organization": ("acme", "north"),
    "branch with no operational branch id": ("acme", "unmapped"),
}


@pytest.mark.parametrize(("org", "branch"), UNREACHABLE.values(), ids=UNREACHABLE.keys())
def test_every_unreachable_tenant_is_the_same_404_and_opens_no_connection(api, saas_db, operational, org, branch):
    response = api.get(PATH.format(org=org, branch=branch), headers=COOKIE)

    assert response.status_code == 404
    assert response.json() == TENANT_NOT_FOUND
    assert operational.log == []
    assert "set-cookie" not in response.headers


def test_the_unreachable_responses_are_byte_for_byte_identical(api, saas_db, operational):
    seen = {
        (r.status_code, r.content, json.dumps(sorted(r.headers.items())))
        for r in (api.get(PATH.format(org=org, branch=branch), headers=COOKIE) for org, branch in UNREACHABLE.values())
    }

    assert len(seen) == 1


def test_no_data_and_unreachable_are_different_answers(api, saas_db, operational):
    operational.row = None

    assert api.get(ACME_MAIN, headers=COOKIE).status_code == 200
    assert api.get(PATH.format(org="acme", branch="shut"), headers=COOKIE).status_code == 404


# =====================================================================================================================
# The request cannot choose the tenant
# =====================================================================================================================

def test_operational_ids_and_other_tenant_hints_in_the_request_change_nothing(api, saas_db, operational):
    plain = api.get(ACME_MAIN, headers=COOKIE)
    operational.queries.clear()

    tampered = api.get(
        ACME_MAIN,
        headers={**COOKIE, "X-Customer-Id": "8202", "X-Branch-Id": "21"},
        params={"customer_id": 8202, "branch_id": 21, "operational_customer_id": 8202, "operational_branch_id": 21,
                "org_slug": "beta", "branch_slug": "north", "user_id": 2, "key_id": KEY_ID, "status": "retired",
                "limit": 1000, "offset": 5, "page": 3, "from": "2020-01-01", "to": "2030-01-01"},
    )

    assert tampered.status_code == 200
    assert tampered.json() == plain.json()
    ((_sql, parameters, settings),) = operational.queries
    assert parameters == {"customer_id": CUSTOMER, "branch_id": BRANCH}
    assert settings == {"customer_id": str(CUSTOMER), "branch_id": str(BRANCH)}


def test_the_route_takes_only_the_two_path_slugs():
    route = next(r for r in main.app.routes if r.path.endswith("/ingest-status"))
    flat = get_flat_dependant(route.dependant)

    assert route.methods == {"GET"}
    assert sorted(p.name for p in flat.path_params) == ["branch_slug", "org_slug"]
    assert flat.query_params == []  # no pagination, no date range, no filter
    assert flat.body_params == []
    assert flat.header_params == []
    assert flat.cookie_params == []


def test_the_route_is_read_only(api):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(api, method)(ACME_MAIN, headers=COOKIE).status_code == 405


# =====================================================================================================================
# Failures are 500, never an empty answer
# =====================================================================================================================

def _assert_generic_500(response) -> None:
    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    for leaked in ("synthetic", str(CUSTOMER), KEY_ID, "ingest_key_ids", "current_setting", "set_config", "SELECT"):
        assert leaked not in response.text, leaked


def test_a_resolver_database_failure_is_500(api, monkeypatch, operational):
    def broken_resolve(user_id, org_slug, branch_slug):
        raise RuntimeError(f"synthetic database failure for customer {CUSTOMER}")

    monkeypatch.setattr(tenant_scope, "resolve_operational_tenant", broken_resolve)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE))
    assert operational.log == []


def test_an_engine_failure_is_500(api, monkeypatch, saas_db):
    def broken_get_engine():
        raise RuntimeError("synthetic engine failure")

    monkeypatch.setattr(tenant_scope, "get_engine", broken_get_engine)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE))


def test_a_failure_setting_the_tenant_context_is_500(api, saas_db, operational):
    operational.fail_on = "set_config('app.operational_branch_id'"

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE))
    assert operational.log == ["open", "fail", "close"]


@pytest.mark.parametrize(
    "read_back",
    [
        lambda settings: {**settings, "customer_id": None},
        lambda settings: {**settings, "branch_id": ""},
        lambda settings: {**settings, "customer_id": "8202"},
        lambda settings: {"customer_id": settings["branch_id"], "branch_id": settings["customer_id"]},
    ],
    ids=["customer context missing", "branch context blank", "another customer's context", "ids swapped"],
)
def test_a_tenant_context_that_does_not_verify_is_500_and_no_query_runs(api, saas_db, operational, read_back):
    operational.read_back = read_back
    operational.row = None  # if the query DID run it would look like "no data": that must not be the answer

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE))
    assert operational.log == ["open", "verify context", "close"]
    assert operational.queries == []


def test_a_failing_read_is_500_not_a_null_status(api, saas_db, operational, caplog):
    operational.fail_on = "FROM ingest_key_ids"

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE))
    assert operational.log == ["open", "verify context", "fail", "close"]
    assert KEY_ID not in caplog.text and "synthetic" not in caplog.text  # logged as a safe summary only


# =====================================================================================================================
# Lifecycle and wiring
# =====================================================================================================================

def test_the_connection_is_closed_before_the_response_is_built(api, monkeypatch, saas_db, operational):
    real_response = operational_routes.JSONResponse

    def recording_response(*args, **kwargs):
        operational.log.append("build response")
        return real_response(*args, **kwargs)

    monkeypatch.setattr(operational_routes, "JSONResponse", recording_response)

    assert api.get(ACME_MAIN, headers=COOKIE).status_code == 200
    assert operational.log == ["open", "verify context", "query", "close", "build response"]


def test_every_request_opens_its_own_connection_and_nothing_is_cached(api, saas_db, operational):
    assert api.get(ACME_MAIN, headers=COOKIE).json()["status"]["health_status"] == "degraded"

    operational.row = {**STATUS_ROW, "health_status": "healthy"}
    assert api.get(ACME_MAIN, headers=COOKIE).json()["status"]["health_status"] == "healthy"

    operational.row = None
    assert api.get(ACME_MAIN, headers=COOKIE).json() == {"status": None}
    assert operational.log == ["open", "verify context", "query", "close"] * 3


def test_the_route_reaches_the_database_only_through_the_tenant_scope_and_the_read_service():
    import inspect

    source = inspect.getsource(operational_routes)

    assert operational_routes.require_resolved_tenant is tenant_scope.require_resolved_tenant
    assert operational_routes.open_customer_tenant_connection is tenant_scope.open_customer_tenant_connection
    for forbidden in ("get_engine", "tenant_connection(", "resolve_operational_tenant", "import database",
                      "from database", "tenant_db", "set_config", "data_loader", "ingest_v2", "pandas", "streamlit",
                      "import main", "from main"):
        assert forbidden not in source.replace("open_customer_tenant_connection(", ""), forbidden
