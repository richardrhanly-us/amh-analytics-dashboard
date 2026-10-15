"""A sorter site's LEGACY pipeline report, for the administration API's diagnostics (A2 item 3).

    get_legacy_diagnostics(conn, tenant) -> LegacyDiagnostics | None

The Contract v2 side of the same page needs nothing new: it is services.operational_read_service.get_latest_ingest_status,
unchanged. This module reads the one legacy row (pipeline_status) a site still reports through until it is cut over.

WHAT IS READ is a fixed list of columns and nothing else:

    run_status, run_reported_at          the scheduled run's status, and when the server received it
    health_status, health_reported_at    the continuous agent's health, and when the server received it
    pending_outbox_count, quarantined_count
    checkins_rows, rejects_rows, uploaded_checkins_rows, uploaded_rejects_rows,
    checkins_bad_datetime_rows, rejects_bad_datetime_rows      the last run's own counters

NEVER READ: the free-text `last_error` and `last_failure_category`; `destination_breakdown`, `transit_items` and
`problem_items` (one library's own destinations, D5); and every NAIVE timestamp -- last_run, last_attempt, updated_at,
watcher_last_active_at, oldest_pending_event_at, last_success_at. A naive time is a wall-clock reading in a zone the row
does not say, so it is not shown, exactly as services.pipeline_status_service never reads it either. The two times
returned are the server's own TIMESTAMPTZ stamps, as aware UTC instants.

NO ROW LEVEL SECURITY protects pipeline_status: the two tenant predicates below are its whole isolation, bound from a
tenant the caller resolved. LIMIT 2: the primary key makes a second row impossible, so one is refused, not chosen from.

Framework-neutral: no Streamlit, no FastAPI. The caller owns the connection. Database errors propagate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from services.pipeline_status_service import _as_utc_instant
from services.tenant_resolution_service import ResolvedOperationalTenant


@dataclass(frozen=True, slots=True)
class LegacyDiagnostics:
    """A site's legacy pipeline_status row: see the module docstring. No identifier of any kind."""

    run_status: str | None
    run_reported_at: datetime | None
    health_status: str | None
    health_reported_at: datetime | None
    pending_outbox_count: int | None
    quarantined_count: int | None
    checkins_rows: int | None
    rejects_rows: int | None
    uploaded_checkins_rows: int | None
    uploaded_rejects_rows: int | None
    checkins_bad_datetime_rows: int | None
    rejects_bad_datetime_rows: int | None


_LEGACY_DIAGNOSTICS_SQL = text("""
    SELECT
        status,
        status_reported_at,
        health_status,
        health_status_reported_at,
        pending_outbox_count,
        quarantined_count,
        checkins_rows,
        rejects_rows,
        uploaded_checkins_rows,
        uploaded_rejects_rows,
        checkins_bad_datetime_rows,
        rejects_bad_datetime_rows
    FROM pipeline_status
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
    LIMIT 2
""")


def get_legacy_diagnostics(conn: Connection, tenant: ResolvedOperationalTenant) -> LegacyDiagnostics | None:
    """The tenant's legacy pipeline_status row, or None if it has none."""
    rows = conn.execute(
        _LEGACY_DIAGNOSTICS_SQL,
        {"customer_id": tenant.operational_customer_id, "branch_id": tenant.operational_branch_id},
    ).mappings().all()
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeError("More than one pipeline status row was found for one branch.")
    row = rows[0]
    return LegacyDiagnostics(
        run_status=row["status"],
        run_reported_at=_as_utc_instant(row["status_reported_at"], "pipeline_status.status_reported_at"),
        health_status=row["health_status"],
        health_reported_at=_as_utc_instant(row["health_status_reported_at"], "pipeline_status.health_status_reported_at"),
        pending_outbox_count=row["pending_outbox_count"],
        quarantined_count=row["quarantined_count"],
        checkins_rows=row["checkins_rows"],
        rejects_rows=row["rejects_rows"],
        uploaded_checkins_rows=row["uploaded_checkins_rows"],
        uploaded_rejects_rows=row["uploaded_rejects_rows"],
        checkins_bad_datetime_rows=row["checkins_bad_datetime_rows"],
        rejects_bad_datetime_rows=row["rejects_bad_datetime_rows"],
    )
