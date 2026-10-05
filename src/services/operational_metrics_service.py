"""Operational metrics for the customer API: the time and cutover rules, and
the check-in count built on them.

A branch's check-in history lives in two tables that keep time differently:

    checkins.event_time         TIMESTAMP    (legacy "v1")
        A NAIVE LOCAL WALL-CLOCK value: what the sorter's own clock read, in
        the library's time zone, with no offset stored.

    checkin_events.event_time   TIMESTAMPTZ  (Contract "v2")
        A true instant.

A question such as "how many check-ins on 2026-06-01?" is about a LOCAL
calendar day, so it has to be asked of each table in that table's own terms:
naive local bounds for v1, UTC instants for v2. This module derives both from
one (local date, time zone) pair, and does the same for a branch's v1 -> v2
cutover instant. Every conversion is done here, in Python, with zoneinfo.
Nothing depends on the database session's time zone or on the process's.

THE PARTITION a branch's cutover defines:

    v1 rows count when   event_time <  cutover, as local wall-clock time
    v2 rows count when   event_time >= cutover, as an instant

so an event exactly at the cutover belongs to v2, and no event is counted
twice or dropped.

A naive v1 value is never labelled UTC here. (The Streamlit dashboard's
mixed-era path currently does label it UTC once a branch has a cutover, which
shifts those rows by the zone's UTC offset; that path is separate from this
module and is not changed by it.)

Framework-neutral: no Streamlit, no FastAPI, no pandas, no caching, no engine.
Every database read takes a connection the caller supplies -- already scoped
to the tenant by row level security -- and database errors propagate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.engine import Connection

from services.reject_reason import (
    REJECT_REASONS,
    classify_legacy_reject_message,
    reason_for_error_class,
)
from services.tenant_resolution_service import ResolvedOperationalTenant

logger = logging.getLogger("sortview.operational_metrics")


def _require_aware(value: datetime, what: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")


# =====================================================================================================================
# One local calendar day, in both time domains
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class LocalDayBounds:
    """One local calendar day as a half-open interval [start, end), expressed
    once for each table.

    v1_start_local / v1_end_local are NAIVE local wall-clock datetimes, for
    comparing with checkins.event_time. They carry no tzinfo on purpose and
    must never be given one.

    v2_start_utc / v2_end_utc are AWARE UTC instants -- the same two local
    midnights -- for comparing with checkin_events.event_time.

    The local day is always exactly one calendar day. Its length in real
    time is whatever the zone makes it: 23 hours on a spring-forward date,
    25 on a fall-back date.
    """

    local_date: date
    timezone_name: str
    v1_start_local: datetime
    v1_end_local: datetime
    v2_start_utc: datetime
    v2_end_utc: datetime


def _local_midnight_utc(local_date: date, zone: ZoneInfo) -> datetime:
    # fold=0. Where the zone changes its clocks AT midnight: if 00:00 does
    # not exist that day, this is the instant the day starts; if 00:00
    # happens twice, this is its first occurrence. Either way the end of one
    # day and the start of the next are computed by this same call, so
    # consecutive days always meet exactly.
    return datetime.combine(local_date, time.min, tzinfo=zone).astimezone(UTC)


def local_day_bounds(local_date: date, zone: ZoneInfo) -> LocalDayBounds:
    """The bounds of `local_date` in `zone`, for both tables."""
    next_date = local_date + timedelta(days=1)

    return LocalDayBounds(
        local_date=local_date,
        timezone_name=zone.key,
        v1_start_local=datetime.combine(local_date, time.min),
        v1_end_local=datetime.combine(next_date, time.min),
        v2_start_utc=_local_midnight_utc(local_date, zone),
        v2_end_utc=_local_midnight_utc(next_date, zone),
    )


# =====================================================================================================================
# The wall-clock hours of one local day, in both time domains
# =====================================================================================================================

WALL_CLOCK_HOURS_PER_DAY = 24


@dataclass(frozen=True, slots=True)
class LocalHourBoundaries:
    """The 25 boundaries that cut one local calendar day into its 24
    WALL-CLOCK hours, expressed once for each table. Hour H -- the hour the
    local clock reads H:xx -- is the half-open interval [boundary H,
    boundary H + 1), so boundary 0 is the start of the day and boundary 24
    its end, the same values local_day_bounds gives.

    v1_boundaries_local are NAIVE local wall-clock datetimes, for comparing
    with checkins.event_time: H:00 on the date, then 00:00 on the next. They
    are the same 25 values on every date and in every zone.

    v2_boundaries_utc are AWARE UTC instants, for comparing with
    checkin_events.event_time: the instant the local clock first reads each
    of those values. They never decrease, but they are not always an hour
    apart, because an hour of the local clock is not always an hour long:

        clocks go forward   the skipped hour is zero wide (its two boundaries
                            are the same instant), so no instant falls in it
        clocks go back      the repeated hour is two hours wide and holds
                            BOTH passes through it

    That is the same answer the v1 side gives by construction. A legacy row
    carries only its wall-clock reading: one stamped in a repeated hour does
    not say which pass it belongs to, so both passes share the one hour that
    reading names; and one stamped in a skipped hour -- a reading that should
    not exist -- still falls in the hour it names. The two passes of a
    repeated hour are never told apart, for either table.
    """

    local_date: date
    timezone_name: str
    v1_boundaries_local: tuple[datetime, ...]
    v2_boundaries_utc: tuple[datetime, ...]


def local_hour_boundaries(local_date: date, zone: ZoneInfo) -> LocalHourBoundaries:
    """The wall-clock hour boundaries of `local_date` in `zone`, for both tables."""
    day = local_day_bounds(local_date, zone)

    v1_boundaries_local = tuple(
        day.v1_start_local + timedelta(hours=hour) for hour in range(WALL_CLOCK_HOURS_PER_DAY + 1)
    )

    # fold=0, as for midnight: a reading that happens twice is its FIRST
    # occurrence, so the repeated hour runs from there to the next reading
    # and takes in both passes; a reading that is skipped is the instant the
    # clocks jump, so the skipped hour is empty. Each instant is then held
    # inside the day and never allowed to precede the one before it, so the
    # 24 intervals always tile the day exactly -- no gap, no overlap -- even
    # in a zone whose clocks jump by more than a day's worth of hours.
    v2_boundaries_utc = [day.v2_start_utc]
    for hour in range(1, WALL_CLOCK_HOURS_PER_DAY):
        instant = datetime.combine(local_date, time(hour), tzinfo=zone).astimezone(UTC)
        v2_boundaries_utc.append(min(max(instant, v2_boundaries_utc[-1]), day.v2_end_utc))
    v2_boundaries_utc.append(day.v2_end_utc)

    return LocalHourBoundaries(
        local_date=local_date,
        timezone_name=zone.key,
        v1_boundaries_local=v1_boundaries_local,
        v2_boundaries_utc=tuple(v2_boundaries_utc),
    )


# =====================================================================================================================
# A branch's cutover, in both time domains
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class CutoverBoundary:
    """A v1 -> v2 cutover instant in the two forms the tables need.

    cutover_utc is the AWARE UTC instant:        v2 rows count from it on  (event_time >= cutover_utc).
    cutover_local_naive is the same moment as a NAIVE local wall-clock
    value:                                       v1 rows count before it   (event_time <  cutover_local_naive).

    KNOWN LIMIT OF THE v1 SIDE. A naive local value cannot say which of two
    passes through a repeated hour it belongs to. On a fall-back date the
    local hour before the clocks go back happens twice (in America/Chicago,
    01:00-01:59), and a legacy row stamped in it carries no offset to tell
    the two apart. If -- and only if -- the cutover instant itself falls
    inside that repeated hour, v1 rows stamped within the hour cannot all be
    placed on the correct side: rows from one pass are indistinguishable
    from rows of the other. The rule above is applied regardless, as a plain
    wall-clock comparison. It is deterministic, it is exact for every cutover
    outside that one hour of the year, and nothing is inferred that the data
    does not contain (not from row order, not from neighbouring rows).
    """

    timezone_name: str
    cutover_utc: datetime
    cutover_local_naive: datetime


def cutover_boundary(cutover_at: datetime, zone: ZoneInfo) -> CutoverBoundary:
    """`cutover_at` -- an aware instant -- in both forms. The local form is
    produced by converting the instant INTO the zone and only then dropping
    the tzinfo; it is never the UTC clock reading with its label removed."""
    _require_aware(cutover_at, "cutover_at")

    return CutoverBoundary(
        timezone_name=zone.key,
        cutover_utc=cutover_at.astimezone(UTC),
        cutover_local_naive=cutover_at.astimezone(zone).replace(tzinfo=None),
    )


# =====================================================================================================================
# The effective cutover of a tenant
# =====================================================================================================================

# v2_cutovers is append-only and has NO row level security, so the tenant
# filter here is the only thing scoping this read: both ids are bound from
# the tenant that was resolved for the request, never from request input.
# The effective cutover is the most recent row by set_at -- the same rule the
# ingestion API and the dashboard apply. A latest row whose cutover_at is NULL
# is a recorded rollback to v1-only.
_EFFECTIVE_CUTOVER_SQL = text("""
    SELECT cutover_at
    FROM v2_cutovers
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
    ORDER BY set_at DESC
    LIMIT 1
