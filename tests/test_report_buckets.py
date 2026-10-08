"""Reports R9D1: the report engine counts by bucket ROW (width_bucket) and gives exactly what it gave when it
counted by bucket COLUMN -- on an in-memory SQLite database, through tests/sqlite_width_bucket.py.

Every case of tests/report_bucket_cases.py, and long sparse ranges, is counted both ways on the same rows and must
agree figure for figure: every count function, every report. The ranges here go far past the API's 92 days: the
engine is asked directly, because the point is the engine. (SQLite allows 2,000 columns, so the old way still
works here up to 1,999 days; PostgreSQL's limit of 1,664 is shown in tests/test_report_buckets_postgres.py.)
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from report_bucket_cases import (
    BRANCH,
    CASES,
    CHICAGO,
    CUSTOMER,
    OTHER_BRANCH,
    OTHER_CUSTOMER,
    TENANT,
    Rows,
    both_ways,
    cutover_is,
    local,
    sparse_rows,
)
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    insert,
)
from sqlalchemy.pool import StaticPool

from services import operational_report_service as engine
from services.operational_report_service import _SOURCES, local_range, report_window

metadata = MetaData()
CHECKINS = Table("checkins", metadata, Column("id", Integer, primary_key=True), Column("customer_id", Integer),
                 Column("branch_id", Integer), Column("event_time", DateTime), Column("destination", Text),
                 Column("bin", Text))
CHECKIN_EVENTS = Table("checkin_events", metadata, Column("id", Integer, primary_key=True), Column("customer_id", Integer),
                       Column("branch_id", Integer), Column("event_time", DateTime(timezone=True)),
                       Column("destination", Text), Column("bin", Text))
REJECTS = Table("rejects", metadata, Column("id", Integer, primary_key=True), Column("customer_id", Integer),
                Column("branch_id", Integer), Column("event_time", DateTime), Column("error_message", Text))
REJECT_EVENTS = Table("reject_events", metadata, Column("id", Integer, primary_key=True), Column("customer_id", Integer),
                      Column("branch_id", Integer), Column("event_time", DateTime(timezone=True)), Column("error_class", Text))


@pytest.fixture
def conn():
    database = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    metadata.create_all(database)
    with database.connect() as connection:
        yield connection
    database.dispose()


def seed(conn, rows: Rows, customer=CUSTOMER, branch=BRANCH) -> None:
    scope = {"customer_id": customer, "branch_id": branch}
    for table, entries, names in (
        (CHECKINS, rows.v1_checkins, ("event_time", "destination", "bin")),
        (CHECKIN_EVENTS, rows.v2_checkins, ("event_time", "destination", "bin")),
        (REJECTS, rows.v1_rejects, ("event_time", "error_message")),
        (REJECT_EVENTS, rows.v2_rejects, ("event_time", "error_class")),
    ):
        if entries:
            conn.execute(insert(table), [{**scope, **dict(zip(names, entry, strict=True))} for entry in entries])


def _assert_the_same(new: dict, old: dict) -> None:
    assert new.keys() == old.keys()
    for figure, value in new.items():
        assert value == old[figure], figure


# =====================================================================================================================
# Every case: the same figures both ways
# =====================================================================================================================

@pytest.mark.parametrize("name", list(CASES))
def test_every_figure_is_what_the_column_per_bucket_engine_gave(conn, name):
    case = CASES[name]
    seed(conn, case.rows)
    seed(conn, case.rows, OTHER_CUSTOMER, OTHER_BRANCH)        # another site's identical rows: in neither answer

    new, old = both_ways(conn, case.first, case.last, case.cutover)

    _assert_the_same(new, old)
    # And the rows were really counted: the case is not trivially empty.
    assert sum(new["checkins by day"]) > 0 and sum(new["rejects by day"]) > 0


def test_each_figure_counts_only_this_sites_rows():
    """With the other site's rows taken away, every figure is the same: none of them was counted."""
    case = CASES["mixed, cutover mid-hour"]
    alone, crowded = (create_engine("sqlite://", poolclass=StaticPool) for _ in range(2))
    for database in (alone, crowded):
        metadata.create_all(database)
    with alone.connect() as one, crowded.connect() as two:
        seed(one, case.rows)
        seed(two, case.rows)
        seed(two, case.rows, OTHER_CUSTOMER, OTHER_BRANCH)
        _assert_the_same(both_ways(one, case.first, case.last, case.cutover)[0],
                         both_ways(two, case.first, case.last, case.cutover)[0])


def test_a_null_stored_value_is_still_one_group_of_its_own(conn):
    seed(conn, Rows(v1_checkins=[(local(2026, 6, 8, 9), None, None), (local(2026, 6, 9, 9), None, "2"),
                                 (local(2026, 6, 9, 10), "Main", None)]))
    window = _window(conn, date(2026, 6, 8), date(2026, 6, 9), None)
    by_destination = next(source for source in _SOURCES if (source.table, source.group_column) == ("checkins", "destination"))

    rows = engine._execute_bucket_count(conn, TENANT, by_destination, window.v1_span, window.local_range.v1_day_boundaries_local)

    assert sorted(rows, key=lambda row: (row[0] is not None, row[0] or "")) == [(None, 1, 1), ("Main", 0, 1)]


