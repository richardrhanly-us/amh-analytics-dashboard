"""Reports R2: the four sorter-site range reports.

    GET /api/organizations/{org_slug}/branches/{branch_slug}/reports/overview?from=&to=
    GET .../reports/volume?from=&to=
    GET .../reports/routing?from=&to=
    GET .../reports/reliability?from=&to=

These tests drive the real production routes through TestClient(main.app):
the session dependency, the tenant-scope dependency, the range dependency,
the scoped and verified connection, the settings read, the report service and
the response schemas all run for real, and so does every SQL statement --
against the in-memory SQLite database of
tests/test_customer_api_checkins_by_destination.py, with the two reject
tables added. As there, the operational engine is that database behind a
thin wrapper that keeps PostgreSQL's two tenant-context settings.

The one thing the routes read that a test must hold still is the clock:
the route module reads the shared controlled clock (tests/controlled_clock.py),
which only a test moves, so no result here depends on the real date.

THE INVARIANT THIS FILE IS BUILT AROUND: for every date of a range, a
report's counts for that date are exactly what the existing single-day
endpoints answer for it.

The pure range, window and statement rules are in
tests/test_operational_report_service.py. Real TIMESTAMP / TIMESTAMPTZ
comparison and the database session time zone are tested against a real
server in tests/test_rls_phase1_postgres.py.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta

import pytest
from controlled_clock import ControlledClock
from entitlement_support import feature, grant
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool
from test_customer_api_checkins_by_destination import (
    _SCHEMA,
    _STORED,
    ACME_SETTINGS,
    BRANCH,
    CANARY,
    CUSTOMER,
    EAST,
    EAST_SETTINGS,
    OTHER_BRANCH,
    OTHER_CUSTOMER,
    ScopedDatabase,
)

import main
from customer_api import report_routes, report_schemas, tenant_scope
from services import (
    operational_report_service,
    session_service,
    tenant_resolution_service,
)
from services.reject_reason import REJECT_REASONS

BASE = "/api/organizations/{org}/branches/{branch}"
REPORTS = ("overview", "volume", "routing", "reliability")
COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 1, "email": "alice@example.invalid", "full_name": "Alice"}

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
TENANT_NOT_FOUND = {"code": "tenant_not_found", "message": "Organization or branch not found."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

# Where the clock starts unless a test moves it: 1 PM on Saturday 20 June 2026 in America/Chicago.
CLOCK = ControlledClock(datetime(2026, 6, 20, 18, 0, tzinfo=UTC))
JUNE_8_TO_12 = {"from": "2026-06-08", "to": "2026-06-12"}
NOON_JUNE_10 = datetime(2026, 6, 10, 17, 0, tzinfo=UTC)     # 12:00 local on 10 June

_REJECT_TABLES = (
    (
        "CREATE TABLE rejects (id INTEGER PRIMARY KEY, customer_id INTEGER, branch_id INTEGER, event_time TEXT, "
        "barcode TEXT, error_message TEXT)"
    ),
    (
        "CREATE TABLE reject_events (id INTEGER PRIMARY KEY, customer_id INTEGER, branch_id INTEGER, key_id TEXT, "
        "event_key TEXT, event_time TEXT, error_class TEXT, item_key TEXT)"
    ),
)


def _local(month: int, day: int, hour: int = 10, minute: int = 0) -> datetime:
    """A NAIVE local wall-clock datetime in 2026, as a legacy table holds it."""
    return datetime(2026, month, day, hour, minute)  # noqa: DTZ001


def _utc(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


class ReportDatabase(ScopedDatabase):
    """The by-destination tests' database, with rejects."""

    def reject_v1(self, message, count: int = 1, *, at: datetime, customer=CUSTOMER, branch=BRANCH) -> None:
        with self.engine.begin() as conn:
            for number in range(count):
                conn.execute(
                    text("INSERT INTO rejects (customer_id, branch_id, event_time, barcode, error_message) "
                         "VALUES (:c, :b, :t, :bc, :m)"),
                    {"c": customer, "b": branch, "t": at.strftime(_STORED), "bc": f"{CANARY}-{number}", "m": message},
                )

    def reject_v2(self, error_class, count: int = 1, *, at: datetime, customer=CUSTOMER, branch=BRANCH) -> None:
        with self.engine.begin() as conn:
            for _ in range(count):
                conn.execute(
                    text("INSERT INTO reject_events (customer_id, branch_id, event_time, error_class, item_key) "
                         "VALUES (:c, :b, :t, :e, :ik)"),
                    {"c": customer, "b": branch, "t": at.astimezone(UTC).strftime(_STORED), "e": error_class, "ik": CANARY},
                )


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in (*_SCHEMA, *_REJECT_TABLES):
            conn.execute(text(statement))
    database = ReportDatabase(engine)
    database.settings(organization=1, document=ACME_SETTINGS)
    database.settings(branch=EAST, document=EAST_SETTINGS)
    monkeypatch.setattr(tenant_resolution_service, "get_engine", lambda: engine)
    monkeypatch.setattr(tenant_scope, "get_engine", lambda: database)
    yield database
    engine.dispose()


@pytest.fixture
def clock():
    """The clock the report routes read. It starts where CLOCK starts and stays there until a test sets it."""
    CLOCK.reset()
    with CLOCK.controlling(report_routes):
        yield CLOCK


@pytest.fixture
def api(monkeypatch, clock):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: dict(USER))
    # Every plan feature, with no history limit: what a plan does to these reports is tested on its own (R9C).
    grant(monkeypatch)
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


def _path(report: str, org="acme", branch="main") -> str:
    return BASE.format(org=org, branch=branch) + f"/reports/{report}"


def _get(api, report: str, params=JUNE_8_TO_12, *, org="acme", branch="main", headers=COOKIE):
    return api.get(_path(report, org, branch), headers=headers, params=params)


def _report(api, report: str, params=JUNE_8_TO_12, **scope) -> dict:
    response = _get(api, report, params, **scope)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def _single_day(api, endpoint: str, day: str, **scope) -> dict:
    response = api.get(BASE.format(org=scope.get("org", "acme"), branch=scope.get("branch", "main")) + f"/{endpoint}",
                       headers=COOKIE, params={"date": day})
    assert response.status_code == 200, response.text
    return response.json()


def _dates(first: str, last: str) -> list[str]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def _ints(value) -> list:
    """Every number anywhere in a JSON value."""
    if isinstance(value, dict):
        return [number for item in value.values() for number in _ints(item)]
    if isinstance(value, list):
        return [number for item in value for number in _ints(item)]
    return [value] if isinstance(value, (int, float)) and not isinstance(value, bool) else []


def _seed_a_mixed_week(db) -> None:
    """Acme's main site, 8-12 June 2026, cut over at noon on the 10th.

        8 June   legacy   Main x4, Westside x2                         rejects: 2
        9 June   legacy   Main x3, Library Express x1, Depot x1        rejects: 0
        10 June  legacy   Main x2 (09:00), Westside x1 (11:59)         rejects: 1 (legacy, 10:00)
                 current  main x3, westside x2 (from 12:00)            rejects: 2 (current)
                 -- and rows on the wrong side of the cutover, which count for nothing
        11 June  current  main x5, library_express x2, unknown x1      rejects: 3
        12 June  nothing
    """
    db.cutover(NOON_JUNE_10)
    db.v1("Main", 4, at=_local(6, 8, 9))
    db.v1("Westside", 2, at=_local(6, 8, 14))
    db.v1("Main", 3, at=_local(6, 9, 9))
    db.v1("Library Express", 1, at=_local(6, 9, 10))
    db.v1("Depot", 1, at=_local(6, 9, 16))
    db.v1("Main", 2, at=_local(6, 10, 9))
    db.v1("Westside", 1, at=_local(6, 10, 11, 59))
    db.v1("Westside", 50, at=_local(6, 10, 12, 0))          # at the cutover on the legacy clock: v2 owns it
    db.v1("Main", 50, at=_local(6, 11, 9))                  # after the cutover: not part of the history
    db.v2("westside", 70, at=_utc(6, 10, 16, 59))           # before the cutover: not part of the history
    db.v2("main", 3, at=NOON_JUNE_10)                       # exactly at the cutover: counts
    db.v2("westside", 2, at=_utc(6, 10, 20))
    db.v2("main", 5, at=_utc(6, 11, 15))
    db.v2("library_express", 2, at=_utc(6, 11, 19))
    db.v2("unknown", 1, at=_utc(6, 11, 21))

    db.reject_v1("Item not found in database", 2, at=_local(6, 8, 10))
    db.reject_v1("ACS timeout", 1, at=_local(6, 10, 10))
    db.reject_v1("Library not found", 30, at=_local(6, 10, 13))         # after the cutover: not counted
    db.reject_v2("rfid_collision", 40, at=_utc(6, 10, 15))              # before the cutover: not counted
    db.reject_v2("rfid_collision", 1, at=_utc(6, 10, 18))
    db.reject_v2("item_not_found", 1, at=_utc(6, 10, 22))
    db.reject_v2("routing_error", 2, at=_utc(6, 11, 15))
    db.reject_v2("something_new", 1, at=_utc(6, 11, 16))                # not a reason code: counted as other


