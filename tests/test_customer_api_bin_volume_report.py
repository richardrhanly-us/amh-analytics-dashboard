"""Reports R7A: a sorter site's Bin Volume report.

    GET /api/organizations/{org_slug}/branches/{branch_slug}/reports/bins?from=&to=

These tests drive the real production route through TestClient(main.app), as
tests/test_customer_api_reports.py drives the other four sorter-site reports
and on the same in-memory SQLite database -- with the legacy check-in table
given the `bin` column it has in production. The session dependency, the
tenant-scope dependency, the range dependency, the scoped and verified
connection, the report service and the response schema all run for real, and
so does every SQL statement.

WHAT THE REPORT IS: check-ins by the physical sort bin each was logged in,
and for each bin by wall-clock hour. Only bins that were observed are
listed: nothing knows which bins a sorter has.

THE INVARIANTS THIS FILE IS BUILT AROUND:

    known_bin_count + unknown_bin_count == checkin_count
    sum(bin.checkin_count)              == known_bin_count
    sum(bin.hours)                      == bin.checkin_count        (24 hours, always)
    checkin_count                       == the overview's and the volume report's, for the same range

The pure key rule is in tests/test_sort_bin.py. Real TEXT grouping, real
TIMESTAMP / TIMESTAMPTZ comparison and row level security are tested against
a real server in tests/test_rls_phase1_postgres.py.

The clock the route reads is the shared controlled clock
(tests/controlled_clock.py), which only a test moves.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, date, datetime, timedelta

import pytest
from controlled_clock import ControlledClock
from entitlement_support import grant
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
    OTHER_BRANCH,
    OTHER_CUSTOMER,
)
from test_customer_api_reports import _REJECT_TABLES, ReportDatabase, _local, _utc

import main
from customer_api import report_routes, report_schemas, tenant_scope
from services import (
    operational_report_service,
    session_service,
    tenant_resolution_service,
)

PATH = "/api/organizations/{org}/branches/{branch}/reports/{report}"
COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 1, "email": "alice@example.invalid", "full_name": "Alice"}

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
TENANT_NOT_FOUND = {"code": "tenant_not_found", "message": "Organization or branch not found."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

# The suspended organization of the shared schema: Alice is a member, and its one branch is ALSO called "main".
PAUSED_CUSTOMER, PAUSED_BRANCH = 8505, 51

# Where the clock starts unless a test moves it: 1 PM on Saturday 20 June 2026 in America/Chicago.
CLOCK = ControlledClock(datetime(2026, 6, 20, 18, 0, tzinfo=UTC))
JUNE_8_TO_12 = {"from": "2026-06-08", "to": "2026-06-12"}
RANGE = {"from": "2026-06-08", "to": "2026-06-12", "days": 5, "timezone": "America/Chicago", "includes_today": False}
NOON_JUNE_10 = datetime(2026, 6, 10, 17, 0, tzinfo=UTC)     # 12:00 local on 10 June


class Database(ReportDatabase):
    """The R2 tests' database, with a sort bin on every check-in."""

    def v1_bin(self, stored_bin, count: int = 1, *, at: datetime, customer=CUSTOMER, branch=BRANCH, destination="Main") -> None:
        """`count` legacy rows logged in this stored bin, at a naive local time."""
        with self.engine.begin() as conn:
            for number in range(count):
                conn.execute(
                    text("INSERT INTO checkins (customer_id, branch_id, event_time, barcode, title, destination, bin) "
                         "VALUES (:c, :b, :t, :bc, :ti, :d, :bin)"),
                    {"c": customer, "b": branch, "t": at.strftime(_STORED), "bc": f"{CANARY}-{number}", "ti": CANARY,
                     "d": destination, "bin": stored_bin},
                )

    def v2_bin(self, stored_bin: str, count: int = 1, *, at: datetime, customer=CUSTOMER, branch=BRANCH, destination="main") -> None:
        """`count` Contract v2 rows logged in this stored bin, at an aware instant (stored as UTC)."""
        with self.engine.begin() as conn:
            for _ in range(count):
                conn.execute(
                    text("INSERT INTO checkin_events (customer_id, branch_id, event_time, item_key, destination, bin) "
                         "VALUES (:c, :b, :t, :ik, :d, :bin)"),
                    {"c": customer, "b": branch, "t": at.astimezone(UTC).strftime(_STORED), "ik": CANARY,
                     "d": destination, "bin": stored_bin},
                )


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in (*_SCHEMA, *_REJECT_TABLES):
            conn.execute(text(statement))
        # checkins.bin: TEXT, nullable -- as in production. (The shared schema's checkin_events already has one.)
        conn.execute(text("ALTER TABLE checkins ADD COLUMN bin TEXT"))
    database = Database(engine)
    database.settings(organization=1, document=ACME_SETTINGS)
    monkeypatch.setattr(tenant_resolution_service, "get_engine", lambda: engine)
    monkeypatch.setattr(tenant_scope, "get_engine", lambda: database)
    yield database
    engine.dispose()


@pytest.fixture
def clock():
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


def _get(api, params=JUNE_8_TO_12, *, org="acme", branch="main", report="bins", headers=COOKIE):
    return api.get(PATH.format(org=org, branch=branch, report=report), headers=headers, params=params)