""")


def get_effective_cutover(conn: Connection, tenant: ResolvedOperationalTenant) -> datetime | None:
    """The tenant's current v1 -> v2 boundary as an aware UTC instant, or None
    if the branch has never been cut over or its latest record is a rollback.
    Either None means the same thing: the branch's history is v1 only."""
    row = conn.execute(
        _EFFECTIVE_CUTOVER_SQL,
        {"customer_id": tenant.operational_customer_id, "branch_id": tenant.operational_branch_id},
    ).first()

    if row is None or row[0] is None:
        return None

    cutover_at = row[0]
    if isinstance(cutover_at, str):  # a driver that hands a TIMESTAMPTZ back as ISO text
        cutover_at = datetime.fromisoformat(cutover_at)

    # A TIMESTAMPTZ is always an instant. A value with no offset would have to
    # be guessed at, and a guessed boundary silently moves rows between eras.
    _require_aware(cutover_at, "v2_cutovers.cutover_at")
    return cutover_at.astimezone(UTC)


# =====================================================================================================================
# Check-in count for one local day
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class CheckinCount:
    """Check-ins on one local calendar day. `total` is the answer; it is
    always v1_count + v2_count. The per-era split is for this service's own
    tests and diagnostics -- it describes the migration, not the library's
    activity, and is not something a customer response needs."""

    total: int
    v1_count: int
    v2_count: int