# =====================================================================================================================
# The four reports, on a week that crosses a cutover
# =====================================================================================================================

RANGE = {"from": "2026-06-08", "to": "2026-06-12", "days": 5, "timezone": "America/Chicago", "includes_today": False}


def test_the_overview_report(api, db):
    _seed_a_mixed_week(db)

    assert _report(api, "overview") == {
        "range": RANGE,
        "checkin_count": 27,
        "active_days": 4,
        "home_count": 17,
        "transit_count": 8,
        "other_count": 2,
        "reject_count": 8,
        "days": [
            {"date": "2026-06-08", "checkin_count": 6, "reject_count": 2},
            {"date": "2026-06-09", "checkin_count": 5, "reject_count": 0},
            {"date": "2026-06-10", "checkin_count": 8, "reject_count": 3},
            {"date": "2026-06-11", "checkin_count": 8, "reject_count": 3},
            {"date": "2026-06-12", "checkin_count": 0, "reject_count": 0},
        ],
    }


def test_the_volume_report(api, db):
    _seed_a_mixed_week(db)

    body = _report(api, "volume")

    assert list(body) == ["range", "checkin_count", "days", "hours"]
    assert body["range"] == RANGE and body["checkin_count"] == 27
    assert body["days"] == [
        {"date": "2026-06-08", "checkin_count": 6},
        {"date": "2026-06-09", "checkin_count": 5},
        {"date": "2026-06-10", "checkin_count": 8},
        {"date": "2026-06-11", "checkin_count": 8},
        {"date": "2026-06-12", "checkin_count": 0},
    ]
    assert [entry["hour"] for entry in body["hours"]] == list(range(24))
    # Totals across the five days, by the hour the local clock read (CDT is UTC-5).
    assert {entry["hour"]: entry["checkin_count"] for entry in body["hours"] if entry["checkin_count"]} == {
        9: 9, 10: 6, 11: 1, 12: 3, 14: 4, 15: 2, 16: 2,
    }


def test_the_routing_report(api, db):
    _seed_a_mixed_week(db)

    assert _report(api, "routing") == {
        "range": RANGE,
        "checkin_count": 27,
        "home": {"label": "Main", "checkin_count": 17},
        "transit": [
            {"key": "westside", "label": "Westside", "checkin_count": 5},
            {"key": "library_express", "label": "Library Express", "checkin_count": 3},
        ],
        "transit_count": 8,
        "other_count": 2,
        "days": [
            {"date": "2026-06-08", "checkin_count": 6, "home_count": 4, "transit_counts": [2, 0], "other_count": 0},
            {"date": "2026-06-09", "checkin_count": 5, "home_count": 3, "transit_counts": [0, 1], "other_count": 1},
            {"date": "2026-06-10", "checkin_count": 8, "home_count": 5, "transit_counts": [3, 0], "other_count": 0},
            {"date": "2026-06-11", "checkin_count": 8, "home_count": 5, "transit_counts": [0, 2], "other_count": 1},
            {"date": "2026-06-12", "checkin_count": 0, "home_count": 0, "transit_counts": [0, 0], "other_count": 0},
        ],
    }


def test_the_reliability_report(api, db):
    _seed_a_mixed_week(db)

    assert _report(api, "reliability") == {
        "range": RANGE,
        "checkin_count": 27,
        "reject_count": 8,
        "reasons": [
            {"reason": "item_not_found", "reject_count": 3},
            {"reason": "ils_acs_failure", "reject_count": 1},
            {"reason": "rfid_collision", "reject_count": 1},
            {"reason": "configuration_error", "reject_count": 0},
            {"reason": "routing_error", "reject_count": 2},
            {"reason": "communication_error", "reject_count": 0},
            {"reason": "other", "reject_count": 1},
            {"reason": "unknown", "reject_count": 0},
        ],
        "days": [
            {"date": "2026-06-08", "checkin_count": 6, "reject_count": 2},
            {"date": "2026-06-09", "checkin_count": 5, "reject_count": 0},
            {"date": "2026-06-10", "checkin_count": 8, "reject_count": 3},
            {"date": "2026-06-11", "checkin_count": 8, "reject_count": 3},
            {"date": "2026-06-12", "checkin_count": 0, "reject_count": 0},
        ],
    }


# =====================================================================================================================
# The invariant: every day of a range is what the single-day endpoints say
# =====================================================================================================================

@pytest.mark.parametrize("cutover", [
    None,                                   # a legacy-only site
    datetime(2026, 6, 1, 5, 0, tzinfo=UTC),     # cut over before the range
    NOON_JUNE_10,                           # inside it
    datetime(2026, 6, 10, 5, 0, tzinfo=UTC),    # exactly at a local midnight inside it
    datetime(2026, 6, 10, 14, 30, tzinfo=UTC),  # inside an hour
    datetime(2026, 6, 30, 5, 0, tzinfo=UTC),    # after the range
])
def test_every_day_of_every_report_is_exactly_what_the_single_day_endpoints_answer(api, db, cutover):
    if cutover is not None:
        db.cutover(cutover)
    # Rows in BOTH tables on every day, at several times of day, whatever the cutover makes of them.
    for day in range(7, 14):
        for hour, destination in ((0, "Main"), (9, "Westside"), (9, "Depot"), (12, "Library Express"), (23, "1")):
            db.v1(destination, day % 3 + 1, at=_local(6, day, hour, 30))
        for hour, destination in ((5, "main"), (14, "westside"), (17, "library_express"), (23, "unknown")):
            db.v2(destination, day % 2 + 1, at=_utc(6, day, hour, 30))
        db.reject_v1("Item not found", day % 2 + 1, at=_local(6, day, 9, 30))
        db.reject_v1("Multiple RFID tags", 1, at=_local(6, day, 23, 30))
        db.reject_v2("routing_error", day % 3, at=_utc(6, day, 14, 30))
        db.reject_v2("ils_acs_failure", 1, at=_utc(6, day, 23, 30))

    overview, volume = _report(api, "overview"), _report(api, "volume")
    routing, reliability = _report(api, "routing"), _report(api, "reliability")
    hours_total = [0] * 24
    reasons_total = dict.fromkeys(REJECT_REASONS, 0)

    for index, day in enumerate(_dates("2026-06-08", "2026-06-12")):
        checkins = _single_day(api, "checkins/count", day)["checkin_count"]
        rejects = _single_day(api, "rejects/count", day)["reject_count"]
        by_destination = _single_day(api, "checkins/by-destination", day)
        for hour in _single_day(api, "checkins/by-hour", day)["hours"]:
            hours_total[hour["hour"]] += hour["checkin_count"]
        for reason in _single_day(api, "rejects/by-reason", day)["reasons"]:
            reasons_total[reason["reason"]] += reason["reject_count"]

        assert overview["days"][index] == {"date": day, "checkin_count": checkins, "reject_count": rejects}
        assert volume["days"][index] == {"date": day, "checkin_count": checkins}
        assert reliability["days"][index] == {"date": day, "checkin_count": checkins, "reject_count": rejects}
        assert routing["days"][index] == {
            "date": day,
            "checkin_count": checkins,
            "home_count": by_destination["home"]["checkin_count"],
            "transit_counts": [entry["checkin_count"] for entry in by_destination["transit"]],
            "other_count": by_destination["other_count"],
        }

    assert [entry["checkin_count"] for entry in volume["hours"]] == hours_total
    assert {entry["reason"]: entry["reject_count"] for entry in reliability["reasons"]} == reasons_total
    assert overview["checkin_count"] == volume["checkin_count"] == routing["checkin_count"] == reliability["checkin_count"]
    assert overview["checkin_count"] > 0 and overview["reject_count"] == reliability["reject_count"] > 0


