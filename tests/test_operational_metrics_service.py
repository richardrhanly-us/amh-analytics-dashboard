"""Blocks 5a and 5b: the time and cutover rules behind customer-facing check-in
metrics, and the check-in count built on them.

    local_day_bounds(local_date, zone)       one local day, as naive-local bounds (v1) and UTC instants (v2)
    cutover_boundary(cutover_at, zone)       a cutover instant, in the same two forms
    get_effective_cutover(conn, tenant)      the tenant's current cutover, from v2_cutovers
    get_checkin_count(conn, tenant, ...)     check-ins on one local day, across both eras

The two helpers are pure. The lookup runs its REAL SQL against an in-memory
SQLite table; v2_cutovers has no row level security in production either, so
the statement's own tenant filter is exactly what is under test.

The dates used are real DST dates for
America/Chicago in 2026: clocks go forward on 8 March and back on 1 November.

Imported the "flat" way (services.operational_metrics_service), the identity
the API process uses.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from services import operational_metrics_service
from services.operational_metrics_service import (
    CutoverBoundary,
    LocalDayBounds,
    cutover_boundary,
    get_effective_cutover,
    local_day_bounds,
)
from services.tenant_resolution_service import ResolvedOperationalTenant

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

CHICAGO = ZoneInfo("America/Chicago")
KOLKATA = ZoneInfo("Asia/Kolkata")      # UTC+05:30, no DST
LONDON = ZoneInfo("Europe/London")      # DST on different dates from Chicago
HAVANA = ZoneInfo("America/Havana")     # changes its clocks AT local midnight

ORDINARY = date(2026, 6, 1)             # CDT, UTC-5
WINTER = date(2026, 1, 15)              # CST, UTC-6
SPRING_FORWARD = date(2026, 3, 8)       # 02:00 -> 03:00
FALL_BACK = date(2026, 11, 1)           # 02:00 -> 01:00


def _utc(year, month, day, hour=0, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _local(year, month, day, hour=0, minute=0, second=0) -> datetime:
    """A NAIVE local wall-clock datetime, exactly as a legacy checkins.event_time
    holds it. Having no tzinfo is the point: this is the one place in the file
    a datetime is built without one."""
    return datetime(year, month, day, hour, minute, second)  # noqa: DTZ001


# =====================================================================================================================
# local_day_bounds
# =====================================================================================================================

def test_an_ordinary_chicago_day():
    bounds = local_day_bounds(ORDINARY, CHICAGO)

    assert bounds == LocalDayBounds(
        local_date=date(2026, 6, 1),
        timezone_name="America/Chicago",
        v1_start_local=_local(2026, 6, 1, 0, 0),
        v1_end_local=_local(2026, 6, 2, 0, 0),
        v2_start_utc=_utc(2026, 6, 1, 5),       # local midnight, CDT
        v2_end_utc=_utc(2026, 6, 2, 5),         # the next local midnight
    )


def test_the_v1_bounds_are_naive_and_the_v2_bounds_are_aware_utc():
    bounds = local_day_bounds(ORDINARY, CHICAGO)

    assert bounds.v1_start_local.tzinfo is None
    assert bounds.v1_end_local.tzinfo is None
    assert bounds.v2_start_utc.tzinfo is UTC
    assert bounds.v2_end_utc.tzinfo is UTC
    assert bounds.v2_start_utc.utcoffset() == timedelta(0)


def test_the_v1_bounds_are_the_local_calendar_day_whatever_the_zone_or_season():
    for local_date in (ORDINARY, WINTER, SPRING_FORWARD, FALL_BACK):
        for zone in (CHICAGO, KOLKATA, LONDON, HAVANA, ZoneInfo("UTC")):
            bounds = local_day_bounds(local_date, zone)

            assert bounds.v1_start_local == _local(local_date.year, local_date.month, local_date.day)
            assert bounds.v1_end_local - bounds.v1_start_local == timedelta(days=1)


def test_an_ordinary_day_spans_24_real_hours_in_summer_and_in_winter():
    summer = local_day_bounds(ORDINARY, CHICAGO)
    winter = local_day_bounds(WINTER, CHICAGO)

    assert summer.v2_end_utc - summer.v2_start_utc == timedelta(hours=24)
    assert winter.v2_end_utc - winter.v2_start_utc == timedelta(hours=24)
    assert winter.v2_start_utc == _utc(2026, 1, 15, 6)   # local midnight, CST


def test_the_spring_forward_date_spans_23_real_hours():
    bounds = local_day_bounds(SPRING_FORWARD, CHICAGO)

    assert bounds.v2_start_utc == _utc(2026, 3, 8, 6)    # midnight is still CST
    assert bounds.v2_end_utc == _utc(2026, 3, 9, 5)      # the next midnight is CDT
    assert bounds.v2_end_utc - bounds.v2_start_utc == timedelta(hours=23)
    assert bounds.v1_end_local - bounds.v1_start_local == timedelta(days=1)  # still one calendar day of wall clock


def test_the_fall_back_date_spans_25_real_hours():
    bounds = local_day_bounds(FALL_BACK, CHICAGO)

    assert bounds.v2_start_utc == _utc(2026, 11, 1, 5)   # midnight is still CDT
    assert bounds.v2_end_utc == _utc(2026, 11, 2, 6)     # the next midnight is CST
    assert bounds.v2_end_utc - bounds.v2_start_utc == timedelta(hours=25)
    assert bounds.v1_end_local - bounds.v1_start_local == timedelta(days=1)


def test_consecutive_days_meet_exactly_with_no_gap_and_no_overlap():
    for zone in (CHICAGO, LONDON, HAVANA, KOLKATA):
        day = date(2026, 1, 1)
        while day < date(2027, 1, 1):
            today, tomorrow = local_day_bounds(day, zone), local_day_bounds(day + timedelta(days=1), zone)

            assert today.v2_end_utc == tomorrow.v2_start_utc, (zone.key, day)
            assert today.v1_end_local == tomorrow.v1_start_local, (zone.key, day)
            assert today.v2_start_utc < today.v2_end_utc, (zone.key, day)
            day += timedelta(days=1)


def test_a_utc_instant_falls_in_exactly_the_local_day_it_belongs_to():
    # 2026-06-02 01:30Z is 20:30 on 1 June in Chicago: an evening event that a
    # UTC-date comparison would put on the wrong day.
    instant = _utc(2026, 6, 2, 1, 30)

    june_1, june_2 = local_day_bounds(date(2026, 6, 1), CHICAGO), local_day_bounds(date(2026, 6, 2), CHICAGO)

    assert june_1.v2_start_utc <= instant < june_1.v2_end_utc
    assert not (june_2.v2_start_utc <= instant < june_2.v2_end_utc)


def test_another_zone_gives_its_own_bounds():
    kolkata = local_day_bounds(ORDINARY, KOLKATA)
    london = local_day_bounds(ORDINARY, LONDON)

    assert kolkata.timezone_name == "Asia/Kolkata"
    assert kolkata.v2_start_utc == _utc(2026, 5, 31, 18, 30)   # midnight IST is 18:30Z the day before
    assert kolkata.v2_end_utc == _utc(2026, 6, 1, 18, 30)
    assert london.v2_start_utc == _utc(2026, 5, 31, 23)        # midnight BST
    assert london.v2_start_utc != local_day_bounds(ORDINARY, CHICAGO).v2_start_utc


def test_a_zone_that_changes_its_clocks_at_midnight_still_gives_a_whole_day():
    # In Havana the clocks go forward at 00:00 on 8 March 2026 (00:00 does not
    # exist) and back at 01:00 on 1 November (00:00 happens twice).
    forward = local_day_bounds(date(2026, 3, 8), HAVANA)
    back = local_day_bounds(date(2026, 11, 1), HAVANA)

    assert forward.v2_end_utc - forward.v2_start_utc == timedelta(hours=23)
    assert back.v2_end_utc - back.v2_start_utc == timedelta(hours=25)


def test_the_bounds_are_an_immutable_value():
    bounds = local_day_bounds(ORDINARY, CHICAGO)

    assert [f.name for f in dataclasses.fields(LocalDayBounds)] == [
        "local_date", "timezone_name", "v1_start_local", "v1_end_local", "v2_start_utc", "v2_end_utc",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        bounds.v1_start_local = _local(2026, 1, 1)


# =====================================================================================================================
# cutover_boundary
# =====================================================================================================================

def test_a_summer_cutover_converts_with_the_daylight_offset():
    boundary = cutover_boundary(_utc(2026, 6, 10, 15, 0), CHICAGO)

    assert boundary == CutoverBoundary(
        timezone_name="America/Chicago",
        cutover_utc=_utc(2026, 6, 10, 15, 0),
        cutover_local_naive=_local(2026, 6, 10, 10, 0),   # UTC-5
    )


def test_a_winter_cutover_converts_with_the_standard_offset():
    boundary = cutover_boundary(_utc(2026, 1, 15, 15, 0), CHICAGO)

    assert boundary.cutover_local_naive == _local(2026, 1, 15, 9, 0)   # UTC-6


def test_the_local_form_is_naive_and_the_utc_form_is_aware():
    boundary = cutover_boundary(_utc(2026, 6, 10, 15, 0), CHICAGO)

    assert boundary.cutover_local_naive.tzinfo is None
    assert boundary.cutover_utc.tzinfo is UTC


def test_the_local_form_is_a_real_conversion_not_the_utc_clock_with_its_label_removed():
    cutover_at = _utc(2026, 6, 10, 3, 0)   # 22:00 on 9 June in Chicago

    boundary = cutover_boundary(cutover_at, CHICAGO)

    assert boundary.cutover_local_naive == _local(2026, 6, 9, 22, 0)          # a different DAY from the UTC date
    assert boundary.cutover_local_naive != cutover_at.replace(tzinfo=None)      # ...which is what stripping would give


def test_the_same_instant_given_with_another_offset_is_the_same_boundary():
    as_utc = cutover_boundary(_utc(2026, 6, 10, 15, 0), CHICAGO)
    as_local = cutover_boundary(datetime(2026, 6, 10, 10, 0, tzinfo=CHICAGO), CHICAGO)
    as_other = cutover_boundary(datetime(2026, 6, 10, 20, 30, tzinfo=KOLKATA), CHICAGO)

    assert as_utc == as_local == as_other


def test_a_naive_cutover_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="timezone-aware"):
        cutover_boundary(_local(2026, 6, 10, 15, 0), CHICAGO)


def test_another_zone_gives_its_own_local_boundary():
    boundary = cutover_boundary(_utc(2026, 6, 10, 15, 0), KOLKATA)

    assert boundary.timezone_name == "Asia/Kolkata"
    assert boundary.cutover_local_naive == _local(2026, 6, 10, 20, 30)


def test_the_partition_is_strictly_before_for_v1_and_at_or_after_for_v2():
    cutover_at = _utc(2026, 6, 10, 15, 0)
    boundary = cutover_boundary(cutover_at, CHICAGO)
    one_second = timedelta(seconds=1)

    def counted_as_v1(local_naive: datetime) -> bool:
        return local_naive < boundary.cutover_local_naive

    def counted_as_v2(instant: datetime) -> bool:
        return instant >= boundary.cutover_utc

    # The same three moments, as each table would hold them.
    for instant in (cutover_at - one_second, cutover_at, cutover_at + one_second):
        local_naive = instant.astimezone(CHICAGO).replace(tzinfo=None)
        # Exactly one era owns every moment: never both, never neither.
        assert counted_as_v1(local_naive) != counted_as_v2(instant), instant

    assert counted_as_v1(_local(2026, 6, 10, 9, 59, 59)) and not counted_as_v2(cutover_at - one_second)
    assert counted_as_v2(cutover_at) and not counted_as_v1(_local(2026, 6, 10, 10, 0, 0))   # exactly at: v2


def test_a_cutover_inside_a_local_day_splits_that_day_between_the_eras():
    day = local_day_bounds(date(2026, 6, 10), CHICAGO)
    boundary = cutover_boundary(_utc(2026, 6, 10, 15, 0), CHICAGO)

    # v1 owns [day start, cutover) in wall-clock terms; v2 owns [cutover, day end) as instants.
    assert day.v1_start_local < boundary.cutover_local_naive < day.v1_end_local
    assert day.v2_start_utc < boundary.cutover_utc < day.v2_end_utc
    v1_share = boundary.cutover_local_naive - day.v1_start_local
    v2_share = day.v2_end_utc - boundary.cutover_utc
    assert v1_share + v2_share == timedelta(hours=24)


def test_the_boundary_is_an_immutable_value():
    boundary = cutover_boundary(_utc(2026, 6, 10, 15, 0), CHICAGO)

    assert [f.name for f in dataclasses.fields(CutoverBoundary)] == [
        "timezone_name", "cutover_utc", "cutover_local_naive",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        boundary.cutover_utc = _utc(2026, 1, 1)


# --- the fall-back repeated hour: a limit of the legacy data, characterized -------------------------------------------

FIRST_0130 = _utc(2026, 11, 1, 6, 30)    # 01:30 CDT, before the clocks go back
SECOND_0130 = _utc(2026, 11, 1, 7, 30)   # 01:30 CST, one real hour later


def test_two_different_instants_in_the_repeated_hour_have_the_same_naive_local_value():
    assert SECOND_0130 - FIRST_0130 == timedelta(hours=1)

    first = FIRST_0130.astimezone(CHICAGO).replace(tzinfo=None)
    second = SECOND_0130.astimezone(CHICAGO).replace(tzinfo=None)

    # This is all a legacy v1 row stores. Nothing in it says which pass it was.
    assert first == second == _local(2026, 11, 1, 1, 30)


def test_a_cutover_inside_the_repeated_hour_cannot_place_every_v1_row_with_certainty():
    """LIMITATION, pinned on purpose. Cutovers an hour apart, in each pass of
    the repeated hour, produce the SAME v1 boundary -- so the v1 side cannot
    tell them apart, and for either cutover some legacy rows stamped in that
    hour land on the wrong side."""
    during_first_pass = cutover_boundary(FIRST_0130, CHICAGO)
    during_second_pass = cutover_boundary(SECOND_0130, CHICAGO)

    assert during_first_pass.cutover_utc != during_second_pass.cutover_utc                    # v2 side: exact
    assert during_first_pass.cutover_local_naive == during_second_pass.cutover_local_naive    # v1 side: identical
    assert during_first_pass.cutover_local_naive == _local(2026, 11, 1, 1, 30)

    # A legacy row stamped 01:15. It could be 06:15Z (first pass) or 07:15Z (second pass).
    stamped_0115 = _local(2026, 11, 1, 1, 15)
    could_be = (_utc(2026, 11, 1, 6, 15), _utc(2026, 11, 1, 7, 15))

    # The deterministic rule counts it as v1 for a cutover at the first 01:30...
    assert stamped_0115 < during_first_pass.cutover_local_naive
    # ...which is right if it was the first pass and wrong if it was the second:
    assert [instant < during_first_pass.cutover_utc for instant in could_be] == [True, False]

    # A legacy row stamped 01:45 is NOT counted as v1 for a cutover at the second 01:30...
    stamped_0145 = _local(2026, 11, 1, 1, 45)
    assert not stamped_0145 < during_second_pass.cutover_local_naive
    # ...although, if it was the first pass (06:45Z), it really was before that cutover (07:30Z).
    assert _utc(2026, 11, 1, 6, 45) < during_second_pass.cutover_utc


def test_outside_the_repeated_hour_the_fall_back_date_is_exact():
    # The same date, a cutover at 12:00 local: every wall-clock time that day
    # -- the repeated hour included -- is unambiguously before it.
    boundary = cutover_boundary(_utc(2026, 11, 1, 18, 0), CHICAGO)

    assert boundary.cutover_local_naive == _local(2026, 11, 1, 12, 0)
    for stamped in (_local(2026, 11, 1, 0, 59), _local(2026, 11, 1, 1, 30), _local(2026, 11, 1, 2, 0)):
        assert stamped < boundary.cutover_local_naive
    assert max(FIRST_0130, SECOND_0130) < boundary.cutover_utc


def test_a_spring_forward_cutover_has_no_such_ambiguity():
    # 02:00-02:59 never happens on 8 March, so no legacy row can be stamped in it.
    just_before = cutover_boundary(_utc(2026, 3, 8, 7, 59), CHICAGO)
    just_after = cutover_boundary(_utc(2026, 3, 8, 8, 0), CHICAGO)

    assert just_before.cutover_local_naive == _local(2026, 3, 8, 1, 59)
    assert just_after.cutover_local_naive == _local(2026, 3, 8, 3, 0)


# =====================================================================================================================
# get_effective_cutover
# =====================================================================================================================

_DDL = """
    CREATE TABLE v2_cutovers (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER,
        branch_id INTEGER,
        cutover_at TEXT,
        set_by TEXT,
        set_at TEXT,
        note TEXT
    )
