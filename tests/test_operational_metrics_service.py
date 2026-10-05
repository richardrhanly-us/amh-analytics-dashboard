"""Blocks 5a and 5b: the time and cutover rules behind customer-facing check-in
metrics, and the check-in count built on them.

    local_day_bounds(local_date, zone)       one local day, as naive-local bounds (v1) and UTC instants (v2)
    local_hour_boundaries(local_date, zone)  that day's 24 wall-clock hours, in the same two forms (Block 6a)
    cutover_boundary(cutover_at, zone)       a cutover instant, in the same two forms
    get_effective_cutover(conn, tenant)      the tenant's current cutover, from v2_cutovers
    get_checkin_count(conn, tenant, ...)     check-ins on one local day, across both eras
    get_checkin_counts_by_hour(conn, ...)    the same check-ins, by wall-clock hour (Block 6b)
    get_reject_count(conn, tenant, ...)      rejects on one local day, across both eras (Block 7a)
    get_reject_counts_by_reason(conn, ...)   the same rejects, by reason (Block 8b)

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
import logging
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
    WALL_CLOCK_HOURS_PER_DAY,
    CutoverBoundary,
    LocalDayBounds,
    LocalHourBoundaries,
    cutover_boundary,
    get_effective_cutover,
    local_day_bounds,
    local_hour_boundaries,
)
from services.reject_reason import REJECT_REASONS
from services.tenant_resolution_service import ResolvedOperationalTenant

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

CHICAGO = ZoneInfo("America/Chicago")
KOLKATA = ZoneInfo("Asia/Kolkata")      # UTC+05:30, no DST
LONDON = ZoneInfo("Europe/London")      # DST on different dates from Chicago
HAVANA = ZoneInfo("America/Havana")     # changes its clocks AT local midnight
LORD_HOWE = ZoneInfo("Australia/Lord_Howe")     # moves its clocks by 30 minutes
APIA = ZoneInfo("Pacific/Apia")         # skipped a whole calendar date in 2011

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
# local_hour_boundaries (Block 6a)
# =====================================================================================================================

def _v2_hour_widths(boundaries: LocalHourBoundaries) -> list[timedelta]:
    instants = boundaries.v2_boundaries_utc
    return [instants[hour + 1] - instants[hour] for hour in range(24)]


def _v2_hour_of(instant: datetime, boundaries: LocalHourBoundaries) -> list[int]:
    """Every wall-clock hour whose half-open interval holds `instant`."""
    instants = boundaries.v2_boundaries_utc
    return [hour for hour in range(24) if instants[hour] <= instant < instants[hour + 1]]


def _v1_hour_of(stamped: datetime, boundaries: LocalHourBoundaries) -> list[int]:
    locals_ = boundaries.v1_boundaries_local
    return [hour for hour in range(24) if locals_[hour] <= stamped < locals_[hour + 1]]


HOUR_BOUNDARY_CASES = [
    (ORDINARY, CHICAGO), (WINTER, CHICAGO), (SPRING_FORWARD, CHICAGO), (FALL_BACK, CHICAGO),
    (ORDINARY, KOLKATA), (ORDINARY, LONDON),
    (date(2026, 3, 8), HAVANA), (date(2026, 11, 1), HAVANA),
    (date(2026, 4, 5), LORD_HOWE), (date(2026, 10, 4), LORD_HOWE),
    (date(2011, 12, 30), APIA),
]


@pytest.mark.parametrize(("local_date", "zone"), HOUR_BOUNDARY_CASES)
def test_there_are_always_25_boundaries_for_each_table(local_date, zone):
    boundaries = local_hour_boundaries(local_date, zone)

    assert WALL_CLOCK_HOURS_PER_DAY == 24
    assert len(boundaries.v1_boundaries_local) == 25
    assert len(boundaries.v2_boundaries_utc) == 25
    assert boundaries.local_date == local_date
    assert boundaries.timezone_name == zone.key


@pytest.mark.parametrize(("local_date", "zone"), HOUR_BOUNDARY_CASES)
def test_the_first_and_last_boundaries_are_the_bounds_of_the_local_day(local_date, zone):
    boundaries = local_hour_boundaries(local_date, zone)
    day = local_day_bounds(local_date, zone)

    assert boundaries.v1_boundaries_local[0] == day.v1_start_local
    assert boundaries.v1_boundaries_local[24] == day.v1_end_local
    assert boundaries.v2_boundaries_utc[0] == day.v2_start_utc
    assert boundaries.v2_boundaries_utc[24] == day.v2_end_utc


@pytest.mark.parametrize(("local_date", "zone"), HOUR_BOUNDARY_CASES)
def test_the_v1_boundaries_are_the_naive_wall_clock_hours_whatever_the_zone(local_date, zone):
    boundaries = local_hour_boundaries(local_date, zone)

    year, month, day = local_date.year, local_date.month, local_date.day
    following = local_date + timedelta(days=1)
    assert boundaries.v1_boundaries_local == (
        *(_local(year, month, day, hour) for hour in range(24)),
        _local(following.year, following.month, following.day),
    )
    assert all(boundary.tzinfo is None for boundary in boundaries.v1_boundaries_local)


@pytest.mark.parametrize(("local_date", "zone"), HOUR_BOUNDARY_CASES)
def test_the_v2_boundaries_are_aware_utc_and_never_decrease(local_date, zone):
    instants = local_hour_boundaries(local_date, zone).v2_boundaries_utc

    assert all(instant.tzinfo is UTC for instant in instants)
    assert all(instants[hour] <= instants[hour + 1] for hour in range(24))


@pytest.mark.parametrize(("local_date", "zone"), HOUR_BOUNDARY_CASES)
def test_the_24_v2_hours_tile_the_local_day_exactly(local_date, zone):
    # Never decreasing, with the day's own bounds at each end: every instant
    # of the day is in exactly one hour, which is what makes the hourly counts
    # add up to the day's count.
    boundaries = local_hour_boundaries(local_date, zone)
    day = local_day_bounds(local_date, zone)

    assert sum(_v2_hour_widths(boundaries), timedelta()) == day.v2_end_utc - day.v2_start_utc

    instant = day.v2_start_utc
    while instant < day.v2_end_utc:
        assert len(_v2_hour_of(instant, boundaries)) == 1
        instant += timedelta(minutes=15)
    assert _v2_hour_of(day.v2_start_utc - timedelta(seconds=1), boundaries) == []
    assert _v2_hour_of(day.v2_end_utc, boundaries) == []


def test_an_ordinary_day_has_24_one_hour_buckets_at_the_daylight_offset():
    boundaries = local_hour_boundaries(ORDINARY, CHICAGO)

    assert _v2_hour_widths(boundaries) == [timedelta(hours=1)] * 24
    assert boundaries.v2_boundaries_utc[0] == _utc(2026, 6, 1, 5)       # 00:00 CDT
    assert boundaries.v2_boundaries_utc[9] == _utc(2026, 6, 1, 14)      # 09:00 CDT
    assert boundaries.v2_boundaries_utc[24] == _utc(2026, 6, 2, 5)


def test_a_winter_day_has_24_one_hour_buckets_at_the_standard_offset():
    boundaries = local_hour_boundaries(WINTER, CHICAGO)

    assert _v2_hour_widths(boundaries) == [timedelta(hours=1)] * 24
    assert boundaries.v2_boundaries_utc[9] == _utc(2026, 1, 15, 15)     # 09:00 CST


def test_on_an_ordinary_day_an_instant_and_its_wall_clock_reading_name_the_same_hour():
    boundaries = local_hour_boundaries(ORDINARY, CHICAGO)

    for hour in range(24):
        stamped = _local(2026, 6, 1, hour, 30)
        instant = stamped.replace(tzinfo=CHICAGO).astimezone(UTC)
        assert _v1_hour_of(stamped, boundaries) == [hour]
        assert _v2_hour_of(instant, boundaries) == [hour]


def test_an_event_exactly_on_the_hour_belongs_to_the_hour_it_starts():
    boundaries = local_hour_boundaries(ORDINARY, CHICAGO)

    assert _v1_hour_of(_local(2026, 6, 1, 9), boundaries) == [9]
    assert _v1_hour_of(_local(2026, 6, 1, 8, 59, 59), boundaries) == [8]
    assert _v2_hour_of(_utc(2026, 6, 1, 14), boundaries) == [9]
    assert _v2_hour_of(_utc(2026, 6, 1, 13, 59, 59), boundaries) == [8]


# --- spring forward: the skipped hour ---------------------------------------------------------------------------------

def test_on_the_spring_forward_date_the_skipped_hour_is_zero_wide_for_v2():
    boundaries = local_hour_boundaries(SPRING_FORWARD, CHICAGO)
    widths = _v2_hour_widths(boundaries)

    assert widths[2] == timedelta(0)
    assert [hour for hour, width in enumerate(widths) if width != timedelta(hours=1)] == [2]
    assert sum(widths, timedelta()) == timedelta(hours=23)
    # 02:00 and 03:00 are the same instant: the moment the clocks jump.
    assert boundaries.v2_boundaries_utc[2] == boundaries.v2_boundaries_utc[3] == _utc(2026, 3, 8, 8)


def test_no_instant_falls_in_the_skipped_hour():
    boundaries = local_hour_boundaries(SPRING_FORWARD, CHICAGO)

    assert _v2_hour_of(_utc(2026, 3, 8, 7, 59, 59), boundaries) == [1]     # 01:59:59 CST
    assert _v2_hour_of(_utc(2026, 3, 8, 8), boundaries) == [3]             # 03:00:00 CDT
    instant = boundaries.v2_boundaries_utc[0]
    while instant < boundaries.v2_boundaries_utc[24]:
        assert _v2_hour_of(instant, boundaries) != [2]
        instant += timedelta(minutes=5)


def test_a_legacy_row_stamped_in_the_skipped_hour_still_belongs_to_the_hour_it_names():
    boundaries = local_hour_boundaries(SPRING_FORWARD, CHICAGO)

    assert _v1_hour_of(_local(2026, 3, 8, 2, 30), boundaries) == [2]
    assert boundaries.v1_boundaries_local[3] - boundaries.v1_boundaries_local[2] == timedelta(hours=1)


# --- fall back: the repeated hour, both passes merged -----------------------------------------------------------------

def test_on_the_fall_back_date_the_repeated_hour_is_two_hours_wide_for_v2():
    boundaries = local_hour_boundaries(FALL_BACK, CHICAGO)
    widths = _v2_hour_widths(boundaries)

    assert widths[1] == timedelta(hours=2)
    assert [hour for hour, width in enumerate(widths) if width != timedelta(hours=1)] == [1]
    assert sum(widths, timedelta()) == timedelta(hours=25)
    assert boundaries.v2_boundaries_utc[1] == _utc(2026, 11, 1, 6)      # 01:00 CDT, the first pass
    assert boundaries.v2_boundaries_utc[2] == _utc(2026, 11, 1, 8)      # 02:00 CST


def test_both_passes_through_the_repeated_hour_fall_in_the_same_bucket():
    boundaries = local_hour_boundaries(FALL_BACK, CHICAGO)

    first_pass = _utc(2026, 11, 1, 6, 30)       # 01:30 CDT
    second_pass = _utc(2026, 11, 1, 7, 30)      # 01:30 CST
    assert first_pass.astimezone(CHICAGO).replace(tzinfo=None) == _local(2026, 11, 1, 1, 30)
    assert second_pass.astimezone(CHICAGO).replace(tzinfo=None) == _local(2026, 11, 1, 1, 30)

    assert _v2_hour_of(first_pass, boundaries) == [1]
    assert _v2_hour_of(second_pass, boundaries) == [1]
    # ...which is the bucket the one legacy reading they share falls in.
    assert _v1_hour_of(_local(2026, 11, 1, 1, 30), boundaries) == [1]


def test_the_hours_either_side_of_the_repeated_hour_are_exact():
    boundaries = local_hour_boundaries(FALL_BACK, CHICAGO)

    assert _v2_hour_of(_utc(2026, 11, 1, 5, 59, 59), boundaries) == [0]    # 00:59:59 CDT
    assert _v2_hour_of(_utc(2026, 11, 1, 6), boundaries) == [1]            # 01:00:00 CDT
    assert _v2_hour_of(_utc(2026, 11, 1, 7, 59, 59), boundaries) == [1]    # 01:59:59 CST
    assert _v2_hour_of(_utc(2026, 11, 1, 8), boundaries) == [2]            # 02:00:00 CST


# --- zones whose offset is not a whole number of hours ----------------------------------------------------------------

def test_a_half_hour_zone_has_boundaries_on_the_utc_half_hour():
    boundaries = local_hour_boundaries(ORDINARY, KOLKATA)

    assert _v2_hour_widths(boundaries) == [timedelta(hours=1)] * 24
    assert boundaries.v2_boundaries_utc[0] == _utc(2026, 5, 31, 18, 30)     # 00:00 IST
    assert boundaries.v2_boundaries_utc[9] == _utc(2026, 6, 1, 3, 30)       # 09:00 IST
    assert boundaries.v2_boundaries_utc[24] == _utc(2026, 6, 1, 18, 30)
    assert all(instant.minute == 30 for instant in boundaries.v2_boundaries_utc)


def test_in_a_half_hour_zone_an_instant_is_bucketed_by_its_local_hour_not_its_utc_hour():
    boundaries = local_hour_boundaries(ORDINARY, KOLKATA)

    # 03:15 and 03:45 UTC share a UTC hour but are 08:45 and 09:15 in Kolkata.
    assert _v2_hour_of(_utc(2026, 6, 1, 3, 15), boundaries) == [8]
    assert _v2_hour_of(_utc(2026, 6, 1, 3, 45), boundaries) == [9]


def test_a_zone_that_moves_its_clocks_by_half_an_hour_has_a_half_hour_and_a_ninety_minute_bucket():
    # Lord Howe Island moves its clocks by 30 minutes: back at 02:00 on
    # 5 April 2026 (01:30-01:59 happens twice) and forward at 02:00 on
    # 4 October 2026 (02:00-02:29 does not exist).
    back = _v2_hour_widths(local_hour_boundaries(date(2026, 4, 5), LORD_HOWE))
    forward = _v2_hour_widths(local_hour_boundaries(date(2026, 10, 4), LORD_HOWE))

    assert back[1] == timedelta(minutes=90)
    assert sum(back, timedelta()) == timedelta(hours=24, minutes=30)
    assert forward[2] == timedelta(minutes=30)
    assert sum(forward, timedelta()) == timedelta(hours=23, minutes=30)


# --- clock changes at the edge of the day, and beyond it --------------------------------------------------------------

def test_a_zone_that_changes_its_clocks_at_midnight_puts_the_change_in_hour_zero():
    forward = _v2_hour_widths(local_hour_boundaries(date(2026, 3, 8), HAVANA))
    back = _v2_hour_widths(local_hour_boundaries(date(2026, 11, 1), HAVANA))

    assert forward[0] == timedelta(0)
    assert sum(forward, timedelta()) == timedelta(hours=23)
    assert back[0] == timedelta(hours=2)
    assert sum(back, timedelta()) == timedelta(hours=25)


def test_a_local_date_that_never_happened_has_24_empty_v2_hours():
    # Samoa crossed the date line at the end of 29 December 2011: the 30th was
    # skipped entirely. Unclamped, the hours of that date would run past the
    # end of the (zero-length) day.
    boundaries = local_hour_boundaries(date(2011, 12, 30), APIA)

    assert _v2_hour_widths(boundaries) == [timedelta(0)] * 24
    assert len(set(boundaries.v2_boundaries_utc)) == 1
    assert len(boundaries.v1_boundaries_local) == 25        # a legacy reading is still bucketed by its label


def test_consecutive_days_hour_boundaries_meet_exactly():
    for zone in (CHICAGO, KOLKATA, HAVANA, LORD_HOWE):
        for first in (date(2026, 3, 7), date(2026, 3, 8), date(2026, 10, 31), date(2026, 11, 1)):
            today = local_hour_boundaries(first, zone)
            tomorrow = local_hour_boundaries(first + timedelta(days=1), zone)

            assert today.v2_boundaries_utc[24] == tomorrow.v2_boundaries_utc[0]
            assert today.v1_boundaries_local[24] == tomorrow.v1_boundaries_local[0]


def test_the_hour_boundaries_are_an_immutable_value():
    boundaries = local_hour_boundaries(ORDINARY, CHICAGO)

    assert [f.name for f in dataclasses.fields(LocalHourBoundaries)] == [
        "local_date", "timezone_name", "v1_boundaries_local", "v2_boundaries_utc",
    ]
    assert isinstance(boundaries.v1_boundaries_local, tuple)
    assert isinstance(boundaries.v2_boundaries_utc, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        boundaries.v2_boundaries_utc = ()


def test_the_helper_is_pure_and_takes_only_a_date_and_a_zone():
    assert list(inspect.signature(local_hour_boundaries).parameters) == ["local_date", "zone"]

    source = inspect.getsource(local_hour_boundaries)
    assert "conn" not in source
    assert "execute" not in source
    assert "now(" not in source
    assert "today(" not in source


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
# Block 6b: get_checkin_counts_by_hour
# =====================================================================================================================
#
# The real SQL again, against the same three in-memory SQLite tables and with
# the same helpers as the day count above. Every expectation is written as
# {hour: count} for the hours that are not zero.

CheckinHourlyCounts = operational_metrics_service.CheckinHourlyCounts
NO_HOURS = (0,) * 24


def _hourly(db, local_date=JUNE_10, *, tenant=TENANT_A, zone=CHICAGO, fail_on=None):
    with db.connect() as conn:
        recorder = Recorder(conn, fail_on=fail_on)
        result = operational_metrics_service.get_checkin_counts_by_hour(
            recorder, tenant, local_date=local_date, zone=zone,
        )
    return result, recorder


def _busy(counts: tuple[int, ...]) -> dict[int, int]:
    assert len(counts) == 24
    return {hour: count for hour, count in enumerate(counts) if count}


def _by_hour(db, local_date=JUNE_10, **kwargs) -> dict[int, int]:
    result, _ = _hourly(db, local_date, **kwargs)
    return _busy(result.counts)


def _assert_the_hours_add_up_to_the_day(db, local_date=JUNE_10, **kwargs) -> int:
    """The invariant: for the same tenant, date and zone, the 24 hourly counts
    sum to get_checkin_count's total -- and era by era, too."""
    hourly, _ = _hourly(db, local_date, **kwargs)
    day, _ = _count(db, local_date, **kwargs)

    assert sum(hourly.counts) == day.total
    assert sum(hourly.v1_counts) == day.v1_count
    assert sum(hourly.v2_counts) == day.v2_count
    return day.total