def test_a_range_of_one_day_is_that_days_single_day_answers(api, db):
    _seed_a_mixed_week(db)
    one_day = {"from": "2026-06-10", "to": "2026-06-10"}

    routing = _report(api, "routing", one_day)
    by_destination = _single_day(api, "checkins/by-destination", "2026-06-10")

    assert routing["range"]["days"] == 1 and len(routing["days"]) == 1
    for field in ("checkin_count", "home", "transit", "transit_count", "other_count"):
        assert routing[field] == by_destination[field]
    assert _report(api, "volume", one_day)["hours"] == _single_day(api, "checkins/by-hour", "2026-06-10")["hours"]
    assert _report(api, "reliability", one_day)["reasons"] == _single_day(api, "rejects/by-reason", "2026-06-10")["reasons"]


# =====================================================================================================================
# What always adds up
# =====================================================================================================================

def test_within_each_report_every_part_adds_up_to_its_total(api, db):
    _seed_a_mixed_week(db)

    overview, volume = _report(api, "overview"), _report(api, "volume")
    routing, reliability = _report(api, "routing"), _report(api, "reliability")

    assert overview["home_count"] + overview["transit_count"] + overview["other_count"] == overview["checkin_count"]
    assert sum(day["checkin_count"] for day in overview["days"]) == overview["checkin_count"]
    assert sum(day["reject_count"] for day in overview["days"]) == overview["reject_count"]
    assert overview["active_days"] == sum(1 for day in overview["days"] if day["checkin_count"] > 0)

    assert sum(day["checkin_count"] for day in volume["days"]) == volume["checkin_count"]
    assert sum(hour["checkin_count"] for hour in volume["hours"]) == volume["checkin_count"]

    assert routing["home"]["checkin_count"] + routing["transit_count"] + routing["other_count"] == routing["checkin_count"]
    assert routing["transit_count"] == sum(entry["checkin_count"] for entry in routing["transit"])
    for day in routing["days"]:
        assert len(day["transit_counts"]) == len(routing["transit"])
        assert day["home_count"] + sum(day["transit_counts"]) + day["other_count"] == day["checkin_count"]
    for slot, entry in enumerate(routing["transit"]):
        assert sum(day["transit_counts"][slot] for day in routing["days"]) == entry["checkin_count"]

    assert sum(reason["reject_count"] for reason in reliability["reasons"]) == reliability["reject_count"]
    assert sum(day["reject_count"] for day in reliability["days"]) == reliability["reject_count"]
    assert sum(day["checkin_count"] for day in reliability["days"]) == reliability["checkin_count"]


@pytest.mark.parametrize("report", REPORTS)
def test_every_figure_is_a_non_negative_integer_and_nothing_derived_is_returned(api, db, report):
    _seed_a_mixed_week(db)

    response = _get(api, report)
    numbers = _ints({key: value for key, value in response.json().items() if key != "range"})

    assert numbers and all(type(number) is int and number >= 0 for number in numbers)
    for derived in ("rate", "percent", "pct", "average", "avg", "busiest", "peak", "share", "mean"):
        assert derived not in response.text


def test_active_days_counts_the_dates_with_at_least_one_check_in(api, db):
    db.v1("Main", 1, at=_local(6, 8, 0, 0))             # the very first instant of the range
    db.v1("Main", 9, at=_local(6, 10, 12))
    db.v1("Westside", 1, at=_local(6, 12, 23, 59))      # the very last minute of it
    db.reject_v1("Item not found", 5, at=_local(6, 9, 12))  # rejects do not make a day active

    assert _report(api, "overview")["active_days"] == 3


# =====================================================================================================================
# A range with nothing in it
# =====================================================================================================================

def test_a_range_with_no_activity_is_a_complete_answer_made_of_zeros(api, db):
    dates = _dates("2026-06-08", "2026-06-12")

    overview, volume = _report(api, "overview"), _report(api, "volume")
    routing, reliability = _report(api, "routing"), _report(api, "reliability")

    assert overview == {
        "range": RANGE, "checkin_count": 0, "active_days": 0, "home_count": 0, "transit_count": 0, "other_count": 0,
        "reject_count": 0, "days": [{"date": day, "checkin_count": 0, "reject_count": 0} for day in dates],
    }
    assert volume["days"] == [{"date": day, "checkin_count": 0} for day in dates]
    assert volume["hours"] == [{"hour": hour, "checkin_count": 0} for hour in range(24)]
    # Every destination enabled in the site's settings is still listed, in configured order.
    assert routing["transit"] == [
        {"key": "westside", "label": "Westside", "checkin_count": 0},
        {"key": "library_express", "label": "Library Express", "checkin_count": 0},
    ]
    assert routing["home"] == {"label": "Main", "checkin_count": 0}
    assert routing["days"] == [
        {"date": day, "checkin_count": 0, "home_count": 0, "transit_counts": [0, 0], "other_count": 0} for day in dates
    ]
    assert reliability["reasons"] == [{"reason": reason, "reject_count": 0} for reason in REJECT_REASONS]
    assert reliability["days"] == [{"date": day, "checkin_count": 0, "reject_count": 0} for day in dates]


def test_rows_outside_the_range_are_not_in_it(api, db):
    db.v1("Westside", 3, at=_local(6, 7, 23, 59))       # the minute before
    db.v1("Westside", 4, at=_local(6, 13, 0, 0))        # the instant after
    db.reject_v1("Item not found", 5, at=_local(6, 7, 23, 59))
    db.reject_v1("Item not found", 6, at=_local(6, 13, 0, 0))

    assert _report(api, "overview")["checkin_count"] == 0
    assert _report(api, "reliability")["reject_count"] == 0
    wider = {"from": "2026-06-07", "to": "2026-06-13"}
    assert _report(api, "overview", wider)["checkin_count"] == 7
    assert _report(api, "reliability", wider)["reject_count"] == 11


# =====================================================================================================================
# The range
# =====================================================================================================================

def _range_of(days: int, *, ending: str = "2026-06-20") -> dict:
    end = date.fromisoformat(ending)
    return {"from": (end - timedelta(days=days - 1)).isoformat(), "to": end.isoformat()}


@pytest.mark.parametrize("report", REPORTS)
def test_ninety_two_days_is_accepted_and_ninety_three_is_refused(api, db, report):
    accepted = _get(api, report, _range_of(92))

    assert accepted.status_code == 200
    assert accepted.json()["range"]["days"] == 92 and len(accepted.json()["days"]) == 92

    db.log.clear()
    refused = _get(api, report, _range_of(93))

    assert refused.status_code == 422
    assert db.log == []


@pytest.mark.parametrize("report", REPORTS)
def test_a_range_that_ends_before_it_starts_is_a_422_and_nothing_is_read(api, db, report):
    response = _get(api, report, {"from": "2026-06-12", "to": "2026-06-08"})

    assert response.status_code == 422
    assert db.log == []


def test_today_is_allowed_and_is_flagged_and_tomorrow_is_refused(api, db):
    db.v1("Main", 2, at=_local(6, 20, 9))

    today = _report(api, "overview", {"from": "2026-06-19", "to": "2026-06-20"})

    assert today["range"] == {"from": "2026-06-19", "to": "2026-06-20", "days": 2, "timezone": "America/Chicago",
                              "includes_today": True}
    assert today["days"][-1] == {"date": "2026-06-20", "checkin_count": 2, "reject_count": 0}
    assert _report(api, "overview", {"from": "2026-06-18", "to": "2026-06-19"})["range"]["includes_today"] is False

    db.log.clear()
    tomorrow = _get(api, "overview", {"from": "2026-06-19", "to": "2026-06-21"})
    assert tomorrow.status_code == 422
    assert db.log == []


