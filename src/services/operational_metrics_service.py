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

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.engine import Connection

from services.tenant_resolution_service import ResolvedOperationalTenant


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
