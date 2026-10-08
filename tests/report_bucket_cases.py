"""Shared cases for the report engine's old-versus-new equivalence (Reports R9D1).

Each case is a site's rows, a range of days and the site's cutover. The same case is counted twice on the same
database -- once by the engine as it is (width_bucket, one row per bucket) and once by the engine as it was before
R9D1 (tests/legacy_bucket_engine.py: one COUNT(*) FILTER column per bucket, hours a week at a time) -- and every
figure every count function and report gives must be the same. Nothing here says what a figure SHOULD be: that is
the reports' own tests. This only says the new way of counting changes no figure.

Used by tests/test_report_buckets.py (SQLite, through tests/sqlite_width_bucket.py) and by
tests/test_report_buckets_postgres.py (a real server: the authority). Every time, destination and message is
synthetic. America/Chicago 2026: clocks go forward on 8 March, back on 1 November.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from legacy_bucket_engine import legacy_execute_bucket_count

from services import operational_report_service as engine
from services.operational_report_service import local_range, report_window
from services.routing_destination import (
    RoutingConfig,
    TransitDestination,
    destination_key,
)

CHICAGO = ZoneInfo("America/Chicago")
CUSTOMER, BRANCH = 8101, 11
OTHER_CUSTOMER, OTHER_BRANCH = 8202, 21          # another site: its rows must never be counted
TENANT = SimpleNamespace(operational_customer_id=CUSTOMER, operational_branch_id=BRANCH)

ROUTING = RoutingConfig(
    home_label="Main",
    home_keys=frozenset({destination_key("Main")}),
    transit=(TransitDestination(destination_key("Westside"), "Westside"),
             TransitDestination(destination_key("Library Express"), "Library Express")),
)


def local(year, month, day, hour=0, minute=0, second=0) -> datetime:
    """A NAIVE local wall-clock time, as a legacy table keeps it."""
    return datetime(year, month, day, hour, minute, second)  # noqa: DTZ001


def utc(year, month, day, hour=0, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


@dataclass
class Rows:
    """A site's rows. v1 groups may be None (the legacy columns are nullable); v2 ones never are."""

    v1_checkins: list[tuple[datetime, str | None, str | None]] = field(default_factory=list)    # (local, destination, bin)
    v2_checkins: list[tuple[datetime, str, str]] = field(default_factory=list)                  # (utc, destination key, bin)
    v1_rejects: list[tuple[datetime, str | None]] = field(default_factory=list)                 # (local, message)
    v2_rejects: list[tuple[datetime, str]] = field(default_factory=list)                        # (utc, error class)


@dataclass
class Case:
    first: date
    last: date
    cutover: datetime | None        # aware; None: legacy only
    rows: Rows


def _week_of_everything(v1: bool, v2: bool) -> Rows:
    """8-14 June 2026: rows on most days, none on the 12th; times on and either side of local midnight; every kind
    of group, a NULL one included."""
    rows = Rows()
    if v1:
        rows.v1_checkins += [
            (local(2026, 6, 8, 9), "Main", "1"), (local(2026, 6, 8, 23, 59, 59), "Westside", "2"),
            (local(2026, 6, 9), "Library Express", "0"),                         # exactly local midnight
            (local(2026, 6, 9, 13), None, None), (local(2026, 6, 10, 7), "Elsewhere", "x"),
            (local(2026, 6, 10, 7, 30), "MAIN ", "01"), (local(2026, 6, 11, 18), "Westside", "12"),
            (local(2026, 6, 13, 0, 0, 1), "Main", "1"), (local(2026, 6, 14, 23, 59, 59), "Library Express", "3"),
        ]
        rows.v1_rejects += [
            (local(2026, 6, 8, 10), "Item not found"), (local(2026, 6, 9), None), (local(2026, 6, 10, 11), "RFID collision"),
            (local(2026, 6, 11, 23, 59, 59), "something unrecognised"), (local(2026, 6, 14, 8), "Item not found"),
        ]
    if v2:
        rows.v2_checkins += [
            (utc(2026, 6, 8, 14), "main", "1"), (utc(2026, 6, 9, 4, 59, 59), "westside", "2"),
            (utc(2026, 6, 9, 5), "library_express", "0"),                        # exactly local midnight (CDT)
            (utc(2026, 6, 10, 12), "elsewhere", "7"), (utc(2026, 6, 11, 23), "westside", "12"),
            (utc(2026, 6, 13, 5), "main", "1"), (utc(2026, 6, 15, 4, 59, 59), "library_express", "3"),
        ]
        rows.v2_rejects += [
            (utc(2026, 6, 8, 15), "item_not_found"), (utc(2026, 6, 10, 16), "rfid_collision"),
            (utc(2026, 6, 11, 5), "not_a_reason_code"), (utc(2026, 6, 14, 13), "item_not_found"),
        ]
    return rows