# =====================================================================================================================
# Long ranges, far past the API's 92 days
# =====================================================================================================================

LONG_RANGES = {
    "365 days": (date(2025, 6, 21), date(2026, 6, 20)),
    "1,664 days": (date(2021, 12, 30), date(2026, 7, 20)),
    "1,700 days": (date(2021, 11, 24), date(2026, 7, 20)),
    "5 years, across 29 February 2024": (date(2021, 6, 21), date(2026, 6, 20)),
    "a leap year's whole winter": (date(2027, 12, 1), date(2028, 3, 31)),
}


def _window(conn, first, last, cutover):
    with cutover_is(cutover):
        return report_window(conn, TENANT, local_range(first, last, CHICAGO))


def test_the_long_ranges_are_the_lengths_they_say():
    assert [(last - first).days + 1 for first, last in LONG_RANGES.values()] == [365, 1664, 1700, 1826, 122]


@pytest.mark.parametrize("era", ["v1 only", "v2 only", "mixed"])
@pytest.mark.parametrize("name", list(LONG_RANGES))
def test_a_long_range_gives_every_figure_the_column_per_bucket_engine_gave(conn, name, era):
    first, last = LONG_RANGES[name]
    middle = first + (last - first) / 2
    cutover = {"v1 only": None, "v2 only": local(2000, 1, 1).replace(tzinfo=CHICAGO),
               "mixed": local(middle.year, middle.month, middle.day, 12).replace(tzinfo=CHICAGO)}[era]
    rows = sparse_rows(first, last)
    seed(conn, rows)

    new, old = both_ways(conn, first, last, cutover)

    _assert_the_same(new, old)
    days = (last - first).days + 1
    assert len(new["checkins by day"]) == days and len(new["checkins by hour"]) == days
    assert sum(new["checkins by day"]) > 0


def test_a_long_ranges_hours_are_one_statement_a_year_and_a_statement_edge_loses_and_repeats_nothing(conn, monkeypatch):
    first, last = LONG_RANGES["5 years, across 29 February 2024"]
    rows = sparse_rows(first, last)
    seed(conn, rows)
    window = _window(conn, first, last, None)
    asked = []
    real = engine._execute_bucket_count
    monkeypatch.setattr(engine, "_execute_bucket_count", lambda *args: asked.append(args[2]) or real(*args))

    by_hour = engine.get_checkin_counts_by_hour(conn, TENANT, window)

    days = (last - first).days + 1
    assert len(asked) == math.ceil(days / engine._HOUR_BUCKET_DAYS_PER_STATEMENT) == 5
    # Each seeded day has one row in its first minute and one in its last: exactly those, wherever a statement ends.
    seeded = {when.date() for when, _destination, _bin in rows.v1_checkins}
    for index, day in enumerate(first + timedelta(days=offset) for offset in range(days)):
        assert (by_hour[index][0], by_hour[index][23]) == ((1, 1) if day in seeded else (0, 0)), day
    assert sum(map(sum, by_hour)) == len(rows.v1_checkins)


# =====================================================================================================================
# What the engine will not do
# =====================================================================================================================

class _Answering:
    """A connection that answers any statement with the rows given."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, _statement, _parameters):
        return iter(self._rows)


@pytest.mark.parametrize("bucket", [0, 4, -1, None, "1", 1.0, True])
def test_a_bucket_outside_the_intervals_asked_for_is_a_fault_never_a_misplaced_count(bucket):
    span, boundaries = (local(2026, 6, 8), local(2026, 6, 11)), (local(2026, 6, 8), local(2026, 6, 9), local(2026, 6, 10), local(2026, 6, 11))

    with pytest.raises(ValueError, match="outside the intervals"):
        engine._execute_bucket_count(_Answering([(bucket, 5)]), TENANT, _SOURCES[0], span, boundaries)


def test_buckets_with_no_rows_are_zero_and_a_plain_count_is_always_one_row():
    span, boundaries = (local(2026, 6, 8), local(2026, 6, 11)), (local(2026, 6, 8), local(2026, 6, 9), local(2026, 6, 10), local(2026, 6, 11))

    assert engine._execute_bucket_count(_Answering([]), TENANT, _SOURCES[0], span, boundaries) == [(0, 0, 0)]
    assert engine._execute_bucket_count(_Answering([(3, 2), (1, 7)]), TENANT, _SOURCES[0], span, boundaries) == [(7, 0, 2)]
    grouped = next(source for source in _SOURCES if source.group_column == "destination" and not source.aware)
    assert engine._execute_bucket_count(_Answering([]), TENANT, grouped, span, boundaries) == []
    assert engine._execute_bucket_count(_Answering([("a", 2, 1), (None, 1, 4), ("a", 3, 2)]), TENANT, grouped, span, boundaries) == [
        ("a", 0, 1, 2), (None, 4, 0, 0)]