# Both statements count rows and nothing else: one table each, no join, no
# DISTINCT -- the dashboard's check-in count is likewise a plain row count,
# with duplicates already prevented when rows are stored. Each filters the
# tenant explicitly as well as relying on row level security.
#
# The time bounds are bound with their types stated: a naive local wall-clock
# value for checkins.event_time (TIMESTAMP), an aware instant for
# checkin_events.event_time (TIMESTAMPTZ). Neither statement converts a time
# zone, casts to a date or reads the clock, so the result cannot depend on
# the database session's time zone.
_V1_CHECKIN_COUNT_SQL = text("""
    SELECT COUNT(*)
    FROM checkins
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :start_local
      AND event_time < :end_local
""").bindparams(
    bindparam("start_local", type_=DateTime(timezone=False)),
    bindparam("end_local", type_=DateTime(timezone=False)),
)

_V2_CHECKIN_COUNT_SQL = text("""
    SELECT COUNT(*)
    FROM checkin_events
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :start_utc
      AND event_time < :end_utc
""").bindparams(
    bindparam("start_utc", type_=DateTime(timezone=True)),
    bindparam("end_utc", type_=DateTime(timezone=True)),
)


def get_checkin_count(
    conn: Connection,
    tenant: ResolvedOperationalTenant,
    *,
    local_date: date,
    zone: ZoneInfo,
) -> CheckinCount:
    """How many check-ins the tenant's branch had on `local_date`, a calendar
    day in `zone`.

    With no effective cutover the branch is v1 only: the whole day is counted
    from `checkins`, and `checkin_events` is not read at all, whatever it
    holds. With a cutover, v1 owns the part of the day strictly before it and
    v2 the part at or after it; a part that is empty is not queried.

    `conn` must already carry the tenant's RLS context. At most three
    statements run -- the cutover lookup, then one count per era that owns
    part of the day. If any of them fails the error propagates: a count from
    one era is never returned as if it were the total.
    """
    day = local_day_bounds(local_date, zone)
    cutover_at = get_effective_cutover(conn, tenant)

    v1_end_local = day.v1_end_local
    v2_start_utc = None
    if cutover_at is not None:
        boundary = cutover_boundary(cutover_at, zone)
        # v1: [day start, the earlier of day end and the cutover) on the local clock.
        v1_end_local = min(day.v1_end_local, boundary.cutover_local_naive)
        # v2: [the later of day start and the cutover, day end) as instants.
        v2_start_utc = max(day.v2_start_utc, boundary.cutover_utc)

    tenant_ids = {"customer_id": tenant.operational_customer_id, "branch_id": tenant.operational_branch_id}

    v1_count = 0
    if day.v1_start_local < v1_end_local:
        v1_count = int(conn.execute(
            _V1_CHECKIN_COUNT_SQL,
            {**tenant_ids, "start_local": day.v1_start_local, "end_local": v1_end_local},
        ).scalar_one())

    v2_count = 0
    if v2_start_utc is not None and v2_start_utc < day.v2_end_utc:
        v2_count = int(conn.execute(
            _V2_CHECKIN_COUNT_SQL,
            {**tenant_ids, "start_utc": v2_start_utc, "end_utc": day.v2_end_utc},
        ).scalar_one())

    return CheckinCount(total=v1_count + v2_count, v1_count=v1_count, v2_count=v2_count)