def _bins(api, params=JUNE_8_TO_12, **scope) -> dict:
    """The answer, after checking it is the approved shape and that every one of its invariants holds."""
    response = _get(api, params, **scope)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()

    assert list(body) == ["range", "checkin_count", "known_bin_count", "unknown_bin_count", "bins"]
    assert body["known_bin_count"] + body["unknown_bin_count"] == body["checkin_count"]
    assert sum(entry["checkin_count"] for entry in body["bins"]) == body["known_bin_count"]
    for entry in body["bins"]:
        assert list(entry) == ["key", "checkin_count", "hours"]
        assert len(entry["hours"]) == 24
        assert all(isinstance(count, int) and not isinstance(count, bool) and count >= 0 for count in entry["hours"])
        assert sum(entry["hours"]) == entry["checkin_count"]
        # Observed bins only: a bin is listed because something was logged in it.
        assert entry["checkin_count"] > 0
        assert entry["key"].isascii() and entry["key"].isdigit() and entry["key"] == str(int(entry["key"]))
    keys = [entry["key"] for entry in body["bins"]]
    assert keys == sorted(keys, key=int) and len(set(keys)) == len(keys)
    return body


def _other(api, report: str, params=JUNE_8_TO_12, **scope) -> dict:
    response = _get(api, params, report=report, **scope)
    assert response.status_code == 200, response.text
    return response.json()


def _hours(**counts: int) -> list[int]:
    """24 hourly counts, zero but for the hours given as h9=3, h14=2, ..."""
    hours = [0] * 24
    for name, count in counts.items():
        hours[int(name[1:])] = count
    return hours


def _totals(body: dict) -> dict[str, int]:
    return {entry["key"]: entry["checkin_count"] for entry in body["bins"]}


def _failing(params=JUNE_8_TO_12):
    return TestClient(main.app, raise_server_exceptions=False).get(
        PATH.format(org="acme", branch="main", report="bins"), headers=COOKIE, params=params)


# =====================================================================================================================
# The report
# =====================================================================================================================

def test_the_bin_volume_report(api, db):
    db.v1_bin("1", 4, at=_local(6, 8, 9))
    db.v1_bin("1", 2, at=_local(6, 9, 14, 30))
    db.v1_bin("2", 3, at=_local(6, 9, 9, 59))
    db.v1_bin("10", 1, at=_local(6, 12, 23, 59))
    db.v1_bin(None, 2, at=_local(6, 10, 10))

    assert _bins(api) == {
        "range": RANGE,
        "checkin_count": 12,
        "known_bin_count": 10,
        "unknown_bin_count": 2,
        "bins": [
            {"key": "1", "checkin_count": 6, "hours": _hours(h9=4, h14=2)},
            {"key": "2", "checkin_count": 3, "hours": _hours(h9=3)},
            {"key": "10", "checkin_count": 1, "hours": _hours(h23=1)},
        ],
    }


def test_one_observed_bin(api, db):
    db.v1_bin("3", 5, at=_local(6, 10, 11))

    body = _bins(api)

    assert body["bins"] == [{"key": "3", "checkin_count": 5, "hours": _hours(h11=5)}]
    assert (body["checkin_count"], body["known_bin_count"], body["unknown_bin_count"]) == (5, 5, 0)


@pytest.mark.parametrize("bins", [
    [1, 2, 3],
    [0, 1, 2, 3, 4, 5, 6],                                   # a typical sorter: seven bins, one of them numbered 0
    list(range(1, 13)),                                      # twelve
    list(range(1, 21)),                                      # twenty
    [0, 5, 12, 40, 120, 9999],                               # nothing says bins are consecutive
])
def test_however_many_bins_were_observed_are_listed_and_no_other(api, db, bins):
    for number in bins:
        db.v1_bin(str(number), number % 4 + 1, at=_local(6, 9, 8 + number % 10))

    body = _bins(api)

    assert [entry["key"] for entry in body["bins"]] == [str(number) for number in bins]
    assert _totals(body) == {str(number): number % 4 + 1 for number in bins}
    for entry in body["bins"]:
        assert entry["hours"] == _hours(**{f"h{8 + int(entry['key']) % 10}": entry["checkin_count"]})
    assert body["known_bin_count"] == body["checkin_count"] == sum(number % 4 + 1 for number in bins)
    assert body["unknown_bin_count"] == 0


def test_bins_that_were_not_observed_are_not_invented_between_or_around_the_ones_that_were(api, db):
    db.v1_bin("2", 1, at=_local(6, 9, 9))
    db.v1_bin("6", 1, at=_local(6, 9, 9))

    keys = [entry["key"] for entry in _bins(api)["bins"]]

    # Not 0..6, not 1..7, not 2..6: nothing fills the gaps, and nothing says how many bins the sorter has.
    assert keys == ["2", "6"]


def test_bins_are_in_numeric_order_not_text_order(api, db):
    for stored in ("10", "2", "1", "0", "20", "100", "9", "11"):
        db.v1_bin(stored, 1, at=_local(6, 9, 9))

    keys = [entry["key"] for entry in _bins(api)["bins"]]

    assert keys == ["0", "1", "2", "9", "10", "11", "20", "100"]
    assert keys != sorted(keys)


def test_a_bin_observed_only_outside_the_range_is_not_in_it(api, db):
    db.v1_bin("1", 3, at=_local(6, 9, 9))
    db.v1_bin("5", 40, at=_local(6, 7, 23, 59))              # the minute before the range
    db.v1_bin("6", 40, at=_local(6, 13, 0, 0))               # the minute after it
    db.v1_bin("1", 40, at=_local(6, 13, 0, 0))

    assert _bins(api)["bins"] == [{"key": "1", "checkin_count": 3, "hours": _hours(h9=3)}]