def test_today_is_the_products_date_not_utcs(api, db, clock):
    # 11:30 PM on 20 June in Chicago is already the 21st in UTC.
    clock.set(datetime(2026, 6, 21, 4, 30, tzinfo=UTC))

    assert _get(api, "overview", {"from": "2026-06-20", "to": "2026-06-20"}).json()["range"]["includes_today"] is True
    assert _get(api, "overview", {"from": "2026-06-20", "to": "2026-06-21"}).status_code == 422

    # Half an hour later it is the 21st in Chicago too.
    clock.set(datetime(2026, 6, 21, 5, 0, tzinfo=UTC))
    assert _get(api, "overview", {"from": "2026-06-20", "to": "2026-06-21"}).json()["range"]["includes_today"] is True


def test_today_follows_the_products_configured_zone(api, db, clock, monkeypatch):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Asia/Tokyo")
    clock.set(datetime(2026, 6, 20, 16, 0, tzinfo=UTC))  # 1 AM on the 21st in Tokyo

    body = _report(api, "overview", {"from": "2026-06-21", "to": "2026-06-21"})

    assert body["range"]["timezone"] == "Asia/Tokyo" and body["range"]["includes_today"] is True


@pytest.mark.parametrize("params", [
    {"from": "2026-06-08"},                                 # no `to`
    {"to": "2026-06-12"},                                   # no `from`
    {},
    {"from": "", "to": "2026-06-12"},
    {"from": "2026-6-8", "to": "2026-06-12"},
    {"from": "2026-06-08T00:00:00", "to": "2026-06-12"},
    {"from": "2026-06-08", "to": "2026-06-12T23:59:59Z"},
    {"from": "2026-06-08", "to": "2026-06-12 "},
    {"from": "yesterday", "to": "today"},
    {"from": "20260608", "to": "20260612"},
    {"from": "2026-02-30", "to": "2026-03-01"},
    {"from": "2026-06-08", "to": "2026-13-01"},
    {"date": "2026-06-10"},                                 # the single-day parameter is not a range
])
def test_anything_but_two_calendar_dates_is_a_422_and_nothing_is_read(api, db, params):
    for report in REPORTS:
        response = _get(api, report, params)

        assert response.status_code == 422, (report, params)
    assert db.log == []


def test_a_refused_range_is_the_ordinary_422_and_says_which_bound_without_echoing_it(api, db):
    refusals = {
        "report_range_order": ({"from": "2026-06-12", "to": "2026-06-08"}, "from"),
        "report_range_in_future": ({"from": "2026-06-19", "to": "2026-07-04"}, "to"),
        "report_range_too_long": (_range_of(93), "to"),
    }

    for kind, (params, field) in refusals.items():
        response = _get(api, "volume", params)

        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "validation_error"
        assert [(error["loc"], error["type"]) for error in body["detail"]] == [(["query", field], kind)]
        assert params["from"] not in response.text and params["to"] not in response.text

    malformed = _get(api, "volume", {"from": "June 8", "to": "2026-06-12"}).json()
    assert malformed["code"] == "validation_error" and malformed["detail"][0]["loc"] == ["query", "from"]


def test_a_request_is_authenticated_and_resolved_before_its_range_is_judged(api, db, monkeypatch):
    bad_range = {"from": "2026-06-12", "to": "2026-06-08"}

    # Not found comes before the range, exactly as it comes before a bad `date` on the single-day reads.
    missing = _get(api, "overview", bad_range, org="beta", branch="north")
    assert (missing.status_code, missing.json()) == (404, TENANT_NOT_FOUND)

    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: None)
    signed_out = _get(api, "overview", bad_range)
    assert (signed_out.status_code, signed_out.json()) == (401, NOT_AUTHENTICATED)
    assert db.log == []


def test_a_leap_day_and_a_range_across_a_year_end_are_ordinary_ranges(api, db, clock):
    clock.set(datetime(2028, 3, 5, 18, 0, tzinfo=UTC))

    leap = _report(api, "volume", {"from": "2028-02-27", "to": "2028-03-02"})
    assert [day["date"] for day in leap["days"]] == ["2028-02-27", "2028-02-28", "2028-02-29", "2028-03-01", "2028-03-02"]

    year_end = _report(api, "volume", {"from": "2027-12-30", "to": "2028-01-02"})
    assert [day["date"] for day in year_end["days"]] == ["2027-12-30", "2027-12-31", "2028-01-01", "2028-01-02"]


# =====================================================================================================================
# Local days and wall-clock hours, across the days the clocks change
# =====================================================================================================================

def test_days_are_days_in_the_products_configured_zone(api, db, monkeypatch):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Asia/Tokyo")
    db.cutover(_utc(6, 1, 0))
    db.v2("main", 1, at=_utc(6, 8, 14, 59))      # 23:59 on the 8th in Tokyo
    db.v2("main", 2, at=_utc(6, 8, 15, 0))       # 00:00 on the 9th
    db.v2("main", 4, at=_utc(6, 9, 14, 59))      # 23:59 on the 9th
    db.v2("main", 8, at=_utc(6, 9, 15, 0))       # 00:00 on the 10th

    body = _report(api, "volume", {"from": "2026-06-08", "to": "2026-06-10"})

    assert body["range"]["timezone"] == "Asia/Tokyo"
    assert [day["checkin_count"] for day in body["days"]] == [1, 6, 8]
    assert {entry["hour"]: entry["checkin_count"] for entry in body["hours"] if entry["checkin_count"]} == {0: 10, 23: 5}


def test_the_day_the_clocks_go_forward_is_one_day_with_an_empty_two_oclock_hour(api, db, clock):
    """8 March 2026 in America/Chicago: 02:00 CST becomes 03:00 CDT. The day is 23 hours long."""
    clock.set(_utc(3, 20, 18))
    db.cutover(_utc(3, 1, 6))
    db.v2("main", 1, at=_utc(3, 8, 5, 59))       # 23:59 CST on the 7th
    db.v2("main", 2, at=_utc(3, 8, 6, 0))        # 00:00 CST on the 8th
    db.v2("main", 4, at=_utc(3, 8, 7, 59))       # 01:59 CST: the last minute before the jump
    db.v2("main", 8, at=_utc(3, 8, 8, 0))        # 03:00 CDT: the first minute after it
    db.v2("main", 16, at=_utc(3, 9, 4, 59))      # 23:59 CDT on the 8th
    db.v2("main", 32, at=_utc(3, 9, 5, 0))       # 00:00 CDT on the 9th

    body = _report(api, "volume", {"from": "2026-03-07", "to": "2026-03-09"})

    assert [day["checkin_count"] for day in body["days"]] == [1, 30, 32]
    hours = [entry["checkin_count"] for entry in body["hours"]]
    assert len(hours) == 24
    assert (hours[0], hours[1], hours[2], hours[3], hours[23]) == (34, 4, 0, 8, 17)
    assert sum(hours) == body["checkin_count"] == 63


def test_the_day_the_clocks_go_back_is_one_day_and_its_repeated_hour_holds_both_passes(api, db, clock):
    """1 November 2026 in America/Chicago: 02:00 CDT becomes 01:00 CST. The day is 25 hours long."""
    clock.set(_utc(11, 20, 18))
    db.cutover(_utc(10, 1, 5))
    db.v2("main", 1, at=_utc(11, 1, 4, 59))      # 23:59 CDT on 31 October
    db.v2("main", 2, at=_utc(11, 1, 5, 0))       # 00:00 CDT on the 1st
    db.v2("main", 4, at=_utc(11, 1, 6, 30))      # 01:30 CDT: the first pass
    db.v2("main", 8, at=_utc(11, 1, 7, 30))      # 01:30 CST: the second pass
    db.v2("main", 16, at=_utc(11, 1, 8, 0))      # 02:00 CST
    db.v2("main", 32, at=_utc(11, 2, 5, 59))     # 23:59 CST on the 1st
    db.v2("main", 64, at=_utc(11, 2, 6, 0))      # 00:00 CST on the 2nd

    body = _report(api, "volume", {"from": "2026-10-31", "to": "2026-11-02"})

    assert [day["checkin_count"] for day in body["days"]] == [1, 62, 64]
    hours = [entry["checkin_count"] for entry in body["hours"]]
    assert len(hours) == 24
    assert (hours[0], hours[1], hours[2], hours[23]) == (66, 12, 16, 33)
    assert sum(hours) == body["checkin_count"] == 127