# --- no cutover: the branch is v1 only --------------------------------------------------------------------------------

def test_a_v1_only_branch_counts_each_row_in_the_hour_it_was_stamped(db):
    _v1(db, _local(2026, 6, 9, 23, 59, 59), _local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 9, 5),
        _local(2026, 6, 10, 9, 55), _local(2026, 6, 10, 14, 30), _local(2026, 6, 10, 23, 59, 59),
        _local(2026, 6, 11, 0, 0))

    result, recorder = _hourly(db)

    assert _busy(result.counts) == {0: 1, 9: 2, 14: 1, 23: 1}
    assert result.v1_counts == result.counts
    assert result.v2_counts == NO_HOURS
    assert recorder.tables() == ["v2_cutovers", "checkins"]   # the v2 table is never read
    assert _assert_the_hours_add_up_to_the_day(db) == 5


def test_with_no_cutover_v2_rows_are_ignored_hour_by_hour_even_if_they_exist(db):
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 15, 0), _utc(2026, 6, 10, 16, 0))

    result, recorder = _hourly(db)

    assert _busy(result.counts) == {9: 1}
    assert "checkin_events" not in recorder.tables()
    _assert_the_hours_add_up_to_the_day(db)


def test_a_tenant_with_no_rows_has_24_zero_hours(db):
    result, recorder = _hourly(db)

    assert result == CheckinHourlyCounts(counts=NO_HOURS, v1_counts=NO_HOURS, v2_counts=NO_HOURS)
    assert recorder.tables() == ["v2_cutovers", "checkins"]


def test_hours_in_which_nothing_happened_are_zero_not_missing(db):
    _v1(db, _local(2026, 6, 10, 3, 0), _local(2026, 6, 10, 22, 0))    # outside any "operating hours"

    result, _ = _hourly(db)

    assert len(result.counts) == 24
    assert result.counts == (0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0)


def test_every_hour_of_the_day_is_its_own_bucket_in_order(db):
    # H + 1 rows stamped in hour H, so a count that landed in the wrong bucket would show.
    for hour in range(24):
        _v1(db, *[_local(2026, 6, 10, hour, 30)] * (hour + 1))

    result, _ = _hourly(db)

    assert result.counts == tuple(range(1, 25))
    assert _assert_the_hours_add_up_to_the_day(db) == 300


def test_an_hour_owns_its_first_instant_and_not_its_last(db):
    _v1(db, _local(2026, 6, 10, 8, 59, 59), _local(2026, 6, 10, 9, 0, 0), _local(2026, 6, 10, 9, 59, 59),
        _local(2026, 6, 10, 10, 0, 0))

    assert _by_hour(db) == {8: 1, 9: 2, 10: 1}

    # The same four moments as v2 instants (09:00 CDT is 14:00Z).
    _cutover(db, _utc(2026, 6, 1, 5, 0))
    _v2(db, _utc(2026, 6, 10, 13, 59, 59), _utc(2026, 6, 10, 14, 0, 0), _utc(2026, 6, 10, 14, 59, 59),
        _utc(2026, 6, 10, 15, 0, 0))

    result, _ = _hourly(db)
    assert _busy(result.v2_counts) == {8: 1, 9: 2, 10: 1}
    assert result.v1_counts == NO_HOURS


def test_every_row_is_counted_in_its_hour_with_no_deduplication(db):
    _v1(db, *[_local(2026, 6, 10, 9, 0)] * 3, barcode="same-barcode")
    _cutover(db, NOON_CUTOVER)
    _v2(db, *[_utc(2026, 6, 10, 18, 0)] * 2)

    assert _by_hour(db) == {9: 3, 13: 2}
    _assert_the_hours_add_up_to_the_day(db)


def test_a_rollback_makes_the_branch_v1_only_again_hour_by_hour(db):
    _v1(db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 15, 0))
    _v2(db, _utc(2026, 6, 10, 20, 0))
    _cutover(db, NOON_CUTOVER, set_at="2026-06-01 00:00:00+00:00")
    _cutover(db, None, set_at="2026-06-12 00:00:00+00:00")   # the latest record: a rollback

    result, recorder = _hourly(db)

    assert _busy(result.counts) == {9: 1, 15: 1}             # 15:00 is v1's again; the v2 row is not counted
    assert result.v2_counts == NO_HOURS
    assert recorder.tables() == ["v2_cutovers", "checkins"]
    _assert_the_hours_add_up_to_the_day(db)


# --- the cutover's position relative to the requested day -------------------------------------------------------------

def test_a_cutover_before_the_day_makes_every_hour_a_v2_hour(db):
    _cutover(db, _utc(2026, 6, 1, 17, 0))
    _v1(db, _local(2026, 6, 10, 9, 0))                # a legacy row after the cutover: not counted
    _v2(db, _utc(2026, 6, 10, 5, 0), _utc(2026, 6, 10, 15, 0), _utc(2026, 6, 11, 4, 59, 59))
    _v2(db, _utc(2026, 6, 10, 4, 59, 59), _utc(2026, 6, 11, 5, 0))   # just outside the local day

    result, recorder = _hourly(db)

    assert _busy(result.counts) == {0: 1, 10: 1, 23: 1}
    assert result.v1_counts == NO_HOURS
    assert recorder.tables() == ["v2_cutovers", "checkin_events"]    # no v1 query at all
    _assert_the_hours_add_up_to_the_day(db)


def test_a_date_long_after_the_cutover_is_v2_only(db):
    _cutover(db, _utc(2026, 1, 5, 12, 0))
    _v2(db, _utc(2026, 6, 10, 13, 15), _utc(2026, 6, 10, 13, 45), _utc(2026, 6, 11, 1, 30))

    result, recorder = _hourly(db)

    assert _busy(result.counts) == {8: 2, 20: 1}      # 20:30 CDT is already 11 June in UTC
    assert result.counts == result.v2_counts
    assert recorder.tables() == ["v2_cutovers", "checkin_events"]


def test_a_cutover_after_the_day_leaves_every_hour_a_v1_hour(db):
    _cutover(db, _utc(2026, 6, 20, 17, 0))
    _v1(db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 21, 0))
    _v2(db, _utc(2026, 6, 10, 15, 0))                 # a v2 row before the cutover: not counted

    result, recorder = _hourly(db)

    assert _busy(result.counts) == {9: 1, 21: 1}
    assert result.v2_counts == NO_HOURS
    assert recorder.tables() == ["v2_cutovers", "checkins"]          # no v2 query at all
    _assert_the_hours_add_up_to_the_day(db)


def test_a_cutover_at_the_local_midnight_that_starts_the_day_gives_every_hour_to_v2(db):
    _cutover(db, _utc(2026, 6, 10, 5, 0))             # 00:00 local on 10 June
    _v1(db, _local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 5, 0), _utc(2026, 6, 10, 15, 0))

    result, recorder = _hourly(db)

    assert _busy(result.counts) == {0: 1, 10: 1}
    assert result.v1_counts == NO_HOURS
    assert recorder.tables() == ["v2_cutovers", "checkin_events"]
    _assert_the_hours_add_up_to_the_day(db)


def test_a_cutover_at_the_next_local_midnight_gives_every_hour_to_v1(db):
    _cutover(db, _utc(2026, 6, 11, 5, 0))             # 00:00 local on 11 June
    _v1(db, _local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 23, 59, 59))
    _v2(db, _utc(2026, 6, 10, 15, 0), _utc(2026, 6, 11, 5, 0))

    result, recorder = _hourly(db)

    assert _busy(result.counts) == {0: 1, 23: 1}
    assert result.v2_counts == NO_HOURS
    assert recorder.tables() == ["v2_cutovers", "checkins"]

    # ...and the day after is entirely v2's, from its first hour.
    assert _by_hour(db, date(2026, 6, 11)) == {0: 1}


def test_a_cutover_exactly_on_an_hour_boundary_splits_the_day_between_whole_hours(db):
    _cutover(db, NOON_CUTOVER)                        # 12:00 local
    # v1: 08:00 and 11:59:59 count; 12:00 and 15:00 do not (v1 is strictly before).
    _v1(db, _local(2026, 6, 10, 8, 0), _local(2026, 6, 10, 11, 59, 59), _local(2026, 6, 10, 12, 0),
        _local(2026, 6, 10, 15, 0))
    # v2: 11:59:59 local does not count; 12:00, 17:00 and 23:59:59 local do; the next midnight does not.
    _v2(db, _utc(2026, 6, 10, 16, 59, 59), _utc(2026, 6, 10, 17, 0), _utc(2026, 6, 10, 22, 0),
        _utc(2026, 6, 11, 4, 59, 59), _utc(2026, 6, 11, 5, 0))

    result, recorder = _hourly(db)

    assert _busy(result.v1_counts) == {8: 1, 11: 1}
    assert _busy(result.v2_counts) == {12: 1, 17: 1, 23: 1}
    assert _busy(result.counts) == {8: 1, 11: 1, 12: 1, 17: 1, 23: 1}
    # No hour is shared: every hour before noon is v1's alone, every hour from noon v2's alone.
    assert all(count == 0 for count in result.v1_counts[12:])
    assert all(count == 0 for count in result.v2_counts[:12])
    assert recorder.tables() == ["v2_cutovers", "checkins", "checkin_events"]
    assert _assert_the_hours_add_up_to_the_day(db) == 5


HALF_PAST_TWO_CUTOVER = _utc(2026, 6, 10, 19, 30)     # 14:30 local on 10 June (CDT)


def test_a_cutover_in_the_middle_of_an_hour_splits_that_hour_between_the_eras(db):
    _cutover(db, HALF_PAST_TWO_CUTOVER)
    # v1, wall clock: 13:50, 14:00, 14:10 and 14:29:59 count; 14:30 and 14:45 do not.
    _v1(db, _local(2026, 6, 10, 13, 50), _local(2026, 6, 10, 14, 0), _local(2026, 6, 10, 14, 10),
        _local(2026, 6, 10, 14, 29, 59), _local(2026, 6, 10, 14, 30), _local(2026, 6, 10, 14, 45))
    # v2, instants: 14:10 and 14:29:59 local do not count; 14:30, 14:40, 14:59:59 and 15:00 local do.
    _v2(db, _utc(2026, 6, 10, 19, 10), _utc(2026, 6, 10, 19, 29, 59), _utc(2026, 6, 10, 19, 30),
        _utc(2026, 6, 10, 19, 40), _utc(2026, 6, 10, 19, 59, 59), _utc(2026, 6, 10, 20, 0))

    result, recorder = _hourly(db)

    assert _busy(result.v1_counts) == {13: 1, 14: 3}
    assert _busy(result.v2_counts) == {14: 3, 15: 1}
    assert _busy(result.counts) == {13: 1, 14: 6, 15: 1}     # hour 14 is the sum of both eras' shares
    assert recorder.tables() == ["v2_cutovers", "checkins", "checkin_events"]
    assert _assert_the_hours_add_up_to_the_day(db) == 8


def test_exactly_at_a_mid_hour_cutover_belongs_to_v2_and_one_second_before_to_v1(db):
    _cutover(db, HALF_PAST_TWO_CUTOVER)
    _v1(db, _local(2026, 6, 10, 14, 29, 59))          # one second before, as v1 holds it
    _v1(db, _local(2026, 6, 10, 14, 30, 0))           # the cutover moment as a v1 row: not v1's
    _v2(db, _utc(2026, 6, 10, 19, 29, 59))            # one second before, as v2 holds it: not v2's
    _v2(db, _utc(2026, 6, 10, 19, 30, 0))             # the cutover moment as a v2 row

    result, _ = _hourly(db)

    assert _busy(result.v1_counts) == {14: 1}
    assert _busy(result.v2_counts) == {14: 1}
    assert _busy(result.counts) == {14: 2}            # each moment counted once: no gap, no double count


def test_a_mid_hour_cutover_clamps_the_range_and_leaves_the_hour_boundaries_whole(db):
    _cutover(db, HALF_PAST_TWO_CUTOVER)

    _, recorder = _hourly(db)

    v1, v2 = recorder.parameters_for("checkins"), recorder.parameters_for("checkin_events")
    # The outer range is the era's share of the day, exactly as the day count binds it...
    assert (v1["start_local"], v1["end_local"]) == (_local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 14, 30))
    assert (v2["start_utc"], v2["end_utc"]) == (HALF_PAST_TWO_CUTOVER, _utc(2026, 6, 11, 5, 0))
    # ...while the 25 hour boundaries are the whole day's, untouched by the cutover.
    hours = local_hour_boundaries(JUNE_10, CHICAGO)
    assert tuple(v1[f"boundary_{index}"] for index in range(25)) == hours.v1_boundaries_local
    assert tuple(v2[f"boundary_{index}"] for index in range(25)) == hours.v2_boundaries_utc


def test_the_days_around_a_cutover_add_up_hour_by_hour_with_nothing_lost_or_counted_twice(db):
    _cutover(db, HALF_PAST_TWO_CUTOVER)
    v1_rows = [_local(2026, 6, 9, 10, 0), _local(2026, 6, 9, 23, 30), _local(2026, 6, 10, 0, 30),
               _local(2026, 6, 10, 14, 15)]
    v2_rows = [_utc(2026, 6, 10, 19, 30), _utc(2026, 6, 10, 23, 0), _utc(2026, 6, 11, 4, 0), _utc(2026, 6, 11, 5, 0),
               _utc(2026, 6, 11, 20, 0)]
    _v1(db, *v1_rows)
    _v2(db, *v2_rows)

    days = (date(2026, 6, 9), date(2026, 6, 10), date(2026, 6, 11))

    assert [_by_hour(db, day) for day in days] == [{10: 1, 23: 1}, {0: 1, 14: 2, 18: 1, 23: 1}, {0: 1, 15: 1}]
    assert sum(_assert_the_hours_add_up_to_the_day(db, day) for day in days) == len(v1_rows) + len(v2_rows)


# --- DST ------------------------------------------------------------------------------------------------------------------

def test_on_the_spring_forward_date_no_v2_row_can_land_in_the_skipped_hour(db):
    _cutover(db, _utc(2026, 3, 1, 6, 0))
    # 01:59:59 CST, then the very next second, 03:00:00 CDT; and the day's two ends.
    _v2(db, _utc(2026, 3, 8, 6, 0), _utc(2026, 3, 8, 7, 59, 59), _utc(2026, 3, 8, 8, 0), _utc(2026, 3, 8, 8, 30),
        _utc(2026, 3, 9, 4, 59, 59))
    _v2(db, _utc(2026, 3, 8, 5, 59, 59), _utc(2026, 3, 9, 5, 0))     # just outside the 23-hour day

    result, recorder = _hourly(db, SPRING_FORWARD)

    assert _busy(result.counts) == {0: 1, 1: 1, 3: 2, 23: 1}
    assert result.counts[2] == 0
    parameters = recorder.parameters_for("checkin_events")
    assert parameters["boundary_2"] == parameters["boundary_3"] == _utc(2026, 3, 8, 8)   # a zero-wide hour
    assert parameters["boundary_24"] - parameters["boundary_0"] == timedelta(hours=23)
    assert _assert_the_hours_add_up_to_the_day(db, SPRING_FORWARD) == 5


def test_a_legacy_row_stamped_in_the_skipped_hour_is_counted_in_the_hour_it_names(db):
    # A wall-clock reading that should not exist. It is a row all the same: it
    # is counted on its day, and so it must be counted in one of the hours.
    _v1(db, _local(2026, 3, 8, 1, 59), _local(2026, 3, 8, 2, 15), _local(2026, 3, 8, 2, 45),
        _local(2026, 3, 8, 3, 0))

    result, _ = _hourly(db, SPRING_FORWARD)

    assert _busy(result.counts) == {1: 1, 2: 2, 3: 1}
    assert _assert_the_hours_add_up_to_the_day(db, SPRING_FORWARD) == 4


def test_a_cutover_inside_the_spring_forward_date_partitions_its_hours_correctly(db):
    _cutover(db, _utc(2026, 3, 8, 15, 30))            # 10:30 CDT, after the clocks went forward
    _v1(db, _local(2026, 3, 8, 1, 30), _local(2026, 3, 8, 2, 30), _local(2026, 3, 8, 10, 29, 59),
        _local(2026, 3, 8, 10, 30))
    _v2(db, _utc(2026, 3, 8, 15, 29, 59), _utc(2026, 3, 8, 15, 30), _utc(2026, 3, 9, 4, 0))

    result, _ = _hourly(db, SPRING_FORWARD)

    assert _busy(result.v1_counts) == {1: 1, 2: 1, 10: 1}
    assert _busy(result.v2_counts) == {10: 1, 23: 1}
    assert _busy(result.counts) == {1: 1, 2: 1, 10: 2, 23: 1}
    _assert_the_hours_add_up_to_the_day(db, SPRING_FORWARD)


def test_on_the_fall_back_date_both_passes_through_the_repeated_hour_share_one_bucket(db):
    _cutover(db, _utc(2026, 10, 1, 5, 0))
    # 00:59:59 CDT | 01:00 CDT, 01:30 CDT, 01:30 CST, 01:59:59 CST | 02:00 CST
    _v2(db, _utc(2026, 11, 1, 5, 59, 59), _utc(2026, 11, 1, 6, 0), FIRST_0130, SECOND_0130,
        _utc(2026, 11, 1, 7, 59, 59), _utc(2026, 11, 1, 8, 0))
    _v2(db, _utc(2026, 11, 1, 4, 59, 59), _utc(2026, 11, 2, 6, 0))   # just outside the 25-hour day

    result, recorder = _hourly(db, FALL_BACK)

    assert _busy(result.counts) == {0: 1, 1: 4, 2: 1}
    parameters = recorder.parameters_for("checkin_events")
    assert parameters["boundary_2"] - parameters["boundary_1"] == timedelta(hours=2)
    assert parameters["boundary_24"] - parameters["boundary_0"] == timedelta(hours=25)
    assert _assert_the_hours_add_up_to_the_day(db, FALL_BACK) == 6


def test_on_the_fall_back_date_legacy_rows_have_the_same_single_merged_bucket(db):
    # Two rows stamped 01:30 -- one from each pass, for all the data can say.
    _v1(db, _local(2026, 11, 1, 0, 30), _local(2026, 11, 1, 1, 30), _local(2026, 11, 1, 1, 30),
        _local(2026, 11, 1, 2, 30))

    result, _ = _hourly(db, FALL_BACK)

    assert _busy(result.counts) == {0: 1, 1: 2, 2: 1}
    assert len(result.counts) == 24                    # 24 buckets on a 25-hour day, too
    _assert_the_hours_add_up_to_the_day(db, FALL_BACK)


