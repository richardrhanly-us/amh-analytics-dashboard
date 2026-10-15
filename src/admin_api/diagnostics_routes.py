"""A sorter site's diagnostics in the administration API (A2 item 3): read-only.

    GET /api/admin/libraries/{org_slug}/sorters/{sorter_slug}/diagnostics

Included in the admin router (admin_api.router), so it inherits its platform-administrator and Origin guards. This is
the only place the deep collector facts are served: the customer API's former ingest-status route is gone.

WHICH SITE. `sorter_slug` is one of the library's sorter sites as services.sorter_inventory_service lists them (the
host branch's slug, as in the library overview). An unknown library or a slug that is not one of its sites is the same
404 sorter_not_found, so neither can be probed separately.

WHAT IS READ, on ONE connection scoped by row level security to exactly that site's operational tenant
(customer_api.tenant_scope.open_customer_tenant_connection, which verifies the scope), resolved without membership by
services.admin_library_service.site_tenant:

    the pipeline state     services.pipeline_status_service.get_pipeline_status -- the same state the overview shows
    the era                services.operational_metrics_service.get_effective_cutover
    v2 (at/after cutover)  services.operational_read_service.get_latest_ingest_status
    legacy (before it)     services.admin_diagnostics_service.get_legacy_diagnostics

Only the current era is read and returned. A site with no operational identity reads nothing: contract `unknown`.
No fact is judged: no threshold, no staleness, no advice. A database failure is the generic 500.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter
from starlette.responses import JSONResponse, Response

from admin_api.diagnostics_schemas import (
    DiagnosticsSorter,
    LegacyReport,
    LegacySection,
    LibraryRef,
    SorterDiagnosticsResponse,
    V2Heartbeat,
    V2Section,
)
from admin_api.library_schemas import HostBranch, PipelineSummary
from customer_api import tenant_scope
from customer_api.errors import NO_STORE_HEADERS, CustomerApiError, CustomerApiRoute
from customer_api.settings import product_timezone
from services import (
    admin_diagnostics_service,
    admin_library_service,
    operational_metrics_service,
    operational_read_service,
    pipeline_status_service,
    sorter_inventory_service,
)


def _sorter_not_found() -> CustomerApiError:
    return CustomerApiError(404, "sorter_not_found", "Sorter not found.")


def _v2_section(conn, tenant) -> V2Section:
    status = operational_read_service.get_latest_ingest_status(conn, tenant)
    if status is None:
        return V2Section(status=None)
    return V2Section(status=V2Heartbeat(
        last_heartbeat_at=status.last_heartbeat_at,
        health_status=status.health_status,
        last_error_class=status.last_error_class,
        collector_schedule_status=status.collector_schedule_status,
        collector_last_run_at=status.collector_last_run_at,
        collector_next_run_at=status.collector_next_run_at,
        collector_run_duration_ms=status.collector_run_duration_ms,
        pending_outbox_count=status.pending_outbox_count,
        quarantined_count=status.quarantined_count,
        oldest_pending_event_at=status.oldest_pending_event_at,
        last_success_at=status.last_success_at,
        watcher_last_active_at=status.watcher_last_active_at,
    ))


def _legacy_section(conn, tenant) -> LegacySection:
    report = admin_diagnostics_service.get_legacy_diagnostics(conn, tenant)
    if report is None:
        return LegacySection(status=None)
    return LegacySection(status=LegacyReport(
        run_status=report.run_status,
        run_reported_at=report.run_reported_at,
        health_status=report.health_status,
        health_reported_at=report.health_reported_at,
        pending_outbox_count=report.pending_outbox_count,
        quarantined_count=report.quarantined_count,
        checkins_rows=report.checkins_rows,
        rejects_rows=report.rejects_rows,
        uploaded_checkins_rows=report.uploaded_checkins_rows,
        uploaded_rejects_rows=report.uploaded_rejects_rows,
        checkins_bad_datetime_rows=report.checkins_bad_datetime_rows,
        rejects_bad_datetime_rows=report.rejects_bad_datetime_rows,
    ))


def create_diagnostics_router() -> APIRouter:
    router = APIRouter(prefix="/libraries", route_class=CustomerApiRoute)

    @router.get("/{org_slug}/sorters/{sorter_slug}/diagnostics")
    def get_sorter_diagnostics(org_slug: str, sorter_slug: str) -> Response:
        library = admin_library_service.get_library_row(org_slug)
        if library is None:
            raise _sorter_not_found()
        site = next((s for s in sorter_inventory_service.list_sorter_sites(org_slug) if s.slug == sorter_slug), None)
        if site is None:
            raise _sorter_not_found()

        sorter = DiagnosticsSorter(
            slug=site.slug,
            name=site.name,
            host_branch=HostBranch(slug=site.host_branch_slug, name=site.host_branch_name),
            status=site.status,
            collector_count=site.collector_count,
            onboarding_status=site.onboarding_status,
        )
        pipeline = PipelineSummary(state="unknown", last_reported_at=None)
        contract: Literal["v2", "legacy", "unknown"] = "unknown"
        cutover_at: datetime | None = None
        v2: V2Section | None = None
        legacy: LegacySection | None = None

        tenant = admin_library_service.site_tenant(org_slug, site.host_branch_slug)
        if tenant is not None:
            now = datetime.now(UTC)
            with tenant_scope.open_customer_tenant_connection(tenant) as conn:
                status = pipeline_status_service.get_pipeline_status(conn, tenant, now=now)
                pipeline = PipelineSummary(state=status.state, last_reported_at=status.last_reported_at)
                cutover_at = operational_metrics_service.get_effective_cutover(conn, tenant)
                # The same boundary the state uses: at the cutover instant itself the site is already current.
                if cutover_at is not None and cutover_at <= now:
                    contract = "v2"
                    v2 = _v2_section(conn, tenant)
                else:
                    contract = "legacy"
                    legacy = _legacy_section(conn, tenant)

        body = SorterDiagnosticsResponse(
            timezone=product_timezone().key,
            library=LibraryRef(slug=library.slug, name=library.name),
            sorter=sorter,
            pipeline=pipeline,
            contract=contract,
            cutover_at=cutover_at,
            v2=v2,
            legacy=legacy,
        )
        return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)

    return router
