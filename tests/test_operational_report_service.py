"""Reports R2: the range, window and statement rules behind the sorter-site
range reports.

    validate_report_range(from_date, to_date, today=)   which ranges may be reported on
    local_range(from_date, to_date, zone)               a run of local days, as boundaries for both tables
    report_window(conn, tenant, local_range)            the part of a range each era owns
    _bucket_count_statement(source, buckets)            the one statement shape every count is made with

The counts themselves are tested through the real routes, against real
tables, in tests/test_customer_api_reports.py -- including that every day of
a range is what the single-day endpoints answer. This file is about the
rules that make that true, and about what the module's SQL may not contain.

The dates used are real DST dates for America/Chicago in 2026: clocks go
forward on 8 March and back on 1 November.

Imported the "flat" way (services.operational_report_service), the identity
the API process uses.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import DateTime, create_engine, text
from sqlalchemy.pool import StaticPool

from services import operational_metrics_service, operational_report_service
from services.operational_metrics_service import local_day_bounds, local_hour_boundaries
from services.operational_report_service import (
    _SOURCES,
    MAX_REPORT_RANGE_DAYS,
    DestinationCounts,
    LocalRange,
    ReportRangeError,
    ReportWindow,
    _bucket_count_statement,
    _Source,
    _week_boundaries,
    local_range,
    report_window,
    sum_destination_counts,
    validate_report_range,
)
from services.routing_destination import RoutingConfig, TransitDestination

CHICAGO = ZoneInfo("America/Chicago")
KOLKATA = ZoneInfo("Asia/Kolkata")      # UTC+05:30, no DST
HAVANA = ZoneInfo("America/Havana")     # changes its clocks AT local midnight
LORD_HOWE = ZoneInfo("Australia/Lord_Howe")     # moves its clocks by 30 minutes
TODAY = date(2026, 6, 20)  # freshness: allow FRESH004 -- passed as today= at every call; the service reads no clock
CUSTOMER, BRANCH = 8101, 11
TENANT = SimpleNamespace(operational_customer_id=CUSTOMER, operational_branch_id=BRANCH)


def _local(year, month, day, hour=0, minute=0) -> datetime:
    """A NAIVE local wall-clock datetime, as a legacy table holds it."""
    return datetime(year, month, day, hour, minute)  # noqa: DTZ001


def _utc(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# =====================================================================================================================
# validate_report_range
# =====================================================================================================================

def test_the_limit_is_ninety_two_days():
    assert MAX_REPORT_RANGE_DAYS == 92


@pytest.mark.parametrize(("from_date", "to_date"), [
    (TODAY, TODAY),                                     # one day, and it is today
    (date(2026, 6, 19), date(2026, 6, 19)),
    (date(2026, 6, 1), TODAY),
    (TODAY - timedelta(days=91), TODAY),                # exactly 92 days, both ends included
    (date(2020, 1, 1), date(2020, 3, 31)),              # long ago, 91 days
    (date(2024, 2, 28), date(2024, 3, 1)),              # across a leap day
])
def test_a_range_that_may_be_reported_on_is_accepted(from_date, to_date):
    assert validate_report_range(from_date, to_date, today=TODAY) is None


@pytest.mark.parametrize(("from_date", "to_date", "problem", "field"), [
    (date(2026, 6, 12), date(2026, 6, 8), "range_order", "from"),
    (TODAY, TODAY - timedelta(days=1), "range_order", "from"),
    (TODAY, TODAY + timedelta(days=1), "range_in_future", "to"),
    (date(2026, 6, 1), date(2027, 1, 1), "range_in_future", "to"),
    (TODAY + timedelta(days=1), TODAY + timedelta(days=2), "range_in_future", "to"),
    (TODAY - timedelta(days=92), TODAY, "range_too_long", "to"),            # 93 days
    (date(2025, 1, 1), date(2025, 12, 31), "range_too_long", "to"),
])
def test_a_range_that_may_not_is_refused_with_which_bound_and_what_kind(from_date, to_date, problem, field):
    with pytest.raises(ReportRangeError) as refused:
        validate_report_range(from_date, to_date, today=TODAY)

    assert (refused.value.problem, refused.value.field) == (problem, field)
    # Neither date is in the message: it is a fixed word.
    assert str(refused.value) == problem


def test_order_is_judged_before_the_future_and_the_future_before_length():
    with pytest.raises(ReportRangeError, match="range_order"):
        validate_report_range(date(2030, 1, 1), date(2029, 1, 1), today=TODAY)
    with pytest.raises(ReportRangeError, match="range_in_future"):
        validate_report_range(date(2026, 1, 1), date(2027, 1, 1), today=TODAY)


def test_today_is_given_and_no_clock_is_read():
    assert list(inspect.signature(validate_report_range).parameters) == ["from_date", "to_date", "today"]
    assert inspect.signature(validate_report_range).parameters["today"].kind is inspect.Parameter.KEYWORD_ONLY


# =====================================================================================================================
# local_range
# =====================================================================================================================

def test_an_ordinary_range_is_its_days_and_the_boundaries_between_them():
    local = local_range(date(2026, 6, 8), date(2026, 6, 10), CHICAGO)

    assert isinstance(local, LocalRange)
    assert (local.from_date, local.to_date, local.days, local.timezone_name) == (
        date(2026, 6, 8), date(2026, 6, 10), 3, "America/Chicago",
    )
    assert local.dates == (date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10))
    assert local.v1_day_boundaries_local == (
        _local(2026, 6, 8), _local(2026, 6, 9), _local(2026, 6, 10), _local(2026, 6, 11),
    )
    assert local.v2_day_boundaries_utc == (
        _utc(2026, 6, 8, 5), _utc(2026, 6, 9, 5), _utc(2026, 6, 10, 5), _utc(2026, 6, 11, 5),      # CDT, UTC-5
    )


def test_a_range_of_one_day_is_exactly_that_days_single_day_bounds():
    for zone in (CHICAGO, KOLKATA, HAVANA, LORD_HOWE, ZoneInfo("UTC")):
        for day in (date(2026, 6, 1), date(2026, 1, 15), date(2026, 3, 8), date(2026, 11, 1)):
            local, bounds = local_range(day, day, zone), local_day_bounds(day, zone)

            assert local.v1_day_boundaries_local == (bounds.v1_start_local, bounds.v1_end_local)
            assert local.v2_day_boundaries_utc == (bounds.v2_start_utc, bounds.v2_end_utc)


def test_every_day_of_a_range_has_exactly_its_single_day_bounds_so_consecutive_days_meet():
    for zone in (CHICAGO, KOLKATA, HAVANA, LORD_HOWE):
        local = local_range(date(2026, 2, 20), date(2026, 5, 22), zone)        # 92 days, across a clock change

        assert local.days == 92
        assert len(local.v1_day_boundaries_local) == len(local.v2_day_boundaries_utc) == 93
        for index, day in enumerate(local.dates):
            bounds = local_day_bounds(day, zone)
            assert (local.v1_day_boundaries_local[index], local.v1_day_boundaries_local[index + 1]) == (
                bounds.v1_start_local, bounds.v1_end_local)
            assert (local.v2_day_boundaries_utc[index], local.v2_day_boundaries_utc[index + 1]) == (
                bounds.v2_start_utc, bounds.v2_end_utc)
        assert list(local.v2_day_boundaries_utc) == sorted(set(local.v2_day_boundaries_utc))     # strictly increasing


def test_the_legacy_boundaries_are_naive_and_the_current_ones_are_aware_utc():
    local = local_range(date(2026, 3, 7), date(2026, 3, 9), CHICAGO)

    assert all(boundary.tzinfo is None for boundary in local.v1_day_boundaries_local)
    assert all(boundary.tzinfo is UTC for boundary in local.v2_day_boundaries_utc)


def test_a_local_day_is_one_day_whatever_its_length_in_real_time():
    spring = local_range(date(2026, 3, 7), date(2026, 3, 9), CHICAGO).v2_day_boundaries_utc
    fall = local_range(date(2026, 10, 31), date(2026, 11, 2), CHICAGO).v2_day_boundaries_utc

    assert [later - earlier for earlier, later in pairwise(spring)] == [
        timedelta(hours=24), timedelta(hours=23), timedelta(hours=24),
    ]
    assert [later - earlier for earlier, later in pairwise(fall)] == [
        timedelta(hours=24), timedelta(hours=25), timedelta(hours=24),
    ]
    # On the legacy clock every day is the same 24 readings.
    legacy = local_range(date(2026, 3, 7), date(2026, 3, 9), CHICAGO).v1_day_boundaries_local
    assert {later - earlier for earlier, later in pairwise(legacy)} == {timedelta(days=1)}


def test_a_range_may_cross_a_month_a_year_and_a_leap_day():
    assert local_range(date(2027, 12, 30), date(2028, 1, 2), CHICAGO).dates == (
        date(2027, 12, 30), date(2027, 12, 31), date(2028, 1, 1), date(2028, 1, 2),
    )
    assert date(2028, 2, 29) in local_range(date(2028, 2, 27), date(2028, 3, 1), CHICAGO).dates


def test_a_range_that_ends_before_it_starts_is_refused_outright():
    with pytest.raises(ValueError, match="must not end before it starts"):
        local_range(date(2026, 6, 10), date(2026, 6, 9), CHICAGO)


def test_local_range_itself_sets_no_limit_on_length():
    """The limit is validate_report_range's, a safeguard for this way of counting -- not part of what a range is."""
    assert local_range(date(2025, 1, 1), date(2025, 12, 31), CHICAGO).days == 365