def test_a_cutover_inside_the_repeated_hour_still_adds_up_and_stays_in_that_one_bucket(db):
    """The 5a limitation, hour by hour. Which legacy rows of the repeated hour
    count is decided by wall-clock value alone, exactly as in the day count;
    whatever that decides, everything from the hour stays in bucket 1."""
    _v1(db, _local(2026, 11, 1, 0, 45), _local(2026, 11, 1, 1, 15), _local(2026, 11, 1, 1, 15),
        _local(2026, 11, 1, 1, 45))
    _v2(db, _utc(2026, 11, 1, 6, 45), _utc(2026, 11, 1, 7, 45), _utc(2026, 11, 1, 9, 0))

    _cutover(db, FIRST_0130, set_at="2026-10-01 00:00:00+00:00")
    first_pass, _ = _hourly(db, FALL_BACK)
    assert _busy(first_pass.v1_counts) == {0: 1, 1: 2}
    assert _busy(first_pass.v2_counts) == {1: 2, 3: 1}
    assert _assert_the_hours_add_up_to_the_day(db, FALL_BACK) == 6

    _cutover(db, SECOND_0130, set_at="2026-10-02 00:00:00+00:00")
    second_pass, _ = _hourly(db, FALL_BACK)
    assert second_pass.v1_counts == first_pass.v1_counts          # v1 cannot tell the two cutovers apart
    assert _busy(second_pass.v2_counts) == {1: 1, 3: 1}
    assert _assert_the_hours_add_up_to_the_day(db, FALL_BACK) == 5


# --- other zones ------------------------------------------------------------------------------------------------------

def test_in_a_half_hour_zone_a_v2_row_is_counted_in_its_local_hour_not_its_utc_hour(db):
    _cutover(db, _utc(2026, 6, 1, 0, 0))
    # 03:15Z and 03:45Z share a UTC hour; in Kolkata they are 08:45 and 09:15.
    _v2(db, _utc(2026, 6, 9, 18, 30), _utc(2026, 6, 10, 3, 15), _utc(2026, 6, 10, 3, 45),
        _utc(2026, 6, 10, 18, 29, 59))
    _v2(db, _utc(2026, 6, 9, 18, 29, 59), _utc(2026, 6, 10, 18, 30))   # just outside the Kolkata day

    result, recorder = _hourly(db, zone=KOLKATA)

    assert _busy(result.counts) == {0: 1, 8: 1, 9: 1, 23: 1}
    parameters = recorder.parameters_for("checkin_events")
    assert all(parameters[f"boundary_{index}"].minute == 30 for index in range(25))
    assert _assert_the_hours_add_up_to_the_day(db, zone=KOLKATA) == 4


def test_a_mid_hour_cutover_in_a_half_hour_zone_splits_the_local_hour(db):
    _cutover(db, _utc(2026, 6, 10, 4, 0))             # 09:30 in Kolkata
    _v1(db, _local(2026, 6, 10, 9, 10), _local(2026, 6, 10, 9, 29, 59), _local(2026, 6, 10, 9, 30))
    _v2(db, _utc(2026, 6, 10, 3, 59, 59), _utc(2026, 6, 10, 4, 0), _utc(2026, 6, 10, 4, 29, 59),
        _utc(2026, 6, 10, 4, 30))

    result, _ = _hourly(db, zone=KOLKATA)

    assert _busy(result.v1_counts) == {9: 2}
    assert _busy(result.v2_counts) == {9: 2, 10: 1}
    assert _busy(result.counts) == {9: 4, 10: 1}
    _assert_the_hours_add_up_to_the_day(db, zone=KOLKATA)


def test_the_hours_are_those_of_the_zone_it_is_given(db):
    _v1(db, _local(2026, 6, 10, 0, 30), _local(2026, 6, 10, 23, 30))
    _cutover(db, _utc(2026, 6, 10, 12, 0))            # 07:00 in Chicago, 17:30 in Kolkata
    _v2(db, _utc(2026, 6, 10, 12, 0), _utc(2026, 6, 10, 20, 0))

    # Chicago: v1 before 07:00 -> hour 0; v2 at 07:00 and 15:00 local.
    assert _by_hour(db, zone=CHICAGO) == {0: 1, 7: 1, 15: 1}
    # Kolkata: v1 before 17:30 -> hour 0; the 12:00Z row is 17:30 local; 20:00Z is already the next day.
    assert _by_hour(db, zone=KOLKATA) == {0: 1, 17: 1}


def test_zones_that_move_their_clocks_by_half_an_hour_or_at_midnight_still_add_up(db):
    _cutover(db, _utc(2026, 1, 1, 0, 0))
    # A v2 row every 20 minutes across both of 2026's clock changes in each zone.
    instants = []
    for start in (_utc(2026, 3, 7), _utc(2026, 4, 4), _utc(2026, 10, 3), _utc(2026, 10, 31)):
        instants += [start + timedelta(minutes=20 * step) for step in range(3 * 24 * 3)]
    _v2(db, *instants)

    for zone, days in (
        (LORD_HOWE, (date(2026, 4, 5), date(2026, 10, 4))),
        (HAVANA, (date(2026, 3, 8), date(2026, 11, 1))),
        (CHICAGO, (SPRING_FORWARD, FALL_BACK)),
    ):
        for day in days:
            bounds = local_day_bounds(day, zone)
            expected = sum(1 for instant in instants if bounds.v2_start_utc <= instant < bounds.v2_end_utc)

            assert expected > 0
            assert _assert_the_hours_add_up_to_the_day(db, day, zone=zone) == expected


# --- the hours always add up to the day -------------------------------------------------------------------------------

_ADD_UP_V1 = [_local(2026, 6, 9, 23, 59, 59), *[_local(2026, 6, 10, hour, minute) for hour in range(24)
                                                for minute in (0, 29, 30, 59)], _local(2026, 6, 11, 0, 0)]
_ADD_UP_V2 = [_utc(2026, 6, 10, 4, 59, 59), *[_utc(2026, 6, 10, 5) + timedelta(minutes=15 * step)
                                              for step in range(24 * 4)], _utc(2026, 6, 11, 5, 0)]


@pytest.mark.parametrize(
    "cutover_at",
    [
        None,
        _utc(2026, 6, 1, 17, 0),
        _utc(2026, 6, 10, 5, 0),
        NOON_CUTOVER,
        HALF_PAST_TWO_CUTOVER,
        _utc(2026, 6, 10, 19, 29, 59),
        _utc(2026, 6, 11, 4, 59, 59),
        _utc(2026, 6, 11, 5, 0),
        _utc(2026, 6, 20, 17, 0),
    ],
    ids=["no cutover", "before the day", "at the day's start", "on an hour boundary", "inside an hour",
         "one second before the half hour", "in the day's last second", "at the day's end", "after the day"],
)
@pytest.mark.parametrize("zone", [CHICAGO, KOLKATA], ids=["Chicago", "Kolkata"])
def test_the_24_hourly_counts_sum_to_the_days_count_wherever_the_cutover_is(db, cutover_at, zone):
    _v1(db, *_ADD_UP_V1)
    _v2(db, *_ADD_UP_V2)
    if cutover_at is not None:
        _cutover(db, cutover_at)

    assert _assert_the_hours_add_up_to_the_day(db, zone=zone) > 0

    hourly, _ = _hourly(db, zone=zone)
    assert hourly.counts == tuple(v1 + v2 for v1, v2 in zip(hourly.v1_counts, hourly.v2_counts, strict=True))


@pytest.mark.parametrize("local_date", [SPRING_FORWARD, FALL_BACK], ids=["spring forward", "fall back"])
@pytest.mark.parametrize("cutover_hour_utc", [None, 7, 8, 15], ids=["no cutover", "07:00Z", "08:00Z", "15:00Z"])
def test_the_24_hourly_counts_sum_to_the_days_count_on_dst_dates(db, local_date, cutover_hour_utc):
    year, month, day = local_date.year, local_date.month, local_date.day
    _v1(db, *[_local(year, month, day, hour, minute) for hour in range(24) for minute in (0, 30)])
    _v2(db, *[_utc(year, month, day, 4) + timedelta(minutes=30 * step) for step in range(56)])
    if cutover_hour_utc is not None:
        _cutover(db, _utc(year, month, day, cutover_hour_utc, 30))

    assert _assert_the_hours_add_up_to_the_day(db, local_date) > 0


# --- the tenant filter (SQLite has no RLS: only the statements' own WHERE separates these rows) -----------------------

def test_another_customers_rows_are_in_no_hour(db):
    _cutover(db, NOON_CUTOVER)
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 18, 0))
    _v1(db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0), customer_id=CUSTOMER_B)
    _v2(db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 19, 0), customer_id=CUSTOMER_B)

    assert _by_hour(db) == {9: 1, 13: 1}


def test_another_branchs_rows_are_in_no_hour(db):
    _cutover(db, NOON_CUTOVER)
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 18, 0))
    _v1(db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0), branch_id=BRANCH_B)
    _v2(db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 19, 0), branch_id=BRANCH_B)

    assert _by_hour(db) == {9: 1, 13: 1}


def test_another_tenants_cutover_does_not_change_this_tenants_hours(db):
    _cutover(db, _utc(2026, 6, 1, 5, 0), customer_id=CUSTOMER_B, branch_id=BRANCH_B)
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 18, 0))

    assert _by_hour(db) == {9: 1}     # this tenant has no cutover: v1 only


def test_each_tenant_gets_its_own_hours_from_the_same_tables(db):
    tenant_b = ResolvedOperationalTenant(
        org_slug="beta", branch_slug="main", access_mode="read_only",
        operational_customer_id=CUSTOMER_B, operational_branch_id=BRANCH_B,
    )
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v1(db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0), _local(2026, 6, 10, 10, 30),
        customer_id=CUSTOMER_B, branch_id=BRANCH_B)

    assert _by_hour(db, tenant=TENANT_A) == {9: 1}
    assert _by_hour(db, tenant=tenant_b) == {9: 1, 10: 2}


def test_every_hourly_statement_is_bound_to_the_resolved_tenants_ids(db):
    _cutover(db, NOON_CUTOVER)

    _, recorder = _hourly(db)

    assert len(recorder.statements) == 3
    for sql, parameters in recorder.statements:
        assert "WHERE customer_id = :customer_id AND branch_id = :branch_id" in sql
        assert parameters["customer_id"] == CUSTOMER_A
        assert parameters["branch_id"] == BRANCH_A


# --- the statements and their parameters ------------------------------------------------------------------------------

_HOURLY_COLUMNS = ", ".join(
    f"COUNT(*) FILTER (WHERE event_time >= :boundary_{hour} AND event_time < :boundary_{hour + 1})"
    for hour in range(24)
)


def test_the_v1_hourly_statement_has_the_approved_shape():
    assert _statement("_V1_CHECKIN_HOURLY_COUNT_SQL") == (
        f"SELECT {_HOURLY_COLUMNS} FROM checkins "
        "WHERE customer_id = :customer_id AND branch_id = :branch_id "
        "AND event_time >= :start_local AND event_time < :end_local"
    )


def test_the_v2_hourly_statement_has_the_approved_shape():
    assert _statement("_V2_CHECKIN_HOURLY_COUNT_SQL") == (
        f"SELECT {_HOURLY_COLUMNS} FROM checkin_events "
        "WHERE customer_id = :customer_id AND branch_id = :branch_id "
        "AND event_time >= :start_utc AND event_time < :end_utc"
    )


def test_each_hourly_statement_shares_its_where_clause_with_the_day_count():
    # The same rows are selected; only what is done with them differs.
    for hourly, daily in (("_V1_CHECKIN_HOURLY_COUNT_SQL", "_V1_CHECKIN_COUNT_SQL"),
                          ("_V2_CHECKIN_HOURLY_COUNT_SQL", "_V2_CHECKIN_COUNT_SQL")):
        assert _statement(hourly).split(" FROM ")[1] == _statement(daily).split(" FROM ")[1]


def test_the_hourly_statements_are_24_conditional_counts_over_one_table_each():
    for name in ("_V1_CHECKIN_HOURLY_COUNT_SQL", "_V2_CHECKIN_HOURLY_COUNT_SQL"):
        sql = _statement(name).upper()

        assert sql.count("COUNT(*) FILTER (WHERE EVENT_TIME >= :BOUNDARY_") == 24
        assert sql.count(" FROM ") == 1
        for forbidden in ("JOIN", "DISTINCT", "GROUP BY", "SELECT *", "REJECT", "ACS", "AT TIME ZONE", "NOW()",
                          "::DATE", "CURRENT_", "EXTRACT", "DATE_TRUNC", "TIMEZONE", "INTERVAL"):
            assert forbidden not in sql, (name, forbidden)


def test_the_hourly_statements_bind_naive_v1_times_and_aware_v2_times():
    time_binds = {f"boundary_{index}" for index in range(25)}

    v1 = operational_metrics_service._V1_CHECKIN_HOURLY_COUNT_SQL._bindparams
    v2 = operational_metrics_service._V2_CHECKIN_HOURLY_COUNT_SQL._bindparams
    assert set(v1) == {"customer_id", "branch_id", "start_local", "end_local"} | time_binds
    assert set(v2) == {"customer_id", "branch_id", "start_utc", "end_utc"} | time_binds

    assert all(v1[name].type.timezone is False for name in time_binds | {"start_local", "end_local"})
    assert all(v2[name].type.timezone is True for name in time_binds | {"start_utc", "end_utc"})


def test_every_time_actually_bound_is_naive_for_v1_and_aware_utc_for_v2(db):
    _cutover(db, HALF_PAST_TWO_CUTOVER)

    _, recorder = _hourly(db)

    v1 = {name: value for name, value in recorder.parameters_for("checkins").items() if isinstance(value, datetime)}
    v2 = {name: value for name, value in recorder.parameters_for("checkin_events").items()
          if isinstance(value, datetime)}
    assert len(v1) == len(v2) == 27
    assert all(value.tzinfo is None for value in v1.values())
    assert all(value.utcoffset() == timedelta(0) for value in v2.values())


def test_without_a_cutover_v1_gets_the_whole_local_day_of_hours(db):
    _, recorder = _hourly(db)

    parameters = recorder.parameters_for("checkins")
    assert (parameters["start_local"], parameters["end_local"]) == (_local(2026, 6, 10), _local(2026, 6, 11))
    assert (parameters["boundary_0"], parameters["boundary_24"]) == (_local(2026, 6, 10), _local(2026, 6, 11))


# --- how many statements run -------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cutover_at", "expected_tables"),
    [
        (None, ["v2_cutovers", "checkins"]),
        (_utc(2026, 6, 1, 17, 0), ["v2_cutovers", "checkin_events"]),
        (_utc(2026, 6, 10, 5, 0), ["v2_cutovers", "checkin_events"]),
        (NOON_CUTOVER, ["v2_cutovers", "checkins", "checkin_events"]),
        (HALF_PAST_TWO_CUTOVER, ["v2_cutovers", "checkins", "checkin_events"]),
        (_utc(2026, 6, 11, 5, 0), ["v2_cutovers", "checkins"]),
        (_utc(2026, 6, 20, 17, 0), ["v2_cutovers", "checkins"]),
    ],
    ids=["no cutover", "cutover before the day", "cutover at the day's start", "cutover on an hour boundary",
         "cutover inside an hour", "cutover at the day's end", "cutover after the day"],
)
def test_the_hourly_counts_take_one_lookup_and_one_statement_per_era_that_owns_part_of_the_day(
    db, cutover_at, expected_tables
):
    # A row of each era at a quarter past every hour, so one statement is shown to be enough for all 24.
    _v1(db, *[_local(2026, 6, 10, hour, 15) for hour in range(24)])
    _v2(db, *[_utc(2026, 6, 10, 5, 15) + timedelta(hours=hour) for hour in range(24)])    # the same 24 moments
    if cutover_at is not None:
        _cutover(db, cutover_at)

    result, recorder = _hourly(db)

    assert recorder.tables() == expected_tables
    assert recorder.tables().count("v2_cutovers") == 1
    assert len(recorder.statements) <= 3
    assert result.counts == (1,) * 24             # each moment once, whichever era's statement returned it


def test_the_hourly_counts_run_exactly_the_statements_the_day_count_runs(db):
    for cutover_at in (None, _utc(2026, 6, 1, 17, 0), HALF_PAST_TWO_CUTOVER, _utc(2026, 6, 20, 17, 0)):
        if cutover_at is not None:
            _cutover(db, cutover_at, set_at=cutover_at.isoformat(sep=" "))

        _, hourly = _hourly(db)
        _, daily = _count(db)

        assert hourly.tables() == daily.tables()


# --- failures -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("failing", ["v2_cutovers", "checkins", "checkin_events"])
def test_a_failure_in_any_hourly_statement_propagates_and_no_partial_counts_are_returned(db, failing):
    _cutover(db, NOON_CUTOVER)
    _v1(db, _local(2026, 6, 10, 9, 0))
    _v2(db, _utc(2026, 6, 10, 18, 0))

    with pytest.raises(RuntimeError, match=f"synthetic failure reading {failing}"):
        _hourly(db, fail_on=failing)


def test_failing_v2_hours_do_not_come_back_as_v1_only_hours(db):
    _cutover(db, NOON_CUTOVER)
    _v1(db, _local(2026, 6, 10, 9, 0))
    with db.begin() as conn:
        conn.execute(text("DROP TABLE checkin_events"))

    with pytest.raises(Exception, match="checkin_events"):
        _hourly(db)


def test_failing_v1_hours_do_not_come_back_as_v2_only_hours(db):
    _cutover(db, NOON_CUTOVER)
    _v2(db, _utc(2026, 6, 10, 18, 0))
    with db.begin() as conn:
        conn.execute(text("DROP TABLE checkins"))

    with pytest.raises(Exception, match="checkins"):
        _hourly(db)


def test_a_statement_that_does_not_return_24_counts_is_refused():
    with pytest.raises(ValueError, match="exactly 24 counts"):
        operational_metrics_service._hourly_counts((1, 2, 3))


# --- the result -----------------------------------------------------------------------------------------------------------