def _around(cutover_local: datetime) -> Rows:
    """Rows either side of a cutover at `cutover_local` (naive Chicago time), in both tables: what each era owns
    must be counted, and what it does not own must not."""
    cutover_utc = cutover_local.replace(tzinfo=CHICAGO).astimezone(UTC)
    rows = _week_of_everything(v1=True, v2=True)
    for minutes in (-61, -1, 0, 1, 61):
        rows.v1_checkins.append((cutover_local + timedelta(minutes=minutes), "Westside", "4"))
        rows.v2_checkins.append((cutover_utc + timedelta(minutes=minutes), "westside", "4"))
        rows.v1_rejects.append((cutover_local + timedelta(minutes=minutes), "Item not found"))
        rows.v2_rejects.append((cutover_utc + timedelta(minutes=minutes), "item_not_found"))
    return rows


def _chicago(naive: datetime) -> datetime:
    return naive.replace(tzinfo=CHICAGO)


CASES: dict[str, Case] = {
    "v1 only": Case(date(2026, 6, 8), date(2026, 6, 14), None, _week_of_everything(v1=True, v2=False)),
    "v2 only": Case(date(2026, 6, 8), date(2026, 6, 14), utc(2020, 1, 1), _week_of_everything(v1=False, v2=True)),
    # Rows of both eras throughout: each era keeps only its own side of the cutover.
    "mixed, cutover at local midnight": Case(date(2026, 6, 8), date(2026, 6, 14), _chicago(local(2026, 6, 11)),
                                             _around(local(2026, 6, 11))),
    "mixed, cutover mid-day": Case(date(2026, 6, 8), date(2026, 6, 14), _chicago(local(2026, 6, 11, 12)),
                                   _around(local(2026, 6, 11, 12))),
    "mixed, cutover mid-hour": Case(date(2026, 6, 8), date(2026, 6, 14), _chicago(local(2026, 6, 11, 12, 30)),
                                    _around(local(2026, 6, 11, 12, 30))),
    "mixed, cutover before the range": Case(date(2026, 6, 8), date(2026, 6, 14), utc(2026, 6, 1),
                                            _week_of_everything(v1=True, v2=True)),
    "mixed, cutover after the range": Case(date(2026, 6, 8), date(2026, 6, 14), utc(2026, 7, 1),
                                           _week_of_everything(v1=True, v2=True)),
    # 8 March: 02:00-02:59 does not happen. The current hour 2 is zero wide; a legacy row stamped 02:30 is in it.
    "DST spring-forward": Case(date(2026, 3, 7), date(2026, 3, 9), _chicago(local(2026, 3, 8, 12)), Rows(
        v1_checkins=[(local(2026, 3, 8, 1, 59, 59), "Main", "1"), (local(2026, 3, 8, 2, 30), "Westside", "2"),
                     (local(2026, 3, 8, 3), "Main", "1"), (local(2026, 3, 7, 23, 59, 59), None, None)],
        v2_checkins=[(utc(2026, 3, 8, 7, 59, 59), "main", "1"), (utc(2026, 3, 8, 8), "westside", "2"),
                     (utc(2026, 3, 8, 18), "main", "1"), (utc(2026, 3, 9, 5), "main", "3")],
        v1_rejects=[(local(2026, 3, 8, 2, 30), "Item not found")],
        v2_rejects=[(utc(2026, 3, 8, 8), "item_not_found"), (utc(2026, 3, 8, 23), "rfid_collision")],
    )),
    # 1 November: 01:00-01:59 happens twice (06:00-07:00 and 07:00-08:00 UTC); the hour holds both passes.
    "DST fall-back": Case(date(2026, 10, 31), date(2026, 11, 2), _chicago(local(2026, 11, 1, 12)), Rows(
        v1_checkins=[(local(2026, 11, 1, 0, 59, 59), "Main", "1"), (local(2026, 11, 1, 1, 30), "Westside", "2"),
                     (local(2026, 11, 1, 2), "Main", "1"), (local(2026, 11, 1, 1, 0), None, "5")],
        v2_checkins=[(utc(2026, 11, 1, 6, 30), "main", "1"), (utc(2026, 11, 1, 7, 30), "westside", "2"),
                     (utc(2026, 11, 1, 8), "main", "1"), (utc(2026, 11, 1, 22), "library_express", "0"),
                     (utc(2026, 11, 2, 6), "main", "1")],
        v1_rejects=[(local(2026, 11, 1, 1, 30), "Item not found")],
        v2_rejects=[(utc(2026, 11, 1, 6, 30), "item_not_found"), (utc(2026, 11, 1, 18), "item_not_found")],
    )),
    # The cutover inside the repeated hour itself: the module's one documented limit, which must also not move.
    "cutover inside the repeated hour": Case(date(2026, 10, 31), date(2026, 11, 2), utc(2026, 11, 1, 6, 45), Rows(
        v1_checkins=[(local(2026, 11, 1, 1, 30), "Main", "1"), (local(2026, 11, 1, 1, 50), "Main", "1")],
        v2_checkins=[(utc(2026, 11, 1, 6, 40), "main", "1"), (utc(2026, 11, 1, 6, 50), "main", "1"),
                     (utc(2026, 11, 1, 7, 30), "westside", "2")],
        v1_rejects=[(local(2026, 11, 1, 1, 40), "Item not found")],
        v2_rejects=[(utc(2026, 11, 1, 7, 10), "item_not_found")],
    )),
}