# =====================================================================================================================
# report_window: the part of a range each era owns
# =====================================================================================================================

@pytest.fixture
def cutovers():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE v2_cutovers (id INTEGER PRIMARY KEY, customer_id INTEGER, branch_id INTEGER, cutover_at TEXT, "
            "set_by TEXT, set_at TEXT)"
        ))

    def record(cutover_at: datetime | None, *, customer=CUSTOMER, branch=BRANCH, set_at="2026-01-01 00:00:00+00:00"):
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, set_at) "
                     "VALUES (:c, :b, :at, 'test', :set_at)"),
                {"c": customer, "b": branch, "at": None if cutover_at is None else cutover_at.isoformat(sep=" "),
                 "set_at": set_at},
            )

    yield SimpleNamespace(engine=engine, record=record)
    engine.dispose()


JUNE = local_range(date(2026, 6, 8), date(2026, 6, 12), CHICAGO)
JUNE_LOCAL = (_local(2026, 6, 8), _local(2026, 6, 13))
JUNE_UTC = (_utc(2026, 6, 8, 5), _utc(2026, 6, 13, 5))


def _window(cutovers, local=JUNE) -> ReportWindow:
    with cutovers.engine.connect() as conn:
        return report_window(conn, TENANT, local)


def test_with_no_cutover_the_legacy_era_owns_the_whole_range(cutovers):
    window = _window(cutovers)

    assert (window.local_range, window.v1_span, window.v2_span) == (JUNE, JUNE_LOCAL, None)