def test_a_legacy_row_is_bucketed_by_the_wall_clock_reading_it_carries_on_those_days_too(api, db, clock):
    clock.set(_utc(11, 20, 18))
    db.v1("Main", 3, at=_local(3, 8, 2, 30))     # a reading that should not exist: it still names hour 2
    db.v1("Main", 5, at=_local(11, 1, 1, 30))    # a reading that happened twice: one hour, either way

    spring = _report(api, "volume", {"from": "2026-03-08", "to": "2026-03-08"})
    fall = _report(api, "volume", {"from": "2026-11-01", "to": "2026-11-01"})

    assert spring["hours"][2] == {"hour": 2, "checkin_count": 3} and spring["checkin_count"] == 3
    assert fall["hours"][1] == {"hour": 1, "checkin_count": 5} and fall["checkin_count"] == 5


def test_the_other_reports_count_the_same_local_days_across_a_clock_change(api, db, clock):
    clock.set(_utc(11, 20, 18))
    db.cutover(_utc(10, 1, 5))
    db.v2("westside", 2, at=_utc(11, 1, 5, 0))       # 00:00 CDT on the 1st
    db.v2("westside", 3, at=_utc(11, 2, 5, 59))      # 23:59 CST on the 1st: the 25th hour of that day
    db.v2("westside", 4, at=_utc(11, 2, 6, 0))       # 00:00 CST on the 2nd
    db.reject_v2("item_not_found", 1, at=_utc(11, 2, 5, 59))
    db.reject_v2("item_not_found", 2, at=_utc(11, 2, 6, 0))
    november = {"from": "2026-11-01", "to": "2026-11-02"}

    assert [day["transit_counts"] for day in _report(api, "routing", november)["days"]] == [[5, 0], [4, 0]]
    assert [day["reject_count"] for day in _report(api, "reliability", november)["days"]] == [1, 2]
    assert [day["checkin_count"] for day in _report(api, "overview", november)["days"]] == [5, 4]


def test_a_week_boundary_inside_a_long_range_loses_and_repeats_nothing(api, db):
    """Hours are counted a week of days at a time. One check-in in the last hour of every day, and one in the
    first, over 23 days: every day has exactly two, whichever statement its hours were in."""
    for day in range(1, 24):
        db.v1("Main", 1, at=_local(5, day, 0, 0))
        db.v1("Main", 1, at=_local(5, day, 23, 59))

    body = _report(api, "volume", {"from": "2026-05-01", "to": "2026-05-23"})

    assert [day["checkin_count"] for day in body["days"]] == [2] * 23
    assert {entry["hour"]: entry["checkin_count"] for entry in body["hours"] if entry["checkin_count"]} == {0: 23, 23: 23}
    assert db.log.count("checkins") == 4            # 23 days: four statements of at most seven days each


# =====================================================================================================================
# The two eras
# =====================================================================================================================

def test_a_legacy_only_site_never_reads_the_current_tables(api, db):
    db.v1("Westside", 2, at=_local(6, 9))
    db.v2("westside", 90, at=_utc(6, 9, 15))            # no cutover: not part of the site's history
    db.reject_v1("Item not found", 1, at=_local(6, 9))
    db.reject_v2("item_not_found", 90, at=_utc(6, 9, 15))

    for report in REPORTS:
        _report(api, report)

    assert _report(api, "overview")["checkin_count"] == 2
    assert _report(api, "reliability")["reject_count"] == 1
    assert "checkin_events" not in db.log and "reject_events" not in db.log


def test_a_site_cut_over_before_the_range_never_reads_the_legacy_tables(api, db):
    db.cutover(_utc(6, 1, 5))
    db.v1("Westside", 90, at=_local(6, 9))              # after the cutover: not part of the history
    db.v2("westside", 2, at=_utc(6, 9, 15))
    db.reject_v1("Item not found", 90, at=_local(6, 9))
    db.reject_v2("item_not_found", 1, at=_utc(6, 9, 15))

    for report in REPORTS:
        _report(api, report)

    assert _report(api, "overview")["checkin_count"] == 2
    assert _report(api, "reliability")["reject_count"] == 1
    assert "checkins" not in db.log and "rejects" not in db.log


def test_a_site_cut_over_after_the_range_is_legacy_for_that_range(api, db):
    db.cutover(_utc(6, 13, 5))                          # midnight after the last day of the range
    db.v1("Westside", 2, at=_local(6, 12, 23, 59))
    db.v2("westside", 90, at=_utc(6, 12, 15))

    assert _report(api, "overview")["checkin_count"] == 2
    assert "checkin_events" not in db.log


def test_an_event_exactly_at_the_cutover_belongs_to_the_current_era(api, db):
    db.cutover(NOON_JUNE_10)
    db.v1("Main", 1, at=_local(6, 10, 11, 59))          # the minute before, on the legacy clock: counts
    db.v1("Main", 10, at=_local(6, 10, 12, 0))          # the cutover's own reading: v2 owns it
    db.v2("main", 100, at=NOON_JUNE_10 - timedelta(microseconds=1))     # just before: not counted
    db.v2("main", 1000, at=NOON_JUNE_10)                # the instant itself: counts

    body = _report(api, "volume", {"from": "2026-06-10", "to": "2026-06-10"})

    assert body["checkin_count"] == 1001
    assert {entry["hour"]: entry["checkin_count"] for entry in body["hours"] if entry["checkin_count"]} == {11: 1, 12: 1000}


def test_a_rollback_returns_the_site_to_its_legacy_rows(api, db):
    db.cutover(NOON_JUNE_10, set_at="2026-06-01 00:00:00+00:00")
    db.cutover(None, set_at="2026-06-02 00:00:00+00:00")
    db.v1("Main", 3, at=_local(6, 11, 15))
    db.v2("main", 90, at=_utc(6, 11, 20))

    assert _report(api, "overview")["checkin_count"] == 3


def test_a_legacy_time_is_the_local_wall_clock_and_is_never_shifted_to_utc(api, db):
    """The dashboard's mixed-era path labels a legacy time UTC once a site has a cutover, moving it five hours.
    These reports do not: 20:30 local on the 9th is in the 8 PM hour of the 9th."""
    db.cutover(_utc(6, 12, 17))
    db.v1("Westside", 2, at=_local(6, 9, 20, 30))
    db.v1("Westside", 1, at=_local(6, 9, 0, 30))
    db.reject_v1("Item not found", 4, at=_local(6, 9, 20, 30))

    volume = _report(api, "volume", {"from": "2026-06-08", "to": "2026-06-10"})

    assert [day["checkin_count"] for day in volume["days"]] == [0, 3, 0]
    assert {entry["hour"]: entry["checkin_count"] for entry in volume["hours"] if entry["checkin_count"]} == {0: 1, 20: 2}
    assert [day["reject_count"] for day in _report(api, "reliability", {"from": "2026-06-08", "to": "2026-06-10"})["days"]] == [0, 4, 0]


# =====================================================================================================================
# Routing
# =====================================================================================================================

def test_destinations_keep_their_configured_order_and_every_days_counts_line_up_with_it(api, db):
    db.settings(organization=1, document={"transit": {"home_branch_label": "Main", "destinations": [
        {"key": "k1", "label": "Zebra Road"}, {"key": "k2", "label": "Alpha Street"},
        {"key": "k3", "label": "Old Annex", "enabled": False}, {"key": "k4", "label": "Midway"},
    ]}})
    db.v1("Midway", 9, at=_local(6, 8))
    db.v1("Alpha Street", 1, at=_local(6, 9))
    db.v1("ZEBRA ROAD", 2, at=_local(6, 9))
    db.v1("Old Annex", 5, at=_local(6, 9))              # configured, but disabled: other

    body = _report(api, "routing", {"from": "2026-06-08", "to": "2026-06-09"})

    assert [(entry["key"], entry["label"], entry["checkin_count"]) for entry in body["transit"]] == [
        ("zebra_road", "Zebra Road", 2), ("alpha_street", "Alpha Street", 1), ("midway", "Midway", 9),
    ]
    assert [day["transit_counts"] for day in body["days"]] == [[0, 0, 9], [2, 1, 0]]
    assert [day["other_count"] for day in body["days"]] == [0, 5]