def test_the_hourly_result_is_an_immutable_value_of_three_tuples_of_24_plain_integers(db):
    _cutover(db, HALF_PAST_TWO_CUTOVER)
    _v1(db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 14, 0))
    _v2(db, _utc(2026, 6, 10, 19, 45))

    result, _ = _hourly(db)

    assert [f.name for f in dataclasses.fields(CheckinHourlyCounts)] == ["counts", "v1_counts", "v2_counts"]
    for counts in dataclasses.astuple(result):
        assert type(counts) is tuple
        assert len(counts) == 24
        assert all(type(count) is int and count >= 0 for count in counts)
    assert _busy(result.counts) == {9: 1, 14: 2}
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.counts = ()


def test_the_hourly_function_takes_a_connection_a_tenant_a_date_and_a_zone():
    parameters = inspect.signature(operational_metrics_service.get_checkin_counts_by_hour).parameters

    assert list(parameters) == ["conn", "tenant", "local_date", "zone"]
    assert parameters["local_date"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["zone"].kind is inspect.Parameter.KEYWORD_ONLY
    assert all(p.default is inspect.Parameter.empty for p in parameters.values())   # nothing defaults, least of all the date


def test_nothing_is_cached_between_hourly_counts(db):
    assert _by_hour(db) == {}

    _v1(db, _local(2026, 6, 10, 9, 0))
    assert _by_hour(db) == {9: 1}

    _cutover(db, _utc(2026, 6, 1, 5, 0))
    assert _by_hour(db) == {}

    _v2(db, _utc(2026, 6, 10, 18, 0))
    assert _by_hour(db) == {13: 1}


def test_the_hourly_service_knows_nothing_of_operating_hours_or_of_the_dashboard():
    source = inspect.getsource(operational_metrics_service.get_checkin_counts_by_hour)

    for forbidden in ("start_hour", "end_hour", "range(7", "mixed_era", "pandas", "streamlit", "fetchall", "now("):
        assert forbidden not in source, forbidden


# --- against the Streamlit dashboard (characterization only; the dashboard is not changed) ---------------------------

def _dashboard_hours(v1_rows, cutover_at, local_date) -> dict[int, int]:
    """What the dashboard's "Checkins by Hour" counts today, before its 7-20
    display window: mixed_era_service's frame, filtered to the day by
    metrics.get_date_filtered_df, then .dt.hour.value_counts()."""
    import pandas as pd

    import metrics
    from services import mixed_era_service

    v1 = pd.DataFrame({"datetime": [pd.Timestamp(row) for row in v1_rows], "barcode": ["synthetic"] * len(v1_rows)})
    v2 = pd.DataFrame(columns=["datetime", "item_key", "destination"])
    cutover = None if cutover_at is None else pd.Timestamp(cutover_at)

    mixed = mixed_era_service._build_mixed_checkins(v1, v2, cutover)
    day = metrics.get_date_filtered_df(mixed, local_date, local_date)
    return {int(hour): int(count) for hour, count in day["datetime"].dt.hour.value_counts().items()}


@pytest.mark.parametrize("local_date", [date(2026, 5, 31), date(2026, 6, 1), date(2026, 6, 2)])
def test_for_a_v1_only_branch_the_sql_hours_equal_the_dashboards_hours(db, local_date):
    _v1(db, *V1_ROWS)

    assert _by_hour(db, local_date) == _dashboard_hours(V1_ROWS, None, local_date)


def test_for_a_cut_over_branch_the_hours_intentionally_differ_from_the_dashboards_shifted_hours(db):
    """Known and deliberate, as for the day count. Once a branch has a cutover
    the dashboard moves each legacy row 5 hours earlier (CDT), so its hours
    are shifted and its earliest rows fall on the day before. This service
    counts each row in the hour it was stamped. Block 6 does not change the
    dashboard."""
    cutover_at = _utc(2026, 6, 10, 5, 0)
    _v1(db, *V1_ROWS)
    _cutover(db, cutover_at)

    assert _by_hour(db, date(2026, 6, 1)) == {0: 1, 2: 1, 9: 2, 19: 1, 23: 1}
    # 09:15 twice -> 04:15; 19:30 -> 14:30; 23:59:59 -> 18:59:59; and 2 June's 00:00 arrives as 19:00.
    assert _dashboard_hours(V1_ROWS, cutover_at, date(2026, 6, 1)) == {4: 2, 14: 1, 18: 1, 19: 1}


# =====================================================================================================================
# Block 7a: get_reject_count
# =====================================================================================================================
#
# The real SQL, against in-memory SQLite, as for the check-in count. This
# database holds the two reject tables and v2_cutovers -- and the two check-in
# tables as well, so it can be shown that neither count ever reads the other's
# rows. It holds no ACS table: a statement reaching for one would fail outright.
#
# The reject tables carry the columns that describe WHAT a reject was
# (error_message on the legacy table, error_class on the v2 one) so the tests
# can show that none of them changes the count.

RejectCount = operational_metrics_service.RejectCount

_REJECT_DDL = (
    (
        "CREATE TABLE rejects (id INTEGER PRIMARY KEY, customer_id INTEGER, branch_id INTEGER, event_time TEXT, "
        "barcode TEXT, error_message TEXT)"
    ),
    (
        "CREATE TABLE reject_events (id INTEGER PRIMARY KEY, customer_id INTEGER, branch_id INTEGER, key_id TEXT, "
        "event_key TEXT, event_time TEXT, error_class TEXT, item_key TEXT)"
    ),
    *_COUNT_DDL,   # checkins, checkin_events, v2_cutovers
)


@pytest.fixture
def reject_db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in _REJECT_DDL:
            conn.execute(text(statement))
    yield engine
    engine.dispose()


def _v1_reject(
    db, *stamped: datetime, customer_id=CUSTOMER_A, branch_id=BRANCH_A, barcode="synthetic",
    error_message="Item not found",
) -> None:
    """Legacy reject rows: each `stamped` is a NAIVE local wall-clock time."""
    with db.begin() as conn:
        for local_naive in stamped:
            assert local_naive.tzinfo is None
            conn.execute(
                text("INSERT INTO rejects (customer_id, branch_id, event_time, barcode, error_message) "
                     "VALUES (:c, :b, :t, :bc, :e)"),
                {"c": customer_id, "b": branch_id, "t": local_naive.strftime(_STORED), "bc": barcode,
                 "e": error_message},
            )


def _v2_reject(
    db, *instants: datetime, customer_id=CUSTOMER_A, branch_id=BRANCH_A, error_class="item_not_found", item_key=None,
) -> None:
    """Contract v2 reject rows: each instant is aware; it is stored as UTC."""
    with db.begin() as conn:
        for instant in instants:
            assert instant.tzinfo is not None
            conn.execute(
                text("INSERT INTO reject_events (customer_id, branch_id, event_time, error_class, item_key) "
                     "VALUES (:c, :b, :t, :e, :k)"),
                {"c": customer_id, "b": branch_id, "t": instant.astimezone(UTC).strftime(_STORED), "e": error_class,
                 "k": item_key},
            )


def _reject_count(db, local_date=JUNE_10, *, tenant=TENANT_A, zone=CHICAGO, fail_on=None):
    with db.connect() as conn:
        recorder = Recorder(conn, fail_on=fail_on)
        result = operational_metrics_service.get_reject_count(recorder, tenant, local_date=local_date, zone=zone)
    return result, recorder


def _reject_counts(db, local_date=JUNE_10, **kwargs) -> tuple[int, int, int]:
    result, _ = _reject_count(db, local_date, **kwargs)
    return result.total, result.v1_count, result.v2_count


# --- no cutover: the branch is v1 only --------------------------------------------------------------------------------

def test_a_v1_only_branch_counts_the_rejects_of_the_local_day(reject_db):
    _v1_reject(reject_db, _local(2026, 6, 9, 23, 59, 59), _local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 9, 30),
               _local(2026, 6, 10, 23, 59, 59), _local(2026, 6, 11, 0, 0))

    result, recorder = _reject_count(reject_db)

    assert result == RejectCount(total=3, v1_count=3, v2_count=0)
    assert recorder.tables() == ["v2_cutovers", "rejects"]   # the v2 table is never read


def test_with_no_cutover_v2_rejects_are_ignored_even_if_they_exist(reject_db):
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 15, 0), _utc(2026, 6, 10, 16, 0))

    result, recorder = _reject_count(reject_db)

    assert (result.total, result.v1_count, result.v2_count) == (1, 1, 0)
    assert "reject_events" not in recorder.tables()


def test_a_tenant_with_no_rejects_has_a_count_of_zero(reject_db):
    result, recorder = _reject_count(reject_db)

    assert result == RejectCount(total=0, v1_count=0, v2_count=0)
    assert recorder.tables() == ["v2_cutovers", "rejects"]


def test_a_rollback_makes_the_branch_v1_only_again_for_rejects(reject_db):
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 15, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 20, 0))
    _cutover(reject_db, NOON_CUTOVER, set_at="2026-06-01 00:00:00+00:00")
    _cutover(reject_db, None, set_at="2026-06-12 00:00:00+00:00")   # the latest record: a rollback

    result, recorder = _reject_count(reject_db)

    assert (result.total, result.v1_count, result.v2_count) == (2, 2, 0)   # 15:00 is v1's again; v2 is not read
    assert recorder.tables() == ["v2_cutovers", "rejects"]


# --- what is counted ----------------------------------------------------------------------------------------------------

def test_every_reject_row_is_counted_with_no_deduplication(reject_db):
    # The same item, the same second, the same message, three times over: three rows, three rejects.
    _v1_reject(reject_db, *[_local(2026, 6, 10, 9, 0)] * 3, barcode="same-barcode")
    _cutover(reject_db, NOON_CUTOVER)
    _v2_reject(reject_db, *[_utc(2026, 6, 10, 18, 0)] * 2, item_key="a" * 64)

    assert _reject_counts(reject_db) == (5, 3, 2)


def test_an_item_rejected_several_times_in_a_day_counts_each_time(reject_db):
    _v1_reject(reject_db, _local(2026, 6, 10, 8, 0), _local(2026, 6, 10, 8, 5), _local(2026, 6, 10, 11, 30),
               barcode="one-item")
    _cutover(reject_db, NOON_CUTOVER)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 18, 1), _utc(2026, 6, 10, 21, 0),
               _utc(2026, 6, 10, 23, 0), item_key="b" * 64)

    assert _reject_counts(reject_db) == (7, 3, 4)


def test_rejects_with_no_item_identifier_are_counted(reject_db):
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 9, 1), barcode=None)
    _cutover(reject_db, NOON_CUTOVER)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), item_key=None)

    assert _reject_counts(reject_db) == (3, 2, 1)


V1_REASONS = ["Item not found", "ACS timeout", "Multiple RFID tags detected", "Collection code missing",
              "Library not found", "Something the dashboard calls Other", "", None]
V2_REASONS = ["item_not_found", "ils_acs_failure", "rfid_collision", "configuration_error", "routing_error",
              "communication_error", "other", "unknown"]


def test_every_kind_of_reject_counts_whatever_its_reason(reject_db):
    for minute, message in enumerate(V1_REASONS):
        _v1_reject(reject_db, _local(2026, 6, 10, 9, minute), error_message=message)
    _cutover(reject_db, NOON_CUTOVER)
    for minute, error_class in enumerate(V2_REASONS):
        _v2_reject(reject_db, _utc(2026, 6, 10, 18, minute), error_class=error_class)

    # Nothing is excluded: not "other", not "unknown", not a missing message.
    assert _reject_counts(reject_db) == (16, 8, 8)


def test_the_reason_columns_are_never_read(reject_db):
    _cutover(reject_db, NOON_CUTOVER)

    _, recorder = _reject_count(reject_db)

    for sql, _ in recorder.statements:
        for column in ("error_message", "error_class", "barcode", "item_key", "event_key", "key_id"):
            assert column not in sql, column


# --- rejects and check-ins are separate counts ------------------------------------------------------------------------

def test_check_in_rows_do_not_affect_the_reject_count(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))
    before = _reject_counts(reject_db)

    _v1(reject_db, *[_local(2026, 6, 10, 9, minute) for minute in range(10)])
    _v2(reject_db, *[_utc(2026, 6, 10, 18, minute) for minute in range(20)])

    result, recorder = _reject_count(reject_db)
    assert (result.total, result.v1_count, result.v2_count) == before == (2, 1, 1)
    assert recorder.tables() == ["v2_cutovers", "rejects", "reject_events"]   # neither check-in table is read


def test_reject_rows_do_not_affect_the_check_in_count(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0))
    _v2(reject_db, _utc(2026, 6, 10, 18, 0))
    before = _counts(reject_db)
    hours_before = _by_hour(reject_db)

    _v1_reject(reject_db, *[_local(2026, 6, 10, 9, minute) for minute in range(10)])
    _v2_reject(reject_db, *[_utc(2026, 6, 10, 18, minute) for minute in range(20)])

    result, recorder = _count(reject_db)
    assert (result.total, result.v1_count, result.v2_count) == before == (3, 2, 1)
    assert recorder.tables() == ["v2_cutovers", "checkins", "checkin_events"]   # neither reject table is read
    assert _by_hour(reject_db) == hours_before == {9: 1, 10: 1, 13: 1}


# --- the cutover's position relative to the requested day -------------------------------------------------------------

def test_a_cutover_inside_the_day_splits_its_rejects_between_the_eras(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    # v1, local wall clock: two before noon count; one at noon and one after do not (v1 is strictly before).
    _v1_reject(reject_db, _local(2026, 6, 10, 8, 0), _local(2026, 6, 10, 11, 59, 59), _local(2026, 6, 10, 12, 0),
               _local(2026, 6, 10, 15, 0))
    # v2, instants: one before the cutover does not count; at it and after it do; the next local midnight does not.
    _v2_reject(reject_db, _utc(2026, 6, 10, 16, 59, 59), _utc(2026, 6, 10, 17, 0), _utc(2026, 6, 10, 22, 0),
               _utc(2026, 6, 11, 4, 59, 59), _utc(2026, 6, 11, 5, 0))

    result, recorder = _reject_count(reject_db)

    assert (result.total, result.v1_count, result.v2_count) == (5, 2, 3)
    assert recorder.tables() == ["v2_cutovers", "rejects", "reject_events"]


def test_a_reject_exactly_at_the_cutover_belongs_to_v2_and_one_second_before_to_v1(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 11, 59, 59))          # one second before, as v1 holds it
    _v1_reject(reject_db, _local(2026, 6, 10, 12, 0, 0))            # the cutover moment as a v1 row: not v1's
    _v2_reject(reject_db, _utc(2026, 6, 10, 16, 59, 59))            # one second before, as v2 holds it: not v2's
    _v2_reject(reject_db, _utc(2026, 6, 10, 17, 0, 0))              # the cutover moment as a v2 row

    assert _reject_counts(reject_db) == (2, 1, 1)


def test_a_cutover_in_the_middle_of_an_hour_splits_rejects_at_that_instant(reject_db):
    _cutover(reject_db, _utc(2026, 6, 10, 19, 30))                  # 14:30 local
    _v1_reject(reject_db, _local(2026, 6, 10, 14, 29, 59), _local(2026, 6, 10, 14, 30), _local(2026, 6, 10, 14, 45))
    _v2_reject(reject_db, _utc(2026, 6, 10, 19, 29, 59), _utc(2026, 6, 10, 19, 30), _utc(2026, 6, 10, 19, 45))

    assert _reject_counts(reject_db) == (3, 1, 2)


def test_a_cutover_before_the_day_makes_it_a_v2_day_for_rejects(reject_db):
    _cutover(reject_db, _utc(2026, 6, 1, 17, 0))
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))                # a legacy row after the cutover: not counted
    _v2_reject(reject_db, _utc(2026, 6, 10, 5, 0), _utc(2026, 6, 10, 15, 0), _utc(2026, 6, 11, 4, 59, 59))
    _v2_reject(reject_db, _utc(2026, 6, 10, 4, 59, 59), _utc(2026, 6, 11, 5, 0))   # just outside the local day

    result, recorder = _reject_count(reject_db)

    assert (result.total, result.v1_count, result.v2_count) == (3, 0, 3)
    assert recorder.tables() == ["v2_cutovers", "reject_events"]    # no v1 query at all


def test_a_cutover_after_the_day_leaves_it_a_v1_day_for_rejects(reject_db):
    _cutover(reject_db, _utc(2026, 6, 20, 17, 0))
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 21, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 15, 0))                 # a v2 row before the cutover: not counted

    result, recorder = _reject_count(reject_db)

    assert (result.total, result.v1_count, result.v2_count) == (2, 2, 0)
    assert recorder.tables() == ["v2_cutovers", "rejects"]          # no v2 query at all


def test_a_cutover_at_the_local_midnight_that_starts_the_day_gives_the_days_rejects_to_v2(reject_db):
    _cutover(reject_db, _utc(2026, 6, 10, 5, 0))                    # 00:00 local on 10 June
    _v1_reject(reject_db, _local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 9, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 5, 0), _utc(2026, 6, 10, 15, 0))

    result, recorder = _reject_count(reject_db)

    assert (result.total, result.v1_count, result.v2_count) == (2, 0, 2)
    assert recorder.tables() == ["v2_cutovers", "reject_events"]

    # ...and the day before is entirely v1's.
    _v1_reject(reject_db, _local(2026, 6, 9, 23, 59, 59))
    assert _reject_counts(reject_db, date(2026, 6, 9)) == (1, 1, 0)


def test_a_cutover_at_the_next_local_midnight_gives_the_days_rejects_to_v1(reject_db):
    _cutover(reject_db, _utc(2026, 6, 11, 5, 0))                    # 00:00 local on 11 June
    _v1_reject(reject_db, _local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 23, 59, 59))
    _v2_reject(reject_db, _utc(2026, 6, 10, 15, 0), _utc(2026, 6, 11, 5, 0))

    result, recorder = _reject_count(reject_db)

    assert (result.total, result.v1_count, result.v2_count) == (2, 2, 0)
    assert recorder.tables() == ["v2_cutovers", "rejects"]

    # ...and the day after is entirely v2's.
    assert _reject_counts(reject_db, date(2026, 6, 11)) == (1, 0, 1)