# =====================================================================================================================
# Check-in counts for each wall-clock hour of one local day
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class CheckinHourlyCounts:
    """Check-ins on one local calendar day, by wall-clock hour. Each tuple has
    exactly 24 entries: index H is the hour the local clock reads H:xx.

    `counts` is the answer; entry by entry it is always v1_counts + v2_counts,
    and its sum is the day's CheckinCount.total. The per-era tuples are for
    this service's own tests and diagnostics, as in CheckinCount.
    """

    counts: tuple[int, ...]
    v1_counts: tuple[int, ...]
    v2_counts: tuple[int, ...]


# One statement per era returns all 24 counts as one row: a conditional count
# for each hour, [boundary H, boundary H + 1). The WHERE clause is the same as
# the day count's -- the tenant, then the part of the day the era owns -- so a
# row is counted here exactly when it is counted there, and the hour it lands
# in is decided only by which pair of boundaries it falls between. Every
# boundary is computed in Python and bound with its type stated, as above;
# nothing is grouped, extracted or converted by the database.
#
# The text is assembled from constants in this module only -- the hour
# numbers 0-24 and fixed SQL. Nothing from a caller is ever part of it.
_HOURLY_COUNT_COLUMNS = ",\n".join(
    f"        COUNT(*) FILTER (WHERE event_time >= :boundary_{hour} AND event_time < :boundary_{hour + 1})"
    for hour in range(WALL_CLOCK_HOURS_PER_DAY)
)
_HOUR_BOUNDARY_BINDS = tuple(f"boundary_{index}" for index in range(WALL_CLOCK_HOURS_PER_DAY + 1))

_V1_CHECKIN_HOURLY_COUNT_SQL = text(
    "    SELECT\n"  # nosec B608 - constants only
    + _HOURLY_COUNT_COLUMNS
    + """
    FROM checkins
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :start_local
      AND event_time < :end_local
"""
).bindparams(
    bindparam("start_local", type_=DateTime(timezone=False)),
    bindparam("end_local", type_=DateTime(timezone=False)),
    *(bindparam(name, type_=DateTime(timezone=False)) for name in _HOUR_BOUNDARY_BINDS),
)

_V2_CHECKIN_HOURLY_COUNT_SQL = text(
    "    SELECT\n"  # nosec B608 - constants only
    + _HOURLY_COUNT_COLUMNS
    + """
    FROM checkin_events
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :start_utc
      AND event_time < :end_utc
"""
).bindparams(
    bindparam("start_utc", type_=DateTime(timezone=True)),
    bindparam("end_utc", type_=DateTime(timezone=True)),
    *(bindparam(name, type_=DateTime(timezone=True)) for name in _HOUR_BOUNDARY_BINDS),
)

_NO_HOURLY_COUNTS = (0,) * WALL_CLOCK_HOURS_PER_DAY


def _hourly_counts(row: object) -> tuple[int, ...]:
    counts = tuple(int(value) for value in row)  # type: ignore[attr-defined]
    if len(counts) != WALL_CLOCK_HOURS_PER_DAY:
        raise ValueError("an hourly count statement must return exactly 24 counts")
    return counts


