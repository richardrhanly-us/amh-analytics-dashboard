"""A branch's pipeline status for the customer API: what its collector last
reported, and when the server received that report.

    get_pipeline_status(conn, tenant, now=...) -> PipelineStatus(state, last_reported_at)

The answer is the same shape whichever of two places a branch's status is
stored in, and says nothing about which one it came from:

    pipeline_status    (legacy)    one row per branch; no row level security
    ingest_key_ids     (current)   one row per ingest key; under row level security

WHICH SOURCE. A branch's one cutover decides, by the same record and the same
rule as the metrics in services.operational_metrics_service: the most recent
v2_cutovers row by set_at. No row, a rollback (cutover_at NULL) or a cutover
that is still in the future means the legacy row; a cutover at or before
`now` means the current one. Nothing else is consulted -- in particular an
ingest key that has been issued for a branch does not make it current. Exactly
one source is then read: the other is never queried "just in case".

`now` IS GIVEN, NEVER READ. Deciding whether a cutover has happened yet needs
the current instant. The caller passes it, as an aware datetime; nothing here
reads a clock, and no statement asks the database for the time.

THE LEGACY ROW HOLDS TWO SIGNALS -- a scheduled run's `status` and a
continuous agent's `health_status` -- written by different programs. The
server stamps each when it accepts a report carrying it (status_reported_at,
health_status_reported_at), and the LATER stamp says which signal is the
branch's last report. If the two are the same instant the health signal
stands: one request carried both, and health_status is the closed, structured
one. A signal with no stamp was never reported through a server that stamps,
so it is not used at all: its naive timestamps and `updated_at` are not read.

THE LEGACY TABLE IS NOT UNDER ROW LEVEL SECURITY. Its statement's own filter
on customer_id AND branch_id -- both bound from the tenant that was resolved
for the request, never from request input -- is the only thing scoping that
read. The current table is under RLS and is filtered explicitly as well.

A STATE WITHOUT A TIME IS NEVER RETURNED. Where no report has been stamped
the answer is `unknown` and no time, even if the row holds a status: an "ok"
of unknown age would be worse than no answer. And a state says what was last
REPORTED: how long ago that was is the caller's to show, not judged here.

EVERY TIME RETURNED IS AN AWARE UTC INSTANT. The stored columns are
TIMESTAMPTZ. A value that comes back with no offset would have to be guessed
at, so it is refused (ValueError), never labelled UTC.

Framework-neutral: no Streamlit, no FastAPI, no pandas, no caching, no engine.
Every read takes a connection the caller supplies -- already scoped to the
tenant -- and database errors propagate: a failed read is never an `unknown`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from services.operational_metrics_service import get_effective_cutover
from services.pipeline_state import (
    PipelineState,
    state_for_current_report,
    state_for_legacy_health,
    state_for_run_status,
)
from services.tenant_resolution_service import ResolvedOperationalTenant


@dataclass(frozen=True, slots=True)
class PipelineStatus:
    """What a branch's collector last reported, and when the server received
    it. `last_reported_at` is an aware UTC instant, or None when nothing has
    been reported -- in which case `state` is always "unknown". It carries no
    identifier and nothing that says where the answer was read from."""

    state: PipelineState
    last_reported_at: datetime | None


_NOTHING_REPORTED = PipelineStatus(state="unknown", last_reported_at=None)


def _as_utc_instant(value: object, what: str) -> datetime | None:
    """`value` -- a TIMESTAMPTZ as the driver returned it -- as an aware UTC
    datetime, or None for NULL. The instant is preserved; only the offset it
    is expressed in changes."""
    if value is None:
        return None
    if isinstance(value, str):  # a driver that hands a TIMESTAMPTZ back as ISO text
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError(f"{what} is not a timestamp")
    # A TIMESTAMPTZ is always an instant. A value with no offset would have to
    # be guessed at, and a guessed offset silently moves the report in time.
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return value.astimezone(UTC)


# =====================================================================================================================
# Legacy: pipeline_status
# =====================================================================================================================

# The two signals and their two server stamps, and nothing else: not the error
# text, not a counter, not the destination breakdown, not updated_at or either
# naive run timestamp.
#
# NO ROW LEVEL SECURITY PROTECTS THIS TABLE: the two tenant predicates below
# are the whole of its isolation. LIMIT 2, not 1: the primary key makes a
# second row for a branch impossible, so a second row would mean that
# guarantee is gone -- and that must be refused, not answered from whichever
# row happened to come first.
_LEGACY_PIPELINE_STATUS_SQL = text("""
    SELECT
        status,
        health_status,
        status_reported_at,
        health_status_reported_at
    FROM pipeline_status
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
    LIMIT 2