def test_the_days_around_a_cutover_add_up_with_no_reject_lost_and_none_counted_twice(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    v1_rows = [_local(2026, 6, 9, 10, 0), _local(2026, 6, 9, 23, 30), _local(2026, 6, 10, 0, 30),
               _local(2026, 6, 10, 11, 0)]
    v2_rows = [_utc(2026, 6, 10, 17, 0), _utc(2026, 6, 10, 23, 0), _utc(2026, 6, 11, 4, 0), _utc(2026, 6, 11, 5, 0),
               _utc(2026, 6, 11, 20, 0)]
    _v1_reject(reject_db, *v1_rows)
    _v2_reject(reject_db, *v2_rows)

    per_day = [_reject_counts(reject_db, day) for day in (date(2026, 6, 9), date(2026, 6, 10), date(2026, 6, 11))]

    assert per_day == [(2, 2, 0), (5, 2, 3), (2, 0, 2)]
    assert sum(total for total, _, _ in per_day) == len(v1_rows) + len(v2_rows)


# --- time zones and DST -----------------------------------------------------------------------------------------------

def test_an_evening_v2_reject_counts_on_its_local_day_not_its_utc_day(reject_db):
    _cutover(reject_db, _utc(2026, 6, 1, 5, 0))
    _v2_reject(reject_db, _utc(2026, 6, 11, 1, 30))                 # 20:30 on 10 June in Chicago; 11 June in UTC

    assert _reject_counts(reject_db, date(2026, 6, 10)) == (1, 0, 1)
    assert _reject_counts(reject_db, date(2026, 6, 11)) == (0, 0, 0)


def test_rejects_on_the_spring_forward_date(reject_db):
    # v1-only: every wall-clock time on 8 March -- one stamped in the hour that did not exist -- none on either side.
    _v1_reject(reject_db, _local(2026, 3, 7, 23, 59, 59), _local(2026, 3, 8, 0, 0), _local(2026, 3, 8, 2, 30),
               _local(2026, 3, 8, 3, 0), _local(2026, 3, 8, 23, 59, 59), _local(2026, 3, 9, 0, 0))
    assert _reject_counts(reject_db, SPRING_FORWARD) == (4, 4, 0)

    # Once cut over before that date: the day is [06:00Z, 05:00Z next day) -- 23 hours.
    _cutover(reject_db, _utc(2026, 3, 1, 6, 0))
    _v2_reject(reject_db, _utc(2026, 3, 8, 5, 59, 59), _utc(2026, 3, 8, 6, 0), _utc(2026, 3, 9, 4, 59, 59),
               _utc(2026, 3, 9, 5, 0))

    result, recorder = _reject_count(reject_db, SPRING_FORWARD)

    assert (result.total, result.v1_count, result.v2_count) == (2, 0, 2)
    parameters = recorder.parameters_for("reject_events")
    assert parameters["end_utc"] - parameters["start_utc"] == timedelta(hours=23)


def test_rejects_on_the_fall_back_date(reject_db):
    # v1-only: both passes through 01:30 are just rows stamped 01:30 on 1 November.
    _v1_reject(reject_db, _local(2026, 10, 31, 23, 59, 59), _local(2026, 11, 1, 0, 0), _local(2026, 11, 1, 1, 30),
               _local(2026, 11, 1, 1, 30), _local(2026, 11, 1, 23, 59, 59), _local(2026, 11, 2, 0, 0))
    assert _reject_counts(reject_db, FALL_BACK) == (4, 4, 0)

    # Once cut over before that date: the day is [05:00Z, 06:00Z next day) -- 25 hours.
    _cutover(reject_db, _utc(2026, 10, 1, 5, 0))
    _v2_reject(reject_db, _utc(2026, 11, 1, 4, 59, 59), _utc(2026, 11, 1, 5, 0), FIRST_0130, SECOND_0130,
               _utc(2026, 11, 2, 5, 59, 59), _utc(2026, 11, 2, 6, 0))

    result, recorder = _reject_count(reject_db, FALL_BACK)

    assert (result.total, result.v1_count, result.v2_count) == (4, 0, 4)   # both real 01:30s are counted, once each
    parameters = recorder.parameters_for("reject_events")
    assert parameters["end_utc"] - parameters["start_utc"] == timedelta(hours=25)


def test_a_cutover_inside_a_dst_date_partitions_its_rejects_correctly(reject_db):
    _cutover(reject_db, _utc(2026, 3, 8, 15, 0))                    # 10:00 CDT, after the clocks went forward
    _v1_reject(reject_db, _local(2026, 3, 8, 1, 30), _local(2026, 3, 8, 9, 59, 59), _local(2026, 3, 8, 10, 0))
    _v2_reject(reject_db, _utc(2026, 3, 8, 14, 59, 59), _utc(2026, 3, 8, 15, 0), _utc(2026, 3, 9, 4, 0))

    result, recorder = _reject_count(reject_db, SPRING_FORWARD)

    assert (result.total, result.v1_count, result.v2_count) == (4, 2, 2)
    assert recorder.parameters_for("rejects")["end_local"] == _local(2026, 3, 8, 10, 0)


def test_the_reject_count_is_made_in_the_zone_it_is_given(reject_db):
    _v1_reject(reject_db, _local(2026, 6, 10, 0, 30), _local(2026, 6, 10, 23, 30))
    _cutover(reject_db, _utc(2026, 6, 10, 12, 0))                   # 07:00 in Chicago, 17:30 in Kolkata
    _v2_reject(reject_db, _utc(2026, 6, 10, 12, 0), _utc(2026, 6, 10, 20, 0))

    # Chicago: v1 before 07:00 local -> 1; v2 from 12:00Z to 05:00Z next day -> 2.
    assert _reject_counts(reject_db, zone=CHICAGO) == (3, 1, 2)
    # Kolkata: v1 before 17:30 local -> 1; the local day ends at 18:30Z, so only the 12:00Z row is v2's.
    assert _reject_counts(reject_db, zone=KOLKATA) == (2, 1, 1)


def test_a_half_hour_zone_bounds_the_v2_day_on_the_utc_half_hour(reject_db):
    _cutover(reject_db, _utc(2026, 6, 1, 0, 0))
    # The Kolkata day of 10 June is [18:30Z on the 9th, 18:30Z on the 10th).
    _v2_reject(reject_db, _utc(2026, 6, 9, 18, 29, 59), _utc(2026, 6, 9, 18, 30), _utc(2026, 6, 10, 18, 29, 59),
               _utc(2026, 6, 10, 18, 30))

    result, recorder = _reject_count(reject_db, zone=KOLKATA)

    assert (result.total, result.v1_count, result.v2_count) == (2, 0, 2)
    parameters = recorder.parameters_for("reject_events")
    assert (parameters["start_utc"], parameters["end_utc"]) == (_utc(2026, 6, 9, 18, 30), _utc(2026, 6, 10, 18, 30))


# --- the tenant filter (SQLite has no RLS: only the statements' own WHERE separates these rows) -----------------------

def test_another_customers_rejects_are_never_counted(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0), customer_id=CUSTOMER_B)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 19, 0), customer_id=CUSTOMER_B)

    assert _reject_counts(reject_db) == (2, 1, 1)


def test_another_branchs_rejects_are_never_counted(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0), branch_id=BRANCH_B)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 19, 0), branch_id=BRANCH_B)

    assert _reject_counts(reject_db) == (2, 1, 1)


def test_another_tenants_cutover_does_not_change_this_tenants_reject_count(reject_db):
    _cutover(reject_db, _utc(2026, 6, 1, 5, 0), customer_id=CUSTOMER_B, branch_id=BRANCH_B)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))

    assert _reject_counts(reject_db) == (1, 1, 0)   # this tenant has no cutover: v1 only


def test_each_tenant_gets_its_own_reject_count_from_the_same_tables(reject_db):
    tenant_b = ResolvedOperationalTenant(
        org_slug="beta", branch_slug="main", access_mode="read_only",
        operational_customer_id=CUSTOMER_B, operational_branch_id=BRANCH_B,
    )
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0), _local(2026, 6, 10, 11, 0),
               customer_id=CUSTOMER_B, branch_id=BRANCH_B)

    assert _reject_counts(reject_db, tenant=TENANT_A) == (1, 1, 0)
    assert _reject_counts(reject_db, tenant=tenant_b) == (3, 3, 0)   # a read_only tenant counts like any other


def test_every_reject_statement_is_bound_to_the_resolved_tenants_ids(reject_db):
    _cutover(reject_db, NOON_CUTOVER)

    _, recorder = _reject_count(reject_db)

    assert len(recorder.statements) == 3
    for sql, parameters in recorder.statements:
        assert "customer_id = :customer_id AND branch_id = :branch_id" in sql
        assert parameters["customer_id"] == CUSTOMER_A
        assert parameters["branch_id"] == BRANCH_A


# --- the statements and their parameters ------------------------------------------------------------------------------

def test_the_v1_reject_statement_has_the_approved_shape():
    assert _statement("_V1_REJECT_COUNT_SQL") == (
        "SELECT COUNT(*) FROM rejects "
        "WHERE customer_id = :customer_id AND branch_id = :branch_id "
        "AND event_time >= :start_local AND event_time < :end_local"   # strictly before its upper bound
    )


def test_the_v2_reject_statement_has_the_approved_shape():
    assert _statement("_V2_REJECT_COUNT_SQL") == (
        "SELECT COUNT(*) FROM reject_events "
        "WHERE customer_id = :customer_id AND branch_id = :branch_id "
        "AND event_time >= :start_utc AND event_time < :end_utc"      # from its lower bound, inclusive
    )


def test_the_reject_statements_only_count_rows_of_one_table_each():
    for name in ("_V1_REJECT_COUNT_SQL", "_V2_REJECT_COUNT_SQL"):
        sql = _statement(name).upper()

        assert sql.startswith("SELECT COUNT(*) FROM REJECT")
        assert sql.count(" FROM ") == 1
        for forbidden in ("JOIN", "DISTINCT", "GROUP BY", "SELECT *", "CHECKIN", "ACS", "REJECTS_CLEAN",
                          "AT TIME ZONE", "NOW()", "::DATE", "CURRENT_", "TIMEZONE", "ERROR_", "BARCODE", "ITEM_KEY"):
            assert forbidden not in sql, (name, forbidden)


def test_the_reject_count_binds_naive_v1_bounds_and_aware_utc_v2_bounds(reject_db):
    _cutover(reject_db, NOON_CUTOVER)

    _, recorder = _reject_count(reject_db)

    v1, v2 = recorder.parameters_for("rejects"), recorder.parameters_for("reject_events")
    assert (v1["start_local"], v1["end_local"]) == (_local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 12, 0))
    assert v1["start_local"].tzinfo is None and v1["end_local"].tzinfo is None
    assert (v2["start_utc"], v2["end_utc"]) == (_utc(2026, 6, 10, 17, 0), _utc(2026, 6, 11, 5, 0))
    assert v2["start_utc"].utcoffset() == timedelta(0) and v2["end_utc"].utcoffset() == timedelta(0)


def test_the_reject_bound_parameters_declare_their_time_zone_handling():
    v1 = operational_metrics_service._V1_REJECT_COUNT_SQL._bindparams
    v2 = operational_metrics_service._V2_REJECT_COUNT_SQL._bindparams
    assert set(v1) == {"customer_id", "branch_id", "start_local", "end_local"}
    assert set(v2) == {"customer_id", "branch_id", "start_utc", "end_utc"}

    assert v1["start_local"].type.timezone is False and v1["end_local"].type.timezone is False
    assert v2["start_utc"].type.timezone is True and v2["end_utc"].type.timezone is True


def test_the_reject_count_binds_exactly_what_the_check_in_count_binds(reject_db):
    # The same day and the same cutover give both metrics the same bounds: one time model, two pairs of tables.
    for cutover_at in (None, _utc(2026, 6, 1, 17, 0), NOON_CUTOVER, _utc(2026, 6, 10, 19, 30), _utc(2026, 6, 20, 17, 0)):
        if cutover_at is not None:
            _cutover(reject_db, cutover_at, set_at=cutover_at.isoformat(sep=" "))

        _, rejects = _reject_count(reject_db)
        _, checkins = _count(reject_db)

        renamed = [table.replace("checkin_events", "reject_events").replace("checkins", "rejects")
                   for table in checkins.tables()]
        assert rejects.tables() == renamed
        assert [parameters for _, parameters in rejects.statements] == [
            parameters for _, parameters in checkins.statements
        ]


def test_without_a_cutover_v1_gets_the_whole_local_day_of_rejects(reject_db):
    _, recorder = _reject_count(reject_db)

    parameters = recorder.parameters_for("rejects")
    assert (parameters["start_local"], parameters["end_local"]) == (_local(2026, 6, 10), _local(2026, 6, 11))


# --- how many statements run -------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cutover_at", "expected_tables"),
    [
        (None, ["v2_cutovers", "rejects"]),
        (_utc(2026, 6, 1, 17, 0), ["v2_cutovers", "reject_events"]),
        (_utc(2026, 6, 10, 5, 0), ["v2_cutovers", "reject_events"]),
        (NOON_CUTOVER, ["v2_cutovers", "rejects", "reject_events"]),
        (_utc(2026, 6, 11, 5, 0), ["v2_cutovers", "rejects"]),
        (_utc(2026, 6, 20, 17, 0), ["v2_cutovers", "rejects"]),
    ],
    ids=["no cutover", "cutover before the day", "cutover at the day's start", "cutover inside the day",
         "cutover at the day's end", "cutover after the day"],
)
def test_the_cutover_is_looked_up_once_and_only_the_eras_that_own_part_of_the_day_count_rejects(
    reject_db, cutover_at, expected_tables
):
    if cutover_at is not None:
        _cutover(reject_db, cutover_at)

    result, recorder = _reject_count(reject_db)

    assert recorder.tables() == expected_tables
    assert recorder.tables().count("v2_cutovers") == 1
    assert len(recorder.statements) <= 3
    assert result.total == result.v1_count + result.v2_count == 0


# --- failures -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("failing", ["v2_cutovers", "rejects", "reject_events"])
def test_a_failure_in_any_reject_statement_propagates_and_no_partial_count_is_returned(reject_db, failing):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))

    with pytest.raises(RuntimeError, match=f"synthetic failure reading {failing}"):
        _reject_count(reject_db, fail_on=failing)


def test_a_failing_v2_reject_count_does_not_come_back_as_a_v1_only_total(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    with reject_db.begin() as conn:
        conn.execute(text("DROP TABLE reject_events"))

    with pytest.raises(Exception, match="reject_events"):
        _reject_count(reject_db)


def test_a_failing_v1_reject_count_does_not_come_back_as_a_v2_only_total(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))
    with reject_db.begin() as conn:
        conn.execute(text("DROP TABLE rejects"))

    with pytest.raises(Exception, match="rejects"):
        _reject_count(reject_db)


def test_the_reject_count_needs_neither_check_in_table(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))
    with reject_db.begin() as conn:
        conn.execute(text("DROP TABLE checkins"))
        conn.execute(text("DROP TABLE checkin_events"))

    assert _reject_counts(reject_db) == (2, 1, 1)


# --- the result -----------------------------------------------------------------------------------------------------------

def test_the_reject_result_is_an_immutable_value_whose_total_is_the_sum_of_the_eras(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))

    result, _ = _reject_count(reject_db)

    assert [f.name for f in dataclasses.fields(RejectCount)] == ["total", "v1_count", "v2_count"]
    assert result == RejectCount(total=3, v1_count=2, v2_count=1)
    assert all(type(value) is int for value in dataclasses.astuple(result))
    assert result.total == result.v1_count + result.v2_count
    assert RejectCount is not operational_metrics_service.CheckinCount   # never mistaken for a check-in count
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.total = 99


def test_the_reject_function_takes_a_connection_a_tenant_a_date_and_a_zone():
    parameters = inspect.signature(operational_metrics_service.get_reject_count).parameters

    assert list(parameters) == ["conn", "tenant", "local_date", "zone"]
    assert parameters["local_date"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["zone"].kind is inspect.Parameter.KEYWORD_ONLY
    assert all(p.default is inspect.Parameter.empty for p in parameters.values())   # nothing defaults, least of all the date


def test_nothing_is_cached_between_reject_counts(reject_db):
    assert _reject_counts(reject_db) == (0, 0, 0)

    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    assert _reject_counts(reject_db) == (1, 1, 0)

    _cutover(reject_db, _utc(2026, 6, 1, 5, 0))
    assert _reject_counts(reject_db) == (0, 0, 0)

    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))
    assert _reject_counts(reject_db) == (1, 0, 1)


def test_the_reject_service_knows_nothing_of_reasons_rates_or_the_dashboard():
    source = inspect.getsource(operational_metrics_service.get_reject_count)

    for forbidden in ("error_", "simplify", "reason", "rate", "checkin_events", "get_checkin", "mixed_era", "pandas",
                      "streamlit", "fetchall", "now("):
        assert forbidden not in source.replace("get_checkin_count:", ""), forbidden


def test_adding_the_reject_count_left_the_check_in_functions_as_they_were():
    # The reject count repeats the check-in count's few lines of era clamping rather than sharing them, so that
    # nothing about the check-in functions had to change. Their statements are still their own.
    checkin_source = inspect.getsource(operational_metrics_service.get_checkin_count)
    hourly_source = inspect.getsource(operational_metrics_service.get_checkin_counts_by_hour)

    assert "_V1_CHECKIN_COUNT_SQL" in checkin_source and "_V2_CHECKIN_COUNT_SQL" in checkin_source
    assert "_V1_CHECKIN_HOURLY_COUNT_SQL" in hourly_source and "_V2_CHECKIN_HOURLY_COUNT_SQL" in hourly_source
    assert "REJECT" not in checkin_source.upper() and "REJECT" not in hourly_source.upper()


# --- against the Streamlit dashboard (characterization only; the dashboard is not changed) ---------------------------

def _dashboard_reject_count(v1_rows, v2_rows, cutover_at, local_date) -> int:
    """What Live Today's "Rejects" card shows for `local_date`:
    mixed_era_service's reject frame, then metrics.get_today_metrics, which
    filters it to the day and takes len()."""
    import pandas as pd

    import metrics
    from services import mixed_era_service

    v1 = pd.DataFrame({"datetime": [pd.Timestamp(row) for row in v1_rows], "barcode": ["synthetic"] * len(v1_rows),
                       "error_message": ["Item not found"] * len(v1_rows)})
    v2 = pd.DataFrame({"datetime": [pd.Timestamp(row) for row in v2_rows], "item_key": ["k"] * len(v2_rows),
                       "error_class": ["item_not_found"] * len(v2_rows)})
    if not v1_rows:
        v1 = pd.DataFrame(columns=["datetime", "barcode", "error_message"])
    if not v2_rows:
        v2 = pd.DataFrame(columns=["datetime", "item_key", "error_class"])

    cutover = None if cutover_at is None else pd.Timestamp(cutover_at)
    mixed = mixed_era_service._build_mixed_rejects(v1, v2, cutover)
    no_checkins = pd.DataFrame(columns=["datetime", "barcode"])
    return metrics.get_today_metrics(no_checkins, mixed, local_date)["today_rejects"]


