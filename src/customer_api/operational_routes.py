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
takes an operational identifier from the request: the tenant comes only from
the two slugs in its path, and any query parameter describes WHAT is asked
(a date), never whose data it is.

The scoped connection is closed before the response is built.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BeforeValidator
from starlette.responses import JSONResponse, Response

from customer_api import settings
from customer_api.errors import NO_STORE_HEADERS, CustomerApiRoute
from customer_api.operational_schemas import (
    CheckinCountResponse,
    CheckinHourCount,
    CheckinsByHourResponse,
    IngestStatusFields,
    IngestStatusResponse,
    RejectCountResponse,
)
from customer_api.tenant_scope import (
    ResolvedOperationalTenant,
    open_customer_tenant_connection,
    require_resolved_tenant,
)
from services.operational_metrics_service import (
    get_checkin_count,
    get_checkin_counts_by_hour,
    get_reject_count,
)
from services.operational_read_service import get_latest_ingest_status

ResolvedTenant = Annotated[ResolvedOperationalTenant, Depends(require_resolved_tenant)]

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _calendar_date(value: object) -> date:
    """Accepts exactly YYYY-MM-DD and nothing else. The default date parsing
    would also take a timestamp whose time part is midnight; a route that
    asks for a calendar day must not accept a timestamp in any form."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise ValueError("must be a calendar date in the form YYYY-MM-DD")
    return date.fromisoformat(value)


# Required: a route never decides for the caller which day "today" is.
LocalDate = Annotated[date, BeforeValidator(_calendar_date), Query(alias="date")]


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

    @router.get("/checkins/count")
    def get_checkins_count(tenant: ResolvedTenant, local_date: LocalDate) -> Response:
        # A calendar day in the product's configured zone. A zone that is set
        # but invalid raises here, before any connection is opened.
        zone = settings.product_timezone()

        with open_customer_tenant_connection(tenant) as conn:
            count = get_checkin_count(conn, tenant, local_date=local_date, zone=zone)

        # The connection is closed. Only the total is returned: which of the
        # branch's two data eras each check-in came from is not the caller's concern.
        body = CheckinCountResponse(date=local_date, timezone=zone.key, checkin_count=count.total)
        return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)

    @router.get("/checkins/by-hour")
    def get_checkins_by_hour(tenant: ResolvedTenant, local_date: LocalDate) -> Response:
        # The same day, in the same zone, as /checkins/count.
        zone = settings.product_timezone()

        with open_customer_tenant_connection(tenant) as conn:
            hourly = get_checkin_counts_by_hour(conn, tenant, local_date=local_date, zone=zone)

        # The connection is closed. Every wall-clock hour is returned, 0 to 23,
        # with its total only: which hours a library is open, and which era a
        # check-in came from, are not decided or disclosed here.
        body = CheckinsByHourResponse(
            date=local_date,
            timezone=zone.key,
            hours=[CheckinHourCount(hour=hour, checkin_count=count) for hour, count in enumerate(hourly.counts)],
        )
        return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)

    @router.get("/rejects/count")
    def get_rejects_count(tenant: ResolvedTenant, local_date: LocalDate) -> Response:
        # A calendar day in the product's configured zone, exactly as for /checkins/count.
        zone = settings.product_timezone()

        with open_customer_tenant_connection(tenant) as conn:
            count = get_reject_count(conn, tenant, local_date=local_date, zone=zone)

        # The connection is closed. Only the total is returned: not which era
        # a reject came from, and nothing about why an item was rejected.
        body = RejectCountResponse(date=local_date, timezone=zone.key, reject_count=count.total)
        return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)

    return router