def test_a_cutover_before_the_range_gives_it_all_to_the_current_era(cutovers):
    cutovers.record(_utc(2026, 6, 1, 5))

    window = _window(cutovers)

    assert (window.v1_span, window.v2_span) == (None, JUNE_UTC)


def test_a_cutover_exactly_at_the_start_of_the_range_gives_it_all_to_the_current_era(cutovers):
    cutovers.record(JUNE_UTC[0])

    assert _window(cutovers).v1_span is None
    assert _window(cutovers).v2_span == JUNE_UTC


def test_a_cutover_inside_the_range_divides_it_with_no_gap_and_no_overlap(cutovers):
    cutovers.record(_utc(2026, 6, 10, 17))              # 12:00 local on the 10th

    window = _window(cutovers)

    assert window.v1_span == (_local(2026, 6, 8), _local(2026, 6, 10, 12))
    assert window.v2_span == (_utc(2026, 6, 10, 17), _utc(2026, 6, 13, 5))
    # The same moment, in the two forms: where v1 stops is where v2 starts.
    assert window.v1_span[1].replace(tzinfo=CHICAGO).astimezone(UTC) == window.v2_span[0]


def test_a_cutover_at_or_after_the_end_of_the_range_leaves_it_all_to_the_legacy_era(cutovers):
    cutovers.record(JUNE_UTC[1])

    assert (_window(cutovers).v1_span, _window(cutovers).v2_span) == (JUNE_LOCAL, None)

    cutovers.record(_utc(2026, 8, 1, 5), set_at="2026-02-01 00:00:00+00:00")
    assert (_window(cutovers).v1_span, _window(cutovers).v2_span) == (JUNE_LOCAL, None)


