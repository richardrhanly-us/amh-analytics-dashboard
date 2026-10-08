"""Range reports for one sorter site: the counts behind the customer API's
Overview, Volume, Routing, Reliability and Bin Volume reports.

Everything here is a COUNT OF STORED ROWS over a range of local calendar
days. Nothing is derived: no rate, average, percentage or "busiest" anything.
Those are arithmetic on these integers and belong to whoever shows them.

THE RULES ARE THE SINGLE-DAY RULES, APPLIED TO MORE DAYS
(services.operational_metrics_service, which this module builds on and does
not change):

    a day      is a calendar day in the product's zone, whatever its length
               in real time -- 23 hours when the clocks go forward, 25 when
               they go back
    an hour    is a wall-clock hour, 0 to 23: the skipped hour is empty, and
               the repeated hour holds both of its passes
    the eras   a site's one cutover divides its history. Legacy rows (naive
               local wall-clock time) count strictly before it; current rows
               (instants) count at or after it. No row is counted twice or
               moved, and a legacy time is never read as UTC.

So every day of a range has exactly the counts the single-day reads give for
that day: the bounds are the same values, computed by the same functions.

HOW A RANGE IS COUNTED. Every boundary -- each local midnight, each
wall-clock hour -- is computed here, in Python, and bound with its type
stated, all of them together as one array. A statement then names, for each
row, the interval it falls in, and counts the rows of each:

    width_bucket(event_time, :boundaries)    the number of boundaries at or before the row's time

so bucket N (from 1) is [boundary N-1, boundary N): the same half-open
intervals, made from the same boundaries, as one COUNT(*) FILTER column per
bucket made before Reports R9D1. Two equal boundaries make an interval no
time can fall in -- the hour the clocks skip -- exactly as before. The
answer is one row per bucket that has rows, optionally per value of the one
stored column that says where an item went or why it was rejected; the
buckets with none are zero here. The database compares and counts. It
converts no time zone, truncates no timestamp, casts nothing and reads no
clock, so no result can depend on the database session's time zone. A stored
destination or reject message is read only as the key of a group, turned
into a public key or reason, counted, and dropped.

ONE STATEMENT FAMILY PER FIGURE. Numbers that must add up are taken from the
same rows: a routing report's day totals, its home / transit / other split
and its range total are all sums of one grouped result, and a reliability
report's reasons and its daily rejects likewise. Counting them with separate
statements would let a row that arrives between two statements break the
sum.

THE RANGE LIMIT. MAX_REPORT_RANGE_DAYS bounds how many days ONE REQUEST may
cover: an engineering guard against a pathological synchronous request, and
nothing more. It is not how much history an organization may see -- that is
its plan's (services.entitlement_service.earliest_report_date), and a plan
with no history limit has none, however long this guard is. The way of
counting above has no limit of its own on the number of buckets (the
one-column-per-bucket statements it replaced stopped at PostgreSQL's 1,664
columns).

Framework-neutral: no Streamlit, no FastAPI, no pandas, no caching, no
engine. Every read takes a connection the caller supplies -- already scoped
to the tenant by row level security -- and database errors propagate: the
counts of one era, or of part of a range, are never returned as the whole.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import ARRAY, DateTime, bindparam, text
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import TextClause

from services.operational_metrics_service import (
    WALL_CLOCK_HOURS_PER_DAY,
    cutover_boundary,
    get_effective_cutover,
    local_day_bounds,
    local_hour_boundaries,
)
from services.reject_reason import (
    REJECT_REASONS,
    classify_legacy_reject_message,
    reason_for_error_class,
)
from services.routing_destination import RoutingConfig, destination_key
from services.sort_bin import bin_key, bin_order
from services.tenant_resolution_service import ResolvedOperationalTenant

logger = logging.getLogger("sortview.operational_reports")

# The most days one request may cover, both ends included: an engineering guard, not a history limit. See THE
# RANGE LIMIT above. (It was 92 until Reports R9D2, while each day was a column of its own.)
MAX_REPORT_RANGE_DAYS = 3660

# How many days of wall-clock hours one statement counts. It once had to be a week: a bucket was a column, and a
# week is 168 of them. A bucket is now a row, so this bounds only the one array of boundaries a statement binds:
# 366 days is at most 366 * 24 + 1 = 8,785 instants -- roughly 350 KB once the driver writes them into the
# statement -- and one statement for each year of a range.
_HOUR_BUCKET_DAYS_PER_STATEMENT = 366


# =====================================================================================================================
# A range of local calendar days
# =====================================================================================================================

class ReportRangeError(ValueError):
    """A range that cannot be reported on. `problem` is one of the fixed
    words below and `field` the bound it is about; neither carries a value."""

    def __init__(self, problem: str, field: str) -> None:
        super().__init__(problem)
        self.problem = problem
        self.field = field


def validate_report_range(from_date: date, to_date: date, *, today: date) -> None:
    """Refuses (ReportRangeError) a range that ends before it starts, ends
    after `today`, or covers more than MAX_REPORT_RANGE_DAYS days. `today` is
    the product's current calendar date, given by the caller: nothing here
    reads a clock. Today itself may be in the range; it is then a day that is
    not over yet."""
    if from_date > to_date:
        raise ReportRangeError("range_order", "from")
    if to_date > today:
        raise ReportRangeError("range_in_future", "to")
    if (to_date - from_date).days + 1 > MAX_REPORT_RANGE_DAYS:
        raise ReportRangeError("range_too_long", "to")


@dataclass(frozen=True, slots=True)
class LocalRange:
    """A run of consecutive local calendar days in one zone, both ends
    included, with the boundaries between them in the two forms the tables
    need.

    `dates` has one entry per day. The two boundary tuples have one MORE
    entry than that: day N is the half-open interval [boundary N,
    boundary N + 1), so the first boundary starts the range and the last
    ends it.

    v1_day_boundaries_local are NAIVE local midnights, for the legacy tables.
    v2_day_boundaries_utc are the same midnights as AWARE UTC instants, for
    the current ones. Each is exactly what local_day_bounds gives for that
    day, so consecutive days always meet.
    """

    from_date: date
    to_date: date
    zone: ZoneInfo
    dates: tuple[date, ...]
    v1_day_boundaries_local: tuple[datetime, ...]
    v2_day_boundaries_utc: tuple[datetime, ...]

    @property
    def days(self) -> int:
        return len(self.dates)

    @property
    def timezone_name(self) -> str:
        return self.zone.key


def local_range(from_date: date, to_date: date, zone: ZoneInfo) -> LocalRange:
    """The days from `from_date` to `to_date`, both included, in `zone`.
    A range that ends before it starts is refused (ValueError); how long a
    range may be is validate_report_range's business, not this function's."""
    if from_date > to_date:
        raise ValueError("a range must not end before it starts")

    dates = tuple(from_date + timedelta(days=offset) for offset in range((to_date - from_date).days + 1))
    bounds = [local_day_bounds(day, zone) for day in dates]

    return LocalRange(
        from_date=from_date,
        to_date=to_date,
        zone=zone,
        dates=dates,
        v1_day_boundaries_local=(*(day.v1_start_local for day in bounds), bounds[-1].v1_end_local),
        v2_day_boundaries_utc=(*(day.v2_start_utc for day in bounds), bounds[-1].v2_end_utc),
    )


# =====================================================================================================================
# The part of a range each era owns
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class ReportWindow:
    """A range, and the part of it each era owns for one site.

    v1_span is [start, end) as NAIVE local wall-clock values: the range, cut
    off at the site's cutover. v2_span is [start, end) as AWARE UTC instants:
    the range from the cutover on. Either is None when that era owns none of
    the range, and that era's tables are then not read at all. With no
    effective cutover the site is legacy only: v1 owns the whole range.

    The two spans never overlap and never leave a gap inside the range. An
    event exactly at the cutover belongs to v2.
    """

    local_range: LocalRange
    v1_span: tuple[datetime, datetime] | None
    v2_span: tuple[datetime, datetime] | None


def report_window(conn: Connection, tenant: ResolvedOperationalTenant, local_range: LocalRange) -> ReportWindow:
    """The window for `tenant`'s site over `local_range`. One statement runs:
    the cutover lookup the single-day reads use."""
    range_start_local, range_end_local = local_range.v1_day_boundaries_local[0], local_range.v1_day_boundaries_local[-1]
    range_start_utc, range_end_utc = local_range.v2_day_boundaries_utc[0], local_range.v2_day_boundaries_utc[-1]

    cutover_at = get_effective_cutover(conn, tenant)

    v1_end_local = range_end_local
    v2_span = None
    if cutover_at is not None:
        boundary = cutover_boundary(cutover_at, local_range.zone)
        # v1: [range start, the earlier of range end and the cutover) on the local clock.
        v1_end_local = min(range_end_local, boundary.cutover_local_naive)
        # v2: [the later of range start and the cutover, range end) as instants.
        v2_start_utc = max(range_start_utc, boundary.cutover_utc)
        if v2_start_utc < range_end_utc:
            v2_span = (v2_start_utc, range_end_utc)

    return ReportWindow(
        local_range=local_range,
        v1_span=(range_start_local, v1_end_local) if range_start_local < v1_end_local else None,
        v2_span=v2_span,
    )


# =====================================================================================================================
# Counting rows between consecutive boundaries
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class _Source:
    """One table a count is made from. `aware` says how it keeps time: False
    for a legacy table (TIMESTAMP, naive local), True for a current one
    (TIMESTAMPTZ). `group_column` is the one stored column a grouped count is
    keyed by, or None for a plain count."""

    table: str
    aware: bool
    group_column: str | None = None


# Every source this module reads. A statement is only ever built from one of
# these: no table or column name comes from anywhere else.
_V1_CHECKINS = _Source("checkins", aware=False)
_V2_CHECKINS = _Source("checkin_events", aware=True)
_V1_REJECTS = _Source("rejects", aware=False)
_V2_REJECTS = _Source("reject_events", aware=True)
_V1_CHECKINS_BY_DESTINATION = _Source("checkins", aware=False, group_column="destination")
_V2_CHECKINS_BY_DESTINATION = _Source("checkin_events", aware=True, group_column="destination")
_V1_REJECTS_BY_MESSAGE = _Source("rejects", aware=False, group_column="error_message")
_V2_REJECTS_BY_CLASS = _Source("reject_events", aware=True, group_column="error_class")
_V1_CHECKINS_BY_BIN = _Source("checkins", aware=False, group_column="bin")
_V2_CHECKINS_BY_BIN = _Source("checkin_events", aware=True, group_column="bin")

_SOURCES = (
    _V1_CHECKINS, _V2_CHECKINS, _V1_REJECTS, _V2_REJECTS,
    _V1_CHECKINS_BY_DESTINATION, _V2_CHECKINS_BY_DESTINATION, _V1_REJECTS_BY_MESSAGE, _V2_REJECTS_BY_CLASS,
    _V1_CHECKINS_BY_BIN, _V2_CHECKINS_BY_BIN,
)


def _bucket_count_statement(source: _Source) -> TextClause:
    """One statement counting `source`'s rows by the interval of the bound
    `boundaries` each falls in: one row of (group value, bucket, count) for
    each group and bucket that has rows. Bucket N (from 1) is
    [boundary N-1, boundary N). The WHERE clause is the single-day reads' --
    the tenant, then the span the era owns -- so a row is counted here
    exactly when it is counted there, and the bucket it lands in is decided
    only by which pair of boundaries it falls between.

    The text is assembled from this module's own constants. Nothing from a
    caller's data is ever part of it, and every time is a bound value: the
    boundaries are one array of the type the table keeps its times in.
    """
    if source not in _SOURCES:
        raise ValueError("a count statement can only be built for one of this module's own sources")

    group = f"{source.group_column}, " if source.group_column is not None else ""
    sql = (
        f"    SELECT {group}width_bucket(event_time, :boundaries) AS bucket, COUNT(*) AS row_count\n"  # nosec B608
        f"    FROM {source.table}\n"
        "    WHERE customer_id = :customer_id\n"
        "      AND branch_id = :branch_id\n"
        "      AND event_time >= :span_start\n"
        "      AND event_time < :span_end\n"
        f"    GROUP BY {group}bucket\n"
    )
    time_type = DateTime(timezone=source.aware)
    return text(sql).bindparams(
        bindparam("span_start", type_=time_type),
        bindparam("span_end", type_=time_type),
        bindparam("boundaries", type_=ARRAY(time_type)),
    )


def _execute_bucket_count(
    conn: Connection,
    tenant: ResolvedOperationalTenant,
    source: _Source,
    span: tuple[datetime, datetime],
    boundaries: Sequence[datetime],
) -> list[tuple]:
    """`source`'s rows in `span`, counted in each of the intervals between
    consecutive `boundaries`: for a plain count, one row of one count per
    interval; for a grouped one, a row for each stored value that has rows in
    the span, that value first and then one count per interval. Every
    interval is there, zero where nothing fell in it."""
    buckets = len(boundaries) - 1
    if buckets < 1:
        raise ValueError("a count statement needs at least one bucket")

    rows = conn.execute(
        _bucket_count_statement(source),
        {
            "customer_id": tenant.operational_customer_id,
            "branch_id": tenant.operational_branch_id,
            "span_start": span[0],
            "span_end": span[1],
            "boundaries": list(boundaries),
        },
    )
    grouped = source.group_column is not None
    counts_by_group: dict[object, list[int]] = {}
    for row in rows:
        *group, bucket, count = tuple(row)
        # The span lies within the boundaries, so a row's bucket is always one of the intervals between them.
        if isinstance(bucket, bool) or not isinstance(bucket, int) or not 1 <= bucket <= buckets:
            raise ValueError("a count statement placed a row outside the intervals it was given")
        counts_by_group.setdefault(group[0] if grouped else None, [0] * buckets)[bucket - 1] += int(count)

    if not grouped:
        return [tuple(counts_by_group.get(None, [0] * buckets))]
    return [(stored, *counts) for stored, counts in counts_by_group.items()]


def _bucket_counts(row: object, buckets: int) -> tuple[int, ...]:
    counts = tuple(int(value) for value in row)  # type: ignore[attr-defined]
    if len(counts) != buckets:
        raise ValueError("a count statement returned a different number of counts than it has buckets")
    return counts


def _eras(window: ReportWindow, v1_source: _Source, v2_source: _Source) -> Iterator[tuple[_Source, tuple, tuple]]:
    """(source, owned span, day boundaries) for each era that owns part of the window."""
    if window.v1_span is not None:
        yield v1_source, window.v1_span, window.local_range.v1_day_boundaries_local
    if window.v2_span is not None:
        yield v2_source, window.v2_span, window.local_range.v2_day_boundaries_utc


def _counts_by_day(
    conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow, v1_source: _Source, v2_source: _Source
) -> tuple[int, ...]:
    """A plain row count for each day of the window, both eras together."""
    days = window.local_range.days
    totals = [0] * days
    for source, span, boundaries in _eras(window, v1_source, v2_source):
        counts = _bucket_counts(_execute_bucket_count(conn, tenant, source, span, boundaries)[0], days)
        totals = [total + count for total, count in zip(totals, counts, strict=True)]
    return tuple(totals)


# =====================================================================================================================
# Check-ins and rejects, by day
# =====================================================================================================================

def get_checkin_counts_by_day(conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow) -> tuple[int, ...]:
    """Check-ins on each day of the window: one count per day, in order, zero
    where there were none. Each is that day's CheckinCount.total. One
    statement runs per era that owns part of the window."""
    return _counts_by_day(conn, tenant, window, _V1_CHECKINS, _V2_CHECKINS)


def get_reject_counts_by_day(conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow) -> tuple[int, ...]:
    """Rejects on each day of the window, as get_checkin_counts_by_day counts
    check-ins. Each is that day's RejectCount.total."""
    return _counts_by_day(conn, tenant, window, _V1_REJECTS, _V2_REJECTS)


