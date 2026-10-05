"""Customer-facing reads of OPERATIONAL data.

The read side of the customer API: plain queries over the operational tables,
run on a connection the caller has already opened and scoped to one tenant.
Deliberately separate from the collector's ingestion code
(services/ingest_v2_service.py) and from the Streamlit dashboard's loaders
(data_loader.py) -- this module imports neither.

EVERY QUERY HERE IS TENANT-SCOPED TWICE. Row level security on the
operational tables is the authoritative boundary: the caller's connection
carries the tenant context (tenant_db) and PostgreSQL returns no other
tenant's rows whatever the statement says. Each statement ALSO filters
customer_id and branch_id explicitly, with the ids of the tenant that was
resolved for the request (services/tenant_resolution_service.py). The
explicit filter is what makes the statement correct on its own, lets it use
the tenant-leading index, and keeps a mistake in either layer from being the
only thing between two tenants.

THE CALLER OWNS THE CONNECTION. Nothing here creates an engine, opens a
connection, sets tenant context, commits or caches. Database errors
propagate: a failed read is never reported as "no data".

Framework-neutral: no Streamlit, no FastAPI, no pandas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from services.tenant_resolution_service import ResolvedOperationalTenant


@dataclass(frozen=True, slots=True)
class IngestStatus:
    """The latest Contract v2 collector heartbeat for a branch: operational
    health only. It carries no identifier of any kind -- not the ingest key,
    not the row, not the tenant."""

    health_status: str | None
    last_error_class: str | None
    pending_outbox_count: int | None
    quarantined_count: int | None
    oldest_pending_event_at: datetime | None
    last_success_at: datetime | None
    watcher_last_active_at: datetime | None
    last_heartbeat_at: datetime | None
    collector_last_run_at: datetime | None
    collector_next_run_at: datetime | None
    collector_run_duration_ms: int | None
    collector_schedule_status: str | None


# Only the columns IngestStatus has: key_id, the row id, the tenant ids and
# the algorithm are never selected. A branch has at most a handful of keys and
# one active at a time; if more than one is active, the one that reported most
# recently wins, and a key that has never reported sorts last.
_LATEST_INGEST_STATUS_SQL = text("""
    SELECT
        health_status,
        last_error_class,
        pending_outbox_count,
        quarantined_count,
        oldest_pending_event_at,
        last_success_at,
        watcher_last_active_at,
        last_heartbeat_at,
        collector_last_run_at,
        collector_next_run_at,
        collector_run_duration_ms,
        collector_schedule_status
    FROM ingest_key_ids
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND status = 'active'
    ORDER BY last_heartbeat_at DESC NULLS LAST
    LIMIT 1
""")


def get_latest_ingest_status(conn: Connection, tenant: ResolvedOperationalTenant) -> IngestStatus | None:
    """The tenant's most recent Contract v2 heartbeat snapshot, or None if the
    branch has no active ingest key (it is not on Contract v2, or its key was
    retired). `conn` must already carry `tenant`'s RLS context."""
    row = conn.execute(
        _LATEST_INGEST_STATUS_SQL,
        {"customer_id": tenant.operational_customer_id, "branch_id": tenant.operational_branch_id},
    ).mappings().first()

    if row is None:
        return None

    return IngestStatus(
        health_status=row["health_status"],
        last_error_class=row["last_error_class"],
        pending_outbox_count=row["pending_outbox_count"],
        quarantined_count=row["quarantined_count"],
        oldest_pending_event_at=row["oldest_pending_event_at"],
        last_success_at=row["last_success_at"],
        watcher_last_active_at=row["watcher_last_active_at"],
        last_heartbeat_at=row["last_heartbeat_at"],
        collector_last_run_at=row["collector_last_run_at"],
        collector_next_run_at=row["collector_next_run_at"],
        collector_run_duration_ms=row["collector_run_duration_ms"],
        collector_schedule_status=row["collector_schedule_status"],
    )