def test_the_latest_record_decides_and_a_rollback_is_legacy_only(cutovers):
    cutovers.record(_utc(2026, 6, 10, 17), set_at="2026-06-01 00:00:00+00:00")
    cutovers.record(None, set_at="2026-06-02 00:00:00+00:00")

    assert (_window(cutovers).v1_span, _window(cutovers).v2_span) == (JUNE_LOCAL, None)


def test_another_tenants_cutover_is_not_this_tenants(cutovers):
    cutovers.record(_utc(2026, 6, 1, 5), customer=8202)
    cutovers.record(_utc(2026, 6, 1, 5), branch=14)

    assert (_window(cutovers).v1_span, _window(cutovers).v2_span) == (JUNE_LOCAL, None)


def test_a_legacy_bound_is_the_cutover_on_the_local_clock_never_the_utc_reading_unlabelled(cutovers):
    cutovers.record(_utc(2026, 6, 10, 17))

    v1_end = _window(cutovers).v1_span[1]

    assert v1_end == _local(2026, 6, 10, 12)            # 17:00Z is noon in Chicago
    assert v1_end != _local(2026, 6, 10, 17)            # ...and not 17:00 with its offset dropped
    assert v1_end.tzinfo is None


def test_the_window_is_the_single_day_partition_for_a_range_of_one_day(cutovers):
    cutovers.record(_utc(2026, 6, 10, 17))
    one_day = local_range(date(2026, 6, 10), date(2026, 6, 10), CHICAGO)

    window = _window(cutovers, one_day)

    assert window.v1_span == (_local(2026, 6, 10), _local(2026, 6, 10, 12))
    assert window.v2_span == (_utc(2026, 6, 10, 17), _utc(2026, 6, 11, 5))


def test_the_window_runs_exactly_one_statement_the_cutover_lookup():
    statements = []

    class Recording:
        def execute(self, statement, parameters=None):
            statements.append((" ".join(str(statement).split()), dict(parameters or {})))
            return SimpleNamespace(first=lambda: None)

    report_window(Recording(), TENANT, JUNE)

    assert len(statements) == 1
    assert "FROM v2_cutovers" in statements[0][0]
    assert statements[0][1] == {"customer_id": CUSTOMER, "branch_id": BRANCH}


# =====================================================================================================================
# The hour boundaries of a week
# =====================================================================================================================

def test_a_weeks_hour_boundaries_are_each_days_twenty_four_then_the_last_days_end():
    days = [date(2026, 3, 7) + timedelta(days=offset) for offset in range(7)]      # includes the spring-forward day
    hours = [local_hour_boundaries(day, CHICAGO) for day in days]

    legacy = _week_boundaries(day.v1_boundaries_local for day in hours)
    current = _week_boundaries(day.v2_boundaries_utc for day in hours)

    assert len(legacy) == len(current) == 7 * 24 + 1
    assert legacy[0] == _local(2026, 3, 7) and legacy[-1] == _local(2026, 3, 14)
    assert current[0] == _utc(2026, 3, 7, 6) and current[-1] == _utc(2026, 3, 14, 5)
    assert list(legacy) == sorted(set(legacy))          # the legacy clock has every reading once
    assert list(current) == sorted(current)             # instants never go backwards...
    assert len(set(current)) == len(current) - 1        # ...and exactly one hour, the skipped one, is zero wide
    for index, day in enumerate(hours):
        assert current[index * 24:index * 24 + 24] == day.v2_boundaries_utc[:24]