def test_one_destination_is_one_destination_in_either_era_and_anything_else_is_other(api, db):
    db.cutover(NOON_JUNE_10)
    for stored in ("Library Express", "LIBRARY EXPRESS", "  library express "):
        db.v1(stored, 1, at=_local(6, 9))
    for stored in ("1", "LOCAL", "Main"):
        db.v1(stored, 1, at=_local(6, 9, 11))
    for stored in ("No Agency Destination", "Northgate Annex", "", None, "???", f"Secret Depot {CANARY}"):
        db.v1(stored, 1, at=_local(6, 9, 12))
    db.v2("library_express", 4, at=_utc(6, 11, 15))
    db.v2("no_agency_destination", 2, at=_utc(6, 11, 15))

    response = _get(api, "routing")
    body = response.json()

    assert (body["home"]["checkin_count"], body["transit"][1]["checkin_count"], body["other_count"]) == (3, 7, 8)
    for leaked in ("Secret", "Depot", "CANARY", "Northgate", "northgate", "No Agency", "no_agency", "branch_1"):
        assert leaked not in response.text


def test_a_site_with_no_routing_settings_has_no_destinations_and_still_accounts_for_every_check_in(api, db):
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM organization_settings"))
        conn.execute(text("DELETE FROM branch_settings"))
    db.v1("Main", 2, at=_local(6, 9))
    db.v1("Westside", 3, at=_local(6, 9))

    body = _report(api, "routing")

    assert body["home"] == {"label": "Main Library", "checkin_count": 2}
    assert body["transit"] == [] and body["transit_count"] == 0 and body["other_count"] == 3
    assert all(day["transit_counts"] == [] for day in body["days"])
    assert _report(api, "overview")["checkin_count"] == 5


@pytest.mark.parametrize("document", ["{}", "null", "[]", '{"transit": null}', '{"transit": {"destinations": 5}}'])
def test_malformed_routing_settings_are_a_site_with_no_destinations(api, db, document):
    db.settings(organization=1, document=document)
    db.v1("Westside", 2, at=_local(6, 9))

    body = _report(api, "routing")

    assert body["transit"] == [] and body["other_count"] == 2 and body["checkin_count"] == 2


def test_a_sites_own_settings_decide_its_routing(api, db):
    db.v1("East", 2, at=_local(6, 9), branch=EAST)
    db.v1("Main Library", 4, at=_local(6, 9), branch=EAST)
    db.v1("Westside", 5, at=_local(6, 9), branch=EAST)      # the organization's destination, not this site's

    body = _report(api, "routing", branch="east")

    assert body["home"] == {"label": "East", "checkin_count": 2}
    # "Main Library" is a destination of the east site. That it is also where another sorter is makes no difference.
    assert body["transit"] == [{"key": "main_library", "label": "Main Library", "checkin_count": 4}]
    assert body["other_count"] == 5


# =====================================================================================================================
# Reject reasons
# =====================================================================================================================

def test_every_reason_code_is_reached_from_both_eras_by_the_customer_apis_own_rules(api, db):
    db.cutover(NOON_JUNE_10)
    legacy = {
        "Item not found in database": "item_not_found", "No item found for tag": "item_not_found",
        "ACS timeout": "ils_acs_failure", "Multiple RFID tags detected": "rfid_collision",
        "Collection code mismatch": "configuration_error", "Library not found": "routing_error",
        "Belt jam at induction": "other", "": "unknown", None: "unknown",
    }
    for message in legacy:
        db.reject_v1(message, 1, at=_local(6, 9))
    for error_class in REJECT_REASONS:
        db.reject_v2(error_class, 2, at=_utc(6, 11, 15))

    reasons = {entry["reason"]: entry["reject_count"] for entry in _report(api, "reliability")["reasons"]}

    expected = dict.fromkeys(REJECT_REASONS, 2)
    for reason in legacy.values():
        expected[reason] += 1
    assert reasons == expected
    assert list(reasons) == list(REJECT_REASONS)
    assert sum(reasons.values()) == 9 + 16


def test_a_stored_class_that_is_not_a_reason_code_is_other_and_is_only_logged_as_a_number(api, db, caplog):
    db.cutover(_utc(6, 1, 5))
    db.reject_v2("something_new", 3, at=_utc(6, 9, 15))
    db.reject_v2("rfid_collision", 1, at=_utc(6, 9, 15))

    with caplog.at_level("WARNING", logger="sortview.operational_reports"):
        response = _get(api, "reliability")

    reasons = {entry["reason"]: entry["reject_count"] for entry in response.json()["reasons"]}
    assert (reasons["other"], reasons["rfid_collision"], response.json()["reject_count"]) == (3, 1, 4)
    assert "something_new" not in response.text
    assert [record.getMessage() for record in caplog.records] == [
        "Reject rows with an unrecognised stored class were counted as other: rows=3"
    ]


def test_the_streamlit_classifier_is_not_what_decides_a_reason(api, db):
    """The dashboard's simplify_error files a stored class such as `item_not_found` under "Other". These reports
    use the customer API's rules, under which it is the reason it names."""
    db.cutover(_utc(6, 1, 5))
    db.reject_v2("item_not_found", 5, at=_utc(6, 9, 15))

    reasons = {entry["reason"]: entry["reject_count"] for entry in _report(api, "reliability")["reasons"]}

    assert (reasons["item_not_found"], reasons["other"]) == (5, 0)
    source = inspect.getsource(operational_report_service)
    assert "reject_logic" not in source and "simplify_error" not in source


def test_no_stored_message_item_or_identifier_is_in_any_report(api, db):
    _seed_a_mixed_week(db)
    db.reject_v1(f"Item not found: {CANARY}", 1, at=_local(6, 9))

    for report in REPORTS:
        text_of = _get(api, report).text

        for leaked in ("CANARY", "Smith", "31234000123456", "barcode", "item_key", "event_key", "key_id", "error_message",
                       "error_class", "customer_id", "branch_id", str(CUSTOMER), "Item not found", "ACS timeout",
                       "cutover", "v1", "v2"):
            assert leaked not in text_of, (report, leaked)


# =====================================================================================================================
# Authentication and tenant scope
# =====================================================================================================================

@pytest.mark.parametrize("report", REPORTS)
def test_no_session_is_a_401_and_nothing_is_read(api, db, monkeypatch, report):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: None)

    for headers in ({}, COOKIE):
        response = _get(api, report, headers=headers)

        assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert db.log == []


@pytest.mark.parametrize(("org", "branch"), [
    ("no-such-org", "main"), ("beta", "north"), ("closed", "main"), ("acme", "no-such-branch"), ("acme", "north"),
    ("acme", "shut"), ("acme", "unmapped"), ("unmapped-org", "main"), ("ACME", "main"),
])
def test_anything_that_does_not_resolve_is_the_same_404_as_the_single_day_reads_give(api, db, org, branch):
    plain = api.get(BASE.format(org=org, branch=branch) + "/checkins/count", headers=COOKIE, params={"date": "2026-06-10"})

    for report in REPORTS:
        response = _get(api, report, org=org, branch=branch)

        assert (response.status_code, response.json()) == (404, TENANT_NOT_FOUND)
        assert (response.status_code, response.content) == (plain.status_code, plain.content)
        assert response.headers["cache-control"] == plain.headers["cache-control"]
    assert db.log == []


def test_a_suspended_organization_can_still_be_read(api, db):
    db.v1("Main", 2, at=_local(6, 9), customer=8505, branch=51)
    db.reject_v1("Item not found", 1, at=_local(6, 9), customer=8505, branch=51)

    assert _report(api, "overview", org="paused", branch="main")["checkin_count"] == 2
    assert _report(api, "reliability", org="paused", branch="main")["reject_count"] == 1


