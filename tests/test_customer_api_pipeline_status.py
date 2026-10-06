"""Block 9e: the branch pipeline status endpoint.

    GET /api/organizations/{org_slug}/branches/{branch_slug}/pipeline-status

    {"timezone": "America/Chicago", "state": "ok", "last_reported_at": "2026-10-05T18:45:03Z"}

`state` is what the branch's collection pipeline last REPORTED -- ok, degraded, failed or unknown -- and
`last_reported_at` the instant that report was received (UTC), or null. The endpoint makes no judgement of how old
the report is.

These tests drive the real production route through TestClient(main.app): the session dependency, the tenant-scope
dependency, the scoped and verified connection, the status service, the state mapping and the response schema all
run for real. What stands in for PostgreSQL is:

- an in-memory SQLite database for the SaaS tables, so the REAL tenant resolver decides which organization/branch
  pairs resolve;
- a recording fake engine for the operational connection, which answers the set_config / current_setting calls the
  way PostgreSQL does and returns a chosen cutover, a chosen legacy row and a chosen current row to the REAL
  service. Times are handed back as aware datetimes in a NON-UTC offset, as a driver in a non-UTC session does.

Where the route needs the current instant it reads `datetime.now(UTC)`; the tests replace that one name in the route
module with a fixed clock, so which source a cutover selects is exact and nothing here depends on when it runs.

Source and signal selection are tested in tests/test_pipeline_status_service.py, the state mapping in
tests/test_pipeline_state.py, and row level security, real TIMESTAMPTZ values and the database session time zone
against a real server in tests/test_rls_phase1_postgres.py. This file is about the HTTP contract.
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import ClassVar

import pytest
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import main
from customer_api import operational_routes, operational_schemas, tenant_scope
from customer_api import settings as customer_settings
from customer_api.operational_schemas import (
    IngestStatusResponse,
    PipelineStatusResponse,
)
from services import (
    pipeline_state,
    pipeline_status_service,
    session_service,
    tenant_resolution_service,
)
from services.pipeline_state import PIPELINE_STATES, PipelineState
from services.pipeline_status_service import PipelineStatus

PATH = "/api/organizations/{org}/branches/{branch}/pipeline-status"
ACME_MAIN = PATH.format(org="acme", branch="main")
INGEST_STATUS_ACME_MAIN = "/api/organizations/acme/branches/main/ingest-status"
REJECT_COUNT_ACME_MAIN = "/api/organizations/acme/branches/main/rejects/count"
COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 1, "email": "alice@example.invalid", "full_name": "Alice"}

CUSTOMER, BRANCH = 8101, 11

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
TENANT_NOT_FOUND = {"code": "tenant_not_found", "message": "Organization or branch not found."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

NOW = datetime(2026, 10, 5, 19, 0, tzinfo=UTC)  # freshness: allow FRESH004 -- the fixed clock the route is given
REPORTED = datetime(2026, 10, 5, 18, 45, 3, tzinfo=UTC)
REPORTED_JSON = "2026-10-05T18:45:03Z"
EARLIER = REPORTED - timedelta(minutes=30)
LONG_AGO = NOW - timedelta(days=30)
TOKYO = timezone(timedelta(hours=9))            # the offset the fake "driver" hands times back in
CANARY = "CANARY-raw-error-31234000123456"
KEY_ID = "3db44444-931c-43cc-af3c-b1001443e761"


def in_session_offset(moment: datetime | None) -> datetime | None:
    """`moment` as a driver in a Tokyo session returns it: the same instant, a different offset."""
    return None if moment is None else moment.astimezone(TOKYO)


class FixedClock:
    """Stands in for the `datetime` name in the route module: now(UTC) is NOW, and every call is recorded."""

    calls: ClassVar[list[object]] = []

    @classmethod
    def now(cls, tz=None):
        cls.calls.append(tz)
        return NOW if tz is None else NOW.astimezone(tz)


# --- SaaS tables for the real resolver -------------------------------------------------------------------------------

_SAAS = (
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
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


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

        if "set_config('app.operational_customer_id'" in sql:
            self.settings["customer_id"] = parameters["v"]
            return _Result([])
        if "set_config('app.operational_branch_id'" in sql:
            self.settings["branch_id"] = parameters["v"]
            return _Result([])
        if "current_setting(" in sql:
            owner.log.append("verify context")
            if owner.fail_on == "context":
                raise RuntimeError("synthetic database failure")
            return _Result([owner.read_back(dict(self.settings))])

        for table, rows in (
            ("v2_cutovers", [] if owner.cutover is None else [(owner.cutover,)]),
            ("pipeline_status", [] if owner.legacy is None else [owner.legacy]),
            ("ingest_key_ids", [] if owner.current is None else [owner.current]),
        ):
            if f"FROM {table} " in sql:
                owner.log.append(table)
                owner.queries.append((table, dict(parameters or {}), dict(self.settings)))
                owner.statements.append(sql)
                if owner.fail_on == table:
                    raise RuntimeError(f"synthetic database failure reading {table} for customer {CUSTOMER} {CANARY}")
                return _Result(rows)

        raise AssertionError(f"unexpected statement: {sql}")


class FakeOperationalDatabase:
    """The flat database engine, as the tenant scope sees it: an optional cutover, an optional legacy row and an
    optional current row. Each row carries MORE than the service selects, so anything that leaked would show."""

    def __init__(self):
        self.cutover: datetime | None = None
        self.legacy: dict | None = None
        self.current: dict | None = None
        self.fail_on: str | None = None
        self.read_back = lambda settings: settings
        self.log: list[str] = []
        self.queries: list[tuple] = []
        self.statements: list[str] = []

    def connect(self):
        return FakeOperationalConnection(self)

    def set_legacy(self, *, status=None, health_status=None, status_at=None, health_at=None) -> None:
        self.legacy = {
            "status": status, "health_status": health_status,
            "status_reported_at": in_session_offset(status_at), "health_status_reported_at": in_session_offset(health_at),
            # never selected, never returned:
            "customer_id": CUSTOMER, "branch_id": BRANCH, "last_error": CANARY, "checkins_rows": 4001,
            "destination_breakdown": {"CANARY-DESTINATION": 9}, "updated_at": datetime(1988, 1, 1),  # noqa: DTZ001
        }

    def set_current(self, *, health_status="healthy", schedule="healthy", heartbeat_at=REPORTED) -> None:
        self.current = {
            "health_status": health_status, "collector_schedule_status": schedule,
            "last_heartbeat_at": in_session_offset(heartbeat_at),
            # the rest of what /ingest-status reads, and what nothing may return:
            "last_error_class": "retryable_infra", "pending_outbox_count": 7003, "quarantined_count": 7004,
            "oldest_pending_event_at": None, "last_success_at": in_session_offset(EARLIER),
            "watcher_last_active_at": in_session_offset(EARLIER), "collector_last_run_at": in_session_offset(EARLIER),
            "collector_next_run_at": in_session_offset(NOW), "collector_run_duration_ms": 7005,
            "key_id": KEY_ID, "customer_id": CUSTOMER, "branch_id": BRANCH,
        }


@pytest.fixture
def operational(monkeypatch):
    database = FakeOperationalDatabase()
    monkeypatch.setattr(tenant_scope, "get_engine", lambda: database)
    return database


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: dict(USER))
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    FixedClock.calls = []
    monkeypatch.setattr(operational_routes, "datetime", FixedClock)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


def _body(state: str, last_reported_at: str | None, timezone_name: str = "America/Chicago") -> dict:
    return {"timezone": timezone_name, "state": state, "last_reported_at": last_reported_at}


# =====================================================================================================================
# A. The public contract
# =====================================================================================================================

def test_a_member_gets_the_branchs_last_reported_state_and_when_it_was_received(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)

    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.status_code == 200
    assert response.json() == {"timezone": "America/Chicago", "state": "ok", "last_reported_at": "2026-10-05T18:45:03Z"}
    assert response.headers["cache-control"] == "no-store"


def test_the_response_has_exactly_the_three_approved_keys(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)

    body = api.get(ACME_MAIN, headers=COOKIE).json()

    assert list(body) == ["timezone", "state", "last_reported_at"]
    assert type(body["timezone"]) is str and type(body["state"]) is str and type(body["last_reported_at"]) is str


def test_the_time_is_a_utc_instant_ending_in_z_whatever_offset_the_database_returned_it_in(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)
    assert operational.legacy["status_reported_at"].utcoffset() == timedelta(hours=9)     # the "driver" said Tokyo

    stamp = api.get(ACME_MAIN, headers=COOKIE).json()["last_reported_at"]

    assert stamp == REPORTED_JSON and stamp.endswith("Z")
    assert "+" not in stamp and stamp.count("-") == 2                                      # no offset of any kind
    assert datetime.fromisoformat(stamp) == REPORTED


def test_sub_second_precision_is_kept_and_is_still_utc(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED + timedelta(microseconds=123456))

    assert api.get(ACME_MAIN, headers=COOKIE).json()["last_reported_at"] == "2026-10-05T18:45:03.123456Z"


def test_a_branch_that_has_reported_nothing_is_unknown_with_a_null_time_and_still_200(api, saas_db, operational):
    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.status_code == 200
    assert response.json() == _body("unknown", None)
    assert response.json()["last_reported_at"] is None
    assert operational.log == ["open", "verify context", "v2_cutovers", "pipeline_status", "close"]   # it really looked


def test_the_response_is_json_with_the_same_headers_as_its_sibling_routes(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)
    operational.set_current()

    status = api.get(ACME_MAIN, headers=COOKIE)
    sibling = api.get(INGEST_STATUS_ACME_MAIN, headers=COOKIE)

    assert status.headers["content-type"] == sibling.headers["content-type"] == "application/json"
    assert json.loads(status.content) == status.json()
    assert sorted(set(status.headers) - {"content-length"}) == sorted(set(sibling.headers) - {"content-length"})
    assert "set-cookie" not in status.headers
    assert "etag" not in status.headers and "last-modified" not in status.headers and "expires" not in status.headers


def test_the_route_takes_the_two_path_slugs_and_nothing_else():
    route = next(r for r in main.app.routes if r.path.endswith("/pipeline-status") and r.path.startswith("/api/"))
    flat = get_flat_dependant(route.dependant)

    assert route.path == "/api/organizations/{org_slug}/branches/{branch_slug}/pipeline-status"
    assert route.methods == {"GET"}
    assert sorted(p.name for p in flat.path_params) == ["branch_slug", "org_slug"]
    assert flat.query_params == [] and flat.body_params == [] and flat.header_params == [] and flat.cookie_params == []


def test_the_route_is_read_only(api):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(api, method)(ACME_MAIN, headers=COOKIE).status_code == 405


# =====================================================================================================================
# D. Each kind of answer
# =====================================================================================================================

@pytest.mark.parametrize(
    ("status", "expected"),
    [("completed", "ok"), ("completed_no_new_rows", "ok"), ("skipped_no_source_changes", "ok"),
     ("failed_upload", "failed"), ("started", "unknown"), ("preflight_check", "unknown"), ("something_new", "unknown")],
)
def test_a_legacy_run_report_is_answered_with_its_state_and_its_time(api, saas_db, operational, status, expected):
    operational.set_legacy(status=status, status_at=REPORTED)

    assert api.get(ACME_MAIN, headers=COOKIE).json() == _body(expected, REPORTED_JSON)


@pytest.mark.parametrize(("health_status", "expected"), [("healthy", "ok"), ("degraded", "degraded"), ("auth_failure", "failed")])
def test_a_legacy_heartbeat_is_answered_with_its_state_and_its_time(api, saas_db, operational, health_status, expected):
    operational.set_legacy(health_status=health_status, health_at=REPORTED)

    assert api.get(ACME_MAIN, headers=COOKIE).json() == _body(expected, REPORTED_JSON)


def test_unknown_with_a_time_is_a_valid_answer(api, saas_db, operational):
    # An install probe was the most recent report: nothing is known about the pipeline, but WHEN that was is.
    operational.set_legacy(status="preflight_check", health_status="healthy", status_at=REPORTED, health_at=EARLIER)

    assert api.get(ACME_MAIN, headers=COOKIE).json() == _body("unknown", REPORTED_JSON)


def test_of_two_legacy_signals_the_one_reported_last_is_the_answer(api, saas_db, operational):
    operational.set_legacy(status="failed_upload", health_status="healthy", status_at=EARLIER, health_at=REPORTED)
    assert api.get(ACME_MAIN, headers=COOKIE).json() == _body("ok", REPORTED_JSON)

    operational.set_legacy(status="failed_upload", health_status="healthy", status_at=REPORTED, health_at=EARLIER)
    assert api.get(ACME_MAIN, headers=COOKIE).json() == _body("failed", REPORTED_JSON)


@pytest.mark.parametrize(
    ("health_status", "schedule", "expected"),
    [("healthy", "healthy", "ok"), ("degraded", "healthy", "degraded"), ("error", "healthy", "failed"),
     ("healthy", None, "ok"), ("healthy", "task_disabled", "failed"), ("healthy", "query_failed", "degraded"),
     (None, "healthy", "unknown")],
)
def test_a_current_report_is_answered_with_its_state_and_its_time(api, saas_db, operational, health_status, schedule, expected):
    operational.cutover = LONG_AGO
    operational.set_current(health_status=health_status, schedule=schedule, heartbeat_at=REPORTED)

    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.json() == _body(expected, REPORTED_JSON)
    assert operational.log == ["open", "verify context", "v2_cutovers", "ingest_key_ids", "close"]


def test_all_four_states_serialize_exactly_as_their_public_codes(api, monkeypatch, saas_db, operational):
    for state in ("ok", "degraded", "failed", "unknown"):
        monkeypatch.setattr(operational_routes, "get_pipeline_status",
                            lambda conn, tenant, state=state, **kwargs: PipelineStatus(state, REPORTED))

        assert api.get(ACME_MAIN, headers=COOKIE).json() == _body(state, REPORTED_JSON)


def test_the_answer_has_the_same_shape_whichever_source_it_came_from(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)
    from_legacy = api.get(ACME_MAIN, headers=COOKIE)

    operational.cutover = LONG_AGO
    operational.set_current(health_status="healthy", heartbeat_at=REPORTED)
    from_current = api.get(ACME_MAIN, headers=COOKIE)

    assert from_legacy.content == from_current.content        # byte for byte: nothing says which source answered
    assert operational.log.count("pipeline_status") == 1 and operational.log.count("ingest_key_ids") == 1


# --- the fixed clock decides the source exactly ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cutover", "expected_state", "expected_table"),
    [
        (None, "failed", "pipeline_status"),
        (NOW + timedelta(seconds=1), "failed", "pipeline_status"),      # still in the future
        (NOW, "ok", "ingest_key_ids"),                                  # at the instant itself: already current
        (NOW - timedelta(seconds=1), "ok", "ingest_key_ids"),
        (LONG_AGO, "ok", "ingest_key_ids"),
    ],
    ids=["no cutover", "future cutover", "cutover exactly now", "cutover a second ago", "cutover long ago"],
)
def test_the_cutover_and_the_current_instant_decide_which_report_is_answered(
    api, saas_db, operational, cutover, expected_state, expected_table
):
    operational.set_legacy(status="failed_upload", status_at=REPORTED)
    operational.set_current(health_status="healthy", heartbeat_at=REPORTED)
    operational.cutover = cutover

    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.json()["state"] == expected_state
    assert operational.log == ["open", "verify context", "v2_cutovers", expected_table, "close"]


def test_an_active_key_does_not_make_a_branch_current_before_its_cutover(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)
    operational.set_current(health_status="error", heartbeat_at=REPORTED)

    assert api.get(ACME_MAIN, headers=COOKIE).json()["state"] == "ok"
    assert "ingest_key_ids" not in operational.log


# =====================================================================================================================
# What is never in the answer
# =====================================================================================================================

LEAKS = ("v1", "v2", "legacy", "current", "contract", "era", "source", "cutover", "customer_id", "branch_id",
         "operational", "key_id", "status_reported_at", "health_status", "health", "heartbeat", "schedule", "collector",
         "task_", "error", "message", "class", "rows", "count", "destination", "updated_at", "stale", "fresh", "age",
         "overdue", "next_run", "duration", "pending", "quarantined", "pipeline_status", "ingest")


@pytest.mark.parametrize("which", ["legacy", "current"])
def test_nothing_but_the_zone_a_state_and_one_time_is_in_the_body(api, saas_db, operational, which):
    operational.set_legacy(status="failed_upload", health_status="degraded", status_at=REPORTED, health_at=EARLIER)
    operational.set_current(health_status="healthy", schedule="task_disabled", heartbeat_at=REPORTED)
    operational.cutover = LONG_AGO if which == "current" else None

    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.json() == _body("failed", REPORTED_JSON)
    text_ = response.text
    for leaked in (*LEAKS, "CANARY", KEY_ID, "retryable_infra", "failed_upload", "task_disabled", "degraded", "healthy",
                   "1988", "4001", "7003", "7004", "7005", str(CUSTOMER), "18:15", "Tokyo", "+09:00"):
        assert leaked not in text_.replace('"last_reported_at"', "").replace('"state"', ""), leaked
    words = set(text_.replace('"', " ").replace(",", " ").replace("{", " ").replace("}", " ").replace(": ", " ").split())
    assert words == {"timezone", "America/Chicago", "state", "failed", "last_reported_at", REPORTED_JSON}


def test_only_the_services_state_and_time_are_returned_never_anything_else_on_the_result(api, monkeypatch, saas_db, operational):
    # A result that carries extra attributes: if the route serialized the whole value, they would appear.
    monkeypatch.setattr(
        operational_routes, "get_pipeline_status",
        lambda conn, tenant, **kwargs: SimpleNamespace(state="degraded", last_reported_at=REPORTED, source="legacy",
                                                       customer_id=CUSTOMER, schedule="task_disabled", raw=CANARY),
    )

    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.json() == _body("degraded", REPORTED_JSON)
    for leaked in ("legacy", str(CUSTOMER), "task_disabled", "CANARY", "source", "raw"):
        assert leaked not in response.text, leaked


def test_the_statements_that_ran_selected_no_column_beyond_what_the_answer_is_made_from(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)
    api.get(ACME_MAIN, headers=COOKIE)
    operational.cutover = LONG_AGO
    operational.set_current()
    api.get(ACME_MAIN, headers=COOKIE)

    select_lists = [sql.split(" FROM ")[0] for sql in operational.statements]
    assert sorted(set(select_lists)) == [
        "SELECT cutover_at",
        "SELECT health_status, collector_schedule_status, last_heartbeat_at",
        "SELECT status, health_status, status_reported_at, health_status_reported_at",
    ]
    for select_list in select_lists:
        for column in ("last_error", "rows", "destination_breakdown", "updated_at", "last_run", "last_attempt", "key_id",
                       "customer_id", "branch_id", "pending", "quarantined", "next_run", "duration", "*"):
            assert column not in select_list, (column, select_list)


# =====================================================================================================================
# The public schema
# =====================================================================================================================

def test_the_response_schema_declares_only_the_approved_fields():
    assert list(PipelineStatusResponse.model_fields) == ["timezone", "state", "last_reported_at"]
    assert PipelineStatusResponse.model_fields["timezone"].annotation is str
    assert PipelineStatusResponse.model_fields["state"].annotation == PipelineState
    assert PipelineStatusResponse.model_fields["last_reported_at"].annotation == (datetime | None)
    assert operational_schemas.PipelineState is pipeline_state.PipelineState      # one definition of the four states
    assert issubclass(PipelineStatusResponse, operational_schemas._ResponseModel)
    assert PipelineStatusResponse.model_config["extra"] == "forbid"
    assert PipelineStatusResponse is not IngestStatusResponse
    assert list(IngestStatusResponse.model_fields) == ["status"]                  # untouched


@pytest.mark.parametrize("state", PIPELINE_STATES)
def test_the_schema_accepts_each_of_the_four_states(state):
    body = PipelineStatusResponse(timezone="America/Chicago", state=state, last_reported_at=None)

    assert body.model_dump(mode="json") == {"timezone": "America/Chicago", "state": state, "last_reported_at": None}


@pytest.mark.parametrize(
    "state",
    ["healthy", "error", "stale", "running", "completed", "failed_upload", "auth_failure", "task_disabled", "OK", "Ok",
     " ok", "ok ", "", None, 0, True, CANARY],
    ids=repr,
)
def test_the_schema_refuses_a_state_that_is_not_one_of_the_four(state):
    # The last line of defence: whatever reached this point, nothing but a public state can be serialized.
    with pytest.raises(ValidationError):
        PipelineStatusResponse(timezone="America/Chicago", state=state, last_reported_at=None)


@pytest.mark.parametrize(
    "extra",
    ["source", "era", "version", "customer_id", "branch_id", "cutover_at", "stale", "is_stale", "age_seconds",
     "last_success_at", "last_attempt_at", "latest_checkin_at", "health_status", "status", "schedule_status",
     "last_error_class", "last_error", "status_reported_at", "health_status_reported_at", "checkins_rows"],
)
def test_the_schema_refuses_any_field_that_is_not_approved(extra):
    with pytest.raises(ValidationError):
        PipelineStatusResponse(timezone="America/Chicago", state="ok", last_reported_at=None, **{extra: 1})


def test_a_utc_time_is_serialized_with_a_z_and_a_null_as_null():
    dumped = PipelineStatusResponse(timezone="America/Chicago", state="ok", last_reported_at=REPORTED).model_dump(mode="json")

    assert dumped["last_reported_at"] == REPORTED_JSON
    assert PipelineStatusResponse(timezone="UTC", state="unknown", last_reported_at=None).model_dump(mode="json")[
        "last_reported_at"] is None


# =====================================================================================================================
# B. Wiring: the tenant, the clock, the zone, the connection
# =====================================================================================================================

def test_the_route_calls_the_service_once_with_the_resolved_tenant_the_scoped_connection_and_now(
    api, monkeypatch, saas_db, operational
):
    calls = []

    def recording(conn, tenant, **kwargs):
        calls.append({"conn": conn, "tenant": tenant, **kwargs})
        return PipelineStatus("ok", REPORTED)

    monkeypatch.setattr(operational_routes, "get_pipeline_status", recording)

    assert api.get(ACME_MAIN, headers=COOKIE).json() == _body("ok", REPORTED_JSON)

    (call,) = calls                                                     # exactly once
    assert set(call) == {"conn", "tenant", "now"}
    assert isinstance(call["conn"], FakeOperationalConnection)          # the scoped connection, not a new one
    assert (call["tenant"].org_slug, call["tenant"].branch_slug) == ("acme", "main")
    assert (call["tenant"].operational_customer_id, call["tenant"].operational_branch_id) == (CUSTOMER, BRANCH)
    assert call["now"] == NOW


def test_now_is_the_current_instant_read_as_aware_utc_at_the_route(api, monkeypatch, saas_db, operational):
    seen = []
    monkeypatch.setattr(operational_routes, "get_pipeline_status",
                        lambda conn, tenant, **kwargs: seen.append(kwargs["now"]) or PipelineStatus("ok", REPORTED))

    api.get(ACME_MAIN, headers=COOKIE)

    assert FixedClock.calls == [UTC]                # one reading of the clock, asked for in UTC
    (now,) = seen
    assert now.tzinfo is UTC and now.utcoffset() == timedelta(0)


def test_with_the_real_clock_now_is_an_aware_utc_instant_inside_the_request(monkeypatch, saas_db, operational):
    # The same route with nothing standing in for `datetime`: the instant it reads is really the present.
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: dict(USER))
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    main.limiter.reset()
    seen = []
    monkeypatch.setattr(operational_routes, "get_pipeline_status",
                        lambda conn, tenant, **kwargs: seen.append(kwargs["now"]) or PipelineStatus("ok", REPORTED))

    before = datetime.now(UTC)
    assert TestClient(main.app).get(ACME_MAIN, headers=COOKIE).status_code == 200
    after = datetime.now(UTC)

    (now,) = seen
    assert now.tzinfo is UTC and before <= now <= after
    assert operational_routes.datetime is datetime     # nothing was replaced


def test_the_resolved_tenant_is_the_one_the_scoped_connection_is_opened_for(api, monkeypatch, saas_db, operational):
    opened, served = [], []
    real_open = tenant_scope.open_customer_tenant_connection
    real_service = pipeline_status_service.get_pipeline_status

    def recording_open(tenant):
        opened.append(tenant)
        return real_open(tenant)

    def recording_service(conn, tenant, **kwargs):
        served.append(tenant)
        return real_service(conn, tenant, **kwargs)

    monkeypatch.setattr(operational_routes, "open_customer_tenant_connection", recording_open)
    monkeypatch.setattr(operational_routes, "get_pipeline_status", recording_service)

    assert api.get(ACME_MAIN, headers=COOKIE).status_code == 200
    assert len(opened) == len(served) == 1 and opened[0] is served[0]


def test_every_statement_is_bound_to_the_resolved_tenant_on_a_connection_carrying_its_context(api, saas_db, operational):
    for cutover in (None, LONG_AGO):
        operational.cutover = cutover
        operational.queries.clear()

        api.get(ACME_MAIN, headers=COOKIE)

        assert len(operational.queries) == 2
        for _table, parameters, context in operational.queries:
            assert parameters == {"customer_id": CUSTOMER, "branch_id": BRANCH}
            assert context == {"customer_id": str(CUSTOMER), "branch_id": str(BRANCH)}


def test_the_zone_comes_from_the_product_time_zone_setting(api, monkeypatch, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Asia/Kolkata")

    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.json() == _body("ok", REPORTED_JSON, "Asia/Kolkata")      # the zone is echoed...
    assert response.json()["last_reported_at"].endswith("Z")                   # ...and the time is still UTC


def test_the_zone_is_read_through_the_settings_function_once(api, monkeypatch, saas_db, operational):
    calls = []
    real = customer_settings.product_timezone

    def recording():
        calls.append(1)
        return real()

    monkeypatch.setattr(operational_routes.settings, "product_timezone", recording)

    assert api.get(ACME_MAIN, headers=COOKIE).json()["timezone"] == "America/Chicago"
    assert calls == [1]


def test_the_connection_is_closed_before_the_response_is_built(api, monkeypatch, saas_db, operational):
    real_model, real_response = operational_routes.PipelineStatusResponse, operational_routes.JSONResponse

    def recording_model(*args, **kwargs):
        operational.log.append("build body")
        return real_model(*args, **kwargs)

    def recording_response(*args, **kwargs):
        operational.log.append("build response")
        return real_response(*args, **kwargs)

    monkeypatch.setattr(operational_routes, "PipelineStatusResponse", recording_model)
    monkeypatch.setattr(operational_routes, "JSONResponse", recording_response)

    assert api.get(ACME_MAIN, headers=COOKIE).status_code == 200
    assert operational.log == ["open", "verify context", "v2_cutovers", "pipeline_status", "close", "build body",
                               "build response"]


def test_every_request_opens_its_own_connection_and_nothing_is_cached(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)
    assert api.get(ACME_MAIN, headers=COOKIE).json()["state"] == "ok"

    operational.set_legacy(status="failed_upload", status_at=REPORTED + timedelta(minutes=1))
    assert api.get(ACME_MAIN, headers=COOKIE).json() == _body("failed", "2026-10-05T18:46:03Z")
    assert operational.log.count("open") == operational.log.count("close") == 2


# =====================================================================================================================
# C. Authentication and tenant reachability
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
    "active branch with no operational branch id": ("acme", "unmapped"),
}


@pytest.mark.parametrize(("org", "branch"), UNREACHABLE.values(), ids=UNREACHABLE.keys())
def test_every_unreachable_tenant_is_the_same_404_and_opens_no_connection(api, saas_db, operational, org, branch):
    response = api.get(PATH.format(org=org, branch=branch), headers=COOKIE)

    assert response.status_code == 404
    assert response.json() == TENANT_NOT_FOUND
    assert operational.log == [] and FixedClock.calls == []      # refused before the clock or the database is touched


def test_the_unreachable_responses_are_byte_for_byte_identical(api, saas_db, operational):
    seen = {
        (r.status_code, r.content, json.dumps(sorted(r.headers.items())))
        for r in (api.get(PATH.format(org=org, branch=branch), headers=COOKIE) for org, branch in UNREACHABLE.values())
    }

    assert len(seen) == 1


def test_an_unreachable_tenant_answers_exactly_as_it_does_on_the_sibling_routes(api, saas_db, operational):
    for org, branch in (("beta", "north"), ("acme", "no-such-branch"), ("acme", "unmapped")):
        status = api.get(PATH.format(org=org, branch=branch), headers=COOKIE)
        ingest = api.get(f"/api/organizations/{org}/branches/{branch}/ingest-status", headers=COOKIE)
        rejects = api.get(f"/api/organizations/{org}/branches/{branch}/rejects/count", headers=COOKIE,
                          params={"date": "2026-06-10"})

        assert (status.status_code, status.content) == (ingest.status_code, ingest.content) == (404, rejects.content)


def test_nothing_reported_and_an_unreachable_tenant_are_different_answers(api, saas_db, operational):
    assert api.get(ACME_MAIN, headers=COOKIE).status_code == 200                              # unknown / null
    assert api.get(PATH.format(org="acme", branch="unmapped"), headers=COOKIE).status_code == 404


def test_a_suspended_organization_reads_exactly_like_a_full_one(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)
    full = api.get(ACME_MAIN, headers=COOKIE)
    with saas_db.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE slug = 'acme'"))

    read_only = api.get(ACME_MAIN, headers=COOKIE)

    assert read_only.status_code == full.status_code == 200
    assert read_only.json() == full.json()


@pytest.mark.parametrize("status", ["active", "trial", "suspended", "cancelled"])
def test_an_organizations_status_decides_this_route_exactly_as_it_decides_ingest_status(api, saas_db, operational, status):
    with saas_db.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = :s WHERE slug = 'acme'"), {"s": status})

    pipeline = api.get(ACME_MAIN, headers=COOKIE)
    ingest = api.get(INGEST_STATUS_ACME_MAIN, headers=COOKIE)

    assert pipeline.status_code == ingest.status_code
    if pipeline.status_code != 200:
        assert pipeline.content == ingest.content


def test_any_role_may_read_as_on_the_sibling_routes(api, saas_db, operational):
    for role in ("viewer", "manager", "admin", "owner"):
        with saas_db.begin() as conn:
            conn.execute(text("UPDATE memberships SET role = :r WHERE organization_id = 1"), {"r": role})

        assert api.get(ACME_MAIN, headers=COOKIE).status_code == 200, role
        assert api.get(INGEST_STATUS_ACME_MAIN, headers=COOKIE).status_code == 200, role


def test_operational_ids_and_other_hints_in_the_request_change_nothing(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)
    operational.set_current(health_status="error")
    plain = api.get(ACME_MAIN, headers=COOKIE)
    plain_queries = list(operational.queries)
    operational.queries.clear()

    # The customer API's policy for a query parameter it does not declare: ignored, never an error, never obeyed.
    tampered = api.get(
        ACME_MAIN,
        headers={**COOKIE, "X-Customer-Id": "8202", "X-Branch-Id": "21", "X-Now": "1999-01-01T00:00:00Z"},
        params={"customer_id": 8202, "branch_id": 21, "operational_customer_id": 8202, "operational_branch_id": 21,
                "org_slug": "beta", "branch_slug": "north", "user_id": 2, "timezone": "Asia/Tokyo", "zone": "UTC",
                "now": "2099-01-01T00:00:00Z", "as_of": "2099-01-01", "cutover": "2020-01-01T00:00:00Z", "era": "v2",
                "source": "current", "v2": "true", "state": "ok", "stale_after": 60, "include": "schedule,error", "raw": "true"},
    )

    assert tampered.status_code == 200
    assert tampered.json() == plain.json() == _body("ok", REPORTED_JSON)
    assert operational.queries == plain_queries       # same statements, same ids, same context: still the legacy row


def test_a_request_body_is_ignored(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)

    response = api.request("GET", ACME_MAIN, headers=COOKIE,
                           json={"customer_id": 8202, "branch_id": 21, "state": "failed", "now": "2099-01-01T00:00:00Z"})

    assert response.status_code == 200 and response.json() == _body("ok", REPORTED_JSON)
    assert all(parameters == {"customer_id": CUSTOMER, "branch_id": BRANCH} for _t, parameters, _c in operational.queries)


# =====================================================================================================================
# E. Failures are 500, never an `unknown`
# =====================================================================================================================

def _assert_generic_500(response) -> None:
    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert response.headers["cache-control"] == "no-store"
    for leaked in ("synthetic", str(CUSTOMER), "CANARY", "state", "last_reported_at", "unknown", "timezone",
                   "pipeline_status", "ingest_key_ids", "v2_cutovers", "current_setting", "SELECT", "ZoneInfo", "Mars",
                   "Traceback", "RuntimeError", "ValueError", "timezone-aware", "1988"):
        assert leaked not in response.text, leaked


def test_a_resolver_database_failure_is_500(api, monkeypatch, operational):
    def broken_resolve(user_id, org_slug, branch_slug):
        raise RuntimeError(f"synthetic database failure for customer {CUSTOMER}")

    monkeypatch.setattr(tenant_scope, "resolve_operational_tenant", broken_resolve)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE))
    assert operational.log == []


@pytest.mark.parametrize("value", ["Mars/Olympus_Mons", "", "   ", "America/Chicagoo", "../etc/passwd"])
def test_an_invalid_configured_zone_is_500_and_opens_no_connection(api, monkeypatch, saas_db, operational, value):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", value)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE))
    assert operational.log == []      # never a silent fall-back to some other zone


def test_an_engine_failure_is_500(api, monkeypatch, saas_db):
    def broken_get_engine():
        raise RuntimeError("synthetic engine failure")

    monkeypatch.setattr(tenant_scope, "get_engine", broken_get_engine)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE))


def test_a_failure_reading_the_tenant_context_back_is_500(api, saas_db, operational):
    operational.fail_on = "context"

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE))
    assert operational.log == ["open", "verify context", "close"]


@pytest.mark.parametrize(
    "read_back",
    [lambda settings: {**settings, "customer_id": None}, lambda settings: {**settings, "branch_id": "21"}],
    ids=["context missing", "another tenant's context"],
)
def test_a_tenant_context_that_does_not_verify_is_500_and_nothing_is_read(api, saas_db, operational, read_back):
    operational.set_legacy(status="completed", status_at=REPORTED)
    operational.read_back = read_back

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE))
    assert operational.log == ["open", "verify context", "close"]
    assert operational.queries == []


@pytest.mark.parametrize(
    ("cutover", "failing"),
    [(None, "v2_cutovers"), (None, "pipeline_status"), (LONG_AGO, "v2_cutovers"), (LONG_AGO, "ingest_key_ids")],
    ids=["legacy: cutover lookup", "legacy: status read", "current: cutover lookup", "current: status read"],
)
def test_a_failure_in_any_statement_is_500_never_unknown(api, saas_db, operational, caplog, cutover, failing):
    operational.set_legacy(status="completed", status_at=REPORTED)
    operational.set_current()
    operational.cutover = cutover
    operational.fail_on = failing

    with caplog.at_level(logging.DEBUG):
        response = api.get(ACME_MAIN, headers=COOKIE)

    _assert_generic_500(response)
    assert operational.log[-2:] == [failing, "close"]
    # Logged as a safe summary only: not the exception's own text.
    assert "synthetic" not in caplog.text and "CANARY" not in caplog.text


def test_a_service_failure_is_500_and_the_connection_is_still_closed(api, monkeypatch, saas_db, operational, caplog):
    def broken_service(conn, tenant, **kwargs):
        raise RuntimeError(f"synthetic service failure for customer {CUSTOMER}: {CANARY}")

    monkeypatch.setattr(operational_routes, "get_pipeline_status", broken_service)

    with caplog.at_level(logging.DEBUG):
        _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE))

    assert operational.log == ["open", "verify context", "close"]
    assert "CANARY" not in caplog.text and "synthetic" not in caplog.text


def test_a_stored_time_with_no_offset_is_500_never_a_guessed_instant(api, saas_db, operational):
    operational.set_legacy(status="completed", status_at=REPORTED)
    operational.legacy["status_reported_at"] = datetime(2026, 10, 5, 18, 45, 3)  # noqa: DTZ001 - naive on purpose

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE))


@pytest.mark.parametrize(
    "result",
    [
        PipelineStatus("healthy", REPORTED),                               # not a public state
        PipelineStatus("failed_upload", REPORTED),                         # a raw stored value
        PipelineStatus(None, None),
        PipelineStatus("ok", "not-a-time"),
        SimpleNamespace(state="ok"),                                       # no time at all
        SimpleNamespace(last_reported_at=REPORTED),                        # no state at all
        None,
    ],
    ids=["a state outside the four", "a raw status", "no state", "a time that is not one", "missing time attribute",
         "missing state attribute", "no result"],
)
def test_a_result_that_is_not_a_public_answer_is_500_never_passed_through(api, monkeypatch, saas_db, operational, result):
    monkeypatch.setattr(operational_routes, "get_pipeline_status", lambda conn, tenant, **kwargs: result)

    response = api.get(ACME_MAIN, headers=COOKIE)

    _assert_generic_500(response)
    for leaked in ("healthy", "failed_upload", "not-a-time"):
        assert leaked not in response.text, leaked


def test_a_500_here_is_the_same_as_on_the_ingest_status_route(api, saas_db, operational):
    operational.fail_on = "context"

    pipeline = api.get(ACME_MAIN, headers=COOKIE)
    ingest = api.get(INGEST_STATUS_ACME_MAIN, headers=COOKIE)

    assert (pipeline.status_code, pipeline.content) == (ingest.status_code, ingest.content) == (500, pipeline.content)


# =====================================================================================================================
# F. How the route is built
# =====================================================================================================================

def _endpoint(suffix: str):
    return next(r for r in main.app.routes
                if getattr(r, "path", "").startswith("/api/") and r.path.endswith(suffix)).endpoint


def test_the_route_reaches_the_database_only_through_the_tenant_scope_and_the_status_service():
    source = inspect.getsource(operational_routes)

    assert operational_routes.get_pipeline_status is pipeline_status_service.get_pipeline_status
    assert operational_routes.open_customer_tenant_connection is tenant_scope.open_customer_tenant_connection
    for forbidden in ("get_engine", "tenant_connection(", "resolve_operational_tenant", "import database",
                      "from database", "tenant_db", "set_config", "SELECT", "FROM pipeline_status", "FROM ingest_key_ids",
                      "data_loader", "mixed_era", "pandas", "streamlit", "import main", "from main", "America/Chicago",
                      "os.environ", "getenv", "pipeline_context", "ingest_v2"):
        assert forbidden not in source.replace("open_customer_tenant_connection(", ""), forbidden


def test_the_route_itself_holds_no_sql_no_mapping_no_source_selection_and_no_era():
    source = inspect.getsource(_endpoint("/pipeline-status"))

    # No statement, no state name, no stored value, no cutover and no era: the service decides all of it.
    for forbidden in ("execute(", "text(", "SELECT", "cutover", "v2_cutovers", "pipeline_status_", "ingest_key",
                      "state_for_", "schedule", "health", "heartbeat", "status_reported_at", "legacy", "current/",
                      "v1", "v2", "Contract", "era", "stale", "timedelta", "if ", "else", " or ", "max(", "min(",
                      "getenv", "environ", "ZoneInfo(", '"ok"', '"degraded"', '"failed"', '"unknown"'):
        assert forbidden not in source, forbidden
    assert source.count("get_pipeline_status(") == 1      # called once
    assert source.count("datetime.now(UTC)") == 1         # the clock is read once, in UTC, here
    assert source.count("pipeline.") == 2                 # `.state` and `.last_reported_at`, and nothing else of the result


def test_the_route_is_built_like_its_sibling_operational_routes():
    routes = {r.path.split("/branches/{branch_slug}/")[1]: r for r in main.app.routes
              if "/branches/{branch_slug}/" in getattr(r, "path", "")}
    pipeline, ingest = routes["pipeline-status"], routes["ingest-status"]

    # The same single declared parameter -- the tenant-scope dependency -- and the same route class.
    signatures = [{name: p.annotation for name, p in inspect.signature(r.endpoint).parameters.items()} for r in (pipeline, ingest)]
    assert signatures[0] == signatures[1] == {"tenant": "ResolvedTenant"}
    assert [d.call for d in pipeline.dependant.dependencies] == [d.call for d in ingest.dependant.dependencies]
    assert type(pipeline) is type(ingest)
    assert not hasattr(pipeline.endpoint, "__wrapped__")       # no limiter decorator of its own
    # The steps, in order: zone, clock, scoped connection, one service call, then -- outside the block -- the body.
    source = inspect.getsource(pipeline.endpoint)
    steps = ["zone = settings.product_timezone()", "now = datetime.now(UTC)",
             "with open_customer_tenant_connection(tenant) as conn:", "get_pipeline_status(conn, tenant, now=now)",
             "PipelineStatusResponse(", "timezone=zone.key", "state=pipeline.state",
             "last_reported_at=pipeline.last_reported_at", "headers=NO_STORE_HEADERS"]
    positions = [source.index(step) for step in steps]
    assert positions == sorted(positions)


def test_the_operational_modules_import_no_dashboard_collector_or_database_code():
    for module in (operational_routes, operational_schemas):
        imports = [line for line in inspect.getsource(module).splitlines() if line.startswith(("import ", "from "))]
        # Every dotted part of every imported module name: "from services.x import y" -> {"services", "x"}.
        parts = {part for line in imports for part in line.split()[1].split(".")}
        for forbidden in ("pandas", "streamlit", "reject_logic", "collector", "ingest_v2", "agent", "data_loader",
                          "dashboard", "mixed_era", "sqlalchemy", "pipeline_context", "os", "zoneinfo", "main"):
            assert not any(part.startswith(forbidden) for part in parts), (module.__name__, forbidden)

    assert "from services.pipeline_status_service import get_pipeline_status" in inspect.getsource(operational_routes)
    assert "from services.pipeline_state import PipelineState" in inspect.getsource(operational_schemas)
    assert "from datetime import UTC, date, datetime" in inspect.getsource(operational_routes)


def test_the_customer_api_still_loads_without_streamlit_or_pandas():
    # The seam itself is proven in a clean process by tests/test_customer_api_import_seam.py; this pins that the
    # modules this block touched, and the two they now reach, name neither.
    for module in (operational_routes, operational_schemas, pipeline_status_service, pipeline_state):
        imports = [line for line in inspect.getsource(module).splitlines() if line.startswith(("import ", "from "))]
        assert not any("streamlit" in line or "pandas" in line for line in imports)


# =====================================================================================================================
# G. The sibling routes are as they were
# =====================================================================================================================

def test_the_ingest_status_route_is_exactly_as_it_was(api, saas_db, operational):
    operational.set_current(health_status="degraded", schedule="task_disabled", heartbeat_at=REPORTED)

    response = api.get(INGEST_STATUS_ACME_MAIN, headers=COOKIE)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    status = response.json()["status"]
    assert list(response.json()) == ["status"]
    assert list(status) == ["health_status", "last_error_class", "pending_outbox_count", "quarantined_count",
                            "oldest_pending_event_at", "last_success_at", "watcher_last_active_at", "last_heartbeat_at",
                            "collector_last_run_at", "collector_next_run_at", "collector_run_duration_ms",
                            "collector_schedule_status"]
    assert (status["health_status"], status["collector_schedule_status"]) == ("degraded", "task_disabled")   # raw, as before
    assert operational.log == ["open", "verify context", "ingest_key_ids", "close"]     # no cutover lookup, no legacy read
    assert "state" not in response.text and "last_reported_at" not in response.text

    source = inspect.getsource(_endpoint("/ingest-status"))
    assert "get_latest_ingest_status(conn, tenant)" in source
    for added in ("pipeline", "datetime.now", "product_timezone", "state"):
        assert added not in source, added


def test_ingest_status_with_no_key_is_still_status_null(api, saas_db, operational):
    assert api.get(INGEST_STATUS_ACME_MAIN, headers=COOKIE).json() == {"status": None}


def test_the_dated_metric_routes_still_require_their_date_and_do_not_read_a_status_table(api, saas_db, operational):
    for suffix in ("checkins/count", "checkins/by-hour", "rejects/count", "rejects/by-reason"):
        path = f"/api/organizations/acme/branches/main/{suffix}"

        assert api.get(path, headers=COOKIE).status_code == 422, suffix
        for added in ("pipeline", "datetime.now"):
            assert added not in inspect.getsource(_endpoint("/" + suffix)), (suffix, added)
    assert operational.log == []


def test_the_branch_routes_are_exactly_the_seven_operational_endpoints_the_five_reports_and_the_efficiency_settings():
    paths = sorted(r.path.split("/branches/{branch_slug}/")[1] for r in main.app.routes
                   if "/branches/{branch_slug}/" in getattr(r, "path", ""))

    assert paths == ["checkins/by-destination", "checkins/by-hour", "checkins/count", "ingest-status", "pipeline-status", "rejects/by-reason",
                     "rejects/count",
                     # The range reports (customer_api.report_routes) are under the same site prefix.
                     # ... and so is the Efficiency report (customer_api.efficiency_report_routes, Reports R6C).
                     "reports/efficiency",
                     "reports/overview", "reports/reliability", "reports/routing", "reports/volume",
                     # Reports R6B: a sorter site's Efficiency settings, read (GET) and replaced (PUT).
                     "settings/efficiency", "settings/efficiency"]


def test_the_customer_api_has_exactly_twenty_four_routes_all_get_except_login_logout_and_the_two_settings_puts():
    customer = sorted((method, route.path) for route in main.customer_router.routes for method in route.methods)

    # Reports R4 added three: the organization-level reports (customer_api.organization_report_routes).
    # Reports R6B added four: Efficiency settings, read and replaced, for an organization and for a sorter site
    # (customer_api.efficiency_settings_routes). The two PUTs are the only routes that change stored data.
    # Reports R6C added one: a sorter site's Efficiency report (customer_api.efficiency_report_routes), a GET.
    assert len(customer) == 24
    assert [path for method, path in customer if method == "POST"] == ["/api/auth/login", "/api/auth/logout"]
    assert [path for method, path in customer if method == "PUT"] == [
        "/api/organizations/{org_slug}/branches/{branch_slug}/settings/efficiency",
        "/api/organizations/{org_slug}/settings/efficiency",
    ]
    assert sum(1 for method, _ in customer if method == "GET") == 20
    assert ("GET", "/api/organizations/{org_slug}/branches/{branch_slug}/pipeline-status") in customer


def test_the_collectors_upload_pipeline_status_route_is_a_different_route_and_is_untouched():
    collector = [r for r in main.app.routes if r.path == "/upload-pipeline-status"]

    assert len(collector) == 1 and collector[0].methods == {"POST"}
    assert not collector[0].path.startswith("/api/")
    assert collector[0].endpoint is not _endpoint("/pipeline-status")