@pytest.mark.parametrize("local_date", [date(2026, 5, 30), date(2026, 5, 31), date(2026, 6, 1), date(2026, 6, 2)])
def test_for_a_v1_only_branch_the_sql_reject_count_equals_the_dashboards(reject_db, local_date):
    _v1_reject(reject_db, *V1_ROWS)

    total, v1_count, v2_count = _reject_counts(reject_db, local_date)

    assert total == _dashboard_reject_count(V1_ROWS, [], None, local_date)
    assert (v1_count, v2_count) == (total, 0)


def test_for_a_cut_over_branch_v2_rejects_are_counted_as_the_dashboard_counts_them(reject_db):
    # The dashboard's handling of v2 instants is correct; only its legacy rows are shifted. With v2 rows alone on
    # each side of a local midnight, the two agree.
    cutover_at = _utc(2026, 6, 1, 5, 0)
    v2_rows = [_utc(2026, 6, 10, 5, 0), _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 11, 4, 59, 59), _utc(2026, 6, 11, 5, 0)]
    _cutover(reject_db, cutover_at)
    _v2_reject(reject_db, *v2_rows)

    for day, expected in ((date(2026, 6, 10), 3), (date(2026, 6, 11), 1)):
        assert _reject_counts(reject_db, day)[0] == _dashboard_reject_count([], v2_rows, cutover_at, day) == expected


def test_for_a_cut_over_branch_the_reject_count_intentionally_differs_from_the_dashboard_on_legacy_days(reject_db):
    """Known and deliberate, exactly as for check-ins. Once a branch has a
    cutover, the dashboard labels legacy naive local reject times as UTC and
    converts them to Central, moving each 5-6 hours earlier; rejects from the
    first hours of a local day land on the day before. This service counts
    each reject on the local day it was stamped. The dashboard is not changed
    by Block 7."""
    cutover_at = _utc(2026, 6, 10, 5, 0)
    _v1_reject(reject_db, *V1_ROWS)
    _cutover(reject_db, cutover_at)

    intended = {day: _reject_counts(reject_db, day)[0]
                for day in (date(2026, 5, 31), date(2026, 6, 1), date(2026, 6, 2))}
    dashboard = {day: _dashboard_reject_count(V1_ROWS, [], cutover_at, day) for day in intended}

    assert intended == {date(2026, 5, 31): 1, date(2026, 6, 1): 6, date(2026, 6, 2): 2}
    assert dashboard == {date(2026, 5, 31): 3, date(2026, 6, 1): 5, date(2026, 6, 2): 1}
    assert sum(intended.values()) == sum(dashboard.values()) == len(V1_ROWS)   # same rows, different days


# =====================================================================================================================
# Block 8b: get_reject_counts_by_reason
# =====================================================================================================================
#
# The real SQL, against the same in-memory SQLite database as the reject count.
# SQLite puts no constraint on reject_events.error_class, so a row can be
# stored here with a class PostgreSQL's pattern check would also let through
# ("jam") -- and with ones it would not, which the service must survive too.
#
# A result is compared by NAME below (_named): only the reasons that have a
# count, keyed by their code. The tuples themselves, their length and their
# order have their own tests.

RejectReasonCounts = operational_metrics_service.RejectReasonCounts

NO_REASON_COUNTS = (0,) * 8
REASON_LOGGER = "sortview.operational_metrics"
UNEXPECTED_CLASS_WARNING = "Reject rows with an unrecognised stored class were counted as other: rows={}"
BEFORE_JUNE = _utc(2026, 6, 1, 5, 0)      # a cutover well before 10 June: that day is entirely v2's


def _reason_counts(db, local_date=JUNE_10, *, tenant=TENANT_A, zone=CHICAGO, fail_on=None):
    with db.connect() as conn:
        recorder = Recorder(conn, fail_on=fail_on)
        result = operational_metrics_service.get_reject_counts_by_reason(
            recorder, tenant, local_date=local_date, zone=zone
        )
    return result, recorder


def _named(counts: tuple[int, ...]) -> dict[str, int]:
    assert len(counts) == len(REJECT_REASONS) == 8
    return {reason: count for reason, count in zip(REJECT_REASONS, counts, strict=True) if count}


def _reasons(db, local_date=JUNE_10, **kwargs) -> dict[str, int]:
    result, _ = _reason_counts(db, local_date, **kwargs)
    return _named(result.counts)


def _reason_warnings(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == REASON_LOGGER]


# --- a legacy row's reason is classified from its message -------------------------------------------------------------

V1_RULE_MESSAGES = [
    ("Item not found in database", "item_not_found"),
    ("No item found for this tag", "item_not_found"),
    ("ACS connection failure", "ils_acs_failure"),
    ("Multiple RFID tags detected", "rfid_collision"),
    ("Multiple tags in the field", "rfid_collision"),
    ("Collection code mismatch", "configuration_error"),
    ("Library not found", "routing_error"),
    ("Something else entirely", "other"),
    ("item not found in ACS", "item_not_found"),        # the first rule that matches decides
    ("ACS: MULTIPLE RFID TAGS", "ils_acs_failure"),
]
V1_NO_TEXT = [None, "", " ", "   ", "\t", "nan", " NaN "]


@pytest.mark.parametrize(("message", "reason"), V1_RULE_MESSAGES)
def test_a_legacy_reject_lands_in_the_slot_of_the_rule_its_message_matches(reject_db, message, reason):
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message=message)

    result, _ = _reason_counts(reject_db)

    assert _named(result.counts) == {reason: 1}
    assert result.v1_counts == result.counts and result.v2_counts == NO_REASON_COUNTS
    assert result.unexpected_class_rows == 0


@pytest.mark.parametrize("message", V1_NO_TEXT, ids=repr)
def test_a_legacy_reject_with_no_text_is_unknown(reject_db, message):
    # NULL, empty, blank and the literal "nan": the Contract v2 meaning of `unknown`, not the dashboard's "Other".
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message=message)

    assert _reasons(reject_db) == {"unknown": 1}


def test_every_kind_of_legacy_message_in_one_day(reject_db):
    for minute, (message, _) in enumerate(V1_RULE_MESSAGES):
        _v1_reject(reject_db, _local(2026, 6, 10, 9, minute), error_message=message)
    for minute, message in enumerate(V1_NO_TEXT):
        _v1_reject(reject_db, _local(2026, 6, 10, 10, minute), error_message=message)

    result, recorder = _reason_counts(reject_db)

    assert _named(result.counts) == {
        "item_not_found": 3, "ils_acs_failure": 2, "rfid_collision": 2, "configuration_error": 1, "routing_error": 1,
        "other": 1, "unknown": 7,
    }
    assert result.counts[REJECT_REASONS.index("communication_error")] == 0   # no message is ever one
    assert recorder.tables() == ["v2_cutovers", "rejects"]


def test_distinct_messages_with_the_same_reason_add_together(reject_db):
    # Four different stored texts -- four groups in the database -- and one reason.
    for minute, message in enumerate(["Item not found", "ITEM NOT FOUND in database", "no item found",
                                      "  Item Not Found (sorter 2)"]):
        _v1_reject(reject_db, _local(2026, 6, 10, 9, minute), _local(2026, 6, 10, 10, minute), error_message=message)
    _v1_reject(reject_db, _local(2026, 6, 10, 11, 0), error_message="Library not found")

    assert _reasons(reject_db) == {"item_not_found": 8, "routing_error": 1}


# --- a v2 row's stored class IS its reason ----------------------------------------------------------------------------

@pytest.mark.parametrize("error_class", V2_REASONS)
def test_each_canonical_v2_class_lands_in_its_own_slot(reject_db, error_class):
    _cutover(reject_db, BEFORE_JUNE)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class=error_class)

    result, _ = _reason_counts(reject_db)

    expected = [0] * 8
    expected[REJECT_REASONS.index(error_class)] = 1
    assert result.counts == tuple(expected)
    assert result.v2_counts == result.counts and result.v1_counts == NO_REASON_COUNTS
    assert result.unexpected_class_rows == 0


def test_all_eight_v2_classes_in_one_day_each_keep_their_own_count(reject_db):
    assert tuple(V2_REASONS) == REJECT_REASONS
    _cutover(reject_db, BEFORE_JUNE)
    for slot, error_class in enumerate(REJECT_REASONS):
        _v2_reject(reject_db, *[_utc(2026, 6, 10, 18, slot)] * (slot + 1), error_class=error_class)

    result, recorder = _reason_counts(reject_db)

    assert result.counts == result.v2_counts == (1, 2, 3, 4, 5, 6, 7, 8)   # REJECT_REASONS order
    assert result.unexpected_class_rows == 0
    assert recorder.tables() == ["v2_cutovers", "reject_events"]


def test_a_v2_class_is_never_classified_as_if_it_were_a_message(reject_db):
    # The dashboard defect this must not repeat: read as text, "rfid_collision" matches no rule and becomes Other.
    _cutover(reject_db, BEFORE_JUNE)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 18, 1), error_class="rfid_collision")
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 2), error_class="unknown")

    assert _reasons(reject_db) == {"rfid_collision": 2, "unknown": 1}


def test_a_communication_error_can_only_come_from_a_v2_row(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message="communication error")
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 1), error_message="connection timed out")
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 18, 1), error_class="communication_error")

    result, _ = _reason_counts(reject_db)

    assert _named(result.v1_counts) == {"other": 2}
    assert _named(result.v2_counts) == {"communication_error": 2}
    assert _named(result.counts) == {"communication_error": 2, "other": 2}


def test_unknown_is_one_reason_whichever_era_it_comes_from(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message=None)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 1), error_message="")
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class="unknown")

    result, _ = _reason_counts(reject_db)

    assert _named(result.counts) == {"unknown": 3}
    assert (_named(result.v1_counts), _named(result.v2_counts)) == ({"unknown": 2}, {"unknown": 1})


# --- what is counted --------------------------------------------------------------------------------------------------

def test_every_reject_row_is_counted_under_its_reason_with_no_deduplication(reject_db):
    # The same item, the same second, the same message, three times over: three rows, three rejects.
    _v1_reject(reject_db, *[_local(2026, 6, 10, 9, 0)] * 3, barcode="same-barcode", error_message="ACS timeout")
    _cutover(reject_db, NOON_CUTOVER)
    _v2_reject(reject_db, *[_utc(2026, 6, 10, 18, 0)] * 2, item_key="a" * 64, error_class="rfid_collision")

    result, _ = _reason_counts(reject_db)

    assert _named(result.counts) == {"ils_acs_failure": 3, "rfid_collision": 2}
    assert sum(result.counts) == _reject_counts(reject_db)[0] == 5


def test_an_item_rejected_several_times_counts_each_time_under_each_reason(reject_db):
    _cutover(reject_db, BEFORE_JUNE)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 18, 5), item_key="b" * 64,
               error_class="item_not_found")
    _v2_reject(reject_db, _utc(2026, 6, 10, 19, 0), item_key="b" * 64, error_class="routing_error")
    _v2_reject(reject_db, _utc(2026, 6, 10, 20, 0), item_key=None, error_class="routing_error")   # no item at all

    assert _reasons(reject_db) == {"item_not_found": 2, "routing_error": 2}


def test_check_in_rows_do_not_affect_the_reason_counts(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))
    before = _reason_counts(reject_db)[0]

    _v1(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0))
    _v2(reject_db, _utc(2026, 6, 10, 18, 0))
    after, recorder = _reason_counts(reject_db)

    assert after == before
    assert recorder.tables() == ["v2_cutovers", "rejects", "reject_events"]   # neither check-in table is read


# --- a stored v2 class that is not one of the eight -------------------------------------------------------------------

def test_an_unexpected_stored_class_is_counted_as_other(reject_db):
    _cutover(reject_db, BEFORE_JUNE)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 18, 1), error_class="jam")
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 2), error_class="sensor_fault")
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 3), error_class="other")            # a real `other`
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 4), error_class="item_not_found")

    result, _ = _reason_counts(reject_db)

    assert _named(result.counts) == {"item_not_found": 1, "other": 4}
    assert result.unexpected_class_rows == 3                                        # rows, not distinct classes
    assert sum(result.counts) == _reject_counts(reject_db)[0] == 5                  # nothing dropped from the total


@pytest.mark.parametrize("stored", ["jam", "Item Not Found", "ITEM_NOT_FOUND", " other", "other ", "", "nan", None],
                         ids=repr)
def test_a_class_is_recognised_only_exactly_as_stored(reject_db, stored):
    # No stripping, no lower-casing, no reading it as a message: not one of the eight codes means `other`.
    _cutover(reject_db, BEFORE_JUNE)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class=stored)

    result, _ = _reason_counts(reject_db)

    assert (_named(result.counts), result.unexpected_class_rows) == ({"other": 1}, 1)


def test_unexpected_classes_log_exactly_one_warning_carrying_only_the_row_count(reject_db, caplog):
    _cutover(reject_db, BEFORE_JUNE)
    _v2_reject(reject_db, *[_utc(2026, 6, 10, 18, 0)] * 4, error_class="jam")
    _v2_reject(reject_db, *[_utc(2026, 6, 10, 19, 0)] * 3, error_class="sensor_fault")
    _v2_reject(reject_db, _utc(2026, 6, 10, 20, 0), error_class="rfid_collision")

    with caplog.at_level(logging.DEBUG):
        result, _ = _reason_counts(reject_db)

    (warning,) = _reason_warnings(caplog)          # one per call, not one per class or per row
    assert warning.levelno == logging.WARNING
    assert warning.getMessage() == UNEXPECTED_CLASS_WARNING.format(7)
    assert warning.args == (7,) == (result.unexpected_class_rows,)
    for leaked in ("jam", "sensor_fault", "rfid_collision", "customer", "branch", "2026", "acme", "main"):
        assert leaked not in warning.getMessage(), leaked
    assert warning.exc_info is None and warning.stack_info is None


def test_no_warning_is_logged_when_every_class_is_canonical(reject_db, caplog):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message="a message matching no rule at all")
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 1), error_message=None)
    for minute, error_class in enumerate(REJECT_REASONS):
        _v2_reject(reject_db, _utc(2026, 6, 10, 18, minute), error_class=error_class)

    with caplog.at_level(logging.DEBUG):
        result, _ = _reason_counts(reject_db)

    assert result.unexpected_class_rows == 0
    assert _reason_warnings(caplog) == []


def test_an_unexpected_class_outside_what_is_counted_is_neither_counted_nor_warned_about(reject_db, caplog):
    _cutover(reject_db, NOON_CUTOVER)
    _v2_reject(reject_db, _utc(2026, 6, 10, 16, 0), error_class="jam")                      # before the cutover
    _v2_reject(reject_db, _utc(2026, 6, 11, 5, 0), error_class="jam")                       # the next local day
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class="jam", customer_id=CUSTOMER_B)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class="jam", branch_id=BRANCH_B)

    with caplog.at_level(logging.DEBUG):
        result, _ = _reason_counts(reject_db)

    assert result == RejectReasonCounts(NO_REASON_COUNTS, NO_REASON_COUNTS, NO_REASON_COUNTS, 0)
    assert _reason_warnings(caplog) == []


def test_with_no_cutover_an_unexpected_v2_class_is_never_even_read(reject_db, caplog):
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class="jam")

    with caplog.at_level(logging.DEBUG):
        result, recorder = _reason_counts(reject_db)

    assert result.unexpected_class_rows == 0 and _reason_warnings(caplog) == []
    assert "reject_events" not in recorder.tables()


# --- raw text never leaves the function -------------------------------------------------------------------------------

CANARY = "CANARY-31234000123456 Smith, Pat"


def test_the_legacy_message_text_is_in_neither_the_result_nor_any_log(reject_db, caplog):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message=f"Item not found {CANARY}", barcode=CANARY)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 1), error_message=CANARY)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class="jam")

    with caplog.at_level(logging.DEBUG):
        result, _ = _reason_counts(reject_db)

    assert _named(result.counts) == {"item_not_found": 1, "other": 2}
    assert "CANARY" not in repr(result) and "jam" not in repr(result)
    assert all(type(value) is int for counts in (result.counts, result.v1_counts, result.v2_counts) for value in counts)
    for record in caplog.records:
        assert "CANARY" not in record.getMessage() and "jam" not in record.getMessage(), record.name


def test_a_failure_after_the_legacy_text_was_read_does_not_carry_it(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message=CANARY)

    with pytest.raises(RuntimeError) as raised:
        _reason_counts(reject_db, fail_on="reject_events")

    assert "CANARY" not in str(raised.value)


# --- the eras share the day exactly as in get_reject_count ------------------------------------------------------------

def test_a_v1_only_branch_gets_its_reasons_from_the_legacy_table_alone(reject_db):
    _v1_reject(reject_db, _local(2026, 6, 9, 23, 59, 59), error_message="Library not found")    # the day before
    _v1_reject(reject_db, _local(2026, 6, 10, 0, 0), error_message="Item not found")
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 30), error_message="ACS timeout")
    _v1_reject(reject_db, _local(2026, 6, 10, 23, 59, 59), error_message="ACS down")
    _v1_reject(reject_db, _local(2026, 6, 11, 0, 0), error_message="Library not found")         # the day after

    result, recorder = _reason_counts(reject_db)

    assert _named(result.counts) == {"item_not_found": 1, "ils_acs_failure": 2}
    assert result.v1_counts == result.counts and result.v2_counts == NO_REASON_COUNTS
    assert recorder.tables() == ["v2_cutovers", "rejects"]   # the v2 table is never read


def test_with_no_cutover_v2_reasons_are_ignored_even_if_they_exist(reject_db):
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message="Collection code missing")
    _v2_reject(reject_db, _utc(2026, 6, 10, 15, 0), _utc(2026, 6, 10, 16, 0), error_class="rfid_collision")

    result, recorder = _reason_counts(reject_db)

    assert _named(result.counts) == {"configuration_error": 1}
    assert "reject_events" not in recorder.tables()