def test_a_range_of_one_day_and_a_longer_one_count_their_own_days(api, db):
    for day in range(8, 13):
        db.v1_bin("4", day, at=_local(6, day, 9))

    assert _totals(_bins(api, {"from": "2026-06-10", "to": "2026-06-10"})) == {"4": 10}
    assert _totals(_bins(api, {"from": "2026-06-09", "to": "2026-06-11"})) == {"4": 9 + 10 + 11}
    assert _totals(_bins(api)) == {"4": 8 + 9 + 10 + 11 + 12}
    assert _bins(api, {"from": "2026-06-10", "to": "2026-06-10"})["range"]["days"] == 1


# =====================================================================================================================
# Known and unknown
# =====================================================================================================================

def test_an_empty_range_is_zeros_and_no_bins(api, db):
    assert _bins(api) == {"range": RANGE, "checkin_count": 0, "known_bin_count": 0, "unknown_bin_count": 0, "bins": []}


def test_check_ins_that_all_have_no_usable_bin_are_all_unknown_and_no_bin_is_listed(api, db):
    db.v1_bin(None, 3, at=_local(6, 9, 9))
    db.v1_bin("", 2, at=_local(6, 9, 10))
    db.v1_bin("   ", 1, at=_local(6, 9, 11))
    db.v1_bin("unknown", 4, at=_local(6, 10, 9))
    db.v1_bin("Westside", 5, at=_local(6, 10, 10))

    assert _bins(api) == {"range": RANGE, "checkin_count": 15, "known_bin_count": 0, "unknown_bin_count": 15, "bins": []}


@pytest.mark.parametrize("stored", [
    None, "", " ", "unknown", "UNKNOWN", "bin1", "Bin 4", "Westside", "-1", "+4", "1.0", "4.5", "1,000", "#4", "12345",
    "00004", "31234000123456", "4'; DROP TABLE checkins; --", "٤", "４",
])
def test_a_stored_value_that_is_not_a_bin_number_is_counted_as_unknown_and_never_listed(api, db, stored):
    db.v1_bin(stored, 3, at=_local(6, 9, 9))
    db.v1_bin("7", 2, at=_local(6, 9, 9))

    body = _bins(api)

    assert (body["checkin_count"], body["known_bin_count"], body["unknown_bin_count"]) == (5, 2, 3)
    # The stored value is not a key, and no key was made from it: the one bin listed is the one that is a number.
    assert [entry["key"] for entry in body["bins"]] == ["7"]


def test_known_and_unknown_together_account_for_every_check_in(api, db):
    db.v1_bin("1", 6, at=_local(6, 8, 9))
    db.v1_bin("2", 4, at=_local(6, 9, 9))
    db.v1_bin(None, 3, at=_local(6, 9, 9))
    db.v1_bin("not a bin", 2, at=_local(6, 11, 9))
    db.v2_bin("unknown", 50, at=_utc(6, 11, 15))             # the site has no cutover: its current rows are not history

    body = _bins(api)

    assert (body["checkin_count"], body["known_bin_count"], body["unknown_bin_count"]) == (15, 10, 5)
    assert _totals(body) == {"1": 6, "2": 4}
    assert body["checkin_count"] == _other(api, "overview")["checkin_count"] == _other(api, "volume")["checkin_count"]


def test_leading_zeros_and_padding_are_one_bin(api, db):
    for stored in ("4", "04", "004", "0004", " 4 ", "04 "):
        db.v1_bin(stored, 2, at=_local(6, 9, 9))
    db.v1_bin("0010", 1, at=_local(6, 9, 9))
    db.v1_bin("10", 1, at=_local(6, 9, 9))

    body = _bins(api)

    assert _totals(body) == {"4": 12, "10": 2}
    assert "04" not in [entry["key"] for entry in body["bins"]]


# =====================================================================================================================
# Bin 0 is a bin
# =====================================================================================================================

def test_bin_zero_is_counted_exactly_like_every_other_bin(api, db):
    for stored in ("0", "1", "2"):
        db.v1_bin(stored, 5, at=_local(6, 9, 9))
        db.v1_bin(stored, 3, at=_local(6, 10, 14))
    db.v1_bin("00", 2, at=_local(6, 11, 8))
    db.v1_bin("000", 1, at=_local(6, 11, 8))
    # Rejects, in both tables, on the same days. They are a different set of rows and have nothing to do with a bin.
    db.reject_v1("Item not found in database", 6, at=_local(6, 9, 9))
    db.reject_v1("Hold shelf", 4, at=_local(6, 10, 14))
    db.reject_v2("item_not_found", 9, at=_utc(6, 11, 15))

    body = _bins(api)
    zero, one, two = body["bins"]

    # Nothing is taken off bin 0 for the rejects, and nothing is added to it.
    assert zero == {"key": "0", "checkin_count": 11, "hours": _hours(h9=5, h14=3, h8=3)}
    assert one == {"key": "1", "checkin_count": 8, "hours": _hours(h9=5, h14=3)}
    assert {key: value for key, value in zero.items() if key != "key"}.keys() == one.keys() - {"key"}
    assert zero["hours"][9] == one["hours"][9] == two["hours"][9] == 5
    assert body["checkin_count"] == 27 and body["unknown_bin_count"] == 0
    assert _other(api, "reliability")["reject_count"] == 10