# =====================================================================================================================
# Adding days up
# =====================================================================================================================

def test_a_ranges_destination_counts_are_its_days_added_up_destination_by_destination():
    routing = RoutingConfig(home_label="Main", home_keys=frozenset({"main"}),
                            transit=(TransitDestination("a", "A"), TransitDestination("b", "B")))
    days = (DestinationCounts(4, (2, 0), 0), DestinationCounts(3, (0, 1), 1), DestinationCounts(0, (0, 0), 0))

    total = sum_destination_counts(days, routing)

    assert total == DestinationCounts(home_count=7, transit_counts=(2, 1), other_count=1)
    assert (total.transit_count, total.total) == (3, 11)
    assert total.total == sum(day.total for day in days)
    assert sum_destination_counts((), routing) == DestinationCounts(0, (0, 0), 0)


# =====================================================================================================================
# The one statement shape, and what the module's SQL may not contain
# =====================================================================================================================

# What tests/test_operational_metrics_service.py forbids in the single-day statements, word for word.
FORBIDDEN_IN_SQL = ("AT TIME ZONE", "NOW()", "CURRENT_DATE", "CURRENT_TIMESTAMP", "::DATE", "TIMEZONE")


def test_the_forbidden_list_is_still_the_single_day_modules_own():
    here = Path(__file__)
    source = here.read_text(encoding="utf-8")
    single_day_guard = (here.parent / "test_operational_metrics_service.py").read_text(encoding="utf-8")

    assert 'for forbidden in ("AT TIME ZONE", "NOW()", "CURRENT_DATE", "CURRENT_TIMESTAMP", "::DATE", "TIMEZONE"):' in single_day_guard
    assert 'FORBIDDEN_IN_SQL = ("AT TIME ZONE", "NOW()", "CURRENT_DATE", "CURRENT_TIMESTAMP", "::DATE", "TIMEZONE")' in source


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.parametrize("buckets", [1, 2, 24, 92, 168])
def test_no_statement_depends_on_the_database_session_time_zone_or_reads_a_clock(source, buckets):
    sql = str(_bucket_count_statement(source, buckets)).upper()

    for forbidden in (*FORBIDDEN_IN_SQL, "DATE_TRUNC", "EXTRACT", "CAST(", "INTERVAL", "TO_CHAR", "STRFTIME", "EPOCH"):
        assert forbidden not in sql, (source.table, buckets, forbidden)
    for identifying in ("BARCODE", "TITLE", "ITEM_KEY", "EVENT_KEY", "KEY_ID", "PATRON", "JOIN", "DISTINCT", "ORDER BY"):
        assert identifying not in sql, (source.table, identifying)


def test_the_module_keeps_no_statement_text_other_than_the_one_builder():
    assert [name for name in vars(operational_report_service) if name.endswith("_SQL")] == []
    source = inspect.getsource(operational_report_service)
    code = "\n".join(line for line in source.split('"""', 2)[2].splitlines() if not line.lstrip().startswith("#"))

    assert code.count("SELECT") == 1 and code.count("text(") == 1


