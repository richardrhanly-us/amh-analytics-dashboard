"""Read-only routes over a branch's OPERATIONAL data.

Every route here is nested under one organization and one branch, named by
slug, and follows the same three steps:

    tenant scope      customer_api.tenant_scope resolves the request to the
                      operational tenant the user may read, or answers 404
    scoped connection the same module opens a connection carrying that
                      tenant's row level security context, verified
    read              services.operational_read_service runs the query

A route never touches an engine, never resolves a tenant itself and never
sets tenant context: those belong to the two modules above. It also never
takes an operational identifier from the request -- its only inputs are the
two slugs in its path.

The scoped connection is closed before the response is built.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from customer_api.errors import NO_STORE_HEADERS, CustomerApiRoute
from customer_api.operational_schemas import IngestStatusFields, IngestStatusResponse
from customer_api.tenant_scope import (
    ResolvedOperationalTenant,
    open_customer_tenant_connection,
    require_resolved_tenant,
)
from services.operational_read_service import get_latest_ingest_status

ResolvedTenant = Annotated[ResolvedOperationalTenant, Depends(require_resolved_tenant)]


def create_operational_router() -> APIRouter:
    router = APIRouter(
        prefix="/organizations/{org_slug}/branches/{branch_slug}",
        route_class=CustomerApiRoute,
    )

    @router.get("/ingest-status")
    def get_ingest_status(tenant: ResolvedTenant) -> Response:
        with open_customer_tenant_connection(tenant) as conn:
            status = get_latest_ingest_status(conn, tenant)

        # The connection is closed. A branch with no active Contract v2 key
        # has nothing to report: that is `status: null`, not "not found".
        body = IngestStatusResponse(
            status=None if status is None else IngestStatusFields(
                health_status=status.health_status,
                last_error_class=status.last_error_class,
                pending_outbox_count=status.pending_outbox_count,
                quarantined_count=status.quarantined_count,
                oldest_pending_event_at=status.oldest_pending_event_at,
                last_success_at=status.last_success_at,
                watcher_last_active_at=status.watcher_last_active_at,
                last_heartbeat_at=status.last_heartbeat_at,
                collector_last_run_at=status.collector_last_run_at,
                collector_next_run_at=status.collector_next_run_at,
                collector_run_duration_ms=status.collector_run_duration_ms,
                collector_schedule_status=status.collector_schedule_status,
            )
        )
        return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)

    return router