def test_nothing_in_the_answer_gives_any_bin_a_meaning(api, db):
    db.v1_bin("0", 5, at=_local(6, 9, 9), destination="Westside")
    db.v1_bin("3", 5, at=_local(6, 9, 9), destination="Library Express")
    db.reject_v1("Item not found in database", 2, at=_local(6, 9, 9))

    text_ = str(_bins(api)).lower()

    for meaning in ("exception", "overflow", "hold", "reject", "estimat", "capacity", "full", "utiliz", "label", "name",
                    "destination", "westside", "library", "transit", "home", "rate", "percent", "share", "average"):
        assert meaning not in text_, meaning


def test_a_bin_is_not_a_routing_destination(api, db):
    # Two bins for one destination, and one bin shared by two destinations: the bin report follows the bin.
    db.v1_bin("1", 3, at=_local(6, 9, 9), destination="Westside")
    db.v1_bin("2", 2, at=_local(6, 9, 9), destination="Westside")
    db.v1_bin("2", 4, at=_local(6, 9, 9), destination="Main")

    assert _totals(_bins(api)) == {"1": 3, "2": 6}
    routing = _other(api, "routing")
    assert {entry["key"]: entry["checkin_count"] for entry in routing["transit"]}["westside"] == 5
    # The routing settings play no part: the same answer with none at all.
    db.settings(organization=1, document="{}")
    assert _totals(_bins(api)) == {"1": 3, "2": 6}


# =====================================================================================================================
# Hours: 24 wall-clock hours of the local day
# =====================================================================================================================

def test_every_hour_of_the_day_is_counted_and_none_is_left_out(api, db):
    for hour in range(24):
        db.v1_bin("5", hour + 1, at=_local(6, 9, hour, 30))

    (entry,) = _bins(api)["bins"]

    # Midnight to 11 PM: not opening hours, and not a window of the day.
    assert entry["hours"] == [hour + 1 for hour in range(24)]
    assert entry["checkin_count"] == sum(range(1, 25))


def test_an_hours_count_is_the_total_for_that_hour_across_every_day_of_the_range(api, db):
    for day in range(8, 13):
        db.v1_bin("5", 2, at=_local(6, day, 9, 15))
    db.v1_bin("5", 1, at=_local(6, 12, 9, 59, ))
    db.v1_bin("5", 1, at=_local(6, 12, 10, 0))

    (entry,) = _bins(api)["bins"]

    assert entry["hours"] == _hours(h9=11, h10=1)


def test_a_current_row_is_in_the_hour_the_products_clock_read_not_utcs(api, db):
    db.cutover(_utc(6, 1, 0))
    db.v2_bin("5", 3, at=_utc(6, 9, 15))                     # 10 AM in Chicago
    db.v2_bin("5", 2, at=_utc(6, 10, 4, 59))                 # 11:59 PM on the 9th
    db.v2_bin("5", 1, at=_utc(6, 10, 5, 0))                  # midnight on the 10th

    (entry,) = _bins(api)["bins"]

    assert entry["hours"] == _hours(h10=3, h23=2, h0=1)


def test_the_hours_follow_the_products_configured_zone(api, db, monkeypatch):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Asia/Tokyo")
    db.cutover(_utc(6, 1, 0))
    db.v2_bin("5", 3, at=_utc(6, 9, 15))                     # midnight on the 10th in Tokyo

    body = _bins(api)

    assert body["range"]["timezone"] == "Asia/Tokyo"
    assert body["bins"] == [{"key": "5", "checkin_count": 3, "hours": _hours(h0=3)}]


def test_the_bins_hours_add_up_to_the_volume_reports_hours(api, db):
    db.cutover(NOON_JUNE_10)
    for index, hour in enumerate((0, 6, 7, 9, 11, 13, 20, 21, 23)):
        db.v1_bin(str(index % 3), index + 1, at=_local(6, 8 + index % 2, hour, 5))
        db.v2_bin(str(index % 4), index + 2, at=_utc(6, 11, hour))

    body = _bins(api)
    volume = _other(api, "volume")

    assert body["unknown_bin_count"] == 0
    summed = [sum(entry["hours"][hour] for entry in body["bins"]) for hour in range(24)]
    assert summed == [entry["checkin_count"] for entry in volume["hours"]]
    assert body["checkin_count"] == volume["checkin_count"]