def get_checkin_counts_by_hour(
    conn: Connection,
    tenant: ResolvedOperationalTenant,
    *,
    local_date: date,
    zone: ZoneInfo,
) -> CheckinHourlyCounts:
    """How many check-ins the tenant's branch had in each wall-clock hour of
    `local_date`, a calendar day in `zone`. Always 24 counts, zero where
    nothing happened.

    The eras share the day exactly as in get_checkin_count: with no effective
    cutover the branch is v1 only and `checkin_events` is not read; with one,
    v1 owns the part of the day strictly before it and v2 the part at or
    after it, and a part that is empty is not queried. A cutover inside an
    hour therefore splits that hour too: its count is the v1 rows before the
    cutover plus the v2 rows from it on, with nothing counted twice.

    The hours are local_hour_boundaries': a skipped hour holds no v2 rows
    (but still holds any legacy row stamped in it), and a repeated hour holds
    both of its passes.

    `conn` must already carry the tenant's RLS context. At most three
    statements run -- the cutover lookup, then one per era that owns part of
    the day. If any of them fails the error propagates: the counts of one era
    are never returned as if they were the whole.
    """
    hours = local_hour_boundaries(local_date, zone)
    cutover_at = get_effective_cutover(conn, tenant)

    # Boundaries 0 and 24 are the day's own bounds, so the clamping below is
    # get_checkin_count's, applied to the same interval.
    v1_start_local, v1_end_local = hours.v1_boundaries_local[0], hours.v1_boundaries_local[-1]
    v2_start_utc, v2_end_utc = None, hours.v2_boundaries_utc[-1]
    if cutover_at is not None:
        boundary = cutover_boundary(cutover_at, zone)
        v1_end_local = min(v1_end_local, boundary.cutover_local_naive)
        v2_start_utc = max(hours.v2_boundaries_utc[0], boundary.cutover_utc)

    tenant_ids = {"customer_id": tenant.operational_customer_id, "branch_id": tenant.operational_branch_id}

    v1_counts = _NO_HOURLY_COUNTS
    if v1_start_local < v1_end_local:
        v1_counts = _hourly_counts(conn.execute(
            _V1_CHECKIN_HOURLY_COUNT_SQL,
            {
                **tenant_ids,
                "start_local": v1_start_local,
                "end_local": v1_end_local,
                **dict(zip(_HOUR_BOUNDARY_BINDS, hours.v1_boundaries_local, strict=True)),
            },
        ).one())

    v2_counts = _NO_HOURLY_COUNTS
    if v2_start_utc is not None and v2_start_utc < v2_end_utc:
        v2_counts = _hourly_counts(conn.execute(
            _V2_CHECKIN_HOURLY_COUNT_SQL,
            {
                **tenant_ids,
                "start_utc": v2_start_utc,
                "end_utc": v2_end_utc,
                **dict(zip(_HOUR_BOUNDARY_BINDS, hours.v2_boundaries_utc, strict=True)),
            },
        ).one())

    return CheckinHourlyCounts(
        counts=tuple(v1 + v2 for v1, v2 in zip(v1_counts, v2_counts, strict=True)),
        v1_counts=v1_counts,
        v2_counts=v2_counts,
    )


# =====================================================================================================================
# Reject count for one local day
# =====================================================================================================================
#
# Rejects are kept in two tables that hold time exactly as the check-in
# tables do, and a branch's one cutover divides them at the same instant:
#
#     rejects.event_time          TIMESTAMP    (legacy "v1")   naive local wall clock
#     reject_events.event_time    TIMESTAMPTZ  (Contract "v2") a true instant
#
# So everything above about local days and the cutover applies unchanged.

@dataclass(frozen=True, slots=True)
class RejectCount:
    """Rejects on one local calendar day. `total` is the answer; it is always
    v1_count + v2_count. The per-era split is for this service's own tests
    and diagnostics, as in CheckinCount."""

    total: int
    v1_count: int
    v2_count: int