def test_a_v2_only_day_gets_its_reasons_from_the_v2_table_alone(reject_db):
    _cutover(reject_db, BEFORE_JUNE)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message="Library not found")   # legacy, after the cutover
    _v2_reject(reject_db, _utc(2026, 6, 10, 5, 0), _utc(2026, 6, 11, 4, 59, 59), error_class="rfid_collision")
    _v2_reject(reject_db, _utc(2026, 6, 10, 15, 0), error_class="configuration_error")
    _v2_reject(reject_db, _utc(2026, 6, 10, 4, 59, 59), _utc(2026, 6, 11, 5, 0), error_class="other")   # outside the day

    result, recorder = _reason_counts(reject_db)

    assert _named(result.counts) == {"rfid_collision": 2, "configuration_error": 1}
    assert result.v2_counts == result.counts and result.v1_counts == NO_REASON_COUNTS
    assert recorder.tables() == ["v2_cutovers", "reject_events"]    # no v1 query at all


def test_a_cutover_inside_the_day_takes_each_eras_reasons_from_its_own_part(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    # v1, local wall clock: the two before noon count; the one at noon and the one after do not.
    _v1_reject(reject_db, _local(2026, 6, 10, 8, 0), error_message="Item not found")
    _v1_reject(reject_db, _local(2026, 6, 10, 11, 59, 59), error_message="ACS timeout")
    _v1_reject(reject_db, _local(2026, 6, 10, 12, 0), _local(2026, 6, 10, 15, 0), error_message="Library not found")
    # v2, instants: the one before the cutover does not count; at it and after it do; the next local midnight does not.
    _v2_reject(reject_db, _utc(2026, 6, 10, 16, 59, 59), error_class="configuration_error")
    _v2_reject(reject_db, _utc(2026, 6, 10, 17, 0), error_class="item_not_found")
    _v2_reject(reject_db, _utc(2026, 6, 10, 22, 0), _utc(2026, 6, 11, 4, 59, 59), error_class="rfid_collision")
    _v2_reject(reject_db, _utc(2026, 6, 11, 5, 0), error_class="communication_error")

    result, recorder = _reason_counts(reject_db)

    assert _named(result.v1_counts) == {"item_not_found": 1, "ils_acs_failure": 1}
    assert _named(result.v2_counts) == {"item_not_found": 1, "rfid_collision": 2}
    assert _named(result.counts) == {"item_not_found": 2, "ils_acs_failure": 1, "rfid_collision": 2}
    assert recorder.tables() == ["v2_cutovers", "rejects", "reject_events"]


def test_a_reject_exactly_at_the_cutover_has_v2s_reason_and_one_second_before_has_v1s(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 11, 59, 59), error_message="ACS timeout")        # v1's
    _v1_reject(reject_db, _local(2026, 6, 10, 12, 0, 0), error_message="Library not found")    # the cutover moment: not v1's
    _v2_reject(reject_db, _utc(2026, 6, 10, 16, 59, 59), error_class="configuration_error")    # one second before: not v2's
    _v2_reject(reject_db, _utc(2026, 6, 10, 17, 0, 0), error_class="rfid_collision")           # the cutover moment: v2's

    result, _ = _reason_counts(reject_db)

    assert _named(result.v1_counts) == {"ils_acs_failure": 1}
    assert _named(result.v2_counts) == {"rfid_collision": 1}


def test_a_cutover_after_the_day_leaves_its_reasons_to_v1(reject_db):
    _cutover(reject_db, _utc(2026, 6, 20, 17, 0))
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 21, 0), error_message="Multiple tags")
    _v2_reject(reject_db, _utc(2026, 6, 10, 15, 0), error_class="item_not_found")   # a v2 row before the cutover

    result, recorder = _reason_counts(reject_db)

    assert _named(result.counts) == {"rfid_collision": 2}
    assert recorder.tables() == ["v2_cutovers", "rejects"]          # no v2 query at all


def test_a_rollback_makes_the_branch_v1_only_again_for_reasons(reject_db):
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message="Item not found")
    _v1_reject(reject_db, _local(2026, 6, 10, 15, 0), error_message="Library not found")
    _v2_reject(reject_db, _utc(2026, 6, 10, 20, 0), error_class="jam")
    _cutover(reject_db, NOON_CUTOVER, set_at="2026-06-01 00:00:00+00:00")
    _cutover(reject_db, None, set_at="2026-06-12 00:00:00+00:00")   # the latest record: a rollback

    result, recorder = _reason_counts(reject_db)

    assert _named(result.counts) == {"item_not_found": 1, "routing_error": 1}   # 15:00 is v1's again
    assert result.unexpected_class_rows == 0                                    # v2 is not read at all
    assert recorder.tables() == ["v2_cutovers", "rejects"]


@pytest.mark.parametrize(
    ("cutover_at", "expected_tables"),
    [
        (None, ["v2_cutovers", "rejects"]),
        (_utc(2026, 6, 1, 17, 0), ["v2_cutovers", "reject_events"]),
        (_utc(2026, 6, 10, 5, 0), ["v2_cutovers", "reject_events"]),
        (NOON_CUTOVER, ["v2_cutovers", "rejects", "reject_events"]),
        (_utc(2026, 6, 11, 5, 0), ["v2_cutovers", "rejects"]),
        (_utc(2026, 6, 20, 17, 0), ["v2_cutovers", "rejects"]),
    ],
    ids=["no cutover", "cutover before the day", "cutover at the day's start", "cutover inside the day",
         "cutover at the day's end", "cutover after the day"],
)
def test_the_cutover_is_looked_up_once_and_only_the_eras_that_own_part_of_the_day_are_grouped(
    reject_db, cutover_at, expected_tables
):
    if cutover_at is not None:
        _cutover(reject_db, cutover_at)

    result, recorder = _reason_counts(reject_db)

    assert recorder.tables() == expected_tables
    assert recorder.tables().count("v2_cutovers") == 1
    assert len(recorder.statements) <= 3
    assert result == RejectReasonCounts(NO_REASON_COUNTS, NO_REASON_COUNTS, NO_REASON_COUNTS, 0)   # an empty day


def test_the_reason_counts_bind_exactly_what_the_reject_count_binds(reject_db):
    # The same day and the same cutover give both functions the same statements' worth of bounds: one time model.
    for cutover_at in (None, _utc(2026, 6, 1, 17, 0), NOON_CUTOVER, _utc(2026, 6, 10, 19, 30), _utc(2026, 6, 20, 17, 0)):
        if cutover_at is not None:
            _cutover(reject_db, cutover_at, set_at=cutover_at.isoformat(sep=" "))

        for local_date, zone in ((JUNE_10, CHICAGO), (JUNE_10, KOLKATA), (SPRING_FORWARD, CHICAGO), (FALL_BACK, CHICAGO)):
            _, reasons = _reason_counts(reject_db, local_date, zone=zone)
            _, total = _reject_count(reject_db, local_date, zone=zone)

            assert reasons.tables() == total.tables()
            assert [parameters for _, parameters in reasons.statements] == [
                parameters for _, parameters in total.statements
            ]


# --- the counts add up to the reject count ----------------------------------------------------------------------------

_SUM_MESSAGES = ["Item not found", "ACS timeout", "Multiple RFID tags", "Collection code missing", "Library not found",
                 "Something else", "", None, "nan", "no item found"]
_SUM_CLASSES = [*REJECT_REASONS, "jam", "sensor_fault"]


def _seed_rejects_around(db, year, month, day) -> None:
    """Rows in both tables on the day before, the day and the day after, every few hours, cycling through every
    kind of message and class (two of the classes are not reason codes)."""
    midnight = _local(year, month, day) - timedelta(days=1)
    for step in range(36):                                      # every two hours for three days
        _v1_reject(db, midnight + timedelta(hours=2 * step, minutes=7),
                   error_message=_SUM_MESSAGES[step % len(_SUM_MESSAGES)])
        _v2_reject(db, (midnight + timedelta(hours=2 * step, minutes=13)).replace(tzinfo=UTC),
                   error_class=_SUM_CLASSES[step % len(_SUM_CLASSES)])


def test_the_reason_counts_always_add_up_to_the_reject_count(reject_db):
    for month, day in ((6, 10), (3, 8), (11, 1)):
        _seed_rejects_around(reject_db, 2026, month, day)

    cutovers = [
        ("never cut over", "skip"),
        ("cut over before everything", _utc(2026, 1, 1, 6, 0)),          # every day is v2's
        ("cut over at noon on 10 June", NOON_CUTOVER),                   # mixed
        ("rolled back", None),                                           # v1 only again
        ("cut over inside the spring-forward date", _utc(2026, 3, 8, 15, 0)),
        ("cut over inside the fall-back date", _utc(2026, 11, 1, 18, 0)),
        ("cut over after everything", _utc(2027, 1, 1, 6, 0)),           # every day is v1's
    ]
    days = [date(2026, 6, 9), JUNE_10, date(2026, 6, 11), date(2026, 3, 7), SPRING_FORWARD, date(2026, 3, 9),
            date(2026, 10, 31), FALL_BACK, date(2026, 11, 2)]
    seen_v1 = seen_v2 = seen_mixed = seen_unexpected = 0

    for order, (label, cutover_at) in enumerate(cutovers):
        if cutover_at != "skip":
            _cutover(reject_db, cutover_at, set_at=f"2026-12-{order + 1:02d} 00:00:00+00:00")

        for local_date in days:
            for zone in (CHICAGO, KOLKATA, LONDON):
                reasons, _ = _reason_counts(reject_db, local_date, zone=zone)
                total, _ = _reject_count(reject_db, local_date, zone=zone)
                where = (label, local_date, zone.key)

                assert sum(reasons.counts) == total.total, where
                assert sum(reasons.v1_counts) == total.v1_count, where
                assert sum(reasons.v2_counts) == total.v2_count, where
                assert reasons.counts == tuple(
                    v1 + v2 for v1, v2 in zip(reasons.v1_counts, reasons.v2_counts, strict=True)
                ), where
                assert len(reasons.counts) == len(reasons.v1_counts) == len(reasons.v2_counts) == 8, where
                assert reasons.unexpected_class_rows <= reasons.v2_counts[REJECT_REASONS.index("other")], where

                seen_v1 += bool(total.v1_count and not total.v2_count)
                seen_v2 += bool(total.v2_count and not total.v1_count)
                seen_mixed += bool(total.v1_count and total.v2_count)
                seen_unexpected += bool(reasons.unexpected_class_rows)

    # The comparison really covered v1-only, v2-only and mixed days, and days holding unexpected classes.
    assert min(seen_v1, seen_v2, seen_mixed, seen_unexpected) > 0


def test_the_days_around_a_cutover_keep_every_reject_under_exactly_one_reason(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 9, 10, 0), _local(2026, 6, 9, 23, 30), _local(2026, 6, 10, 0, 30),
               _local(2026, 6, 10, 11, 0), error_message="ACS timeout")
    _v2_reject(reject_db, _utc(2026, 6, 10, 17, 0), _utc(2026, 6, 10, 23, 0), _utc(2026, 6, 11, 4, 0),
               _utc(2026, 6, 11, 5, 0), _utc(2026, 6, 11, 20, 0), error_class="routing_error")

    per_day = [_reasons(reject_db, day) for day in (date(2026, 6, 9), date(2026, 6, 10), date(2026, 6, 11))]

    assert per_day == [{"ils_acs_failure": 2}, {"ils_acs_failure": 2, "routing_error": 3}, {"routing_error": 2}]
    assert sum(sum(day.values()) for day in per_day) == 9   # nine rows stored, nine counted, none twice


# --- time zones and DST -----------------------------------------------------------------------------------------------

def test_reasons_on_the_spring_forward_date(reject_db):
    # v1-only: every wall-clock time on 8 March -- one stamped in the hour that did not exist -- none on either side.
    _v1_reject(reject_db, _local(2026, 3, 7, 23, 59, 59), _local(2026, 3, 9, 0, 0), error_message="Collection code")
    _v1_reject(reject_db, _local(2026, 3, 8, 0, 0), error_message="Item not found")
    _v1_reject(reject_db, _local(2026, 3, 8, 2, 30), error_message="ACS timeout")
    _v1_reject(reject_db, _local(2026, 3, 8, 3, 0), error_message="Library not found")
    _v1_reject(reject_db, _local(2026, 3, 8, 23, 59, 59), error_message="")
    assert _reasons(reject_db, SPRING_FORWARD) == {
        "item_not_found": 1, "ils_acs_failure": 1, "routing_error": 1, "unknown": 1,
    }

    # Once cut over before that date: the day is [06:00Z, 05:00Z next day) -- 23 hours.
    _cutover(reject_db, _utc(2026, 3, 1, 6, 0))
    _v2_reject(reject_db, _utc(2026, 3, 8, 5, 59, 59), _utc(2026, 3, 9, 5, 0), error_class="communication_error")
    _v2_reject(reject_db, _utc(2026, 3, 8, 6, 0), error_class="rfid_collision")
    _v2_reject(reject_db, _utc(2026, 3, 9, 4, 59, 59), error_class="other")

    result, recorder = _reason_counts(reject_db, SPRING_FORWARD)

    assert _named(result.counts) == {"rfid_collision": 1, "other": 1}
    assert result.v1_counts == NO_REASON_COUNTS
    parameters = recorder.parameters_for("reject_events")
    assert parameters["end_utc"] - parameters["start_utc"] == timedelta(hours=23)


def test_reasons_on_the_fall_back_date(reject_db):
    # v1-only: both passes through 01:30 are just rows stamped 01:30 on 1 November.
    _v1_reject(reject_db, _local(2026, 10, 31, 23, 59, 59), _local(2026, 11, 2, 0, 0), error_message="Collection code")
    _v1_reject(reject_db, _local(2026, 11, 1, 0, 0), error_message="Item not found")
    _v1_reject(reject_db, _local(2026, 11, 1, 1, 30), _local(2026, 11, 1, 1, 30), error_message="Multiple tags")
    _v1_reject(reject_db, _local(2026, 11, 1, 23, 59, 59), error_message="Library not found")
    assert _reasons(reject_db, FALL_BACK) == {"item_not_found": 1, "rfid_collision": 2, "routing_error": 1}

    # Once cut over before that date: the day is [05:00Z, 06:00Z next day) -- 25 hours.
    _cutover(reject_db, _utc(2026, 10, 1, 5, 0))
    _v2_reject(reject_db, _utc(2026, 11, 1, 4, 59, 59), _utc(2026, 11, 2, 6, 0), error_class="communication_error")
    _v2_reject(reject_db, _utc(2026, 11, 1, 5, 0), error_class="item_not_found")
    _v2_reject(reject_db, FIRST_0130, error_class="ils_acs_failure")
    _v2_reject(reject_db, SECOND_0130, error_class="unknown")
    _v2_reject(reject_db, _utc(2026, 11, 2, 5, 59, 59), error_class="ils_acs_failure")

    result, recorder = _reason_counts(reject_db, FALL_BACK)

    # Both real 01:30s are counted, once each, each under its own reason.
    assert _named(result.counts) == {"item_not_found": 1, "ils_acs_failure": 2, "unknown": 1}
    parameters = recorder.parameters_for("reject_events")
    assert parameters["end_utc"] - parameters["start_utc"] == timedelta(hours=25)


def test_a_cutover_inside_a_dst_date_partitions_its_reasons_correctly(reject_db):
    _cutover(reject_db, _utc(2026, 3, 8, 15, 0))                    # 10:00 CDT, after the clocks went forward
    _v1_reject(reject_db, _local(2026, 3, 8, 1, 30), _local(2026, 3, 8, 9, 59, 59), error_message="ACS timeout")
    _v1_reject(reject_db, _local(2026, 3, 8, 10, 0), error_message="Library not found")
    _v2_reject(reject_db, _utc(2026, 3, 8, 14, 59, 59), error_class="configuration_error")
    _v2_reject(reject_db, _utc(2026, 3, 8, 15, 0), _utc(2026, 3, 9, 4, 0), error_class="rfid_collision")

    result, recorder = _reason_counts(reject_db, SPRING_FORWARD)

    assert _named(result.counts) == {"ils_acs_failure": 2, "rfid_collision": 2}
    assert recorder.parameters_for("rejects")["end_local"] == _local(2026, 3, 8, 10, 0)


def test_the_reason_counts_are_made_in_the_zone_they_are_given(reject_db):
    _v1_reject(reject_db, _local(2026, 6, 10, 0, 30), error_message="Item not found")
    _v1_reject(reject_db, _local(2026, 6, 10, 23, 30), error_message="Library not found")
    _cutover(reject_db, _utc(2026, 6, 10, 12, 0))                   # 07:00 in Chicago, 17:30 in Kolkata
    _v2_reject(reject_db, _utc(2026, 6, 10, 12, 0), error_class="rfid_collision")
    _v2_reject(reject_db, _utc(2026, 6, 10, 20, 0), error_class="configuration_error")

    # Chicago: v1 before 07:00 local; v2 from 12:00Z to 05:00Z next day.
    assert _reasons(reject_db, zone=CHICAGO) == {"item_not_found": 1, "rfid_collision": 1, "configuration_error": 1}
    # Kolkata: v1 before 17:30 local; the local day ends at 18:30Z, so only the 12:00Z row is v2's.
    assert _reasons(reject_db, zone=KOLKATA) == {"item_not_found": 1, "rfid_collision": 1}


def test_a_half_hour_zone_bounds_the_v2_reasons_on_the_utc_half_hour(reject_db):
    _cutover(reject_db, _utc(2026, 6, 1, 0, 0))
    # The Kolkata day of 10 June is [18:30Z on the 9th, 18:30Z on the 10th).
    _v2_reject(reject_db, _utc(2026, 6, 9, 18, 29, 59), _utc(2026, 6, 10, 18, 30), error_class="other")
    _v2_reject(reject_db, _utc(2026, 6, 9, 18, 30), error_class="routing_error")
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 29, 59), error_class="unknown")

    result, recorder = _reason_counts(reject_db, zone=KOLKATA)

    assert _named(result.counts) == {"routing_error": 1, "unknown": 1}
    parameters = recorder.parameters_for("reject_events")
    assert (parameters["start_utc"], parameters["end_utc"]) == (_utc(2026, 6, 9, 18, 30), _utc(2026, 6, 10, 18, 30))


def test_an_evening_v2_reject_has_its_reason_on_its_local_day_not_its_utc_day(reject_db):
    _cutover(reject_db, BEFORE_JUNE)
    _v2_reject(reject_db, _utc(2026, 6, 11, 1, 30), error_class="routing_error")   # 20:30 on 10 June in Chicago

    assert _reasons(reject_db, date(2026, 6, 10)) == {"routing_error": 1}
    assert _reasons(reject_db, date(2026, 6, 11)) == {}


# --- the tenant filter (SQLite has no RLS: only the statements' own WHERE separates these rows) -----------------------