"""

CUSTOMER_A, BRANCH_A = 8101, 11
CUSTOMER_B, BRANCH_B = 8202, 21
TENANT_A = ResolvedOperationalTenant(
    org_slug="acme", branch_slug="main", access_mode="full",
    operational_customer_id=CUSTOMER_A, operational_branch_id=BRANCH_A,
)


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(_DDL))
    yield engine
    engine.dispose()


def _record(engine, cutover_at, set_at, *, customer_id=CUSTOMER_A, branch_id=BRANCH_A) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, set_at) "
                 "VALUES (:c, :b, :cutover_at, 'operator', :set_at)"),
            {"c": customer_id, "b": branch_id, "cutover_at": cutover_at, "set_at": set_at},
        )


def _effective(engine, tenant=TENANT_A):
    with engine.connect() as conn:
        return get_effective_cutover(conn, tenant)


def test_a_branch_with_no_cutover_row_has_none(engine):
    assert _effective(engine) is None


def test_the_recorded_cutover_is_returned_as_an_aware_utc_instant(engine):
    _record(engine, "2026-06-10 15:00:00+00:00", "2026-06-09 12:00:00+00:00")

    cutover = _effective(engine)

    assert cutover == _utc(2026, 6, 10, 15, 0)
    assert cutover.tzinfo is UTC


def test_a_cutover_stored_with_another_offset_is_normalized_to_utc(engine):
    _record(engine, "2026-06-10 10:00:00-05:00", "2026-06-09 12:00:00+00:00")

    cutover = _effective(engine)

    assert cutover == _utc(2026, 6, 10, 15, 0)
    assert cutover.utcoffset() == timedelta(0)


def test_the_latest_row_by_set_at_wins(engine):
    _record(engine, "2026-06-01 05:00:00+00:00", "2026-05-30 12:00:00+00:00")
    _record(engine, "2026-07-01 05:00:00+00:00", "2026-06-25 12:00:00+00:00")   # recorded last
    _record(engine, "2026-08-01 05:00:00+00:00", "2026-06-10 12:00:00+00:00")   # later cutover, recorded earlier

    assert _effective(engine) == _utc(2026, 7, 1, 5)


def test_a_latest_row_with_a_null_cutover_is_a_rollback(engine):
    _record(engine, "2026-06-01 05:00:00+00:00", "2026-05-30 12:00:00+00:00")
    _record(engine, None, "2026-06-05 12:00:00+00:00")

    assert _effective(engine) is None


def test_a_cutover_recorded_again_after_a_rollback_is_effective(engine):
    _record(engine, "2026-06-01 05:00:00+00:00", "2026-05-30 12:00:00+00:00")
    _record(engine, None, "2026-06-05 12:00:00+00:00")
    _record(engine, "2026-06-20 05:00:00+00:00", "2026-06-15 12:00:00+00:00")

    assert _effective(engine) == _utc(2026, 6, 20, 5)


def test_another_customers_later_cutover_never_wins(engine):
    _record(engine, "2026-06-01 05:00:00+00:00", "2026-05-30 12:00:00+00:00")
    _record(engine, "2026-09-01 05:00:00+00:00", "2026-08-30 12:00:00+00:00", customer_id=CUSTOMER_B)

    assert _effective(engine) == _utc(2026, 6, 1, 5)


def test_another_branchs_later_cutover_never_wins(engine):
    _record(engine, "2026-06-01 05:00:00+00:00", "2026-05-30 12:00:00+00:00")
    _record(engine, "2026-09-01 05:00:00+00:00", "2026-08-30 12:00:00+00:00", branch_id=BRANCH_B)

    assert _effective(engine) == _utc(2026, 6, 1, 5)


def test_a_tenant_with_no_row_of_its_own_has_none_whatever_other_tenants_have(engine):
    _record(engine, "2026-09-01 05:00:00+00:00", "2026-08-30 12:00:00+00:00", customer_id=CUSTOMER_B, branch_id=BRANCH_B)
    _record(engine, "2026-09-01 05:00:00+00:00", "2026-08-30 12:00:00+00:00", customer_id=CUSTOMER_A, branch_id=BRANCH_B)
    _record(engine, "2026-09-01 05:00:00+00:00", "2026-08-30 12:00:00+00:00", customer_id=CUSTOMER_B, branch_id=BRANCH_A)

    assert _effective(engine) is None


def test_another_tenants_rollback_does_not_cancel_this_tenants_cutover(engine):
    _record(engine, "2026-06-01 05:00:00+00:00", "2026-05-30 12:00:00+00:00")
    _record(engine, None, "2026-08-30 12:00:00+00:00", customer_id=CUSTOMER_B, branch_id=BRANCH_B)

    assert _effective(engine) == _utc(2026, 6, 1, 5)


def test_each_tenant_reads_its_own_cutover(engine):
    tenant_b = ResolvedOperationalTenant(
        org_slug="beta", branch_slug="main", access_mode="read_only",
        operational_customer_id=CUSTOMER_B, operational_branch_id=BRANCH_B,
    )
    _record(engine, "2026-06-01 05:00:00+00:00", "2026-05-30 12:00:00+00:00")
    _record(engine, "2026-09-01 05:00:00+00:00", "2026-08-30 12:00:00+00:00", customer_id=CUSTOMER_B, branch_id=BRANCH_B)

    assert _effective(engine, TENANT_A) == _utc(2026, 6, 1, 5)
    assert _effective(engine, tenant_b) == _utc(2026, 9, 1, 5)


def test_a_cutover_in_the_future_is_still_the_effective_cutover(engine):
    # Whether it has taken effect yet is for whoever uses the boundary; the lookup does not consult the clock.
    far_future = "2099-01-01 06:00:00+00:00"
    _record(engine, far_future, "2026-06-09 12:00:00+00:00")

    assert _effective(engine) == _utc(2099, 1, 1, 6)


def test_a_stored_cutover_with_no_offset_is_refused_rather_than_guessed(engine):
    _record(engine, "2026-06-10 15:00:00", "2026-06-09 12:00:00+00:00")

    with pytest.raises(ValueError, match="timezone-aware"):
        _effective(engine)


def test_the_tenant_ids_are_bound_from_the_resolved_tenant():
    class RecordingConnection:
        def __init__(self):
            self.calls = []

        def execute(self, statement, parameters=None):
            self.calls.append((statement, parameters))
            return self

        def first(self):
            return None

    conn = RecordingConnection()

    assert get_effective_cutover(conn, TENANT_A) is None
    ((statement, parameters),) = conn.calls   # exactly one statement, on the connection that was passed in
    assert parameters == {"customer_id": CUSTOMER_A, "branch_id": BRANCH_A}
    assert statement is operational_metrics_service._EFFECTIVE_CUTOVER_SQL


def test_the_lookup_statement_has_the_approved_shape():
    sql = " ".join(str(operational_metrics_service._EFFECTIVE_CUTOVER_SQL).split())

    assert sql == (
        "SELECT cutover_at FROM v2_cutovers "
        "WHERE customer_id = :customer_id AND branch_id = :branch_id "
        "ORDER BY set_at DESC LIMIT 1"
    )


def test_a_failing_query_propagates_instead_of_meaning_no_cutover(engine):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE v2_cutovers"))

    with pytest.raises(Exception, match="v2_cutovers"):
        _effective(engine)


def test_the_callers_own_database_error_passes_through_unchanged():
    class SyntheticDatabaseError(Exception):
        pass

    class BrokenConnection:
        def execute(self, *_args, **_kwargs):
            raise SyntheticDatabaseError("synthetic database failure")

    with pytest.raises(SyntheticDatabaseError, match="synthetic database failure"):
        get_effective_cutover(BrokenConnection(), TENANT_A)


def test_nothing_is_cached_between_lookups(engine):
    assert _effective(engine) is None

    _record(engine, "2026-06-01 05:00:00+00:00", "2026-05-30 12:00:00+00:00")
    assert _effective(engine) == _utc(2026, 6, 1, 5)

    _record(engine, None, "2026-06-05 12:00:00+00:00")
    assert _effective(engine) is None


# =====================================================================================================================
# The lookup and the conversion together
# =====================================================================================================================

def test_a_looked_up_cutover_converts_to_the_correct_local_boundary(engine):
    _record(engine, "2026-06-10 15:00:00+00:00", "2026-06-09 12:00:00+00:00")

    boundary = cutover_boundary(_effective(engine), CHICAGO)

    assert boundary.cutover_utc == _utc(2026, 6, 10, 15, 0)
    assert boundary.cutover_local_naive == _local(2026, 6, 10, 10, 0)


# =====================================================================================================================
# How this differs from the Streamlit dashboard's mixed-era path (characterization only)
# =====================================================================================================================

def test_the_dashboard_labels_a_naive_v1_time_as_utc_and_this_module_does_not():
    """The dashboard's mixed_era_service is NOT changed by Block 5. This pins
    the difference so it is a known one: once a branch has a cutover, the
    dashboard labels a legacy naive local time as UTC and then converts it to
    Central, moving it 5-6 hours earlier. A check-in at 02:00 local on 1 June
    is then shown on 31 May. The bounds here compare the naive value as the
    local wall-clock time it is, so the same row stays on 1 June."""
    import pandas as pd

    import metrics
    from services import mixed_era_service

    stamped = _local(2026, 6, 1, 2, 0)   # a legacy row: 02:00 local, naive
    v1 = pd.DataFrame({"datetime": [pd.Timestamp(stamped)], "barcode": ["synthetic"]})
    cutover = pd.Timestamp("2026-06-10T05:00:00Z")

    # The dashboard today, for a cut-over branch:
    mixed = mixed_era_service._build_mixed_checkins(v1, pd.DataFrame(), cutover)
    assert len(metrics.get_date_filtered_df(mixed, date(2026, 6, 1), date(2026, 6, 1))) == 0
    assert len(metrics.get_date_filtered_df(mixed, date(2026, 5, 31), date(2026, 5, 31))) == 1

    # This module's bounds for the same two days:
    june_1, may_31 = local_day_bounds(date(2026, 6, 1), CHICAGO), local_day_bounds(date(2026, 5, 31), CHICAGO)
    assert june_1.v1_start_local <= stamped < june_1.v1_end_local
    assert not (may_31.v1_start_local <= stamped < may_31.v1_end_local)

    # ...and, with no cutover, the dashboard agrees with them.
    v1_only = mixed_era_service._build_mixed_checkins(v1, pd.DataFrame(), None)
    assert len(metrics.get_date_filtered_df(v1_only, date(2026, 6, 1), date(2026, 6, 1))) == 1


# =====================================================================================================================
# Block 5b: get_checkin_count
# =====================================================================================================================
#
# The real SQL, against in-memory SQLite. The database holds ONLY the three
# tables the count may touch -- checkins, checkin_events, v2_cutovers -- so a
# statement reaching for rejects or ACS data would fail outright.
#
# SQLite stores a timestamp as text and compares it as text. Rows are
# therefore written in exactly the form SQLAlchemy renders a datetime bind in
# for SQLite, so a text comparison orders them the way PostgreSQL orders the
# real values: v1 rows as the naive local wall-clock time, v2 rows as the UTC
# instant.

_COUNT_DDL = (
    (
        "CREATE TABLE checkins (id INTEGER PRIMARY KEY, customer_id INTEGER, branch_id INTEGER, event_time TEXT, "
        "barcode TEXT, title TEXT)"
    ),
    (
        "CREATE TABLE checkin_events (id INTEGER PRIMARY KEY, customer_id INTEGER, branch_id INTEGER, key_id TEXT, "
        "event_key TEXT, event_time TEXT, item_key TEXT)"
    ),
    _DDL,   # v2_cutovers, as above
)
_STORED = "%Y-%m-%d %H:%M:%S.%f"

JUNE_10 = date(2026, 6, 10)
NOON_CUTOVER = _utc(2026, 6, 10, 17, 0)   # 12:00 local on 10 June (CDT)


class Recorder:
    """Wraps the caller's connection: records every statement with the
    Python-level parameters it was given, and can fail a chosen statement."""

    def __init__(self, conn, fail_on: str | None = None):
        self._conn = conn
        self.fail_on = fail_on
        self.statements: list[tuple[str, dict]] = []

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        self.statements.append((sql, dict(parameters or {})))
        if self.fail_on and f"FROM {self.fail_on} " in sql:
            raise RuntimeError(f"synthetic failure reading {self.fail_on}")
        return self._conn.execute(statement, parameters)

    def tables(self) -> list[str]:
        return [sql.split(" FROM ")[1].split(" ")[0] for sql, _ in self.statements]

    def parameters_for(self, table: str) -> dict:
        (found,) = [parameters for sql, parameters in self.statements if f"FROM {table} " in sql]
        return found


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in _COUNT_DDL:
            conn.execute(text(statement))
    yield engine
    engine.dispose()


def _v1(db, *stamped: datetime, customer_id=CUSTOMER_A, branch_id=BRANCH_A, barcode="synthetic") -> None:
    """Legacy rows: each `stamped` is a NAIVE local wall-clock time."""
    with db.begin() as conn:
        for local_naive in stamped:
            assert local_naive.tzinfo is None
            conn.execute(
                text("INSERT INTO checkins (customer_id, branch_id, event_time, barcode) VALUES (:c, :b, :t, :bc)"),
                {"c": customer_id, "b": branch_id, "t": local_naive.strftime(_STORED), "bc": barcode},
            )


def _v2(db, *instants: datetime, customer_id=CUSTOMER_A, branch_id=BRANCH_A) -> None:
    """Contract v2 rows: each instant is aware; it is stored as UTC."""
    with db.begin() as conn:
        for instant in instants:
            assert instant.tzinfo is not None
            conn.execute(
                text("INSERT INTO checkin_events (customer_id, branch_id, event_time) VALUES (:c, :b, :t)"),
                {"c": customer_id, "b": branch_id, "t": instant.astimezone(UTC).strftime(_STORED)},
            )


def _cutover(db, cutover_at: datetime | None, *, set_at="2026-01-01 00:00:00+00:00", **scope) -> None:
    _record(db, None if cutover_at is None else cutover_at.isoformat(sep=" "), set_at, **scope)


def _count(db, local_date=JUNE_10, *, tenant=TENANT_A, zone=CHICAGO, fail_on=None):
    with db.connect() as conn:
        recorder = Recorder(conn, fail_on=fail_on)
        result = operational_metrics_service.get_checkin_count(recorder, tenant, local_date=local_date, zone=zone)
    return result, recorder


def _counts(db, local_date=JUNE_10, **kwargs) -> tuple[int, int, int]:
    result, _ = _count(db, local_date, **kwargs)
    return result.total, result.v1_count, result.v2_count


# --- no cutover: the branch is v1 only --------------------------------------------------------------------------------

def test_a_v1_only_branch_counts_the_rows_of_the_local_day(db):
    _v1(db, _local(2026, 6, 9, 23, 59, 59), _local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 9, 30),
        _local(2026, 6, 10, 23, 59, 59), _local(2026, 6, 11, 0, 0))

    result, recorder = _count(db)

    assert result == operational_metrics_service.CheckinCount(total=3, v1_count=3, v2_count=0)
    assert recorder.tables() == ["v2_cutovers", "checkins"]   # the v2 table is never read


def test_with_no_cutover_v2_rows_are_ignored_even_if_they_exist(db):
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 15, 0), _utc(2026, 6, 10, 16, 0))

    result, recorder = _count(db)

    assert (result.total, result.v1_count, result.v2_count) == (1, 1, 0)
    assert "checkin_events" not in recorder.tables()


def test_a_tenant_with_no_rows_has_a_count_of_zero(db):
    result, recorder = _count(db)

    assert result == operational_metrics_service.CheckinCount(total=0, v1_count=0, v2_count=0)
    assert recorder.tables() == ["v2_cutovers", "checkins"]


def test_a_rollback_makes_the_branch_v1_only_again(db):
    _v1(db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 15, 0))
    _v2(db, _utc(2026, 6, 10, 20, 0))
    _cutover(db, NOON_CUTOVER, set_at="2026-06-01 00:00:00+00:00")
    _cutover(db, None, set_at="2026-06-12 00:00:00+00:00")   # the latest record: a rollback

    result, recorder = _count(db)

    assert (result.total, result.v1_count, result.v2_count) == (2, 2, 0)
    assert recorder.tables() == ["v2_cutovers", "checkins"]


def test_every_row_is_counted_with_no_deduplication(db):
    # The same item three times at the same second is three rows, and three check-ins.
    _v1(db, *[_local(2026, 6, 10, 9, 0)] * 3, barcode="same-barcode")
    _cutover(db, NOON_CUTOVER)
    _v2(db, *[_utc(2026, 6, 10, 18, 0)] * 2)

    assert _counts(db) == (5, 3, 2)


# --- the cutover's position relative to the requested day -------------------------------------------------------------

def test_a_cutover_inside_the_day_splits_it_between_the_eras(db):
    _cutover(db, NOON_CUTOVER)
    # v1, local wall clock: two before noon count; one at noon and one after do not (v1 is strictly before).
    _v1(db, _local(2026, 6, 10, 8, 0), _local(2026, 6, 10, 11, 59, 59), _local(2026, 6, 10, 12, 0),
        _local(2026, 6, 10, 15, 0))
    # v2, instants: one before the cutover does not count; at it and after it do.
    _v2(db, _utc(2026, 6, 10, 16, 59, 59), _utc(2026, 6, 10, 17, 0), _utc(2026, 6, 10, 22, 0),
        _utc(2026, 6, 11, 4, 59, 59), _utc(2026, 6, 11, 5, 0))

    result, recorder = _count(db)

    assert (result.total, result.v1_count, result.v2_count) == (5, 2, 3)
    assert recorder.tables() == ["v2_cutovers", "checkins", "checkin_events"]


def test_exactly_at_the_cutover_belongs_to_v2_and_one_second_before_to_v1(db):
    _cutover(db, NOON_CUTOVER)
    _v1(db, _local(2026, 6, 10, 11, 59, 59))          # one second before, as v1 holds it
    _v1(db, _local(2026, 6, 10, 12, 0, 0))            # the cutover moment as a v1 row: not v1's
    _v2(db, _utc(2026, 6, 10, 16, 59, 59))            # one second before, as v2 holds it: not v2's
    _v2(db, _utc(2026, 6, 10, 17, 0, 0))              # the cutover moment as a v2 row

    assert _counts(db) == (2, 1, 1)


def test_a_cutover_before_the_day_makes_it_a_v2_day(db):
    _cutover(db, _utc(2026, 6, 1, 17, 0))
    _v1(db, _local(2026, 6, 10, 9, 0))                # a legacy row after the cutover: not counted
    _v2(db, _utc(2026, 6, 10, 5, 0), _utc(2026, 6, 10, 15, 0), _utc(2026, 6, 11, 4, 59, 59))
    _v2(db, _utc(2026, 6, 10, 4, 59, 59), _utc(2026, 6, 11, 5, 0))   # just outside the local day

    result, recorder = _count(db)

    assert (result.total, result.v1_count, result.v2_count) == (3, 0, 3)
    assert recorder.tables() == ["v2_cutovers", "checkin_events"]    # no v1 query at all


def test_a_cutover_after_the_day_leaves_it_a_v1_day(db):
    _cutover(db, _utc(2026, 6, 20, 17, 0))
    _v1(db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 21, 0))
    _v2(db, _utc(2026, 6, 10, 15, 0))                 # a v2 row before the cutover: not counted

    result, recorder = _count(db)

    assert (result.total, result.v1_count, result.v2_count) == (2, 2, 0)
    assert recorder.tables() == ["v2_cutovers", "checkins"]          # no v2 query at all


def test_a_cutover_at_the_local_midnight_that_starts_the_day_gives_the_whole_day_to_v2(db):
    _cutover(db, _utc(2026, 6, 10, 5, 0))             # 00:00 local on 10 June
    _v1(db, _local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 5, 0), _utc(2026, 6, 10, 15, 0))

    result, recorder = _count(db)

    assert (result.total, result.v1_count, result.v2_count) == (2, 0, 2)
    assert recorder.tables() == ["v2_cutovers", "checkin_events"]


def test_a_cutover_at_the_next_local_midnight_gives_the_whole_day_to_v1(db):
    _cutover(db, _utc(2026, 6, 11, 5, 0))             # 00:00 local on 11 June
    _v1(db, _local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 23, 59, 59))
    _v2(db, _utc(2026, 6, 10, 15, 0), _utc(2026, 6, 11, 5, 0))

    result, recorder = _count(db)

    assert (result.total, result.v1_count, result.v2_count) == (2, 2, 0)
    assert recorder.tables() == ["v2_cutovers", "checkins"]

    # ...and the day after is entirely v2's.
    assert _counts(db, date(2026, 6, 11)) == (1, 0, 1)


def test_a_cutover_in_the_future_counts_only_v1_before_it(db):
    _cutover(db, _utc(2099, 1, 1, 6, 0))
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 15, 0))

    assert _counts(db) == (1, 1, 0)


def test_a_cutover_with_no_v2_rows_yet_counts_v1_before_it_and_nothing_after(db):
    _cutover(db, NOON_CUTOVER)
    _v1(db, _local(2026, 6, 10, 8, 0), _local(2026, 6, 10, 14, 0))

    assert _counts(db) == (1, 1, 0)


def test_a_cutover_with_no_v1_rows_counts_v2_from_it(db):
    _cutover(db, NOON_CUTOVER)
    _v2(db, _utc(2026, 6, 10, 16, 0), _utc(2026, 6, 10, 18, 0))

    assert _counts(db) == (1, 0, 1)


def test_the_days_around_a_cutover_add_up_with_nothing_lost_and_nothing_counted_twice(db):
    _cutover(db, NOON_CUTOVER)
    v1_rows = [_local(2026, 6, 9, 10, 0), _local(2026, 6, 9, 23, 30), _local(2026, 6, 10, 0, 30),
               _local(2026, 6, 10, 11, 0)]
    v2_rows = [_utc(2026, 6, 10, 17, 0), _utc(2026, 6, 10, 23, 0), _utc(2026, 6, 11, 4, 0), _utc(2026, 6, 11, 5, 0),
               _utc(2026, 6, 11, 20, 0)]
    _v1(db, *v1_rows)
    _v2(db, *v2_rows)

    per_day = [_counts(db, day) for day in (date(2026, 6, 9), date(2026, 6, 10), date(2026, 6, 11))]

    assert per_day == [(2, 2, 0), (5, 2, 3), (2, 0, 2)]
    assert sum(total for total, _, _ in per_day) == len(v1_rows) + len(v2_rows)


# --- time zones and DST -----------------------------------------------------------------------------------------------

def test_an_evening_v2_event_counts_on_its_local_day_not_its_utc_day(db):
    _cutover(db, _utc(2026, 6, 1, 5, 0))
    _v2(db, _utc(2026, 6, 11, 1, 30))                 # 20:30 on 10 June in Chicago; 11 June in UTC

    assert _counts(db, date(2026, 6, 10)) == (1, 0, 1)
    assert _counts(db, date(2026, 6, 11)) == (0, 0, 0)


def test_the_spring_forward_date_is_one_calendar_day_of_v1_and_23_hours_of_v2(db):
    # v1-only: every wall-clock time on 8 March, none on either side.
    _v1(db, _local(2026, 3, 7, 23, 59, 59), _local(2026, 3, 8, 0, 0), _local(2026, 3, 8, 1, 59),
        _local(2026, 3, 8, 3, 0), _local(2026, 3, 8, 23, 59, 59), _local(2026, 3, 9, 0, 0))
    assert _counts(db, SPRING_FORWARD) == (4, 4, 0)

    # Once cut over before that date: the day is [06:00Z, 05:00Z next day) -- 23 hours.
    _cutover(db, _utc(2026, 3, 1, 6, 0))
    _v2(db, _utc(2026, 3, 8, 5, 59, 59), _utc(2026, 3, 8, 6, 0), _utc(2026, 3, 9, 4, 59, 59), _utc(2026, 3, 9, 5, 0))

    result, recorder = _count(db, SPRING_FORWARD)

    assert (result.total, result.v1_count, result.v2_count) == (2, 0, 2)
    parameters = recorder.parameters_for("checkin_events")
    assert parameters["end_utc"] - parameters["start_utc"] == timedelta(hours=23)


def test_a_cutover_inside_the_spring_forward_date_partitions_it_correctly(db):
    _cutover(db, _utc(2026, 3, 8, 15, 0))             # 10:00 CDT, after the clocks went forward
    _v1(db, _local(2026, 3, 8, 1, 30), _local(2026, 3, 8, 9, 59, 59), _local(2026, 3, 8, 10, 0))
    _v2(db, _utc(2026, 3, 8, 14, 59, 59), _utc(2026, 3, 8, 15, 0), _utc(2026, 3, 9, 4, 0))

    result, recorder = _count(db, SPRING_FORWARD)

    assert (result.total, result.v1_count, result.v2_count) == (4, 2, 2)
    assert recorder.parameters_for("checkins")["end_local"] == _local(2026, 3, 8, 10, 0)


def test_the_fall_back_date_is_one_calendar_day_of_v1_and_25_hours_of_v2(db):
    # v1-only: both passes through 01:30 are just rows stamped 01:30 on 1 November.
    _v1(db, _local(2026, 10, 31, 23, 59, 59), _local(2026, 11, 1, 0, 0), _local(2026, 11, 1, 1, 30),
        _local(2026, 11, 1, 1, 30), _local(2026, 11, 1, 23, 59, 59), _local(2026, 11, 2, 0, 0))
    assert _counts(db, FALL_BACK) == (4, 4, 0)

    # Once cut over before that date: the day is [05:00Z, 06:00Z next day) -- 25 hours.
    _cutover(db, _utc(2026, 10, 1, 5, 0))
    _v2(db, _utc(2026, 11, 1, 4, 59, 59), _utc(2026, 11, 1, 5, 0), FIRST_0130, SECOND_0130,
        _utc(2026, 11, 2, 5, 59, 59), _utc(2026, 11, 2, 6, 0))

    result, recorder = _count(db, FALL_BACK)

    assert (result.total, result.v1_count, result.v2_count) == (4, 0, 4)   # both real 01:30s are counted, once each
    parameters = recorder.parameters_for("checkin_events")
    assert parameters["end_utc"] - parameters["start_utc"] == timedelta(hours=25)


def test_a_cutover_on_the_fall_back_date_outside_the_repeated_hour_is_exact(db):
    _cutover(db, _utc(2026, 11, 1, 18, 0))            # 12:00 CST
    _v1(db, _local(2026, 11, 1, 1, 30), _local(2026, 11, 1, 1, 30), _local(2026, 11, 1, 11, 59, 59),
        _local(2026, 11, 1, 12, 0))
    _v2(db, _utc(2026, 11, 1, 17, 59, 59), _utc(2026, 11, 1, 18, 0), _utc(2026, 11, 2, 5, 0))

    assert _counts(db, FALL_BACK) == (5, 3, 2)


def test_a_cutover_inside_the_repeated_hour_follows_the_deterministic_wall_clock_rule(db):
    """The 5a limitation, at the level of a count. Legacy rows stamped in the
    repeated hour are split by their wall-clock value alone -- 'before 01:30'
    counts, '01:30 or later' does not -- whichever pass each really came
    from. The answer is the same for a cutover in either pass, and v2's side
    of the boundary is exact in both."""
    _v1(db, _local(2026, 11, 1, 0, 45), _local(2026, 11, 1, 1, 15), _local(2026, 11, 1, 1, 15),
        _local(2026, 11, 1, 1, 45))
    _v2(db, _utc(2026, 11, 1, 6, 45), _utc(2026, 11, 1, 7, 45), _utc(2026, 11, 1, 9, 0))

    _cutover(db, FIRST_0130, set_at="2026-10-01 00:00:00+00:00")
    first_pass, recorder = _count(db, FALL_BACK)
    assert recorder.parameters_for("checkins")["end_local"] == _local(2026, 11, 1, 1, 30)
    assert (first_pass.v1_count, first_pass.v2_count) == (3, 3)   # v2: 06:45Z, 07:45Z and 09:00Z are all >= 06:30Z

    _cutover(db, SECOND_0130, set_at="2026-10-02 00:00:00+00:00")
    second_pass, recorder = _count(db, FALL_BACK)
    assert recorder.parameters_for("checkins")["end_local"] == _local(2026, 11, 1, 1, 30)   # the same v1 boundary
    assert (second_pass.v1_count, second_pass.v2_count) == (3, 2)  # v2: only 07:45Z and 09:00Z are >= 07:30Z

    assert first_pass.v1_count == second_pass.v1_count  # v1 cannot tell the two cutovers apart


def test_the_count_is_made_in_the_zone_it_is_given(db):
    _v1(db, _local(2026, 6, 10, 0, 30), _local(2026, 6, 10, 23, 30))
    _cutover(db, _utc(2026, 6, 10, 12, 0))            # 07:00 in Chicago, 17:30 in Kolkata
    _v2(db, _utc(2026, 6, 10, 12, 0), _utc(2026, 6, 10, 20, 0))

    # Chicago: v1 before 07:00 local -> 1; v2 from 12:00Z to 05:00Z next day -> 2.
    assert _counts(db, zone=CHICAGO) == (3, 1, 2)
    # Kolkata: v1 before 17:30 local -> 1; the local day ends at 18:30Z, so only the 12:00Z row is v2's.
    assert _counts(db, zone=KOLKATA) == (2, 1, 1)


# --- the tenant filter (SQLite has no RLS: only the statements' own WHERE separates these rows) -----------------------

def test_another_customers_rows_are_never_counted(db):
    _cutover(db, NOON_CUTOVER)
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 18, 0))
    _v1(db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0), customer_id=CUSTOMER_B)
    _v2(db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 19, 0), customer_id=CUSTOMER_B)

    assert _counts(db) == (2, 1, 1)


def test_another_branchs_rows_are_never_counted(db):
    _cutover(db, NOON_CUTOVER)
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 18, 0))
    _v1(db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0), branch_id=BRANCH_B)
    _v2(db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 19, 0), branch_id=BRANCH_B)

    assert _counts(db) == (2, 1, 1)


def test_another_tenants_cutover_does_not_change_this_tenants_count(db):
    _cutover(db, _utc(2026, 6, 1, 5, 0), customer_id=CUSTOMER_B, branch_id=BRANCH_B)
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 18, 0))

    assert _counts(db) == (1, 1, 0)   # this tenant has no cutover: v1 only


def test_each_tenant_gets_its_own_count_from_the_same_tables(db):
    tenant_b = ResolvedOperationalTenant(
        org_slug="beta", branch_slug="main", access_mode="read_only",
        operational_customer_id=CUSTOMER_B, operational_branch_id=BRANCH_B,
    )
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v1(db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0), _local(2026, 6, 10, 11, 0),
        customer_id=CUSTOMER_B, branch_id=BRANCH_B)

    assert _counts(db, tenant=TENANT_A) == (1, 1, 0)
    assert _counts(db, tenant=tenant_b) == (3, 3, 0)   # a read_only tenant counts like any other


def test_every_statement_is_bound_to_the_resolved_tenants_ids(db):
    _cutover(db, NOON_CUTOVER)

    _, recorder = _count(db)

    assert len(recorder.statements) == 3
    for sql, parameters in recorder.statements:
        assert "customer_id = :customer_id AND branch_id = :branch_id" in sql
        assert parameters["customer_id"] == CUSTOMER_A
        assert parameters["branch_id"] == BRANCH_A


# --- the statements and their parameters ------------------------------------------------------------------------------

def _statement(name: str) -> str:
    return " ".join(str(getattr(operational_metrics_service, name)).split())


def test_the_v1_statement_has_the_approved_shape():
    assert _statement("_V1_CHECKIN_COUNT_SQL") == (
        "SELECT COUNT(*) FROM checkins "
        "WHERE customer_id = :customer_id AND branch_id = :branch_id "
        "AND event_time >= :start_local AND event_time < :end_local"   # strictly before its upper bound
    )


def test_the_v2_statement_has_the_approved_shape():
    assert _statement("_V2_CHECKIN_COUNT_SQL") == (
        "SELECT COUNT(*) FROM checkin_events "
        "WHERE customer_id = :customer_id AND branch_id = :branch_id "
        "AND event_time >= :start_utc AND event_time < :end_utc"      # from its lower bound, inclusive
    )


def test_the_count_statements_only_count_rows_of_one_table_each():
    for name in ("_V1_CHECKIN_COUNT_SQL", "_V2_CHECKIN_COUNT_SQL"):
        sql = _statement(name).upper()

        assert sql.startswith("SELECT COUNT(*) FROM ")
        assert sql.count(" FROM ") == 1
        for forbidden in ("JOIN", "DISTINCT", "GROUP BY", "SELECT *", "REJECT", "ACS", "AT TIME ZONE", "NOW()",
                          "::DATE", "CURRENT_"):
            assert forbidden not in sql, (name, forbidden)


def test_the_count_binds_naive_v1_bounds_and_aware_utc_v2_bounds(db):
    _cutover(db, NOON_CUTOVER)

    _, recorder = _count(db)

    v1, v2 = recorder.parameters_for("checkins"), recorder.parameters_for("checkin_events")
    assert (v1["start_local"], v1["end_local"]) == (_local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 12, 0))
    assert v1["start_local"].tzinfo is None and v1["end_local"].tzinfo is None
    assert (v2["start_utc"], v2["end_utc"]) == (_utc(2026, 6, 10, 17, 0), _utc(2026, 6, 11, 5, 0))
    assert v2["start_utc"].utcoffset() == timedelta(0) and v2["end_utc"].utcoffset() == timedelta(0)


def test_the_bound_parameters_declare_their_time_zone_handling():
    v1 = operational_metrics_service._V1_CHECKIN_COUNT_SQL.compile().params
    v2 = operational_metrics_service._V2_CHECKIN_COUNT_SQL.compile().params
    assert set(v1) == {"customer_id", "branch_id", "start_local", "end_local"}
    assert set(v2) == {"customer_id", "branch_id", "start_utc", "end_utc"}

    v1_types = {name: bind.type for name, bind in operational_metrics_service._V1_CHECKIN_COUNT_SQL._bindparams.items()}
    v2_types = {name: bind.type for name, bind in operational_metrics_service._V2_CHECKIN_COUNT_SQL._bindparams.items()}
    assert v1_types["start_local"].timezone is False and v1_types["end_local"].timezone is False
    assert v2_types["start_utc"].timezone is True and v2_types["end_utc"].timezone is True


def test_without_a_cutover_v1_gets_the_whole_local_day(db):
    _, recorder = _count(db)

    parameters = recorder.parameters_for("checkins")
    assert (parameters["start_local"], parameters["end_local"]) == (_local(2026, 6, 10), _local(2026, 6, 11))


# --- how many statements run -------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cutover_at", "expected_tables"),
    [
        (None, ["v2_cutovers", "checkins"]),
        (_utc(2026, 6, 1, 17, 0), ["v2_cutovers", "checkin_events"]),
        (_utc(2026, 6, 10, 5, 0), ["v2_cutovers", "checkin_events"]),
        (NOON_CUTOVER, ["v2_cutovers", "checkins", "checkin_events"]),
        (_utc(2026, 6, 11, 5, 0), ["v2_cutovers", "checkins"]),
        (_utc(2026, 6, 20, 17, 0), ["v2_cutovers", "checkins"]),
    ],
    ids=["no cutover", "cutover before the day", "cutover at the day's start", "cutover inside the day",
         "cutover at the day's end", "cutover after the day"],
)
def test_the_cutover_is_looked_up_once_and_only_the_eras_that_own_part_of_the_day_are_counted(
    db, cutover_at, expected_tables
):
    if cutover_at is not None:
        _cutover(db, cutover_at)

    result, recorder = _count(db)

    assert recorder.tables() == expected_tables
    assert recorder.tables().count("v2_cutovers") == 1
    assert result.total == result.v1_count + result.v2_count == 0


# --- failures -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("failing", ["v2_cutovers", "checkins", "checkin_events"])
def test_a_failure_in_any_statement_propagates_and_no_partial_count_is_returned(db, failing):
    _cutover(db, NOON_CUTOVER)
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 18, 0))

    with pytest.raises(RuntimeError, match=f"synthetic failure reading {failing}"):
        _count(db, fail_on=failing)


def test_a_failing_v2_count_does_not_come_back_as_a_v1_only_total(db):
    _cutover(db, NOON_CUTOVER)
    _v1(db, _local(2026, 6, 10, 9, 0))
    with db.begin() as conn:
        conn.execute(text("DROP TABLE checkin_events"))

    with pytest.raises(Exception, match="checkin_events"):
        _count(db)


def test_a_failing_v1_count_does_not_come_back_as_a_v2_only_total(db):
    _cutover(db, NOON_CUTOVER)
    _v2(db, _utc(2026, 6, 10, 18, 0))
    with db.begin() as conn:
        conn.execute(text("DROP TABLE checkins"))

    with pytest.raises(Exception, match="checkins"):
        _count(db)


# --- the result -----------------------------------------------------------------------------------------------------------

def test_the_result_is_an_immutable_value_whose_total_is_the_sum_of_the_eras(db):
    CheckinCount = operational_metrics_service.CheckinCount
    _cutover(db, NOON_CUTOVER)
    _v1(db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0))
    _v2(db, _utc(2026, 6, 10, 18, 0))

    result, _ = _count(db)

    assert [f.name for f in dataclasses.fields(CheckinCount)] == ["total", "v1_count", "v2_count"]
    assert result == CheckinCount(total=3, v1_count=2, v2_count=1)
    assert all(type(value) is int for value in dataclasses.astuple(result))
    assert result.total == result.v1_count + result.v2_count
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.total = 99


def test_the_function_takes_a_connection_a_tenant_a_date_and_a_zone():
    parameters = inspect.signature(operational_metrics_service.get_checkin_count).parameters

    assert list(parameters) == ["conn", "tenant", "local_date", "zone"]
    assert parameters["local_date"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["zone"].kind is inspect.Parameter.KEYWORD_ONLY
    assert all(p.default is inspect.Parameter.empty for p in parameters.values())   # nothing defaults, least of all the date


def test_nothing_is_cached_between_counts(db):
    assert _counts(db) == (0, 0, 0)

    _v1(db, _local(2026, 6, 10, 9, 0))
    assert _counts(db) == (1, 1, 0)

    _cutover(db, _utc(2026, 6, 1, 5, 0))
    assert _counts(db) == (0, 0, 0)

    _v2(db, _utc(2026, 6, 10, 18, 0))
    assert _counts(db) == (1, 0, 1)


# --- against the Streamlit dashboard (characterization only; the dashboard is not changed) ---------------------------

def _dashboard_count(v1_rows, v2_rows, cutover_at, local_date) -> int:
    """What the dashboard counts today: mixed_era_service's frame, filtered
    by metrics.get_date_filtered_df, then len()."""
    import pandas as pd

    import metrics
    from services import mixed_era_service

    v1 = pd.DataFrame({"datetime": [pd.Timestamp(row) for row in v1_rows], "barcode": ["synthetic"] * len(v1_rows)})
    v2 = pd.DataFrame({"datetime": [pd.Timestamp(row) for row in v2_rows], "item_key": ["k"] * len(v2_rows),
                       "destination": ["main"] * len(v2_rows)})
    if not v1_rows:
        v1 = pd.DataFrame(columns=["datetime", "barcode"])
    if not v2_rows:
        v2 = pd.DataFrame(columns=["datetime", "item_key", "destination"])

    cutover = None if cutover_at is None else pd.Timestamp(cutover_at)
    mixed = mixed_era_service._build_mixed_checkins(v1, v2, cutover)
    return len(metrics.get_date_filtered_df(mixed, local_date, local_date))


V1_ROWS = [
    _local(2026, 5, 31, 23, 59, 59), _local(2026, 6, 1, 0, 0), _local(2026, 6, 1, 2, 0), _local(2026, 6, 1, 9, 15),
    _local(2026, 6, 1, 9, 15), _local(2026, 6, 1, 19, 30), _local(2026, 6, 1, 23, 59, 59), _local(2026, 6, 2, 0, 0),
    _local(2026, 6, 2, 12, 0),
]


@pytest.mark.parametrize("local_date", [date(2026, 5, 30), date(2026, 5, 31), date(2026, 6, 1), date(2026, 6, 2)])
def test_for_a_v1_only_branch_the_sql_count_equals_the_dashboards_row_count(db, local_date):
    _v1(db, *V1_ROWS)

    total, v1_count, v2_count = _counts(db, local_date)

    assert total == _dashboard_count(V1_ROWS, [], None, local_date)
    assert (v1_count, v2_count) == (total, 0)


def test_for_a_cut_over_branch_the_count_intentionally_differs_from_the_dashboard_where_its_relabelling_shifts_a_day(db):
    """Known and deliberate. Once a branch has a cutover, the dashboard labels
    legacy naive local times as UTC and converts them to Central, moving each
    5-6 hours earlier; rows from the first hours of a local day land on the
    day before. This service counts each row on the local day it was stamped.
    The dashboard is not changed by Block 5."""
    cutover_at = _utc(2026, 6, 10, 5, 0)
    _v1(db, *V1_ROWS)
    _cutover(db, cutover_at)

    intended = {day: _counts(db, day)[0] for day in (date(2026, 5, 31), date(2026, 6, 1), date(2026, 6, 2))}
    dashboard = {day: _dashboard_count(V1_ROWS, [], cutover_at, day) for day in intended}

    assert intended == {date(2026, 5, 31): 1, date(2026, 6, 1): 6, date(2026, 6, 2): 2}
    assert dashboard == {date(2026, 5, 31): 3, date(2026, 6, 1): 5, date(2026, 6, 2): 1}
    assert sum(intended.values()) == sum(dashboard.values()) == len(V1_ROWS)   # same rows, different days


# =====================================================================================================================
# Module boundaries
# =====================================================================================================================

def test_the_module_depends_only_on_the_standard_library_sqlalchemy_and_the_resolved_tenant_type():
    imports = [line.strip() for line in inspect.getsource(operational_metrics_service).splitlines()
               if line.startswith(("import ", "from "))]

    assert imports == [
        "from __future__ import annotations",
        "from dataclasses import dataclass",
        "from datetime import UTC, date, datetime, time, timedelta",
        "from zoneinfo import ZoneInfo",
        "from sqlalchemy import DateTime, bindparam, text",
        "from sqlalchemy.engine import Connection",
        "from services.tenant_resolution_service import ResolvedOperationalTenant",
    ]


def test_the_module_reads_no_clock_no_environment_and_creates_no_engine():
    source = inspect.getsource(operational_metrics_service)
    code = "\n".join(line for line in source.split('"""')[2].splitlines() if not line.lstrip().startswith("#"))

    for forbidden in ("datetime.now", "utcnow", ".today(", "time.time", "os.environ", "getenv", "localtime",
                      "get_engine", "create_engine", ".connect(", ".begin(", "set_config", "cache"):
        assert forbidden not in code, forbidden
    assert "astimezone()" not in code   # every conversion names its target zone


