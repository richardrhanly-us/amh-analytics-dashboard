"""Block 6c: the check-ins-by-hour endpoint.

    GET /api/organizations/{org_slug}/branches/{branch_slug}/checkins/by-hour?date=YYYY-MM-DD

These tests drive the real production route through TestClient(main.app): the
session dependency, the tenant-scope dependency, the scoped and verified
connection, the metrics service and the response schema all run for real.
What stands in for PostgreSQL is:

- an in-memory SQLite database for the SaaS tables, so the REAL tenant
  resolver decides which organization/branch pairs resolve;
- a recording fake engine for the operational connection, which answers the
  set_config / current_setting calls the way PostgreSQL does and returns a
  chosen cutover and chosen per-era rows of 24 counts to the REAL metrics
  service.

The hour boundaries and the cutover rules themselves are tested in
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
from customer_api.operational_schemas import CheckinHourCount, CheckinsByHourResponse
from services import (
    operational_metrics_service,
    session_service,
    tenant_resolution_service,
)
from services.operational_metrics_service import CheckinHourlyCounts

PATH = "/api/organizations/{org}/branches/{branch}/checkins/by-hour"
ACME_MAIN = PATH.format(org="acme", branch="main")
COUNT_ACME_MAIN = "/api/organizations/acme/branches/main/checkins/count"
COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 1, "email": "alice@example.invalid", "full_name": "Alice"}

CUSTOMER, BRANCH = 8101, 11
JUNE_10 = {"date": "2026-06-10"}
NOON_CUTOVER = datetime(2026, 6, 10, 17, 0, tzinfo=UTC)          # 12:00 local on 10 June in America/Chicago
HALF_PAST_TWO_CUTOVER = datetime(2026, 6, 10, 19, 30, tzinfo=UTC)   # 14:30 local

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
TENANT_NOT_FOUND = {"code": "tenant_not_found", "message": "Organization or branch not found."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

NO_HOURS = (0,) * 24


def _hours(busy: dict[int, int] | None = None) -> tuple[int, ...]:
    """24 counts, zero except where `busy` says otherwise."""
    return tuple((busy or {}).get(hour, 0) for hour in range(24))


def _body_hours(counts) -> list[dict]:
    return [{"hour": hour, "checkin_count": count} for hour, count in enumerate(counts)]


def _local(year, month, day, hour=0, minute=0) -> datetime:
    """A NAIVE local wall-clock datetime, as checkins.event_time holds it."""
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

        for table, answer in (
            ("v2_cutovers", lambda: None if owner.cutover is None else (owner.cutover,)),
            ("checkins", lambda: owner.v1_hours),
            ("checkin_events", lambda: owner.v2_hours),
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
    """The flat database engine, as the tenant scope sees it. Each era's
    statement returns one row of 24 counts, as the real ones do."""

    def __init__(self):
        self.cutover: datetime | None = None
        self.v1_hours: tuple[int, ...] = NO_HOURS
        self.v2_hours: tuple[int, ...] = NO_HOURS
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

def test_a_member_gets_24_hourly_counts_for_the_requested_local_date(api, saas_db, operational):
    operational.v1_hours = _hours({9: 12, 10: 30, 14: 7, 19: 4})

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 200
    assert response.json() == {
        "date": "2026-06-10",
        "timezone": "America/Chicago",
        "hours": [
            {"hour": 0, "checkin_count": 0}, {"hour": 1, "checkin_count": 0}, {"hour": 2, "checkin_count": 0},
            {"hour": 3, "checkin_count": 0}, {"hour": 4, "checkin_count": 0}, {"hour": 5, "checkin_count": 0},
            {"hour": 6, "checkin_count": 0}, {"hour": 7, "checkin_count": 0}, {"hour": 8, "checkin_count": 0},
            {"hour": 9, "checkin_count": 12}, {"hour": 10, "checkin_count": 30}, {"hour": 11, "checkin_count": 0},
            {"hour": 12, "checkin_count": 0}, {"hour": 13, "checkin_count": 0}, {"hour": 14, "checkin_count": 7},
            {"hour": 15, "checkin_count": 0}, {"hour": 16, "checkin_count": 0}, {"hour": 17, "checkin_count": 0},
            {"hour": 18, "checkin_count": 0}, {"hour": 19, "checkin_count": 4}, {"hour": 20, "checkin_count": 0},
            {"hour": 21, "checkin_count": 0}, {"hour": 22, "checkin_count": 0}, {"hour": 23, "checkin_count": 0},
        ],
    }
    assert response.headers["cache-control"] == "no-store"


def test_the_response_has_exactly_the_approved_fields_at_both_levels(api, saas_db, operational):
    operational.v1_hours = _hours({9: 1})

    body = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()

    assert list(body) == ["date", "timezone", "hours"]
    assert all(list(entry) == ["hour", "checkin_count"] for entry in body["hours"])
    assert all(type(entry["hour"]) is int and type(entry["checkin_count"]) is int for entry in body["hours"])


def test_there_are_always_exactly_24_entries_for_hours_0_to_23_in_order(api, saas_db, operational):
    for day in ("2026-06-10", "2026-03-08", "2026-11-01", "2028-02-29", "1999-01-01", "2099-12-31"):
        body = api.get(ACME_MAIN, headers=COOKIE, params={"date": day}).json()

        assert len(body["hours"]) == 24, day
        assert [entry["hour"] for entry in body["hours"]] == list(range(24)), day


def test_hours_with_nothing_in_them_are_returned_as_zero_not_left_out(api, saas_db, operational):
    # Activity only at 03:00 and 22:00 -- outside any library's opening hours.
    operational.v1_hours = _hours({3: 5, 22: 6})

    body = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()

    assert body["hours"] == _body_hours(_hours({3: 5, 22: 6}))
    assert [entry["hour"] for entry in body["hours"] if entry["checkin_count"] == 0] == [
        hour for hour in range(24) if hour not in (3, 22)
    ]


def test_a_day_with_no_rows_is_24_zeros(api, saas_db, operational):
    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 200
    assert response.json()["hours"] == _body_hours(NO_HOURS)
    assert operational.log == ["open", "verify context", "v2_cutovers", "checkins", "close"]   # it really looked


def test_each_services_count_is_returned_against_its_own_hour(api, monkeypatch, saas_db, operational):
    counts = tuple(100 + hour for hour in range(24))     # every hour different, so a shifted or reordered list shows

    def fixed_counts(conn, tenant, **kwargs):
        return CheckinHourlyCounts(counts=counts, v1_counts=counts, v2_counts=NO_HOURS)

    monkeypatch.setattr(operational_routes, "get_checkin_counts_by_hour", fixed_counts)

    body = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()

    assert body["hours"] == [{"hour": hour, "checkin_count": 100 + hour} for hour in range(24)]


def test_an_hour_shared_by_both_eras_is_returned_as_one_total(api, saas_db, operational):
    operational.cutover = HALF_PAST_TWO_CUTOVER
    operational.v1_hours = _hours({9: 5, 14: 3})          # v1's share of 14:00-14:29
    operational.v2_hours = _hours({14: 4, 15: 6})         # v2's share of 14:30-14:59

    body = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()

    assert body["hours"] == _body_hours(_hours({9: 5, 14: 7, 15: 6}))


def test_the_era_split_and_the_cutover_are_never_exposed(api, saas_db, operational):
    operational.cutover = HALF_PAST_TWO_CUTOVER
    operational.v1_hours = _hours({14: 4001})             # distinctive, so a leak would be visible
    operational.v2_hours = _hours({14: 2003})

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json()["hours"][14] == {"hour": 14, "checkin_count": 6004}
    for leaked in ("4001", "2003", "v1", "v2", "cutover", "era", "source", "19:30", "14:30", "counts", "total",
                   str(CUSTOMER), "customer_id", "branch_id", "operational"):
        assert leaked not in response.text, leaked


def test_the_hours_add_up_to_what_the_count_endpoint_returns_for_the_same_day(api, monkeypatch, saas_db, operational):
    # Both routes, both real services, one fake database answering each statement in its own shape.
    operational.cutover = HALF_PAST_TWO_CUTOVER
    operational.v1_hours = _hours({8: 2, 9: 5, 14: 3})
    operational.v2_hours = _hours({14: 4, 15: 6, 23: 1})
    hourly = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()

    real_execute = FakeOperationalConnection.execute

    def execute_answering_day_counts(self, statement, parameters=None):
        result = real_execute(self, statement, parameters)
        value = result.one()
        return _Result(sum(value)) if isinstance(value, tuple) and len(value) == 24 else result

    monkeypatch.setattr(FakeOperationalConnection, "execute", execute_answering_day_counts)
    daily = api.get(COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10).json()

    assert sum(entry["checkin_count"] for entry in hourly["hours"]) == daily["checkin_count"] == 21
    assert (hourly["date"], hourly["timezone"]) == (daily["date"], daily["timezone"])


def test_a_suspended_organization_reads_exactly_like_a_full_one(api, saas_db, operational):
    operational.v1_hours = _hours({9: 7})
    full = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    with saas_db.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE slug = 'acme'"))

    read_only = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert read_only.status_code == full.status_code == 200
    assert read_only.json() == full.json()


def test_the_read_only_tenant_reaches_the_service_marked_as_such(api, monkeypatch, saas_db, operational):
    seen = []
    real = operational_metrics_service.get_checkin_counts_by_hour

    def recording(conn, tenant, **kwargs):
        seen.append(tenant.access_mode)
        return real(conn, tenant, **kwargs)

    monkeypatch.setattr(operational_routes, "get_checkin_counts_by_hour", recording)
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

    def recording_counts(conn, tenant, **kwargs):
        seen.update(kwargs, conn=conn, tenant=tenant)
        return CheckinHourlyCounts(counts=_hours({9: 5}), v1_counts=_hours({9: 5}), v2_counts=NO_HOURS)

    monkeypatch.setattr(operational_routes, "get_checkin_counts_by_hour", recording_counts)

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json()["hours"][9]["checkin_count"] == 5
    assert seen["local_date"] == date(2026, 6, 10) and type(seen["local_date"]) is date
    assert seen["zone"] == ZoneInfo("America/Chicago") and isinstance(seen["zone"], ZoneInfo)
    assert isinstance(seen["conn"], FakeOperationalConnection)       # the scoped connection, not a new one
    assert (seen["tenant"].operational_customer_id, seen["tenant"].operational_branch_id) == (CUSTOMER, BRANCH)
    assert set(seen) == {"local_date", "zone", "conn", "tenant"}


def test_the_statements_are_the_hourly_ones_bounded_by_the_local_day_of_the_configured_zone(
    api, saas_db, operational
):
    operational.cutover = NOON_CUTOVER

    api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    v1, v2 = operational.parameters_for("checkins"), operational.parameters_for("checkin_events")
    assert (v1["start_local"], v1["end_local"]) == (_local(2026, 6, 10), _local(2026, 6, 10, 12))
    assert (v2["start_utc"], v2["end_utc"]) == (NOON_CUTOVER, datetime(2026, 6, 11, 5, 0, tzinfo=UTC))
    assert (v1["boundary_0"], v1["boundary_9"], v1["boundary_24"]) == (
        _local(2026, 6, 10), _local(2026, 6, 10, 9), _local(2026, 6, 11),
    )
    assert (v2["boundary_0"], v2["boundary_9"], v2["boundary_24"]) == (
        datetime(2026, 6, 10, 5, tzinfo=UTC), datetime(2026, 6, 10, 14, tzinfo=UTC),
        datetime(2026, 6, 11, 5, tzinfo=UTC),
    )
    era_statements = [sql for sql in operational.statements if "FROM v2_cutovers " not in sql]
    assert len(era_statements) == 2
    assert all(sql.count("COUNT(*) FILTER (WHERE") == 24 for sql in era_statements)


def test_every_query_runs_for_the_resolved_tenant_on_a_connection_carrying_its_context(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER

    api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert [table for table, _, _ in operational.queries] == ["v2_cutovers", "checkins", "checkin_events"]
    for _table, parameters, context in operational.queries:
        assert (parameters["customer_id"], parameters["branch_id"]) == (CUSTOMER, BRANCH)
        assert context == {"customer_id": str(CUSTOMER), "branch_id": str(BRANCH)}


def test_the_configured_zone_is_echoed_and_used(api, monkeypatch, saas_db, operational):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Asia/Kolkata")
    operational.cutover = NOON_CUTOVER   # 22:30 on 10 June in Kolkata

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json()["timezone"] == "Asia/Kolkata"
    assert len(response.json()["hours"]) == 24
    assert operational.parameters_for("checkins")["end_local"] == _local(2026, 6, 10, 22, 30)
    # 09:00 in Kolkata is 03:30Z: the hour boundaries are the zone's own, on the half hour.
    assert operational.parameters_for("checkin_events")["boundary_9"] == datetime(2026, 6, 10, 3, 30, tzinfo=UTC)


def test_a_local_day_is_24_buckets_however_many_real_hours_it_has(api, saas_db, operational):
    operational.cutover = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)   # long before: v2 owns each whole day

    seen = {}
    for label, day in (("spring forward", "2026-03-08"), ("ordinary", "2026-06-10"), ("fall back", "2026-11-01")):
        operational.queries.clear()
        body = api.get(ACME_MAIN, headers=COOKIE, params={"date": day}).json()
        v2 = operational.parameters_for("checkin_events")
        seen[label] = (len(body["hours"]), v2["boundary_24"] - v2["boundary_0"])

    assert seen == {
        "spring forward": (24, timedelta(hours=23)),
        "ordinary": (24, timedelta(hours=24)),
        "fall back": (24, timedelta(hours=25)),
    }


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
        "2026-06-10T00:00:00", "2026-06-10T12:30:00Z", "2026-06-10 00:00:00", "2026-06-10T09", "1781136000",
        "2026-06-10 ", " 2026-06-10", "2026-06", "+2026-06-10",
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


def test_an_hour_or_a_range_cannot_be_asked_for(api, saas_db, operational):
    operational.v1_hours = _hours({9: 5, 14: 2})
    plain = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    plain_queries = list(operational.queries)
    operational.queries.clear()

    narrowed = api.get(
        ACME_MAIN, headers=COOKIE,
        params={**JUNE_10, "hour": 9, "start_hour": 7, "end_hour": 20, "from": "2026-06-01", "to": "2026-06-30",
                "start_date": "2026-06-01", "end_date": "2026-06-30"},
    )

    assert narrowed.status_code == 200
    assert narrowed.json() == plain.json()            # still one day, still all 24 hours
    assert operational.queries == plain_queries


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


def test_an_unreachable_tenant_answers_exactly_as_it_does_on_the_count_endpoint(api, saas_db, operational):
    by_hour = api.get(PATH.format(org="beta", branch="north"), headers=COOKIE, params=JUNE_10)
    count = api.get("/api/organizations/beta/branches/north/checkins/count", headers=COOKIE, params=JUNE_10)

    assert (by_hour.status_code, by_hour.content) == (count.status_code, count.content) == (404, by_hour.content)


def test_24_zeros_and_an_unreachable_tenant_are_different_answers(api, saas_db, operational):
    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 200
    assert api.get(PATH.format(org="acme", branch="shut"), headers=COOKIE, params=JUNE_10).status_code == 404


# =====================================================================================================================
# The request cannot choose the tenant
# =====================================================================================================================

def test_operational_ids_and_other_tenant_hints_in_the_request_change_nothing(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_hours, operational.v2_hours = _hours({9: 3}), _hours({13: 4})
    plain = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    plain_queries = list(operational.queries)
    operational.queries.clear()

    tampered = api.get(
        ACME_MAIN,
        headers={**COOKIE, "X-Customer-Id": "8202", "X-Branch-Id": "21"},
        params={**JUNE_10, "customer_id": 8202, "branch_id": 21, "operational_customer_id": 8202,
                "operational_branch_id": 21, "org_slug": "beta", "branch_slug": "north", "user_id": 2,
                "timezone": "Asia/Tokyo", "zone": "UTC", "cutover": "2020-01-01T00:00:00Z", "era": "v2", "v1": "false"},
    )

    assert tampered.status_code == 200
    assert tampered.json() == plain.json()
    assert operational.queries == plain_queries   # same statements, same ids, same bounds, same context


def test_the_route_takes_the_two_path_slugs_and_one_required_date():
    route = next(r for r in main.app.routes if r.path.endswith("/checkins/by-hour"))
    flat = get_flat_dependant(route.dependant)

    assert route.path == "/api/organizations/{org_slug}/branches/{branch_slug}/checkins/by-hour"
    assert route.methods == {"GET"}
    assert sorted(p.name for p in flat.path_params) == ["branch_slug", "org_slug"]
    # One query parameter, required: no range, no hour window, no zone, no paging.
    assert [(p.alias, p.field_info.is_required()) for p in flat.query_params] == [("date", True)]
    assert flat.body_params == []
    assert flat.header_params == []
    assert flat.cookie_params == []


def test_the_route_is_read_only(api):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(api, method)(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 405


# =====================================================================================================================
# Failures are 500, never a partial or empty answer
# =====================================================================================================================

def _assert_generic_500(response) -> None:
    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert "hours" not in response.text
    for leaked in ("synthetic", str(CUSTOMER), "checkins", "checkin_events", "v2_cutovers", "current_setting",
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


@pytest.mark.parametrize("failing", ["v2_cutovers", "checkins", "checkin_events"])
def test_a_failure_in_any_metric_query_is_500_never_partial_hours(api, saas_db, operational, caplog, failing):
    operational.cutover = NOON_CUTOVER
    operational.v1_hours, operational.v2_hours = _hours({9: 4001}), _hours({13: 2003})
    operational.fail_on = failing

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    _assert_generic_500(response)
    assert "4001" not in response.text and "2003" not in response.text   # neither era's hours come back alone
    assert operational.log[-2:] == [failing, "close"]
    assert "synthetic" not in caplog.text   # logged as a safe summary only


def test_a_service_failure_is_500(api, monkeypatch, saas_db, operational):
    def broken_service(conn, tenant, **kwargs):
        raise RuntimeError(f"synthetic service failure for customer {CUSTOMER}")

    monkeypatch.setattr(operational_routes, "get_checkin_counts_by_hour", broken_service)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))
    assert operational.log == ["open", "verify context", "close"]   # the scoped connection is still closed


@pytest.mark.parametrize("width", [0, 23, 25])
def test_a_statement_that_does_not_return_24_counts_is_500_not_a_short_or_long_list(
    api, saas_db, operational, width
):
    operational.v1_hours = (1,) * width

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))


# =====================================================================================================================
# Lifecycle and wiring
# =====================================================================================================================

def test_the_connection_is_closed_before_the_response_is_built(api, monkeypatch, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    real_model, real_response = operational_routes.CheckinsByHourResponse, operational_routes.JSONResponse

    def recording_model(*args, **kwargs):
        operational.log.append("build body")
        return real_model(*args, **kwargs)

    def recording_response(*args, **kwargs):
        operational.log.append("build response")
        return real_response(*args, **kwargs)

    monkeypatch.setattr(operational_routes, "CheckinsByHourResponse", recording_model)
    monkeypatch.setattr(operational_routes, "JSONResponse", recording_response)

    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 200
    assert operational.log == [
        "open", "verify context", "v2_cutovers", "checkins", "checkin_events", "close", "build body", "build response",
    ]


def test_every_request_opens_its_own_connection_and_nothing_is_cached(api, saas_db, operational):
    operational.v1_hours = _hours({9: 5})
    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()["hours"][9]["checkin_count"] == 5

    operational.v1_hours = _hours({9: 9})
    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()["hours"][9]["checkin_count"] == 9
    assert operational.log.count("open") == operational.log.count("close") == 2


def test_the_response_is_never_cacheable(api, saas_db, operational):
    ok = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    count = api.get(COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert ok.headers["cache-control"] == count.headers["cache-control"] == "no-store"
    assert "etag" not in ok.headers and "last-modified" not in ok.headers and "expires" not in ok.headers


def test_the_route_reaches_the_database_only_through_the_tenant_scope_and_the_metrics_service():
    source = inspect.getsource(operational_routes)

    assert operational_routes.get_checkin_counts_by_hour is operational_metrics_service.get_checkin_counts_by_hour
    assert operational_routes.open_customer_tenant_connection is tenant_scope.open_customer_tenant_connection
    for forbidden in ("get_engine", "tenant_connection(", "resolve_operational_tenant", "import database",
                      "from database", "tenant_db", "set_config", "SELECT", "data_loader", "mixed_era", "pandas",
                      "streamlit", "import main", "from main", "America/Chicago", "start_hour", "end_hour",
                      "v1_counts", "v2_counts"):
        assert forbidden not in source.replace("open_customer_tenant_connection(", ""), forbidden


def test_the_two_checkin_routes_share_their_date_parsing_tenant_scope_and_zone():
    routes = {r.path.rsplit("/", 1)[1]: r for r in main.app.routes if "/checkins/" in getattr(r, "path", "")}
    count, by_hour = (inspect.signature(routes[name].endpoint).parameters for name in ("count", "by-hour"))

    # The same two declared parameters: the tenant-scope dependency and the strict required date.
    assert {name: p.annotation for name, p in by_hour.items()} == {name: p.annotation for name, p in count.items()}
    assert {name: p.annotation for name, p in by_hour.items()} == {"tenant": "ResolvedTenant", "local_date": "LocalDate"}
    assert [d.call for d in routes["by-hour"].dependant.dependencies] == [
        d.call for d in routes["count"].dependant.dependencies
    ]
    assert inspect.getsource(operational_routes).count("zone = settings.product_timezone()") == 2


def test_the_response_schemas_declare_only_the_approved_fields():
    assert list(CheckinsByHourResponse.model_fields) == ["date", "timezone", "hours"]
    assert CheckinsByHourResponse.model_fields["date"].annotation is date
    assert CheckinsByHourResponse.model_fields["hours"].annotation == list[CheckinHourCount]
    assert list(CheckinHourCount.model_fields) == ["hour", "checkin_count"]
    assert CheckinHourCount.model_fields["hour"].annotation is int
    assert CheckinHourCount.model_fields["checkin_count"].annotation is int


@pytest.mark.parametrize("extra", ["total", "v1_counts", "v2_counts", "customer_id", "branch_id", "cutover_at", "era"])
def test_the_response_schemas_refuse_any_field_that_is_not_approved(extra):
    from pydantic import ValidationError

    assert CheckinsByHourResponse.model_config["extra"] == CheckinHourCount.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError):
        CheckinsByHourResponse(date=date(2026, 6, 10), timezone="America/Chicago", hours=[], **{extra: 1})
    with pytest.raises(ValidationError):
        CheckinHourCount(hour=9, checkin_count=1, **{extra: 1})