def test_another_customers_rejects_never_reach_the_reason_counts(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message="Item not found")
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class="rfid_collision")
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0), customer_id=CUSTOMER_B,
               error_message="Library not found")
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 19, 0), customer_id=CUSTOMER_B,
               error_class="communication_error")

    assert _reasons(reject_db) == {"item_not_found": 1, "rfid_collision": 1}


def test_another_branchs_rejects_never_reach_the_reason_counts(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message="Item not found")
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class="rfid_collision")
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0), branch_id=BRANCH_B,
               error_message="Library not found")
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), _utc(2026, 6, 10, 19, 0), branch_id=BRANCH_B,
               error_class="communication_error")

    assert _reasons(reject_db) == {"item_not_found": 1, "rfid_collision": 1}


def test_another_tenants_cutover_does_not_change_this_tenants_reason_counts(reject_db):
    _cutover(reject_db, BEFORE_JUNE, customer_id=CUSTOMER_B, branch_id=BRANCH_B)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message="ACS timeout")
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class="rfid_collision")

    result, recorder = _reason_counts(reject_db)

    assert _named(result.counts) == {"ils_acs_failure": 1}   # this tenant has no cutover: v1 only
    assert recorder.tables() == ["v2_cutovers", "rejects"]


def test_each_tenant_gets_its_own_reason_counts_from_the_same_tables(reject_db):
    tenant_b = ResolvedOperationalTenant(
        org_slug="beta", branch_slug="main", access_mode="read_only",
        operational_customer_id=CUSTOMER_B, operational_branch_id=BRANCH_B,
    )
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message="Item not found")
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0), _local(2026, 6, 10, 11, 0),
               customer_id=CUSTOMER_B, branch_id=BRANCH_B, error_message="Library not found")

    assert _reasons(reject_db, tenant=TENANT_A) == {"item_not_found": 1}
    assert _reasons(reject_db, tenant=tenant_b) == {"routing_error": 3}   # a read_only tenant reads like any other


def test_every_reason_statement_is_bound_to_the_resolved_tenants_ids(reject_db):
    tenant_b = ResolvedOperationalTenant(
        org_slug="beta", branch_slug="main", access_mode="full",
        operational_customer_id=CUSTOMER_B, operational_branch_id=BRANCH_B,
    )
    _cutover(reject_db, NOON_CUTOVER)
    _cutover(reject_db, NOON_CUTOVER, customer_id=CUSTOMER_B, branch_id=BRANCH_B)

    for tenant in (TENANT_A, tenant_b):
        _, recorder = _reason_counts(reject_db, tenant=tenant)

        assert len(recorder.statements) == 3
        for sql, parameters in recorder.statements:
            assert "customer_id = :customer_id AND branch_id = :branch_id" in sql
            assert parameters["customer_id"] == tenant.operational_customer_id
            assert parameters["branch_id"] == tenant.operational_branch_id


# --- the statements and their parameters ------------------------------------------------------------------------------

def test_the_v1_reason_statement_has_the_approved_shape():
    assert _statement("_V1_REJECT_REASON_COUNT_SQL") == (
        "SELECT error_message, COUNT(*) FROM rejects "
        "WHERE customer_id = :customer_id AND branch_id = :branch_id "
        "AND event_time >= :start_local AND event_time < :end_local "   # strictly before its upper bound
        "GROUP BY error_message"
    )


def test_the_v2_reason_statement_has_the_approved_shape():
    assert _statement("_V2_REJECT_REASON_COUNT_SQL") == (
        "SELECT error_class, COUNT(*) FROM reject_events "
        "WHERE customer_id = :customer_id AND branch_id = :branch_id "
        "AND event_time >= :start_utc AND event_time < :end_utc "      # from its lower bound, inclusive
        "GROUP BY error_class"
    )


def test_each_reason_statement_is_the_reject_count_statement_with_only_the_grouping_added():
    for name, column in (("_V1_REJECT_REASON_COUNT_SQL", "error_message"), ("_V2_REJECT_REASON_COUNT_SQL", "error_class")):
        count_statement = _statement(name.replace("_REASON", ""))

        assert _statement(name) == (
            count_statement.replace("SELECT COUNT(*)", f"SELECT {column}, COUNT(*)") + f" GROUP BY {column}"
        )


@pytest.mark.parametrize(
    ("name", "reason_column"),
    [("_V1_REJECT_REASON_COUNT_SQL", "error_message"), ("_V2_REJECT_REASON_COUNT_SQL", "error_class")],
)
def test_a_reason_statement_names_only_the_tenant_the_time_and_its_one_reason_column(name, reason_column):
    sql = _statement(name)
    words = set(sql.replace(",", " ").replace("(", " ").replace(")", " ").replace(":", " ").split())

    assert words - {"SELECT", "COUNT", "*", "FROM", "WHERE", "AND", "GROUP", "BY", "=", ">=", "<"} == {
        "rejects" if reason_column == "error_message" else "reject_events",
        "customer_id", "branch_id", "event_time", reason_column,
        *(("start_local", "end_local") if reason_column == "error_message" else ("start_utc", "end_utc")),
    }
    assert sql.upper().count(" FROM ") == 1
    for forbidden in ("BARCODE", "ITEM_KEY", "EVENT_KEY", "KEY_ID", "SOURCE_FILE", "JOIN", "DISTINCT", "ORDER BY",
                      "CAST", "SELECT *", "CHECKIN", "ACS", "REJECTS_CLEAN", "AT TIME ZONE", "NOW()", "::", "CURRENT_",
                      "TIMEZONE", "LOWER", "LIKE", "CASE", "HAVING", "LIMIT", "UNION"):
        assert forbidden not in sql.upper(), (name, forbidden)


def test_the_two_reason_statements_never_name_each_others_reason_column():
    assert "error_class" not in _statement("_V1_REJECT_REASON_COUNT_SQL")
    assert "error_message" not in _statement("_V2_REJECT_REASON_COUNT_SQL")


def test_the_reason_counts_bind_naive_v1_bounds_and_aware_utc_v2_bounds(reject_db):
    _cutover(reject_db, NOON_CUTOVER)

    _, recorder = _reason_counts(reject_db)

    v1, v2 = recorder.parameters_for("rejects"), recorder.parameters_for("reject_events")
    assert (v1["start_local"], v1["end_local"]) == (_local(2026, 6, 10, 0, 0), _local(2026, 6, 10, 12, 0))
    assert v1["start_local"].tzinfo is None and v1["end_local"].tzinfo is None
    assert (v2["start_utc"], v2["end_utc"]) == (_utc(2026, 6, 10, 17, 0), _utc(2026, 6, 11, 5, 0))
    assert v2["start_utc"].utcoffset() == timedelta(0) and v2["end_utc"].utcoffset() == timedelta(0)
    # Nothing else is bound: no reason, no pattern, no limit.
    assert set(v1) == {"customer_id", "branch_id", "start_local", "end_local"}
    assert set(v2) == {"customer_id", "branch_id", "start_utc", "end_utc"}


def test_the_reason_bound_parameters_declare_their_time_zone_handling():
    v1 = operational_metrics_service._V1_REJECT_REASON_COUNT_SQL._bindparams
    v2 = operational_metrics_service._V2_REJECT_REASON_COUNT_SQL._bindparams
    assert set(v1) == {"customer_id", "branch_id", "start_local", "end_local"}
    assert set(v2) == {"customer_id", "branch_id", "start_utc", "end_utc"}

    assert v1["start_local"].type.timezone is False and v1["end_local"].type.timezone is False
    assert v2["start_utc"].type.timezone is True and v2["end_utc"].type.timezone is True


def test_without_a_cutover_v1_gets_the_whole_local_day_of_reasons(reject_db):
    _, recorder = _reason_counts(reject_db)

    parameters = recorder.parameters_for("rejects")
    assert (parameters["start_local"], parameters["end_local"]) == (_local(2026, 6, 10), _local(2026, 6, 11))


# --- failures ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("failing", ["v2_cutovers", "rejects", "reject_events"])
def test_a_failure_in_any_reason_statement_propagates_and_no_partial_counts_are_returned(reject_db, failing):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))

    with pytest.raises(RuntimeError, match=f"synthetic failure reading {failing}"):
        _reason_counts(reject_db, fail_on=failing)


def test_a_failing_v2_grouping_does_not_come_back_as_v1_only_reasons(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    with reject_db.begin() as conn:
        conn.execute(text("DROP TABLE reject_events"))

    with pytest.raises(Exception, match="reject_events"):
        _reason_counts(reject_db)


def test_a_failing_v1_grouping_does_not_come_back_as_v2_only_reasons(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))
    with reject_db.begin() as conn:
        conn.execute(text("DROP TABLE rejects"))

    with pytest.raises(Exception, match="rejects"):
        _reason_counts(reject_db)


def test_a_failing_cutover_lookup_is_never_read_as_no_cutover(reject_db):
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    with reject_db.begin() as conn:
        conn.execute(text("DROP TABLE v2_cutovers"))

    with pytest.raises(Exception, match="v2_cutovers"):
        _reason_counts(reject_db)


def test_a_failure_logs_no_unexpected_class_warning(reject_db, caplog):
    _cutover(reject_db, NOON_CUTOVER)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class="jam")

    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        _reason_counts(reject_db, fail_on="reject_events")

    assert _reason_warnings(caplog) == []


def test_the_reason_counts_need_neither_check_in_table(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))
    with reject_db.begin() as conn:
        conn.execute(text("DROP TABLE checkins"))
        conn.execute(text("DROP TABLE checkin_events"))

    assert _reasons(reject_db) == {"item_not_found": 2}


# --- the result -------------------------------------------------------------------------------------------------------

def test_an_empty_day_is_eight_zeros_in_every_tuple(reject_db):
    result, _ = _reason_counts(reject_db)

    assert result == RejectReasonCounts(
        counts=(0, 0, 0, 0, 0, 0, 0, 0), v1_counts=(0, 0, 0, 0, 0, 0, 0, 0), v2_counts=(0, 0, 0, 0, 0, 0, 0, 0),
        unexpected_class_rows=0,
    )


def test_the_reason_result_always_has_one_entry_per_reason_in_the_fixed_order(reject_db):
    assert REJECT_REASONS == ("item_not_found", "ils_acs_failure", "rfid_collision", "configuration_error",
                              "routing_error", "communication_error", "other", "unknown")
    _cutover(reject_db, NOON_CUTOVER)
    # Stored in an order unlike the reasons': the result's order comes from REJECT_REASONS, never from the data.
    _v2_reject(reject_db, *[_utc(2026, 6, 10, 18, 0)] * 2, error_class="unknown")
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 1), error_class="other")
    _v1_reject(reject_db, *[_local(2026, 6, 10, 9, 0)] * 3, error_message="Library not found")
    _v1_reject(reject_db, *[_local(2026, 6, 10, 9, 1)] * 4, error_message="Item not found")

    result, _ = _reason_counts(reject_db)

    assert result.v1_counts == (4, 0, 0, 0, 3, 0, 0, 0)
    assert result.v2_counts == (0, 0, 0, 0, 0, 0, 1, 2)
    assert result.counts == (4, 0, 0, 0, 3, 0, 1, 2)


def test_the_reason_result_is_an_immutable_value_whose_counts_are_the_sum_of_the_eras(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), _local(2026, 6, 10, 10, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0))
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 1), error_class="jam")

    result, _ = _reason_counts(reject_db)

    assert [f.name for f in dataclasses.fields(RejectReasonCounts)] == [
        "counts", "v1_counts", "v2_counts", "unexpected_class_rows",
    ]
    assert result == RejectReasonCounts(
        counts=(3, 0, 0, 0, 0, 0, 1, 0), v1_counts=(2, 0, 0, 0, 0, 0, 0, 0), v2_counts=(1, 0, 0, 0, 0, 0, 1, 0),
        unexpected_class_rows=1,
    )
    for counts in (result.counts, result.v1_counts, result.v2_counts):
        assert type(counts) is tuple and len(counts) == 8
        assert all(type(value) is int for value in counts)
    assert type(result.unexpected_class_rows) is int
    assert result.counts == tuple(v1 + v2 for v1, v2 in zip(result.v1_counts, result.v2_counts, strict=True))
    assert RejectReasonCounts is not RejectCount
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.counts = NO_REASON_COUNTS
    assert not hasattr(result, "__dict__")   # slots: nothing can be attached to it either


def test_the_reason_function_takes_a_connection_a_tenant_a_date_and_a_zone():
    parameters = inspect.signature(operational_metrics_service.get_reject_counts_by_reason).parameters

    assert list(parameters) == ["conn", "tenant", "local_date", "zone"]
    assert parameters["local_date"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["zone"].kind is inspect.Parameter.KEYWORD_ONLY
    assert all(p.default is inspect.Parameter.empty for p in parameters.values())   # nothing defaults, least of all the date
    assert list(parameters) == list(inspect.signature(operational_metrics_service.get_reject_count).parameters)


def test_nothing_is_cached_between_reason_counts(reject_db):
    assert _reasons(reject_db) == {}

    _v1_reject(reject_db, _local(2026, 6, 10, 9, 0), error_message="ACS timeout")
    assert _reasons(reject_db) == {"ils_acs_failure": 1}

    _cutover(reject_db, BEFORE_JUNE)
    assert _reasons(reject_db) == {}

    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class="routing_error")
    assert _reasons(reject_db) == {"routing_error": 1}


# --- what the new function is built from, and what it left alone ------------------------------------------------------

def test_the_reason_counts_use_the_block_8a_reasons_and_classifier():
    from services import reject_reason

    assert operational_metrics_service.REJECT_REASONS is reject_reason.REJECT_REASONS
    assert operational_metrics_service.classify_legacy_reject_message is reject_reason.classify_legacy_reject_message
    assert operational_metrics_service.reason_for_error_class is reject_reason.reason_for_error_class

    source = inspect.getsource(operational_metrics_service.get_reject_counts_by_reason)
    for used in ("local_day_bounds(local_date, zone)", "get_effective_cutover(conn, tenant)",
                 "cutover_boundary(cutover_at, zone)", "classify_legacy_reject_message(error_message)",
                 "reason_for_error_class(error_class)", "_V1_REJECT_REASON_COUNT_SQL", "_V2_REJECT_REASON_COUNT_SQL"):
        assert used in source, used


def test_the_reason_counts_know_nothing_of_the_dashboard_the_collector_or_any_other_metric():
    source = inspect.getsource(operational_metrics_service.get_reject_counts_by_reason)

    for forbidden in ("simplify", "reject_logic", "pandas", "streamlit", "mixed_era", "collector", "v2_normalize",
                      "checkin", "rate", "barcode", "item_key", "event_key", "fetchall", "now(", "_REJECT_COUNT_SQL",
                      "DISTINCT", "set("):
        assert forbidden not in source, forbidden


def test_adding_the_reason_counts_left_the_reject_count_and_the_check_in_functions_as_they_were():
    # Additive: the reason counts repeat the few lines of era clamping rather than sharing them, so the existing
    # functions and statements did not have to change.
    reject_source = inspect.getsource(operational_metrics_service.get_reject_count)
    assert "_V1_REJECT_COUNT_SQL" in reject_source and "_V2_REJECT_COUNT_SQL" in reject_source

    for function in (operational_metrics_service.get_reject_count, operational_metrics_service.get_checkin_count,
                     operational_metrics_service.get_checkin_counts_by_hour):
        source = inspect.getsource(function).lower()
        for added in ("reason", "classify", "logger", "unexpected", "group by", "error_"):
            assert added not in source, (function.__name__, added)

    for name in ("_V1_REJECT_COUNT_SQL", "_V2_REJECT_COUNT_SQL", "_V1_CHECKIN_COUNT_SQL", "_V2_CHECKIN_COUNT_SQL",
                 "_V1_CHECKIN_HOURLY_COUNT_SQL", "_V2_CHECKIN_HOURLY_COUNT_SQL"):
        assert "GROUP BY" not in _statement(name) and "error_" not in _statement(name), name


def test_the_reject_count_is_unaffected_by_what_the_reason_counts_read(reject_db):
    _cutover(reject_db, NOON_CUTOVER)
    for minute, message in enumerate(V1_NO_TEXT):
        _v1_reject(reject_db, _local(2026, 6, 10, 9, minute), error_message=message)
    _v2_reject(reject_db, _utc(2026, 6, 10, 18, 0), error_class="jam")

    before = _reject_count(reject_db)[0]
    reasons, _ = _reason_counts(reject_db)
    after, recorder = _reject_count(reject_db)

    assert before == after == RejectCount(total=8, v1_count=7, v2_count=1)
    assert sum(reasons.counts) == 8
    for sql, _ in recorder.statements:                      # the count still reads no reason column
        assert "error_" not in sql and "GROUP BY" not in sql


def test_the_reason_warning_is_the_modules_only_log_call_and_names_no_value():
    source = inspect.getsource(operational_metrics_service)

    assert operational_metrics_service.logger.name == REASON_LOGGER
    assert source.count("logger.") == 1
    assert source.count("logger.warning(") == 1
    call = source[source.index("logger.warning("):].split(")\n")[0]
    assert '"Reject rows with an unrecognised stored class were counted as other: rows=%d"' in call
    assert call.rstrip().endswith("unexpected_class_rows")   # the one argument: an integer count


# =====================================================================================================================
# Module boundaries
# =====================================================================================================================

def test_the_module_depends_only_on_the_standard_library_sqlalchemy_and_the_resolved_tenant_type():
    imports = [line.strip() for line in inspect.getsource(operational_metrics_service).splitlines()
               if line.startswith(("import ", "from "))]

    assert imports == [
        "from __future__ import annotations",
        "import logging",
        "from dataclasses import dataclass",
        "from datetime import UTC, date, datetime, time, timedelta",
        "from zoneinfo import ZoneInfo",
        "from sqlalchemy import DateTime, bindparam, text",
        "from sqlalchemy.engine import Connection",
        "from services.reject_reason import (",
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
    assert sorted(statements) == [
        "_EFFECTIVE_CUTOVER_SQL", "_V1_CHECKIN_COUNT_SQL", "_V1_CHECKIN_HOURLY_COUNT_SQL", "_V1_REJECT_COUNT_SQL",
        "_V1_REJECT_REASON_COUNT_SQL", "_V2_CHECKIN_COUNT_SQL", "_V2_CHECKIN_HOURLY_COUNT_SQL", "_V2_REJECT_COUNT_SQL",
        "_V2_REJECT_REASON_COUNT_SQL",
    ]

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