def test_no_sql_in_the_module_depends_on_the_database_session_time_zone():
    statements = [name for name in vars(operational_metrics_service) if name.endswith("_SQL")]
    assert sorted(statements) == ["_EFFECTIVE_CUTOVER_SQL", "_V1_CHECKIN_COUNT_SQL", "_V2_CHECKIN_COUNT_SQL"]

    for name in statements:
        sql = str(getattr(operational_metrics_service, name)).upper()
        for forbidden in ("AT TIME ZONE", "NOW()", "CURRENT_DATE", "CURRENT_TIMESTAMP", "::DATE", "TIMEZONE"):
            assert forbidden not in sql, (name, forbidden)


def test_the_helpers_take_the_zone_explicitly_and_nothing_is_hardcoded_to_central():
    source = inspect.getsource(operational_metrics_service)

    assert list(inspect.signature(local_day_bounds).parameters) == ["local_date", "zone"]
    assert list(inspect.signature(cutover_boundary).parameters) == ["cutover_at", "zone"]
    assert list(inspect.signature(get_effective_cutover).parameters) == ["conn", "tenant"]
    assert "ZoneInfo(" not in source                       # no zone is constructed here
    assert "SORTVIEW_LIVE_TIMEZONE" not in source


_PROCESS_PROBE = """
    import json, sys
    from datetime import UTC, date, datetime
    from zoneinfo import ZoneInfo

    for blocked in ("streamlit", "pandas", "fastapi"):
        sys.modules[blocked] = None      # importing any of them would raise
    sys.path.insert(0, sys.argv[1])

    from services.operational_metrics_service import cutover_boundary, local_day_bounds

    zone = ZoneInfo("America/Chicago")
    days = [local_day_bounds(d, zone) for d in (date(2026, 3, 8), date(2026, 6, 1), date(2026, 11, 1))]
    boundary = cutover_boundary(datetime(2026, 6, 10, 3, 0, tzinfo=UTC), zone)
    print("RESULT_JSON=" + json.dumps({
        "days": [[b.v1_start_local.isoformat(), b.v1_end_local.isoformat(),
                  b.v2_start_utc.isoformat(), b.v2_end_utc.isoformat()] for b in days],
        "boundary": [boundary.cutover_utc.isoformat(), boundary.cutover_local_naive.isoformat()],
        "loaded": sorted(n for n in ("streamlit", "pandas", "fastapi", "starlette") if sys.modules.get(n) is not None),
    }))
"""


def _run_in_a_process_whose_local_time_zone_is(tz: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "PYTHONPATH"}}
    env["TZ"] = tz
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(_PROCESS_PROBE), str(SRC)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False,
    )

    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT_JSON=")]
    assert len(lines) == 1, f"probe failed:\n{completed.stdout}\n{completed.stderr[-2000:]}"
    return json.loads(lines[0][len("RESULT_JSON="):])


def test_the_results_do_not_depend_on_the_process_time_zone_and_need_no_streamlit_pandas_or_fastapi():
    in_utc = _run_in_a_process_whose_local_time_zone_is("UTC0")
    in_tokyo = _run_in_a_process_whose_local_time_zone_is("JST-9")
    in_central = _run_in_a_process_whose_local_time_zone_is("CST6CDT")

    assert in_utc == in_tokyo == in_central
    assert in_utc["loaded"] == []
    assert in_utc["days"][1] == [
        "2026-06-01T00:00:00", "2026-06-02T00:00:00", "2026-06-01T05:00:00+00:00", "2026-06-02T05:00:00+00:00",
    ]
    assert in_utc["boundary"] == ["2026-06-10T03:00:00+00:00", "2026-06-09T22:00:00"]