""")


def _legacy_pipeline_status(conn: Connection, tenant: ResolvedOperationalTenant) -> PipelineStatus:
    rows = conn.execute(
        _LEGACY_PIPELINE_STATUS_SQL,
        {"customer_id": tenant.operational_customer_id, "branch_id": tenant.operational_branch_id},
    ).mappings().all()

    if not rows:
        return _NOTHING_REPORTED
    if len(rows) != 1:
        raise RuntimeError("More than one pipeline status row was found for one branch.")
    row = rows[0]

    status_reported_at = _as_utc_instant(row["status_reported_at"], "pipeline_status.status_reported_at")
    health_reported_at = _as_utc_instant(row["health_status_reported_at"], "pipeline_status.health_status_reported_at")

    if status_reported_at is None and health_reported_at is None:
        return _NOTHING_REPORTED

    # The later stamp is the branch's last report. On an exact tie the health
    # signal stands (>=): see the module docstring.
    health_is_latest = health_reported_at is not None and (
        status_reported_at is None or health_reported_at >= status_reported_at
    )
    if health_is_latest:
        return PipelineStatus(state=state_for_legacy_health(row["health_status"]), last_reported_at=health_reported_at)
    return PipelineStatus(state=state_for_run_status(row["status"]), last_reported_at=status_reported_at)


# =====================================================================================================================
# Current: ingest_key_ids
# =====================================================================================================================

# The same row operational_read_service.get_latest_ingest_status reads -- the
# tenant's ACTIVE keys, the one that reported most recently first, one that
# has never reported last -- but only the three columns this answer is made
# from. key_id, the row id, the tenant ids, the error class and every counter
# and schedule time are never selected.
_CURRENT_PIPELINE_STATUS_SQL = text("""
    SELECT
        health_status,
        collector_schedule_status,
        last_heartbeat_at
    FROM ingest_key_ids
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND status = 'active'
    ORDER BY last_heartbeat_at DESC NULLS LAST
    LIMIT 1
""")


def _current_pipeline_status(conn: Connection, tenant: ResolvedOperationalTenant) -> PipelineStatus:
    row = conn.execute(
        _CURRENT_PIPELINE_STATUS_SQL,
        {"customer_id": tenant.operational_customer_id, "branch_id": tenant.operational_branch_id},
    ).mappings().first()

    if row is None:
        return _NOTHING_REPORTED

    last_heartbeat_at = _as_utc_instant(row["last_heartbeat_at"], "ingest_key_ids.last_heartbeat_at")
    if last_heartbeat_at is None:
        return _NOTHING_REPORTED  # a key that has never reported

    return PipelineStatus(
        state=state_for_current_report(row["health_status"], row["collector_schedule_status"]),
        last_reported_at=last_heartbeat_at,
    )


# =====================================================================================================================
# The status of a tenant
# =====================================================================================================================

def get_pipeline_status(
    conn: Connection,
    tenant: ResolvedOperationalTenant,
    *,
    now: datetime,
) -> PipelineStatus:
    """What the tenant's branch last reported about its pipeline, as of `now`.

    `now` is the current instant, as an AWARE datetime; a naive one is
    refused (ValueError) before anything is read. It is used for one thing:
    whether the branch's cutover has happened yet.

    `conn` must already carry the tenant's RLS context. Exactly two statements
    run -- the cutover lookup, then the one source the branch's status is in.
    If either fails the error propagates: `unknown` always means "nothing has
    been reported", never "the read failed".
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    cutover_at = get_effective_cutover(conn, tenant)

    # At the cutover instant itself the branch is already current: the same
    # boundary the metrics use (v2 owns everything at or after the cutover).
    if cutover_at is not None and cutover_at <= now:
        return _current_pipeline_status(conn, tenant)
    return _legacy_pipeline_status(conn, tenant)