@pytest.mark.parametrize("report", REPORTS)
def test_every_read_is_bound_to_the_resolved_tenant_and_runs_under_its_context(api, db, report):
    db.cutover(NOON_JUNE_10)

    _report(api, report)

    assert db.log[0] == "open" and db.log[-1] == "close" and db.log.count("open") == 1
    assert db.queries
    for _table, parameters, context in db.queries:
        assert (parameters["customer_id"], parameters["branch_id"]) == (CUSTOMER, BRANCH)
        assert context == {"customer_id": str(CUSTOMER), "branch_id": str(BRANCH)}


def test_another_organizations_rows_and_another_sites_rows_are_never_counted(api, db):
    db.v1("Westside", 1, at=_local(6, 9))
    db.reject_v1("Item not found", 1, at=_local(6, 9))
    for customer, branch in ((OTHER_CUSTOMER, OTHER_BRANCH), (OTHER_CUSTOMER, BRANCH), (CUSTOMER, EAST)):
        db.v1("Westside", 70, at=_local(6, 9), customer=customer, branch=branch)
        db.reject_v1("Item not found", 80, at=_local(6, 9), customer=customer, branch=branch)
        db.cutover(_utc(6, 1, 5), customer=customer, branch=branch)     # their cutovers are not this site's
        db.v2("westside", 60, at=_utc(6, 9, 15), customer=customer, branch=branch)
        db.reject_v2("item_not_found", 50, at=_utc(6, 9, 15), customer=customer, branch=branch)

    assert _report(api, "overview")["checkin_count"] == 1
    assert _report(api, "volume")["checkin_count"] == 1
    assert _report(api, "routing")["transit_count"] == 1
    assert _report(api, "reliability")["reject_count"] == 1
    # The east site reads its own rows, and only those.
    assert _report(api, "overview", branch="east")["checkin_count"] == 60
    assert _report(api, "reliability", branch="east")["reject_count"] == 50


def test_a_tenant_named_in_the_query_string_is_ignored(api, db):
    db.v1("Westside", 1, at=_local(6, 9))
    db.v1("Westside", 9, at=_local(6, 9), customer=OTHER_CUSTOMER, branch=OTHER_BRANCH)

    body = _report(api, "overview", {**JUNE_8_TO_12, "customer_id": OTHER_CUSTOMER, "branch_id": OTHER_BRANCH,
                                     "org_slug": "beta", "branch_slug": "north", "date": "2026-06-09"})

    assert body["checkin_count"] == 1


def test_a_tenant_context_that_does_not_match_is_a_500_and_nothing_is_read(api, db):
    db.read_back = lambda settings: {**settings, "branch_id": str(OTHER_BRANCH)}

    for report in REPORTS:
        response = TestClient(main.app, raise_server_exceptions=False).get(_path(report), headers=COOKIE, params=JUNE_8_TO_12)

        assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    assert set(db.log) == {"open", "close"}


@pytest.mark.parametrize(("report", "table"), [
    ("overview", "organizations"), ("overview", "v2_cutovers"), ("overview", "checkins"), ("overview", "checkin_events"),
    ("overview", "rejects"), ("overview", "reject_events"), ("volume", "v2_cutovers"), ("volume", "checkins"),
    ("volume", "checkin_events"), ("routing", "organizations"), ("routing", "checkins"), ("routing", "checkin_events"),
    ("reliability", "checkins"), ("reliability", "rejects"), ("reliability", "reject_events"),
])
def test_a_failed_read_is_a_500_that_says_nothing_and_is_never_a_partial_report(api, db, report, table):
    _seed_a_mixed_week(db)
    db.fail_on = table

    response = TestClient(main.app, raise_server_exceptions=False).get(_path(report), headers=COOKIE, params=JUNE_8_TO_12)

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    for leaked in ("synthetic", "CANARY", str(CUSTOMER), "checkin", "reject", "Smith"):
        assert leaked not in response.text
    assert db.log[-1] == "close"


# =====================================================================================================================
# The routes, and what they leave alone
# =====================================================================================================================

@pytest.mark.parametrize("report", REPORTS)
def test_each_route_takes_the_two_path_slugs_and_the_two_dates_and_nothing_else(report):
    route = next(r for r in main.app.routes if r.path.endswith(f"/reports/{report}"))
    flat = get_flat_dependant(route.dependant)

    assert route.path == f"/api/organizations/{{org_slug}}/branches/{{branch_slug}}/reports/{report}"
    assert route.methods == {"GET"}
    # A slug can be read by more than one dependency (R9C: the range's history window reads the organization's
    # plan): still the same two path parameters, and nothing else.
    assert sorted({p.name for p in flat.path_params}) == ["branch_slug", "org_slug"]
    assert sorted((p.alias, p.field_info.is_required()) for p in flat.query_params) == [("from", True), ("to", True)]
    assert flat.body_params == [] and flat.header_params == []
    # The tenant is resolved first, then (for Routing only) the plan's transit feature, the range last: 401, then
    # 404, then 403, then 422.
    expected = ["tenant", "_transits", "requested"] if report == "routing" else ["tenant", "requested"]
    assert list(inspect.signature(route.endpoint).parameters) == expected


def test_the_routes_are_read_only(api, db):
    for report in REPORTS:
        for method in ("post", "put", "patch", "delete"):
            assert getattr(api, method)(_path(report), headers=COOKIE).status_code == 405


def test_the_handlers_count_nothing_themselves():
    source = inspect.getsource(report_routes)

    for forbidden in ("text(", "SELECT", "execute(", "get_engine", "destination_key", "classify_legacy", "+=", "sum("):
        assert forbidden not in source, forbidden
    for module in (report_routes, report_schemas, operational_report_service):
        imports = "\n".join(line for line in inspect.getsource(module).splitlines() if line.startswith(("import ", "from ")))
        for unwanted in ("streamlit", "pandas", "data_loader", "mixed_era_service", "transit_logic", "reject_logic", "metrics "):
            assert unwanted not in imports, (module.__name__, unwanted)


def test_the_response_models_have_exactly_the_approved_fields_and_refuse_any_other():
    fields = {name: list(model.model_fields) for name, model in vars(report_schemas).items()
              if isinstance(model, type) and issubclass(model, report_schemas._ResponseModel) and name[0] != "_"}

    assert fields == {
        "ReportRange": ["from_date", "to_date", "days", "timezone", "includes_today"],
        "OverviewDay": ["date", "checkin_count", "reject_count"],
        "OverviewReportResponse": ["range", "checkin_count", "active_days", "home_count", "transit_count", "other_count",
                                   "reject_count", "days"],
        "VolumeDay": ["date", "checkin_count"],
        "VolumeHour": ["hour", "checkin_count"],
        "VolumeReportResponse": ["range", "checkin_count", "days", "hours"],
        "RoutingDay": ["date", "checkin_count", "home_count", "transit_counts", "other_count"],
        "RoutingReportResponse": ["range", "checkin_count", "home", "transit", "transit_count", "other_count", "days"],
        # Reports R7A: Bin Volume (tests/test_customer_api_bin_volume_report.py).
        "BinVolumeBin": ["key", "checkin_count", "hours"],
        "BinVolumeReportResponse": ["range", "checkin_count", "known_bin_count", "unknown_bin_count", "bins"],
        "ReliabilityReason": ["reason", "reject_count"],
        "ReliabilityDay": ["date", "checkin_count", "reject_count"],
        "ReliabilityReportResponse": ["range", "checkin_count", "reject_count", "reasons", "days"],
    }
    for name in fields:
        assert getattr(report_schemas, name).model_config.get("extra") == "forbid"


def test_the_single_day_endpoints_answer_exactly_as_before(api, db):
    _seed_a_mixed_week(db)

    assert _single_day(api, "checkins/count", "2026-06-10") == {
        "date": "2026-06-10", "timezone": "America/Chicago", "checkin_count": 8,
    }
    assert _single_day(api, "rejects/count", "2026-06-10") == {
        "date": "2026-06-10", "timezone": "America/Chicago", "reject_count": 3,
    }
    assert list(_single_day(api, "checkins/by-destination", "2026-06-10")) == [
        "date", "timezone", "checkin_count", "home", "transit", "transit_count", "other_count",
    ]
    # A single-day read still takes `date`, and not a range.
    refused = api.get(BASE.format(org="acme", branch="main") + "/checkins/count", headers=COOKIE, params=JUNE_8_TO_12)
    assert refused.status_code == 422