# Plain row counts, one table each: no join, no DISTINCT, and no filter on
# what kind of reject a row is -- every stored reject counts, whatever its
# reason, as on the dashboard. Each filters the tenant explicitly as well as
# relying on row level security, and binds its time bounds with their types
# stated, as the check-in counts do.
_V1_REJECT_COUNT_SQL = text("""
    SELECT COUNT(*)
    FROM rejects
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :start_local
      AND event_time < :end_local
""").bindparams(
    bindparam("start_local", type_=DateTime(timezone=False)),
    bindparam("end_local", type_=DateTime(timezone=False)),
)

_V2_REJECT_COUNT_SQL = text("""
    SELECT COUNT(*)
    FROM reject_events
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :start_utc
      AND event_time < :end_utc
""").bindparams(
    bindparam("start_utc", type_=DateTime(timezone=True)),
    bindparam("end_utc", type_=DateTime(timezone=True)),
)


def get_reject_count(
    conn: Connection,
    tenant: ResolvedOperationalTenant,
    *,
    local_date: date,
    zone: ZoneInfo,
) -> RejectCount:
    """How many rejects the tenant's branch had on `local_date`, a calendar
    day in `zone`.

    The eras share the day exactly as in get_checkin_count: with no effective
    cutover the branch is v1 only and `reject_events` is not read at all;
    with one, v1 owns the part of the day strictly before it and v2 the part
    at or after it, and a part that is empty is not queried.

    `conn` must already carry the tenant's RLS context. At most three
    statements run -- the cutover lookup, then one count per era that owns
    part of the day. If any of them fails the error propagates: a count from
    one era is never returned as if it were the total.
    """
    day = local_day_bounds(local_date, zone)
    cutover_at = get_effective_cutover(conn, tenant)

    v1_end_local = day.v1_end_local
    v2_start_utc = None
    if cutover_at is not None:
        boundary = cutover_boundary(cutover_at, zone)
        v1_end_local = min(day.v1_end_local, boundary.cutover_local_naive)
        v2_start_utc = max(day.v2_start_utc, boundary.cutover_utc)

    tenant_ids = {"customer_id": tenant.operational_customer_id, "branch_id": tenant.operational_branch_id}

    v1_count = 0
    if day.v1_start_local < v1_end_local:
        v1_count = int(conn.execute(
            _V1_REJECT_COUNT_SQL,
            {**tenant_ids, "start_local": day.v1_start_local, "end_local": v1_end_local},
        ).scalar_one())

    v2_count = 0
    if v2_start_utc is not None and v2_start_utc < day.v2_end_utc:
        v2_count = int(conn.execute(
            _V2_REJECT_COUNT_SQL,
            {**tenant_ids, "start_utc": v2_start_utc, "end_utc": day.v2_end_utc},
        ).scalar_one())

    return RejectCount(total=v1_count + v2_count, v1_count=v1_count, v2_count=v2_count)


# =====================================================================================================================
# Reject counts by reason for one local day
# =====================================================================================================================
#
# The same rows as get_reject_count, sorted by WHY the item was rejected. The
# two tables say that differently (services.reject_reason):
#
#     rejects.error_message        the sorter's own free text -- classified here, in Python
#     reject_events.error_class    already one of the reason codes -- only recognised
#
# The legacy text is raw. It is read only as the key of a group, used to pick
# a reason, and dropped: it is never returned, logged or put in an error.

@dataclass(frozen=True, slots=True)
class RejectReasonCounts:
    """Rejects on one local calendar day, by reason. Each tuple has exactly
    one entry per reason, in the order of services.reject_reason.REJECT_REASONS:
    index N is the count for REJECT_REASONS[N], zero where there were none.

    `counts` is the answer; entry by entry it is always v1_counts + v2_counts,
    and its sum is the day's RejectCount.total. The per-era tuples are for
    this service's own tests and diagnostics, as in RejectCount.

    `unexpected_class_rows` is how many v2 rows carried a stored class that
    is not one of the reason codes. They are counted under `other` -- so they
    are in `counts` and `v2_counts` already -- and never under a name of
    their own.
    """

    counts: tuple[int, ...]
    v1_counts: tuple[int, ...]
    v2_counts: tuple[int, ...]
    unexpected_class_rows: int


