"""Block 7b: the single-date reject count endpoint.

    GET /api/organizations/{org_slug}/branches/{branch_slug}/rejects/count?date=YYYY-MM-DD

These tests drive the real production route through TestClient(main.app): the
session dependency, the tenant-scope dependency, the scoped and verified
connection, the metrics service and the response schema all run for real.
What stands in for PostgreSQL is:

- an in-memory SQLite database for the SaaS tables, so the REAL tenant
  resolver decides which organization/branch pairs resolve;
- a recording fake engine for the operational connection, which answers the
  set_config / current_setting calls the way PostgreSQL does and returns a
  chosen cutover and chosen per-era counts to the REAL metrics service. It
  answers the check-in statements too, with different numbers, so that a
  route reading the wrong pair of tables would show.

The time and cutover rules themselves are tested in
tests/test_operational_metrics_service.py; row level security, real TIMESTAMP
/ TIMESTAMPTZ comparison and the database session time zone are tested
against a real server in tests/test_rls_phase1_postgres.py. This file is
about the HTTP contract.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import main
from customer_api import operational_routes, tenant_scope
from customer_api.operational_schemas import CheckinCountResponse, RejectCountResponse
from services import (
    operational_metrics_service,
    session_service,
    tenant_resolution_service,
)
from services.operational_metrics_service import RejectCount

PATH = "/api/organizations/{org}/branches/{branch}/rejects/count"
ACME_MAIN = PATH.format(org="acme", branch="main")
CHECKIN_COUNT_ACME_MAIN = "/api/organizations/acme/branches/main/checkins/count"
CHECKINS_BY_HOUR_ACME_MAIN = "/api/organizations/acme/branches/main/checkins/by-hour"
COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 1, "email": "alice@example.invalid", "full_name": "Alice"}

CUSTOMER, BRANCH = 8101, 11
JUNE_10 = {"date": "2026-06-10"}
NOON_CUTOVER = datetime(2026, 6, 10, 17, 0, tzinfo=UTC)   # 12:00 local on 10 June in America/Chicago

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
TENANT_NOT_FOUND = {"code": "tenant_not_found", "message": "Organization or branch not found."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

REJECT_TABLES = ("rejects", "reject_events")


def _local(year, month, day, hour=0, minute=0) -> datetime:
    """A NAIVE local wall-clock datetime, as rejects.event_time holds it."""
    return datetime(year, month, day, hour, minute)  # noqa: DTZ001


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
    def __init__(self, value):
        self._value = value

    def mappings(self):
        return self

    def first(self):
        return self._value

    def one(self):
        return self._value

    def scalar_one(self):
        return self._value


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
            return _Result(None)
        if "set_config('app.operational_branch_id'" in sql:
            self.settings["branch_id"] = parameters["v"]
            return _Result(None)
        if "current_setting(" in sql:
            owner.log.append("verify context")
            if owner.fail_on == "context":
                raise RuntimeError("synthetic database failure")
            return _Result(owner.read_back(dict(self.settings)))

        hourly = "FILTER" in sql
        for table, answer in (
            ("v2_cutovers", lambda: None if owner.cutover is None else (owner.cutover,)),
            ("rejects", lambda: owner.v1_rejects),
            ("reject_events", lambda: owner.v2_rejects),
            ("checkins", lambda: (owner.v1_checkins,) + (0,) * 23 if hourly else owner.v1_checkins),
            ("checkin_events", lambda: (owner.v2_checkins,) + (0,) * 23 if hourly else owner.v2_checkins),
        ):
            if f"FROM {table} " in sql:
                owner.log.append(table)
                owner.queries.append((table, dict(parameters or {}), dict(self.settings)))
                owner.statements.append(sql)
                if owner.fail_on == table:
                    raise RuntimeError(f"synthetic database failure reading {table} for customer {CUSTOMER}")
                return _Result(answer())

        raise AssertionError(f"unexpected statement: {sql}")


class FakeOperationalDatabase:
    """The flat database engine, as the tenant scope sees it. Reject and
    check-in statements are answered separately, with different numbers."""

    def __init__(self):
        self.cutover: datetime | None = None
        self.v1_rejects = 0
        self.v2_rejects = 0
        self.v1_checkins = 90001       # never a reject count: appears only if the wrong tables are read
        self.v2_checkins = 80002
        self.fail_on: str | None = None
        self.read_back = lambda settings: settings
        self.log: list[str] = []
        self.queries: list[tuple] = []
        self.statements: list[str] = []

    def connect(self):
        return FakeOperationalConnection(self)

    def parameters_for(self, table: str) -> dict:
        (found,) = [parameters for name, parameters, _ in self.queries if name == table]
        return found


@pytest.fixture
def operational(monkeypatch):
    database = FakeOperationalDatabase()
    monkeypatch.setattr(tenant_scope, "get_engine", lambda: database)
    return database


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: dict(USER))
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


# =====================================================================================================================
# Success
# =====================================================================================================================

def test_a_member_gets_the_reject_count_for_the_requested_local_date(api, saas_db, operational):
    operational.v1_rejects = 7

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 200
    assert response.json() == {"date": "2026-06-10", "timezone": "America/Chicago", "reject_count": 7}
    assert response.headers["cache-control"] == "no-store"


def test_the_response_has_exactly_the_three_approved_fields(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_rejects, operational.v2_rejects = 40, 2

    body = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()

    assert list(body) == ["date", "timezone", "reject_count"]
    assert body["reject_count"] == 42           # the two eras added together...
    assert type(body["reject_count"]) is int


def test_only_the_services_total_is_returned_never_its_per_era_counts(api, monkeypatch, saas_db, operational):
    # A deliberately inconsistent result: if the route added the eras up itself, or serialized the whole value,
    # 4001 or 2003 or 6004 would appear. Only `total` may.
    def distinctive_count(conn, tenant, **kwargs):
        return RejectCount(total=7919, v1_count=4001, v2_count=2003)

    monkeypatch.setattr(operational_routes, "get_reject_count", distinctive_count)

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json() == {"date": "2026-06-10", "timezone": "America/Chicago", "reject_count": 7919}
    for leaked in ("4001", "2003", "6004", "v1_count", "v2_count", "total"):
        assert leaked not in response.text, leaked


def test_the_era_split_the_cutover_and_the_ids_are_never_exposed(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_rejects, operational.v2_rejects = 4001, 2003   # distinctive, so a leak would be visible

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json()["reject_count"] == 6004
    for leaked in ("4001", "2003", "v1", "v2", "cutover", "era", "source", "17:00", str(CUSTOMER), "customer_id",
                   "branch_id", "operational"):
        assert leaked not in response.text, leaked


def test_nothing_about_why_items_were_rejected_is_exposed(api, saas_db, operational):
    operational.v1_rejects = 5

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    for leaked in ("reason", "error", "message", "class", "item_not_found", "Item Not Found", "rfid", "acs",
                   "barcode", "item_key", "rate", "hours"):
        assert leaked not in response.text.lower().replace("reject_count", ""), leaked
    # ...and the statements that ran never selected such a column in the first place.
    for sql in operational.statements:
        assert "error_" not in sql and "barcode" not in sql and "item_key" not in sql


def test_a_day_with_no_rejects_is_a_count_of_zero(api, saas_db, operational):
    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 200
    assert response.json() == {"date": "2026-06-10", "timezone": "America/Chicago", "reject_count": 0}
    assert operational.log == ["open", "verify context", "v2_cutovers", "rejects", "close"]   # it really looked


def test_a_future_date_is_accepted_and_counts_zero(api, saas_db, operational):
    response = api.get(ACME_MAIN, headers=COOKIE, params={"date": "2099-12-31"})

    assert response.status_code == 200
    assert response.json() == {"date": "2099-12-31", "timezone": "America/Chicago", "reject_count": 0}


def test_a_date_long_before_any_data_is_accepted(api, saas_db, operational):
    response = api.get(ACME_MAIN, headers=COOKIE, params={"date": "1999-01-01"})

    assert response.status_code == 200
    assert response.json()["date"] == "1999-01-01"


def test_a_suspended_organization_reads_exactly_like_a_full_one(api, saas_db, operational):
    operational.v1_rejects = 7
    full = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    with saas_db.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE slug = 'acme'"))

    read_only = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert read_only.status_code == full.status_code == 200
    assert read_only.json() == full.json()


def test_the_read_only_tenant_reaches_the_service_marked_as_such(api, monkeypatch, saas_db, operational):
    seen = []
    real = operational_metrics_service.get_reject_count

    def recording(conn, tenant, **kwargs):
        seen.append(tenant.access_mode)
        return real(conn, tenant, **kwargs)

    monkeypatch.setattr(operational_routes, "get_reject_count", recording)
    with saas_db.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE slug = 'acme'"))

    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 200
    assert seen == ["read_only"]


def test_any_role_may_read(api, saas_db, operational):
    for role in ("viewer", "manager", "admin", "owner"):
        with saas_db.begin() as conn:
            conn.execute(text("UPDATE memberships SET role = :r WHERE organization_id = 1"), {"r": role})

        assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 200, role


# =====================================================================================================================
# The date and the zone reach the service exactly
# =====================================================================================================================

def test_the_route_passes_the_parsed_local_date_and_the_configured_zone_to_the_service(
    api, monkeypatch, saas_db, operational
):
    seen = {}

    def recording_count(conn, tenant, **kwargs):
        seen.update(kwargs, conn=conn, tenant=tenant)
        return RejectCount(total=5, v1_count=5, v2_count=0)

    monkeypatch.setattr(operational_routes, "get_reject_count", recording_count)

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json()["reject_count"] == 5
    assert seen["local_date"] == date(2026, 6, 10) and type(seen["local_date"]) is date
    assert seen["zone"] == ZoneInfo("America/Chicago") and isinstance(seen["zone"], ZoneInfo)
    assert isinstance(seen["conn"], FakeOperationalConnection)       # the scoped connection, not a new one
    assert (seen["tenant"].operational_customer_id, seen["tenant"].operational_branch_id) == (CUSTOMER, BRANCH)
    assert set(seen) == {"local_date", "zone", "conn", "tenant"}


def test_the_queries_read_the_reject_tables_for_the_local_day_of_the_configured_zone(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER

    api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert [table for table, _, _ in operational.queries] == ["v2_cutovers", "rejects", "reject_events"]
    v1, v2 = operational.parameters_for("rejects"), operational.parameters_for("reject_events")
    assert (v1["start_local"], v1["end_local"]) == (_local(2026, 6, 10), _local(2026, 6, 10, 12))
    assert (v2["start_utc"], v2["end_utc"]) == (NOON_CUTOVER, datetime(2026, 6, 11, 5, 0, tzinfo=UTC))


def test_every_query_runs_for_the_resolved_tenant_on_a_connection_carrying_its_context(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER

    api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert len(operational.queries) == 3
    for _table, parameters, context in operational.queries:
        assert (parameters["customer_id"], parameters["branch_id"]) == (CUSTOMER, BRANCH)
        assert context == {"customer_id": str(CUSTOMER), "branch_id": str(BRANCH)}


def test_the_configured_zone_is_echoed_and_used(api, monkeypatch, saas_db, operational):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Asia/Kolkata")
    operational.cutover = NOON_CUTOVER   # 22:30 on 10 June in Kolkata

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json()["timezone"] == "Asia/Kolkata"
    assert operational.parameters_for("rejects")["end_local"] == _local(2026, 6, 10, 22, 30)
    # Local midnight in Kolkata is 18:30Z the day before; the day ends at 18:30Z, 90 minutes after the cutover.
    assert operational.parameters_for("reject_events")["end_utc"] == datetime(2026, 6, 10, 18, 30, tzinfo=UTC)


def test_a_local_day_is_never_assumed_to_be_24_hours(api, saas_db, operational):
    operational.cutover = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)   # long before: v2 owns each whole day

    spans = {}
    for label, day in (("spring forward", "2026-03-08"), ("ordinary", "2026-06-10"), ("fall back", "2026-11-01")):
        operational.queries.clear()
        api.get(ACME_MAIN, headers=COOKIE, params={"date": day})
        v2 = operational.parameters_for("reject_events")
        spans[label] = v2["end_utc"] - v2["start_utc"]

    assert spans == {
        "spring forward": timedelta(hours=23), "ordinary": timedelta(hours=24), "fall back": timedelta(hours=25),
    }


# =====================================================================================================================
# Rejects and check-ins are separate endpoints over separate tables
# =====================================================================================================================

def test_the_reject_route_never_reads_a_check_in_table(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_rejects, operational.v2_rejects = 3, 4

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json()["reject_count"] == 7
    assert "checkins" not in operational.log and "checkin_events" not in operational.log
    assert "90001" not in response.text and "80002" not in response.text


def test_the_check_in_count_route_is_unchanged_and_never_reads_a_reject_table(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_rejects, operational.v2_rejects = 3, 4
    operational.v1_checkins, operational.v2_checkins = 100, 20

    response = api.get(CHECKIN_COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 200
    assert response.json() == {"date": "2026-06-10", "timezone": "America/Chicago", "checkin_count": 120}
    assert response.headers["cache-control"] == "no-store"
    assert operational.log == ["open", "verify context", "v2_cutovers", "checkins", "checkin_events", "close"]


def test_the_check_ins_by_hour_route_is_unchanged_and_never_reads_a_reject_table(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_rejects, operational.v2_rejects = 3, 4
    operational.v1_checkins, operational.v2_checkins = 100, 20

    response = api.get(CHECKINS_BY_HOUR_ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 200
    assert list(response.json()) == ["date", "timezone", "hours"]
    assert response.json()["hours"][0] == {"hour": 0, "checkin_count": 120}
    assert len(response.json()["hours"]) == 24
    assert operational.log == ["open", "verify context", "v2_cutovers", "checkins", "checkin_events", "close"]


def test_the_two_counts_for_one_day_are_independent(api, saas_db, operational):
    operational.v1_rejects, operational.v1_checkins = 9, 250

    rejects = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()
    checkins = api.get(CHECKIN_COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10).json()

    assert (rejects["reject_count"], checkins["checkin_count"]) == (9, 250)
    assert "checkin_count" not in rejects and "reject_count" not in checkins
    assert (rejects["date"], rejects["timezone"]) == (checkins["date"], checkins["timezone"])


# =====================================================================================================================
# The date parameter
# =====================================================================================================================

def test_the_date_is_required_and_never_defaults_to_today(api, saas_db, operational):
    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.status_code == 422
    assert operational.log == []


@pytest.mark.parametrize(
    "value",
    [
        "", "today", "not-a-date-CANARY", "2026-6-1", "20260610", "2026/06/10", "10-06-2026", "2026-W24-3",
        "2026-06-10T00:00:00", "2026-06-10T12:30:00Z", "2026-06-10 00:00:00", "1781136000", "2026-06-10 ",
        " 2026-06-10", "2026-06", "+2026-06-10",
    ],
)
def test_anything_that_is_not_exactly_a_calendar_date_is_422(api, saas_db, operational, value):
    response = api.get(ACME_MAIN, headers=COOKIE, params={"date": value})

    assert response.status_code == 422
    assert operational.log == []
    if value.strip():
        assert value.strip() not in response.text   # the hardened 422 never echoes a submitted value


@pytest.mark.parametrize("value", ["2026-02-30", "2026-13-01", "2026-00-10", "2026-06-31", "2025-02-29", "0000-01-01"])
def test_an_impossible_date_is_422(api, saas_db, operational, value):
    response = api.get(ACME_MAIN, headers=COOKIE, params={"date": value})

    assert response.status_code == 422
    assert operational.log == []


def test_a_leap_day_is_a_valid_date(api, saas_db, operational):
    assert api.get(ACME_MAIN, headers=COOKIE, params={"date": "2028-02-29"}).status_code == 200


def test_a_repeated_date_parameter_is_not_silently_merged(api, saas_db, operational):
    response = api.get(ACME_MAIN + "?date=2026-06-10&date=2026-06-11", headers=COOKIE)

    # One date reaches the service -- never a range built from two values.
    assert response.status_code == 200
    assert response.json()["date"] in ("2026-06-10", "2026-06-11")
    assert operational.log.count("rejects") == 1


def test_a_422_here_is_the_same_as_on_the_check_in_count_route(api, saas_db, operational):
    rejects = api.get(ACME_MAIN, headers=COOKIE, params={"date": "2026-06-10T00:00:00"})
    checkins = api.get(CHECKIN_COUNT_ACME_MAIN, headers=COOKIE, params={"date": "2026-06-10T00:00:00"})

    assert (rejects.status_code, rejects.content) == (checkins.status_code, checkins.content)
    assert rejects.status_code == 422


# =====================================================================================================================
# Authentication and tenant reachability
# =====================================================================================================================

def test_no_session_is_401_and_nothing_is_resolved_or_read(saas_db, operational):
    main.limiter.reset()

    response = TestClient(main.app).get(ACME_MAIN, params=JUNE_10)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert operational.log == []


def test_a_session_that_does_not_validate_is_401(monkeypatch, saas_db, operational):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: None)

    response = TestClient(main.app).get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

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
    response = api.get(PATH.format(org=org, branch=branch), headers=COOKIE, params=JUNE_10)

    assert response.status_code == 404
    assert response.json() == TENANT_NOT_FOUND
    assert operational.log == []


def test_the_unreachable_responses_are_byte_for_byte_identical(api, saas_db, operational):
    seen = {
        (r.status_code, r.content, json.dumps(sorted(r.headers.items())))
        for r in (
            api.get(PATH.format(org=org, branch=branch), headers=COOKIE, params=JUNE_10)
            for org, branch in UNREACHABLE.values()
        )
    }

    assert len(seen) == 1


def test_an_unreachable_tenant_answers_exactly_as_it_does_on_the_check_in_count_route(api, saas_db, operational):
    for org, branch in (("beta", "north"), ("acme", "no-such-branch")):
        rejects = api.get(PATH.format(org=org, branch=branch), headers=COOKIE, params=JUNE_10)
        checkins = api.get(f"/api/organizations/{org}/branches/{branch}/checkins/count", headers=COOKIE, params=JUNE_10)

        assert (rejects.status_code, rejects.content) == (checkins.status_code, checkins.content)
        assert rejects.status_code == 404


def test_zero_rejects_and_an_unreachable_tenant_are_different_answers(api, saas_db, operational):
    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 200
    assert api.get(PATH.format(org="acme", branch="shut"), headers=COOKIE, params=JUNE_10).status_code == 404


# =====================================================================================================================
# The request cannot choose the tenant, or narrow the count
# =====================================================================================================================

def test_operational_ids_and_other_hints_in_the_request_change_nothing(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_rejects, operational.v2_rejects = 3, 4
    plain = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    plain_queries = list(operational.queries)
    operational.queries.clear()

    tampered = api.get(
        ACME_MAIN,
        headers={**COOKIE, "X-Customer-Id": "8202", "X-Branch-Id": "21"},
        params={**JUNE_10, "customer_id": 8202, "branch_id": 21, "operational_customer_id": 8202,
                "operational_branch_id": 21, "org_slug": "beta", "branch_slug": "north", "user_id": 2,
                "timezone": "Asia/Tokyo", "zone": "UTC", "cutover": "2020-01-01T00:00:00Z", "start_date": "2020-01-01",
                "end_date": "2030-01-01", "from": "2020-01-01", "to": "2030-01-01", "era": "v2", "v1": "false",
                "reason": "item_not_found", "error_class": "rfid_collision", "hour": 9, "distinct": "true"},
    )

    assert tampered.status_code == 200
    assert tampered.json() == plain.json()
    assert operational.queries == plain_queries   # same statements, same ids, same bounds, same context


def test_the_route_takes_the_two_path_slugs_and_one_required_date():
    route = next(r for r in main.app.routes if r.path.endswith("/rejects/count"))
    flat = get_flat_dependant(route.dependant)

    assert route.path == "/api/organizations/{org_slug}/branches/{branch_slug}/rejects/count"
    assert route.methods == {"GET"}
    assert sorted(p.name for p in flat.path_params) == ["branch_slug", "org_slug"]
    # One query parameter, required: no range, no reason filter, no zone, no paging.
    assert [(p.alias, p.field_info.is_required()) for p in flat.query_params] == [("date", True)]
    assert flat.body_params == []
    assert flat.header_params == []
    assert flat.cookie_params == []


def test_the_route_is_read_only(api):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(api, method)(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 405


def test_there_is_exactly_one_reject_route():
    paths = [r.path for r in main.app.routes if "reject" in getattr(r, "path", "")]

    assert paths == ["/api/organizations/{org_slug}/branches/{branch_slug}/rejects/count"]   # no by-hour, reasons, rate


# =====================================================================================================================
# Failures are 500, never a partial or empty answer
# =====================================================================================================================

def _assert_generic_500(response) -> None:
    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert "reject_count" not in response.text
    for leaked in ("synthetic", str(CUSTOMER), "rejects", "reject_events", "v2_cutovers", "current_setting",
                   "SELECT", "ZoneInfo", "Mars"):
        assert leaked not in response.text, leaked


def test_a_resolver_database_failure_is_500(api, monkeypatch, operational):
    def broken_resolve(user_id, org_slug, branch_slug):
        raise RuntimeError(f"synthetic database failure for customer {CUSTOMER}")

    monkeypatch.setattr(tenant_scope, "resolve_operational_tenant", broken_resolve)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))
    assert operational.log == []


@pytest.mark.parametrize("value", ["Mars/Olympus_Mons", "", "   ", "America/Chicagoo", "../etc/passwd"])
def test_an_invalid_configured_zone_is_500_and_opens_no_connection(api, monkeypatch, saas_db, operational, value):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", value)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))
    assert operational.log == []   # never a silent fall-back to some other zone, and never a query in the wrong one


def test_an_engine_failure_is_500(api, monkeypatch, saas_db):
    def broken_get_engine():
        raise RuntimeError("synthetic engine failure")

    monkeypatch.setattr(tenant_scope, "get_engine", broken_get_engine)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))


def test_a_failure_reading_the_tenant_context_back_is_500(api, saas_db, operational):
    operational.fail_on = "context"

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))
    assert operational.log == ["open", "verify context", "close"]


@pytest.mark.parametrize(
    "read_back",
    [
        lambda settings: {**settings, "customer_id": None},
        lambda settings: {**settings, "branch_id": "21"},
    ],
    ids=["context missing", "another tenant's context"],
)
def test_a_tenant_context_that_does_not_verify_is_500_and_nothing_is_counted(api, saas_db, operational, read_back):
    operational.read_back = read_back

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))
    assert operational.log == ["open", "verify context", "close"]
    assert operational.queries == []


@pytest.mark.parametrize("failing", ["v2_cutovers", "rejects", "reject_events"])
def test_a_failure_in_any_metric_query_is_500_never_a_partial_count(api, saas_db, operational, caplog, failing):
    operational.cutover = NOON_CUTOVER
    operational.v1_rejects, operational.v2_rejects = 4001, 2003
    operational.fail_on = failing

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    _assert_generic_500(response)
    assert "4001" not in response.text and "2003" not in response.text   # neither era's count comes back alone
    assert operational.log[-2:] == [failing, "close"]
    assert "synthetic" not in caplog.text   # logged as a safe summary only


def test_a_service_failure_is_500(api, monkeypatch, saas_db, operational):
    def broken_service(conn, tenant, **kwargs):
        raise RuntimeError(f"synthetic service failure for customer {CUSTOMER}")

    monkeypatch.setattr(operational_routes, "get_reject_count", broken_service)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))
    assert operational.log == ["open", "verify context", "close"]   # the scoped connection is still closed


def test_a_failure_of_the_check_in_tables_does_not_touch_the_reject_count(api, saas_db, operational):
    operational.v1_rejects = 6
    operational.fail_on = "checkins"

    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()["reject_count"] == 6


# =====================================================================================================================
# Lifecycle and wiring
# =====================================================================================================================

def test_the_connection_is_closed_before_the_response_is_built(api, monkeypatch, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    real_model, real_response = operational_routes.RejectCountResponse, operational_routes.JSONResponse

    def recording_model(*args, **kwargs):
        operational.log.append("build body")
        return real_model(*args, **kwargs)

    def recording_response(*args, **kwargs):
        operational.log.append("build response")
        return real_response(*args, **kwargs)

    monkeypatch.setattr(operational_routes, "RejectCountResponse", recording_model)
    monkeypatch.setattr(operational_routes, "JSONResponse", recording_response)

    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 200
    assert operational.log == [
        "open", "verify context", "v2_cutovers", "rejects", "reject_events", "close", "build body", "build response",
    ]


def test_every_request_opens_its_own_connection_and_nothing_is_cached(api, saas_db, operational):
    operational.v1_rejects = 5
    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()["reject_count"] == 5

    operational.v1_rejects = 9
    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()["reject_count"] == 9
    assert operational.log.count("open") == operational.log.count("close") == 2


def test_the_response_is_never_cacheable(api, saas_db, operational):
    ok = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    checkins = api.get(CHECKIN_COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert ok.headers["cache-control"] == checkins.headers["cache-control"] == "no-store"
    assert "etag" not in ok.headers and "last-modified" not in ok.headers and "expires" not in ok.headers


def test_the_route_reaches_the_database_only_through_the_tenant_scope_and_the_metrics_service():
    source = inspect.getsource(operational_routes)

    assert operational_routes.get_reject_count is operational_metrics_service.get_reject_count
    assert operational_routes.open_customer_tenant_connection is tenant_scope.open_customer_tenant_connection
    for forbidden in ("get_engine", "tenant_connection(", "resolve_operational_tenant", "import database",
                      "from database", "tenant_db", "set_config", "SELECT", "data_loader", "mixed_era", "pandas",
                      "streamlit", "import main", "from main", "America/Chicago", "v1_count", "v2_count",
                      "reject_logic", "simplify_error"):
        assert forbidden not in source.replace("open_customer_tenant_connection(", ""), forbidden

    # The reject route itself names nothing about why an item was rejected. (The ingest-status route, elsewhere
    # in the module, legitimately reports a collector's last_error_class.)
    route = next(r for r in main.app.routes if r.path.endswith("/rejects/count"))
    for forbidden in ("error", "reason", "message", "class", "rate"):
        assert forbidden not in inspect.getsource(route.endpoint), forbidden


def test_the_reject_route_is_built_like_the_check_in_count_route():
    routes = {r.path.split("/branches/{branch_slug}/")[1]: r for r in main.app.routes
              if "/branches/{branch_slug}/" in getattr(r, "path", "")}
    rejects, checkins = routes["rejects/count"], routes["checkins/count"]

    # The same two declared parameters -- the tenant-scope dependency and the strict required date...
    signatures = [{name: p.annotation for name, p in inspect.signature(r.endpoint).parameters.items()}
                  for r in (rejects, checkins)]
    assert signatures[0] == signatures[1] == {"tenant": "ResolvedTenant", "local_date": "LocalDate"}
    assert [d.call for d in rejects.dependant.dependencies] == [d.call for d in checkins.dependant.dependencies]
    # ...the same route class producing the same error bodies, and the same steps in the same order.
    assert type(rejects) is type(checkins)
    source = inspect.getsource(rejects.endpoint)
    steps = ["zone = settings.product_timezone()", "with open_customer_tenant_connection(tenant) as conn:",
             "get_reject_count(conn, tenant, local_date=local_date, zone=zone)", "RejectCountResponse(",
             "reject_count=count.total", "headers=NO_STORE_HEADERS"]
    positions = [source.index(step) for step in steps]
    assert positions == sorted(positions)


def test_the_response_schema_declares_only_the_approved_fields():
    assert list(RejectCountResponse.model_fields) == ["date", "timezone", "reject_count"]
    assert RejectCountResponse.model_fields["date"].annotation is date
    assert RejectCountResponse.model_fields["reject_count"].annotation is int
    assert RejectCountResponse is not CheckinCountResponse
    assert list(CheckinCountResponse.model_fields) == ["date", "timezone", "checkin_count"]   # untouched


@pytest.mark.parametrize(
    "extra",
    ["total", "v1_count", "v2_count", "customer_id", "branch_id", "cutover_at", "era", "reasons", "error_class",
     "reject_rate", "checkin_count", "hours"],
)
def test_the_response_schema_refuses_any_field_that_is_not_approved(extra):
    from pydantic import ValidationError

    assert RejectCountResponse.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError):
        RejectCountResponse(date=date(2026, 6, 10), timezone="America/Chicago", reject_count=1, **{extra: 1})


def test_the_customer_api_still_loads_without_streamlit_or_pandas():
    # The seam itself is proven in a clean process by tests/test_customer_api_import_seam.py; this pins that the
    # modules this block touched name neither.
    from customer_api import operational_schemas

    for module in (operational_routes, operational_schemas, operational_metrics_service):
        imports = [line for line in inspect.getsource(module).splitlines() if line.startswith(("import ", "from "))]
        assert not any("streamlit" in line or "pandas" in line or "reject_logic" in line for line in imports)