@pytest.mark.parametrize("source", _SOURCES)
def test_a_statement_is_the_single_day_where_clause_and_one_count_per_bucket(source):
    sql = " ".join(str(_bucket_count_statement(source, 3)).split())
    select, rest = sql.split(" FROM ", 1)
    grouped = source.group_column is not None

    assert rest.startswith(
        f"{source.table} WHERE customer_id = :customer_id AND branch_id = :branch_id "
        "AND event_time >= :span_start AND event_time < :span_end"
    )
    assert rest.endswith(f"GROUP BY {source.group_column}") is grouped
    assert select.count("COUNT(*) FILTER") == 3
    for bucket in range(3):
        assert f"(WHERE event_time >= :boundary_{bucket} AND event_time < :boundary_{bucket + 1})" in select
    assert select.startswith(f"SELECT {source.group_column}," if grouped else "SELECT COUNT(*)")


@pytest.mark.parametrize("source", _SOURCES)
def test_every_time_is_bound_with_the_type_its_table_keeps(source):
    statement = _bucket_count_statement(source, 2)
    times = {name: bind.type for name, bind in statement._bindparams.items() if name not in ("customer_id", "branch_id")}

    assert sorted(times) == ["boundary_0", "boundary_1", "boundary_2", "span_end", "span_start"]
    for bound_type in times.values():
        assert isinstance(bound_type, DateTime) and bound_type.timezone is source.aware


def test_the_sources_are_the_four_tables_in_both_forms_and_nothing_else():
    assert sorted((source.table, source.aware, source.group_column or "") for source in _SOURCES) == [
        ("checkin_events", True, ""), ("checkin_events", True, "destination"),
        ("checkins", False, ""), ("checkins", False, "destination"),
        ("reject_events", True, ""), ("reject_events", True, "error_class"),
        ("rejects", False, ""), ("rejects", False, "error_message"),
    ]


@pytest.mark.parametrize("source", [
    _Source("checkins; DROP TABLE checkins", aware=False),
    _Source("acs_events", aware=False),
    _Source("checkins", aware=False, group_column="barcode"),
    _Source("checkins", aware=True),                    # the right table, the wrong way of keeping time
    _Source("agent_tokens", aware=False, group_column="token_hash"),
])
def test_a_statement_cannot_be_built_for_any_other_table_or_column(source):
    with pytest.raises(ValueError, match="one of this module's own sources"):
        _bucket_count_statement(source, 1)


@pytest.mark.parametrize("buckets", [0, -1])
def test_a_statement_needs_at_least_one_bucket(buckets):
    with pytest.raises(ValueError, match="at least one bucket"):
        _bucket_count_statement(_SOURCES[0], buckets)


def test_the_module_reads_no_clock_no_environment_and_creates_no_engine():
    source = inspect.getsource(operational_report_service)
    code = "\n".join(line for line in source.split('"""', 2)[2].splitlines() if not line.lstrip().startswith("#"))

    for forbidden in ("datetime.now", "utcnow", ".today(", "time.time", "os.environ", "getenv", "localtime",
                      "get_engine", "create_engine", ".connect(", ".begin(", "set_config", "lru_cache", "ZoneInfo("):
        assert forbidden not in code, forbidden
    assert "astimezone()" not in code


def test_the_module_depends_only_on_the_standard_library_sqlalchemy_and_the_single_day_rules():
    imports = [line.strip() for line in inspect.getsource(operational_report_service).splitlines()
               if line.startswith(("import ", "from "))]

    assert imports == [
        "from __future__ import annotations",
        "import logging",
        "from collections.abc import Iterator, Sequence",
        "from dataclasses import dataclass",
        "from datetime import date, datetime, timedelta",
        "from zoneinfo import ZoneInfo",
        "from sqlalchemy import DateTime, bindparam, text",
        "from sqlalchemy.engine import Connection",
        "from sqlalchemy.sql.elements import TextClause",
        "from services.operational_metrics_service import (",
        "from services.reject_reason import (",
        "from services.routing_destination import RoutingConfig, destination_key",
        "from services.tenant_resolution_service import ResolvedOperationalTenant",
    ]


def test_the_single_day_module_was_not_changed_to_make_room_for_this_one():
    statements = sorted(name for name in vars(operational_metrics_service) if name.endswith("_SQL"))

    assert len(statements) == 11
    assert "report" not in inspect.getsource(operational_metrics_service).lower()