# =====================================================================================================================
# Check-ins, by day and wall-clock hour
# =====================================================================================================================

def get_checkin_counts_by_hour(
    conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow
) -> tuple[tuple[int, ...], ...]:
    """Check-ins in each wall-clock hour of each day of the window: one tuple
    of 24 counts per day, in order. Each is that day's CheckinHourlyCounts
    .counts, so a row's sum is the day's check-in count.

    The hours are local_hour_boundaries': on the day the clocks go forward
    the skipped hour holds no current rows, and on the day they go back the
    repeated hour holds both of its passes. A cutover inside an hour splits
    that hour between the eras, with nothing counted twice.

    The days are counted _HOUR_BUCKET_DAYS_PER_STATEMENT at a time, and a
    stretch of days an era owns none of is not asked for.
    """
    by_day = [[0] * WALL_CLOCK_HOURS_PER_DAY for _ in window.local_range.dates]

    for first, source, owned, boundaries in _weeks_of_hours(window, _V1_CHECKINS, _V2_CHECKINS):
        buckets = len(boundaries) - 1
        counts = _bucket_counts(_execute_bucket_count(conn, tenant, source, owned, boundaries)[0], buckets)
        for index, count in enumerate(counts):
            by_day[first + index // WALL_CLOCK_HOURS_PER_DAY][index % WALL_CLOCK_HOURS_PER_DAY] += count

    return tuple(tuple(hours) for hours in by_day)


def _weeks_of_hours(
    window: ReportWindow, v1_source: _Source, v2_source: _Source
) -> Iterator[tuple[int, _Source, tuple[datetime, datetime], tuple[datetime, ...]]]:
    """The statements an hourly count of the window needs, a stretch of
    _HOUR_BUCKET_DAYS_PER_STATEMENT days of wall-clock hours at a time (the
    name is from when a stretch was a week): (index of the stretch's first
    day, source, the part of that stretch the source's era owns, the
    stretch's hour boundaries).

    Bucket N of a stretch is hour N % 24 of its day N // 24. A stretch an era
    owns none of is not yielded, so its table is not read for it.
    """
    local = window.local_range
    hours_per_day = [local_hour_boundaries(day, local.zone) for day in local.dates]

    for first in range(0, local.days, _HOUR_BUCKET_DAYS_PER_STATEMENT):
        week = hours_per_day[first:first + _HOUR_BUCKET_DAYS_PER_STATEMENT]
        for source, span, boundaries in (
            (v1_source, window.v1_span, _week_boundaries(day.v1_boundaries_local for day in week)),
            (v2_source, window.v2_span, _week_boundaries(day.v2_boundaries_utc for day in week)),
        ):
            if span is None:
                continue
            # The part of this week the era owns. Boundaries outside it simply count nothing.
            owned = (max(span[0], boundaries[0]), min(span[1], boundaries[-1]))
            if owned[0] >= owned[1]:
                continue
            yield first, source, owned, boundaries


def _week_boundaries(days_of_boundaries) -> tuple[datetime, ...]:
    """The hour boundaries of consecutive days as one run: each day's first
    24 boundaries, then the last day's end. A day's 25th boundary is the next
    day's first, so it is taken once."""
    days = [tuple(boundaries) for boundaries in days_of_boundaries]
    return (*(boundary for day in days for boundary in day[:WALL_CLOCK_HOURS_PER_DAY]), days[-1][-1])


# =====================================================================================================================
# Check-ins, by day and destination
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class DestinationCounts:
    """Check-ins by where the sorter routed them, for one day or a whole
    range. `transit_counts` has one entry per destination of the routing
    configuration the counts were made with, in its order. Every check-in is
    in exactly one of the three, so `total` is their sum."""

    home_count: int
    transit_counts: tuple[int, ...]
    other_count: int

    @property
    def transit_count(self) -> int:
        return sum(self.transit_counts)

    @property
    def total(self) -> int:
        return self.home_count + self.transit_count + self.other_count


def get_checkin_destination_counts_by_day(
    conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow, routing: RoutingConfig
) -> tuple[DestinationCounts, ...]:
    """Check-ins on each day of the window by destination: home, each
    destination in `routing` in its order, and everything else. One
    DestinationCounts per day, in order.

    The classification is the single-day read's
    (operational_metrics_service.get_checkin_counts_by_destination): each
    stored destination becomes its routing_destination.destination_key, the
    same key whichever table it came from. A destination that is neither
    home nor configured -- or has no usable value, or none at all -- is
    "other"; nothing is left out. Each day's total is that day's check-in
    count.

    One statement runs per era that owns part of the window.
    """
    days = window.local_range.days
    destinations = len(routing.transit)
    transit_slot = {destination.key: slot for slot, destination in enumerate(routing.transit)}
    home = [0] * days
    transit = [[0] * destinations for _ in range(days)]
    other = [0] * days

    for source, span, boundaries in _eras(window, _V1_CHECKINS_BY_DESTINATION, _V2_CHECKINS_BY_DESTINATION):
        # One row per distinct stored destination, a NULL one included.
        for stored, *row in _execute_bucket_count(conn, tenant, source, span, boundaries):
            key = destination_key(stored)
            for day, count in enumerate(_bucket_counts(row, days)):
                if not count:
                    continue
                if key in routing.home_keys:
                    home[day] += count
                elif key in transit_slot:
                    transit[day][transit_slot[key]] += count
                else:
                    other[day] += count

    return tuple(
        DestinationCounts(home_count=home[day], transit_counts=tuple(transit[day]), other_count=other[day])
        for day in range(days)
    )


def sum_destination_counts(by_day: Sequence[DestinationCounts], routing: RoutingConfig) -> DestinationCounts:
    """The whole range's counts: the days', added up destination by destination."""
    return DestinationCounts(
        home_count=sum(day.home_count for day in by_day),
        transit_counts=tuple(
            sum(day.transit_counts[slot] for day in by_day) for slot in range(len(routing.transit))
        ),
        other_count=sum(day.other_count for day in by_day),
    )


# =====================================================================================================================
# Rejects, by day and reason
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class RejectReasonRangeCounts:
    """Rejects over a range, by reason and by day -- two views of the same
    rows, so both add up to the same total.

    `reason_counts` has exactly one entry per reason, in the order of
    services.reject_reason.REJECT_REASONS. `day_counts` has one entry per day
    of the range. `unexpected_class_rows` is how many current rows carried a
    stored class that is not one of the reason codes: they are counted under
    `other`, so they are in both tuples already.
    """

    reason_counts: tuple[int, ...]
    day_counts: tuple[int, ...]
    unexpected_class_rows: int

    @property
    def total(self) -> int:
        return sum(self.reason_counts)


_REASON_SLOT = {reason: slot for slot, reason in enumerate(REJECT_REASONS)}


def get_reject_reason_counts_by_day(
    conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow
) -> RejectReasonRangeCounts:
    """Rejects in the window, for each reason and for each day.

    The reasons are the single-day read's
    (operational_metrics_service.get_reject_counts_by_reason, and the rules
    in services.reject_reason): a legacy row's reason is classified from its
    message; a current row's stored class is its reason, and one that is not
    a reason code is counted as `other`, with a warning carrying only the
    number of such rows. Every stored reject is counted once, under exactly
    one reason and on exactly one day.

    One statement runs per era that owns part of the window. A stored
    message is read only as the key of a group: it is never returned, logged
    or put in an error.
    """
    days = window.local_range.days
    reason_counts = [0] * len(REJECT_REASONS)
    day_counts = [0] * days
    unexpected_class_rows = 0

    for source, span, boundaries in _eras(window, _V1_REJECTS_BY_MESSAGE, _V2_REJECTS_BY_CLASS):
        # One row per distinct stored message or class, a NULL one included.
        for stored, *row in _execute_bucket_count(conn, tenant, source, span, boundaries):
            counts = _bucket_counts(row, days)
            rows = sum(counts)
            if source.aware:
                reason = reason_for_error_class(stored)
                if reason is None:
                    reason = "other"
                    unexpected_class_rows += rows
            else:
                reason = classify_legacy_reject_message(stored)
            reason_counts[_REASON_SLOT[reason]] += rows
            day_counts = [total + count for total, count in zip(day_counts, counts, strict=True)]

    if unexpected_class_rows:
        # The number only: not the stored value, and nothing that says whose rows they are.
        logger.warning("Reject rows with an unrecognised stored class were counted as other: rows=%d",
                       unexpected_class_rows)

    return RejectReasonRangeCounts(
        reason_counts=tuple(reason_counts),
        day_counts=tuple(day_counts),
        unexpected_class_rows=unexpected_class_rows,
    )


# =====================================================================================================================
# Check-ins, by sort bin and wall-clock hour
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class BinCounts:
    """Check-ins logged in one sort bin over a range. `key` is the bin's
    number (services.sort_bin.bin_key). `hour_counts` has 24 entries: entry N
    is the TOTAL for wall-clock hour N across every day of the range."""

    key: str
    hour_counts: tuple[int, ...]

    @property
    def checkin_count(self) -> int:
        return sum(self.hour_counts)


@dataclass(frozen=True, slots=True)
class BinVolumeReport:
    """A site's check-ins over a range by the sort bin each was logged in.

    `bins` has one entry for each bin that at least one check-in of the range
    was logged in, in numeric order -- and no other: nothing here knows which
    bins a sorter has. `unknown_count` is the check-ins whose stored bin
    names no bin. Every check-in is in exactly one of the two, so
    `checkin_count` is the range's check-in count.
    """

    bins: tuple[BinCounts, ...]
    unknown_count: int

    @property
    def known_count(self) -> int:
        return sum(counts.checkin_count for counts in self.bins)

    @property
    def checkin_count(self) -> int:
        return self.known_count + self.unknown_count


def get_bin_volume_report(conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow) -> BinVolumeReport:
    """Check-ins in the window by sort bin, and for each bin by wall-clock
    hour.

    The rows are the volume report's rows, counted in the same hour buckets
    (get_checkin_counts_by_hour) and only grouped by the one stored column
    that says which bin: so the total here is the range's check-in count, an
    hour is a wall-clock hour on every day including the days the clocks
    change, and each era's rows count only on its own side of the cutover.

    A stored bin becomes its services.sort_bin.bin_key -- the same key
    whichever table it came from -- or, when it names no bin, is counted as
    unknown. Nothing is left out and nothing is counted twice. Every figure
    is a sum of the one grouped count, so the bins, their hours and the
    totals always agree.

    One statement runs per week of the window per era that owns part of it.
    A stored bin is read only as the key of a group.
    """
    by_key: dict[str, list[int]] = {}
    unknown_count = 0

    for _first, source, owned, boundaries in _weeks_of_hours(window, _V1_CHECKINS_BY_BIN, _V2_CHECKINS_BY_BIN):
        buckets = len(boundaries) - 1
        # One row per distinct stored bin, a NULL one included.
        for stored, *row in _execute_bucket_count(conn, tenant, source, owned, boundaries):
            counts = _bucket_counts(row, buckets)
            key = bin_key(stored)
            if key is None:
                unknown_count += sum(counts)
                continue
            hours = by_key.setdefault(key, [0] * WALL_CLOCK_HOURS_PER_DAY)
            for index, count in enumerate(counts):
                hours[index % WALL_CLOCK_HOURS_PER_DAY] += count

    return BinVolumeReport(
        bins=tuple(
            BinCounts(key=key, hour_counts=tuple(by_key[key]))
            for key in sorted(by_key, key=bin_order)
            # Observed bins only: a bin is here because a check-in of the range was logged in it.
            if any(by_key[key])
        ),
        unknown_count=unknown_count,
    )


# =====================================================================================================================
# The reports
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class OverviewReport:
    """A site's headline counts over a range. `checkin_days` and
    `reject_days` have one entry per day. `routing` is the range's check-ins
    by destination, so `routing.total` is the range's check-in count and the
    sum of `checkin_days`. `active_days` is how many days had at least one
    check-in."""

    checkin_days: tuple[int, ...]
    reject_days: tuple[int, ...]
    routing: DestinationCounts

    @property
    def checkin_count(self) -> int:
        return self.routing.total

    @property
    def reject_count(self) -> int:
        return sum(self.reject_days)

    @property
    def active_days(self) -> int:
        return sum(1 for count in self.checkin_days if count > 0)


def get_overview_report(
    conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow, routing: RoutingConfig
) -> OverviewReport:
    """The overview. Every check-in figure in it comes from the one grouped
    count by destination, so the home / transit / other split, the daily
    counts and the total always agree. Rejects are counted separately: they
    are a different set of rows."""
    by_destination = get_checkin_destination_counts_by_day(conn, tenant, window, routing)
    return OverviewReport(
        checkin_days=tuple(day.total for day in by_destination),
        reject_days=get_reject_counts_by_day(conn, tenant, window),
        routing=sum_destination_counts(by_destination, routing),
    )


@dataclass(frozen=True, slots=True)
class VolumeReport:
    """A site's check-ins over a range, by day and by wall-clock hour.
    `checkin_days` has one entry per day; `checkin_hours` has 24, each the
    TOTAL for that hour across every day of the range -- not an average.
    Both add up to `checkin_count`."""

    checkin_days: tuple[int, ...]
    checkin_hours: tuple[int, ...]

    @property
    def checkin_count(self) -> int:
        return sum(self.checkin_days)


def get_volume_report(conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow) -> VolumeReport:
    """The volume report. Both views are sums of the one day-by-hour count."""
    by_hour = get_checkin_counts_by_hour(conn, tenant, window)
    return VolumeReport(
        checkin_days=tuple(sum(hours) for hours in by_hour),
        checkin_hours=tuple(
            sum(hours[hour] for hours in by_hour) for hour in range(WALL_CLOCK_HOURS_PER_DAY)
        ),
    )


@dataclass(frozen=True, slots=True)
class RoutingReport:
    """A site's check-ins over a range by destination: for each day, and for
    the range as a whole (`total`, the days added up)."""

    days: tuple[DestinationCounts, ...]
    total: DestinationCounts


def get_routing_report(
    conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow, routing: RoutingConfig
) -> RoutingReport:
    by_day = get_checkin_destination_counts_by_day(conn, tenant, window, routing)
    return RoutingReport(days=by_day, total=sum_destination_counts(by_day, routing))


@dataclass(frozen=True, slots=True)
class ReliabilityReport:
    """A site's rejects over a range, by reason and by day, beside its
    check-ins by day. Rejects are not a subset of check-ins: the two are
    counted from different tables and only shown together."""

    checkin_days: tuple[int, ...]
    rejects: RejectReasonRangeCounts

    @property
    def checkin_count(self) -> int:
        return sum(self.checkin_days)

    @property
    def reject_count(self) -> int:
        return self.rejects.total


def get_reliability_report(
    conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow
) -> ReliabilityReport:
    return ReliabilityReport(
        checkin_days=get_checkin_counts_by_day(conn, tenant, window),
        rejects=get_reject_reason_counts_by_day(conn, tenant, window),
    )