def test_the_day_the_clocks_go_forward_has_an_empty_two_oclock_hour(api, db, clock):
    clock.set(_utc(3, 20, 18))
    db.cutover(datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    spring = {"from": "2026-03-08", "to": "2026-03-08"}
    db.v2_bin("1", 2, at=_utc(3, 8, 7, 30))                  # 1:30 AM CST
    db.v2_bin("1", 3, at=_utc(3, 8, 8, 30))                  # 3:30 AM CDT: there was no 2:30
    db.v2_bin("1", 1, at=_utc(3, 9, 4, 59))                  # 11:59 PM CDT, the last minute of the 23-hour day

    body = _bins(api, spring)

    assert body["bins"] == [{"key": "1", "checkin_count": 6, "hours": _hours(h1=2, h3=3, h23=1)}]
    assert [entry["checkin_count"] for entry in _other(api, "volume", spring)["hours"]] == body["bins"][0]["hours"]


def test_the_day_the_clocks_go_back_holds_both_passes_of_the_repeated_hour(api, db, clock):
    clock.set(_utc(11, 10, 18))
    db.cutover(datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    autumn = {"from": "2026-11-01", "to": "2026-11-01"}
    db.v2_bin("1", 2, at=_utc(11, 1, 6, 30))                 # 1:30 AM CDT
    db.v2_bin("1", 3, at=_utc(11, 1, 7, 30))                 # 1:30 AM CST: the same hour on the clock, again
    db.v2_bin("1", 1, at=_utc(11, 1, 8, 30))                 # 2:30 AM CST

    body = _bins(api, autumn)

    assert body["bins"] == [{"key": "1", "checkin_count": 6, "hours": _hours(h1=5, h2=1)}]
    assert [entry["checkin_count"] for entry in _other(api, "volume", autumn)["hours"]] == body["bins"][0]["hours"]


# =====================================================================================================================
# Mixed eras: each era's rows on its own side of the cutover, one key for both
# =====================================================================================================================

def test_a_range_across_the_cutover_counts_each_row_once_under_one_key(api, db):
    db.cutover(NOON_JUNE_10)
    db.v1_bin("04", 3, at=_local(6, 9, 9))                   # legacy, before the cutover
    db.v1_bin("04", 2, at=_local(6, 10, 11, 59))             # legacy, the minute before it
    db.v1_bin("04", 50, at=_local(6, 10, 12, 0))             # AT the cutover on the legacy clock: v2 owns that instant
    db.v1_bin("4", 50, at=_local(6, 11, 9))                  # legacy, after it: not part of the history
    db.v2_bin("4", 70, at=_utc(6, 10, 16, 59))               # current, the minute before it: not part of the history
    db.v2_bin("4", 5, at=NOON_JUNE_10)                       # current, exactly at the cutover: counted, once
    db.v2_bin("4", 1, at=_utc(6, 11, 15))                    # current, after it
    db.v1_bin(None, 2, at=_local(6, 9, 10))
    db.v2_bin("unknown", 3, at=_utc(6, 11, 15))

    body = _bins(api)

    # "04" in the legacy table and "4" in the current one are bin 4.
    assert body == {
        "range": RANGE,
        "checkin_count": 16,
        "known_bin_count": 11,
        "unknown_bin_count": 5,
        "bins": [{"key": "4", "checkin_count": 11, "hours": _hours(h9=3, h11=2, h12=5, h10=1)}],
    }
    assert body["checkin_count"] == _other(api, "overview")["checkin_count"] == _other(api, "volume")["checkin_count"]


def test_an_event_exactly_at_the_cutover_is_counted_once_as_a_current_row(api, db):
    db.cutover(NOON_JUNE_10)
    db.v1_bin("1", 1, at=_local(6, 10, 12, 0))               # the legacy clock's reading of the cutover instant
    db.v2_bin("2", 1, at=NOON_JUNE_10)

    body = _bins(api, {"from": "2026-06-10", "to": "2026-06-10"})

    assert body["bins"] == [{"key": "2", "checkin_count": 1, "hours": _hours(h12=1)}]
    assert body["checkin_count"] == 1


def test_a_legacy_only_site_never_reads_the_current_table_and_a_cut_over_site_never_reads_the_legacy_one(api, db):
    db.v1_bin("1", 2, at=_local(6, 9, 9))
    db.v2_bin("2", 9, at=_utc(6, 9, 15))

    assert _totals(_bins(api)) == {"1": 2}
    assert [table for table, _params, _settings in db.queries] == ["v2_cutovers", "checkins"]

    db.cutover(_utc(6, 1, 0))
    db.queries.clear()

    assert _totals(_bins(api)) == {"2": 9}
    assert [table for table, _params, _settings in db.queries] == ["v2_cutovers", "checkin_events"]


def test_a_rollback_returns_the_site_to_its_legacy_rows(api, db):
    db.v1_bin("1", 2, at=_local(6, 9, 9))
    db.v2_bin("2", 9, at=_utc(6, 9, 15))
    db.cutover(_utc(6, 1, 0), set_at="2026-01-01 00:00:00+00:00")
    db.cutover(None, set_at="2026-02-01 00:00:00+00:00")

    assert _totals(_bins(api)) == {"1": 2}


def test_a_legacy_time_is_the_local_wall_clock_and_is_never_shifted_to_utc(api, db):
    # 10 PM on the 9th, as the legacy table holds it. Read as UTC it would be 5 PM -- and at 11 PM, the next day.
    db.v1_bin("1", 1, at=_local(6, 9, 22, 0))
    db.v1_bin("1", 1, at=_local(6, 12, 23, 30))

    body = _bins(api)

    assert body["bins"] == [{"key": "1", "checkin_count": 2, "hours": _hours(h22=1, h23=1)}]


@pytest.mark.parametrize("cutover", [
    None,
    datetime(2026, 6, 1, 5, 0, tzinfo=UTC),
    datetime(2026, 6, 9, 5, 0, tzinfo=UTC),                 # at a local midnight inside the range
    NOON_JUNE_10,
    datetime(2026, 6, 10, 17, 30, tzinfo=UTC),              # inside an hour
    datetime(2026, 6, 30, 5, 0, tzinfo=UTC),
])
def test_wherever_the_cutover_falls_the_total_is_the_overviews_and_the_volume_reports(api, db, cutover):
    if cutover is not None:
        db.cutover(cutover)
    for day in range(8, 13):
        for hour in (0, 9, 11, 12, 13, 23):
            db.v1_bin(str(hour % 3), 2, at=_local(6, day, hour, 15))
            db.v1_bin(None, 1, at=_local(6, day, hour, 45))
            db.v2_bin(str(hour % 4), 3, at=_utc(6, day, hour))
            db.v2_bin("unknown", 1, at=_utc(6, day, hour, 30))

    body = _bins(api)
    overview, volume = _other(api, "overview"), _other(api, "volume")

    assert body["checkin_count"] == overview["checkin_count"] == volume["checkin_count"] > 0
    assert body["unknown_bin_count"] > 0 and body["known_bin_count"] > 0


# =====================================================================================================================
# The range: the other sorter-site reports' own rules
# =====================================================================================================================

def _range_of(days: int, *, ending: str = "2026-06-20") -> dict:
    end = date.fromisoformat(ending)
    return {"from": (end - timedelta(days=days - 1)).isoformat(), "to": end.isoformat()}


def test_ninety_two_days_is_accepted_and_ninety_three_is_refused(api, db):
    db.v1_bin("1", 1, at=datetime(2026, 3, 21, 9, 0))  # noqa: DTZ001 - the first day of the 92
    db.v1_bin("1", 2, at=_local(6, 20, 9))
    db.v1_bin("9", 40, at=datetime(2026, 3, 20, 23, 59))  # noqa: DTZ001 - the minute before them

    accepted = _bins(api, _range_of(92))

    assert accepted["range"]["days"] == 92 and accepted["range"]["from"] == "2026-03-21"
    assert accepted["bins"] == [{"key": "1", "checkin_count": 3, "hours": _hours(h9=3)}]

    db.log.clear()
    refused = _get(api, _range_of(93))

    assert refused.status_code == 422
    assert db.log == []


def test_a_long_range_is_counted_a_week_at_a_time_and_loses_and_repeats_nothing_at_a_week_boundary(api, db):
    first = date(2026, 3, 21)
    for offset in range(92):
        day = first + timedelta(days=offset)
        db.v1_bin(str(offset % 5), 1, at=datetime(day.year, day.month, day.day, offset % 24, 0))  # noqa: DTZ001

    body = _bins(api, _range_of(92))

    assert body["checkin_count"] == 92 == _other(api, "volume", _range_of(92))["checkin_count"]
    assert _totals(body) == {str(key): sum(1 for offset in range(92) if offset % 5 == key) for key in range(5)}
    # One cutover lookup, then one grouped statement: since Reports R9D1 a statement counts up to 366 days of hours
    # (it was one a week, fourteen here). That a boundary between statements loses and repeats nothing is tested on
    # the service itself, over longer ranges, in tests/test_report_buckets.py.
    statements = [table for table, _params, _settings in db.queries]
    assert statements == ["v2_cutovers", "checkins"] * 2      # the bin report, then the volume report beside it


@pytest.mark.parametrize("params", [
    {"from": "2026-06-12", "to": "2026-06-08"},
    {"from": "2026-06-19", "to": "2026-06-21"},
    {"from": "2026-06-21", "to": "2026-06-22"},
    {"from": "2026-06-08"},
    {"to": "2026-06-12"},
    {},
    {"from": "2026-6-8", "to": "2026-06-12"},
    {"from": "2026-06-08T00:00:00", "to": "2026-06-12"},
    {"from": "06/08/2026", "to": "06/12/2026"},
    {"from": "2026-02-30", "to": "2026-03-02"},
    {"from": "today", "to": "today"},
    {"date": "2026-06-10"},
])
def test_a_range_that_cannot_be_reported_on_is_refused_exactly_as_the_other_reports_refuse_it(api, db, params):
    db.v1_bin("1", 3, at=_local(6, 9, 9))

    response = _get(api, params)
    overview = _get(api, params, report="overview")

    assert response.status_code == 422
    assert (response.status_code, response.json()) == (overview.status_code, overview.json())
    assert db.log == []


def test_today_is_allowed_and_flagged_and_tomorrow_is_refused(api, db):
    db.v1_bin("1", 2, at=_local(6, 20, 9))

    today = _bins(api, {"from": "2026-06-19", "to": "2026-06-20"})

    assert today["range"] == {"from": "2026-06-19", "to": "2026-06-20", "days": 2, "timezone": "America/Chicago",
                              "includes_today": True}
    assert _totals(today) == {"1": 2}
    assert _bins(api, {"from": "2026-06-18", "to": "2026-06-19"})["range"]["includes_today"] is False
    assert _get(api, {"from": "2026-06-19", "to": "2026-06-21"}).status_code == 422


def test_the_range_is_the_other_reports_own_dependency(api, db):
    (route,) = [route for route in main.app.routes if getattr(route, "path", "").endswith("/reports/bins")]

    calls = [dependency.call for dependency in get_flat_dependant(route.dependant).dependencies]
    assert report_routes.require_report_range in calls and tenant_scope.require_resolved_tenant in calls
    assert _bins(api)["range"] == _other(api, "overview")["range"] == _other(api, "volume")["range"]


# =====================================================================================================================
# Who may read it, and whose rows
# =====================================================================================================================

def test_no_session_is_a_401_and_nothing_is_read(api, db, monkeypatch):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: None)

    for headers in (COOKIE, {}):
        response = _get(api, headers=headers)
        assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert db.log == []


@pytest.mark.parametrize(("org", "branch"), [
    ("beta", "north"), ("nope", "main"), ("acme", "nope"), ("acme", "shut"), ("acme", "unmapped"),
    ("unmapped-org", "main"), ("closed", "main"), ("acme", "north"),
])
def test_anything_that_does_not_resolve_is_the_same_404_the_other_reports_give(api, db, org, branch):
    db.v1_bin("1", 3, at=_local(6, 9, 9), customer=OTHER_CUSTOMER, branch=OTHER_BRANCH)

    response = _get(api, org=org, branch=branch)
    overview = _get(api, org=org, branch=branch, report="overview")

    assert (response.status_code, response.json()) == (404, TENANT_NOT_FOUND)
    assert (response.status_code, response.content) == (overview.status_code, overview.content)
    assert db.log == []


def test_any_member_may_read_it_it_is_not_an_owner_or_admin_report(api, db):
    # Alice is a "viewer" of Acme -- the least a member can be -- and reads it as she reads the other four.
    with db.engine.connect() as conn:
        role = conn.execute(text("SELECT role FROM memberships WHERE organization_id = 1 AND user_id = 1")).scalar_one()
    db.v1_bin("1", 3, at=_local(6, 9, 9))

    assert role == "viewer"
    assert _totals(_bins(api)) == {"1": 3}
    for report in ("overview", "volume", "routing", "reliability"):
        assert _get(api, report=report).status_code == 200
    source = inspect.getsource(report_routes)
    for gate in ("role", "admin", "owner", "permission"):
        assert gate not in source, gate
    # The organization's plan reaches these routes in two ways only (R9C), neither of them about who the member is:
    # the history window every range is held to, and transit routing for the Routing report. Bin volume is neither.
    assert re.findall(r"entitlement_service\.(\w+)\(", source) == ["earliest_report_date"]
    assert "def get_site_routing_report(tenant: ResolvedTenant, _transits: Transits, requested: Range)" in source
    assert "_transits" not in source.split('@router.get("/bins")')[1]


def test_a_suspended_organization_can_still_be_read(api, db):
    db.v1_bin("3", 4, at=_local(6, 9, 9), customer=PAUSED_CUSTOMER, branch=PAUSED_BRANCH)

    assert _totals(_bins(api, org="paused")) == {"3": 4}


def test_two_organizations_branches_with_the_same_slug_never_share_a_row(api, db):
    # Acme and the suspended organization each have a branch called "main". Alice may read both.
    db.v1_bin("1", 2, at=_local(6, 9, 9))
    db.v1_bin("8", 7, at=_local(6, 9, 9), customer=PAUSED_CUSTOMER, branch=PAUSED_BRANCH)
    db.v1_bin(None, 5, at=_local(6, 9, 9), customer=PAUSED_CUSTOMER, branch=PAUSED_BRANCH)

    acme, paused = _bins(api), _bins(api, org="paused")

    assert (_totals(acme), acme["unknown_bin_count"]) == ({"1": 2}, 0)
    assert (_totals(paused), paused["unknown_bin_count"]) == ({"8": 7}, 5)


def test_another_organizations_rows_and_another_sites_rows_are_never_counted(api, db):
    db.cutover(NOON_JUNE_10)
    db.v1_bin("1", 2, at=_local(6, 9, 9))
    db.v2_bin("1", 1, at=_utc(6, 11, 15))
    for customer, branch in ((OTHER_CUSTOMER, OTHER_BRANCH), (CUSTOMER, EAST), (OTHER_CUSTOMER, BRANCH), (CUSTOMER, OTHER_BRANCH)):
        db.v1_bin("1", 30, at=_local(6, 9, 9), customer=customer, branch=branch)
        db.v1_bin("66", 30, at=_local(6, 9, 9), customer=customer, branch=branch)
        db.v2_bin("1", 30, at=_utc(6, 11, 15), customer=customer, branch=branch)
        db.v2_bin("77", 30, at=_utc(6, 11, 15), customer=customer, branch=branch)

    body = _bins(api)

    assert body["bins"] == [{"key": "1", "checkin_count": 3, "hours": _hours(h9=2, h10=1)}]
    # East is Acme's own other site, with no cutover: its 60 legacy rows are its own and no one else's.
    assert _totals(_bins(api, branch="east")) == {"1": 30, "66": 30}


def test_every_statement_is_bound_to_the_resolved_tenant_and_runs_under_its_context(api, db):
    db.cutover(NOON_JUNE_10)
    db.v1_bin("1", 2, at=_local(6, 9, 9))
    db.v2_bin("1", 1, at=_utc(6, 11, 15))

    _bins(api)

    assert [table for table, _params, _settings in db.queries] == ["v2_cutovers", "checkins", "checkin_events"]
    for _table, parameters, settings in db.queries:
        assert (str(parameters["customer_id"]), str(parameters["branch_id"])) == (str(CUSTOMER), str(BRANCH))
        assert (settings["customer_id"], settings["branch_id"]) == (str(CUSTOMER), str(BRANCH))
    assert [entry for entry in db.log if entry in ("open", "close")] == ["open", "close"]


def test_a_tenant_named_in_the_query_string_is_ignored(api, db):
    db.v1_bin("1", 2, at=_local(6, 9, 9))
    db.v1_bin("9", 30, at=_local(6, 9, 9), customer=OTHER_CUSTOMER, branch=OTHER_BRANCH)

    body = _bins(api, {**JUNE_8_TO_12, "customer_id": OTHER_CUSTOMER, "branch_id": OTHER_BRANCH, "branch": "north", "bin": "9"})

    assert _totals(body) == {"1": 2}


# =====================================================================================================================
# Failure, and what never leaves the server
# =====================================================================================================================

@pytest.mark.parametrize("table", ["v2_cutovers", "checkins", "checkin_events"])
def test_a_failed_read_is_a_500_that_says_nothing_and_is_never_a_partial_report(api, db, table):
    db.cutover(NOON_JUNE_10)
    db.v1_bin("1", 2, at=_local(6, 9, 9))
    db.v2_bin("1", 1, at=_utc(6, 11, 15))
    db.fail_on = table

    response = _failing()

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    for leaked in ("synthetic", "CANARY", str(CUSTOMER), "checkin", "bin", "Smith", "SELECT"):
        assert leaked not in response.text
    assert db.log[-1] == "close"


def test_a_tenant_context_that_does_not_match_is_a_500_and_nothing_is_read(api, db):
    db.v1_bin("1", 2, at=_local(6, 9, 9))
    db.read_back = lambda settings: {**settings, "branch_id": str(OTHER_BRANCH)}

    response = _failing()

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    assert set(db.log) == {"open", "close"}


def test_no_stored_text_item_or_identifier_is_in_the_report(api, db):
    db.v1_bin("1", 2, at=_local(6, 9, 9), destination="Westside")
    db.v1_bin(f"{CANARY}", 2, at=_local(6, 9, 9))
    db.v1_bin("Westside", 1, at=_local(6, 9, 9))
    db.cutover(NOON_JUNE_10)
    db.v2_bin("1", 1, at=_utc(6, 11, 15))

    body = _bins(api)
    text_ = str(body)

    for leaked in ("CANARY", "Smith", "31234", "Westside", "admin_lock", "branch_1", "8101", "8202"):
        assert leaked not in text_, leaked
    assert set(body) == {"range", "checkin_count", "known_bin_count", "unknown_bin_count", "bins"}
    # A bin's key is the only value that came from a stored column, and it is a number.
    assert [entry["key"] for entry in body["bins"]] == ["1"]


# =====================================================================================================================
# The route, the schema and the service
# =====================================================================================================================

def test_the_route_takes_the_two_path_slugs_and_the_two_dates_and_nothing_else_and_is_read_only(api, db):
    (route,) = [route for route in main.app.routes if getattr(route, "path", "").endswith("/reports/bins")]
    dependant = get_flat_dependant(route.dependant)

    assert route.path == "/api/organizations/{org_slug}/branches/{branch_slug}/reports/bins"
    assert route.methods == {"GET"}
    assert sorted({parameter.name for parameter in dependant.path_params}) == ["branch_slug", "org_slug"]
    assert sorted(parameter.alias for parameter in dependant.query_params) == ["from", "to"]
    assert dependant.body_params == [] and dependant.header_params == []
    for method in ("post", "put", "patch", "delete"):
        assert getattr(api, method)(PATH.format(org="acme", branch="main", report="bins"), headers=COOKIE).status_code == 405
    assert db.log == []


def test_it_is_registered_beside_the_other_four_reports_by_the_same_router():
    paths = sorted(route.path.rsplit("/", 1)[1] for route in report_routes.create_report_router().routes)

    assert paths == ["bins", "overview", "reliability", "routing", "volume"]


def test_the_response_models_have_exactly_the_approved_fields():
    assert list(report_schemas.BinVolumeReportResponse.model_fields) == [
        "range", "checkin_count", "known_bin_count", "unknown_bin_count", "bins"]
    assert list(report_schemas.BinVolumeBin.model_fields) == ["key", "checkin_count", "hours"]
    for model in (report_schemas.BinVolumeReportResponse, report_schemas.BinVolumeBin):
        assert model.model_config.get("extra") == "forbid"
    with pytest.raises(ValueError):
        report_schemas.BinVolumeBin(key="1", checkin_count=1, hours=[0] * 24, label="Holds")


def test_the_feature_is_bin_volume_and_is_never_called_utilization_or_routing():
    for module in (report_routes, report_schemas, operational_report_service):
        source = inspect.getsource(module).lower()
        for wrong in ("bin utilization", "bin utilisation", "bin routing", "bin_utilization", "bin_routing", "utiliz"):
            assert wrong not in source, (module.__name__, wrong)


def test_the_service_counts_bins_with_the_volume_reports_own_week_of_hours_and_window():
    # The code of each function, without its docstring.
    source = inspect.getsource(operational_report_service.get_bin_volume_report).split('"""', 2)[2]
    hourly = inspect.getsource(operational_report_service.get_checkin_counts_by_hour).split('"""', 2)[2]

    # One helper decides which statements an hourly count needs, for both: the bin report has no era logic of its own.
    assert "_weeks_of_hours(window, _V1_CHECKINS_BY_BIN, _V2_CHECKINS_BY_BIN)" in source
    assert "_weeks_of_hours(window, _V1_CHECKINS, _V2_CHECKINS)" in hourly
    for own_logic in ("cutover", "v1_span", "v2_span", "ZoneInfo", "astimezone", "local_hour_boundaries", "timedelta",
                      "range(7", "range(8", "20", "pandas"):
        assert own_logic not in source, own_logic
    assert "bin_key(stored)" in source and "bin_order" in source


def test_no_bin_is_named_or_assumed_anywhere_the_report_is_made():
    source = inspect.getsource(operational_report_service.get_bin_volume_report).split('"""', 2)[2]
    route = inspect.getsource(report_routes).split('@router.get("/bins")', 1)[1]

    for assumed in ('"0"', "'0'", "range(", "7", "exception", "overflow", "hold", "reject", "nbpl", "destination"):
        assert assumed not in source, assumed
        assert assumed not in route, assumed


def test_the_report_result_is_counts_and_keys_only():
    report = operational_report_service.BinVolumeReport(
        bins=(operational_report_service.BinCounts(key="0", hour_counts=(1,) * 24),
              operational_report_service.BinCounts(key="3", hour_counts=(0,) * 23 + (5,))),
        unknown_count=4,
    )

    assert (report.known_count, report.unknown_count, report.checkin_count) == (29, 4, 33)
    assert [counts.checkin_count for counts in report.bins] == [24, 5]