@contextmanager
def cutover_is(cutover: datetime | None) -> Iterator[None]:
    original = engine.get_effective_cutover
    engine.get_effective_cutover = lambda conn, tenant: cutover  # type: ignore[assignment]
    try:
        yield
    finally:
        engine.get_effective_cutover = original  # type: ignore[assignment]


@contextmanager
def the_engine_before_r9d1() -> Iterator[None]:
    """The old statement, and the old week of hours per statement."""
    executor, chunk = engine._execute_bucket_count, engine._HOUR_BUCKET_DAYS_PER_STATEMENT
    engine._execute_bucket_count = legacy_execute_bucket_count  # type: ignore[assignment]
    engine._HOUR_BUCKET_DAYS_PER_STATEMENT = 7
    try:
        yield
    finally:
        engine._execute_bucket_count, engine._HOUR_BUCKET_DAYS_PER_STATEMENT = executor, chunk  # type: ignore[assignment]


def every_figure(conn, first: date, last: date, cutover: datetime | None) -> dict[str, object]:
    """Every count function and every report the engine has, for TENANT over first..last."""
    with cutover_is(cutover):
        window = report_window(conn, TENANT, local_range(first, last, CHICAGO))
    return {
        "window": window,
        "checkins by day": engine.get_checkin_counts_by_day(conn, TENANT, window),
        "rejects by day": engine.get_reject_counts_by_day(conn, TENANT, window),
        "checkins by hour": engine.get_checkin_counts_by_hour(conn, TENANT, window),
        "checkins by destination": engine.get_checkin_destination_counts_by_day(conn, TENANT, window, ROUTING),
        "rejects by reason": engine.get_reject_reason_counts_by_day(conn, TENANT, window),
        "bins": engine.get_bin_volume_report(conn, TENANT, window),
        "overview": engine.get_overview_report(conn, TENANT, window, ROUTING),
        "volume": engine.get_volume_report(conn, TENANT, window),
        "routing": engine.get_routing_report(conn, TENANT, window, ROUTING),
        "reliability": engine.get_reliability_report(conn, TENANT, window),
    }


def both_ways(conn, first: date, last: date, cutover: datetime | None) -> tuple[dict[str, object], dict[str, object]]:
    new = every_figure(conn, first, last, cutover)
    with the_engine_before_r9d1():
        old = every_figure(conn, first, last, cutover)
    return new, old


def sparse_rows(first: date, last: date) -> Rows:
    """A few rows on the first and last day, on 29 February of any leap year, on every 1st of a month and on the
    days either side of every 366th day (where one hourly statement ends and the next begins), in both eras'
    tables: each era counts only the ones on its own side of the cutover."""
    days = (last - first).days + 1
    picked = {first, last}
    picked |= {first + timedelta(days=offset) for offset in range(days) if (first + timedelta(days=offset)).day == 1}
    picked |= {first + timedelta(days=offset) for offset in range(days)
               if (first + timedelta(days=offset)).month == 2 and (first + timedelta(days=offset)).day == 29}
    for statement_edge in range(366, days, 366):
        picked |= {first + timedelta(days=statement_edge - 1), first + timedelta(days=statement_edge)}
    rows = Rows()
    for day in sorted(picked):
        for hour, minute in ((0, 0), (23, 59)):
            wall = local(day.year, day.month, day.day, hour, minute)
            instant = wall.replace(tzinfo=CHICAGO).astimezone(UTC)
            rows.v1_checkins.append((wall, "Westside" if hour else "Main", str(day.month)))
            rows.v2_checkins.append((instant, "westside" if hour else "main", str(day.month)))
            rows.v1_rejects.append((wall, "Item not found"))
            rows.v2_rejects.append((instant, "item_not_found"))
    return rows