# One statement per era: the WHERE clause is the day count's, unchanged -- the
# tenant, then the part of the day the era owns, bound with the same stated
# types -- so a row is counted here exactly when it is counted there. The only
# thing added is the grouping, by the one column that says why. Nothing is
# joined, de-duplicated, ordered, cast or converted by the database, and no
# column that identifies an item or an event is read.
_V1_REJECT_REASON_COUNT_SQL = text("""
    SELECT error_message, COUNT(*)
    FROM rejects
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :start_local
      AND event_time < :end_local
    GROUP BY error_message
""").bindparams(
    bindparam("start_local", type_=DateTime(timezone=False)),
    bindparam("end_local", type_=DateTime(timezone=False)),
)

_V2_REJECT_REASON_COUNT_SQL = text("""
    SELECT error_class, COUNT(*)
    FROM reject_events
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :start_utc
      AND event_time < :end_utc
    GROUP BY error_class
""").bindparams(
    bindparam("start_utc", type_=DateTime(timezone=True)),
    bindparam("end_utc", type_=DateTime(timezone=True)),
)

_REASON_SLOT = {reason: slot for slot, reason in enumerate(REJECT_REASONS)}


def get_reject_counts_by_reason(
    conn: Connection,
    tenant: ResolvedOperationalTenant,
    *,
    local_date: date,
    zone: ZoneInfo,
) -> RejectReasonCounts:
    """How many rejects the tenant's branch had on `local_date`, a calendar
    day in `zone`, for each reason. Always one count per reason, zero where
    there were none.

    The eras share the day exactly as in get_reject_count: with no effective
    cutover the branch is v1 only and `reject_events` is not read at all;
    with one, v1 owns the part of the day strictly before it and v2 the part
    at or after it, and a part that is empty is not queried. Every stored row
    is counted once, under exactly one reason, so the counts add up to
    get_reject_count's total.

    A legacy row's reason is classified from its message. A v2 row's stored
    class is its reason; one that is not a reason code is counted as `other`,
    and a warning carrying only the number of such rows is logged.

    `conn` must already carry the tenant's RLS context. At most three
    statements run -- the cutover lookup, then one per era that owns part of
    the day. If any of them fails the error propagates: the counts of one era
    are never returned as if they were the whole.
    """
    day = local_day_bounds(local_date, zone)
    cutover_at = get_effective_cutover(conn, tenant)

    v1_end_local = day.v1_end_local
    v2_start_utc = None
    if cutover_at is not None:
        boundary = cutover_boundary(cutover_at, zone)
        v1_end_local = min(day.v1_end_local, boundary.cutover_local_naive)
        v2_start_utc = max(day.v2_start_utc, boundary.cutover_utc)

    tenant_ids = {"customer_id": tenant.operational_customer_id, "branch_id": tenant.operational_branch_id}

    v1_counts = [0] * len(REJECT_REASONS)
    if day.v1_start_local < v1_end_local:
        grouped = conn.execute(
            _V1_REJECT_REASON_COUNT_SQL,
            {**tenant_ids, "start_local": day.v1_start_local, "end_local": v1_end_local},
        )
        # One row per distinct message, a NULL message included.
        for error_message, row_count in grouped:
            v1_counts[_REASON_SLOT[classify_legacy_reject_message(error_message)]] += int(row_count)

    v2_counts = [0] * len(REJECT_REASONS)
    unexpected_class_rows = 0
    if v2_start_utc is not None and v2_start_utc < day.v2_end_utc:
        grouped = conn.execute(
            _V2_REJECT_REASON_COUNT_SQL,
            {**tenant_ids, "start_utc": v2_start_utc, "end_utc": day.v2_end_utc},
        )
        for error_class, row_count in grouped:
            reason = reason_for_error_class(error_class)
            if reason is None:
                # The database only holds a stored class to a pattern, so one
                # outside the reason codes can exist. Its rows are still
                # rejects: they stay in the total, as `other`.
                reason = "other"
                unexpected_class_rows += int(row_count)
            v2_counts[_REASON_SLOT[reason]] += int(row_count)

    if unexpected_class_rows:
        # The number only: not the stored value, and nothing that says whose rows they are.
        logger.warning("Reject rows with an unrecognised stored class were counted as other: rows=%d",
                       unexpected_class_rows)

    return RejectReasonCounts(
        counts=tuple(v1 + v2 for v1, v2 in zip(v1_counts, v2_counts, strict=True)),
        v1_counts=tuple(v1_counts),
        v2_counts=tuple(v2_counts),
        unexpected_class_rows=unexpected_class_rows,
    )