# =====================================================================================================================
# R9C: how far back a range may start (the plan's history_days), and transit routing (the plan's transits)
# =====================================================================================================================

BEFORE_HISTORY = {"code": "range_before_history", "message": "The selected range starts before this organization's available reporting window."}
NOT_AVAILABLE = {"code": "feature_not_available", "message": "This feature is not available for this organization."}


def _from(first: str, last: str = "2026-06-20") -> dict:
    return {"from": first, "to": last}


def _refused_kind(response) -> str:
    assert response.status_code == 422, response.text
    body = response.json()
    return body["code"] if body["code"] != "validation_error" else body["detail"][0]["type"]


@pytest.mark.parametrize("report", REPORTS)
def test_a_thirty_day_window_starts_twenty_nine_days_before_today_and_not_a_day_earlier(api, db, monkeypatch, report):
    grant(monkeypatch, history_days=feature(True, 30))

    assert _get(api, report, _from("2026-05-22")).status_code == 200            # today, 20 June, and the 29 days before
    refused = _get(api, report, _from("2026-05-21"))
    assert (refused.status_code, refused.json()) == (422, BEFORE_HISTORY)
    assert refused.headers["cache-control"] == "no-store"
    assert "2026-05-21" not in refused.text


def test_a_longer_window_takes_an_old_start_but_one_request_is_still_at_most_92_days(api, db, monkeypatch):
    grant(monkeypatch, history_days=feature(True, 90))
    assert _get(api, "volume", _from("2026-03-23", "2026-04-30")).status_code == 200     # 89 days before today
    assert _refused_kind(_get(api, "volume", _from("2026-03-22", "2026-04-30"))) == "range_before_history"

    grant(monkeypatch, history_days=feature(True, 3650))
    assert _get(api, "volume", _from("2020-01-01", "2020-03-31")).status_code == 200     # 91 days, years back
    assert _refused_kind(_get(api, "volume", _from("2026-01-01"))) == "report_range_too_long"


def test_no_limit_means_no_earliest_date_and_the_92_day_cap_still_holds(api, db, monkeypatch):
    grant(monkeypatch, history_days=feature(True, None))

    old = _report(api, "overview", _from("2001-01-01", "2001-01-31"))
    assert old["checkin_count"] == 0 and old["range"]["days"] == 31
    assert _refused_kind(_get(api, "overview", _from("2001-01-01", "2001-06-30"))) == "report_range_too_long"


@pytest.mark.parametrize("history", [
    None,                        # no history feature at all
    feature(False, 3650),        # switched off
    feature(False, None),
    feature(True, 0),            # not a usable number of days
    feature(True, -5),
    feature(True, "3650"),
    feature(True, 36.5),
    feature(True, True),
])
def test_a_missing_switched_off_or_unusable_history_feature_is_thirty_days_never_more(api, db, monkeypatch, history):
    grant(monkeypatch, history_days=history)

    assert _get(api, "reliability", _from("2026-05-22")).status_code == 200
    assert _refused_kind(_get(api, "reliability", _from("2026-05-21"))) == "range_before_history"


def test_the_other_range_rules_are_unchanged_and_come_first(api, db, monkeypatch):
    grant(monkeypatch, history_days=feature(True, 30))

    assert _refused_kind(_get(api, "volume", _from("2026-06-12", "2026-06-08"))) == "report_range_order"
    assert _refused_kind(_get(api, "volume", _from("2026-06-19", "2026-06-21"))) == "report_range_in_future"
    assert _refused_kind(_get(api, "volume", _from("2026-01-01"))) == "report_range_too_long"
    assert _get(api, "volume", {"from": "June 8", "to": "2026-06-12"}).json()["code"] == "validation_error"


def test_who_and_which_organization_are_settled_before_the_history_window(api, db, monkeypatch):
    asked = grant(monkeypatch, history_days=feature(True, 30))
    old = _from("2020-01-01", "2020-01-31")

    assert _get(api, "volume", old, headers={}).status_code == 401
    assert _get(api, "volume", old, org="beta", branch="north").json() == TENANT_NOT_FOUND     # not a member
    assert _get(api, "volume", old, org="closed").json() == TENANT_NOT_FOUND                   # cancelled
    assert asked == []
    # The plan read is the organization's in the path, for the signed-in user -- once for the request.
    assert _refused_kind(_get(api, "volume", old)) == "range_before_history"
    assert asked == [(USER["id"], "acme")]


def test_a_suspended_organization_reads_its_reports_within_its_window(api, db, monkeypatch):
    grant(monkeypatch, history_days=feature(True, 30))

    assert _get(api, "volume", _from("2026-05-22"), org="paused").status_code == 200
    assert _refused_kind(_get(api, "volume", _from("2026-05-21"), org="paused")) == "range_before_history"


def _by_destination(api, params=None, *, org="acme", branch="main", headers=COOKIE):
    return api.get(
        BASE.format(org=org, branch=branch) + "/checkins/by-destination",
        headers=headers,
        params={"date": "2026-06-10"} if params is None else params,
    )


@pytest.mark.parametrize("transits", [None, feature(False), feature(False, 5)])
def test_without_transit_routing_the_routing_report_and_the_days_routing_are_403(api, db, monkeypatch, transits):
    grant(monkeypatch, transits=transits)

    for response in (_get(api, "routing"), _by_destination(api)):
        assert (response.status_code, response.json()) == (403, NOT_AVAILABLE)
        assert response.headers["cache-control"] == "no-store"
    # Nothing else is about transit routing, and nothing else is refused.
    for report in ("overview", "volume", "reliability"):
        assert _get(api, report).status_code == 200, report
    count = api.get(BASE.format(org="acme", branch="main") + "/checkins/count", headers=COOKIE, params={"date": "2026-06-10"})
    assert count.status_code == 200


def test_with_transit_routing_both_answer_as_before(api, db, monkeypatch):
    grant(monkeypatch, transits=feature(True))
    db.v1("Westside", 2, at=_local(6, 10))

    assert _get(api, "routing").status_code == 200
    assert _by_destination(api).json()["checkin_count"] == 2
    assert _by_destination(api, org="paused").status_code == 200     # suspended: still readable


def test_the_transit_gate_comes_after_who_and_where_and_before_the_dates(api, db, monkeypatch):
    grant(monkeypatch, transits=None)
    bad_range, bad_date = _from("2026-06-12", "2026-06-08"), {"date": "June 10"}

    assert _get(api, "routing", headers={}).json() == NOT_AUTHENTICATED
    assert _by_destination(api, headers={}).json() == NOT_AUTHENTICATED
    assert _get(api, "routing", org="beta", branch="north").json() == TENANT_NOT_FOUND
    assert _by_destination(api, org="closed").json() == TENANT_NOT_FOUND
    assert _get(api, "routing", bad_range).json() == NOT_AVAILABLE
    assert _by_destination(api, bad_date).json() == NOT_AVAILABLE

    grant(monkeypatch, transits=feature(True))
    assert _refused_kind(_get(api, "routing", bad_range)) == "report_range_order"
    assert _by_destination(api, bad_date).status_code == 422


def test_every_range_route_is_held_to_the_history_window_by_the_one_shared_range():
    """Every customer route that takes a `from`/`to` range takes it through report_routes.require_report_range --
    the one place the history window is applied -- so none can be added that skips it."""

    def calls(dependant):
        yield dependant.call
        for sub in dependant.dependencies:
            yield from calls(sub)

    ranged = [
        route for route in main.app.routes
        if getattr(route, "path", "").startswith("/api/")
        and {p.alias for p in get_flat_dependant(route.dependant).query_params} >= {"from", "to"}
    ]
    assert sorted(route.path for route in ranged) == sorted([
        "/api/organizations/{org_slug}/branches/{branch_slug}/reports/" + report
        for report in ("overview", "volume", "routing", "reliability", "bins", "efficiency", "holds")
    ] + [
        "/api/organizations/{org_slug}/reports/" + report for report in ("overview", "routing-network", "reliability")
    ])
    for route in ranged:
        assert report_routes.require_report_range in set(calls(route.dependant)), route.path
