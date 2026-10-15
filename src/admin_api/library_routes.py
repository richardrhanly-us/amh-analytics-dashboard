"""The administration API's libraries (A2 item 2): read-only.

    GET /api/admin/libraries              every library, with its primary sorter site's pipeline state
    GET /api/admin/libraries/{org_slug}   one library, with every sorter site's pipeline state

Included in the admin router (admin_api.router), so both inherit its platform-administrator and Origin guards.

PIPELINE STATE is the customer app's own: services.pipeline_status_service.get_pipeline_status, which reads a site's
Contract v2 heartbeat once it is cut over and its legacy report before -- never the frozen legacy row of a site that
has moved to v2. It is read on a connection scoped by row level security to exactly that site's operational tenant
(customer_api.tenant_scope.open_customer_tenant_connection, which verifies the scope), resolved without membership by
services.admin_library_service.site_tenant. A site with no operational identity is not read: `unknown`, no time.

A LIBRARY'S STATE in the list is its PRIMARY sorter site's -- the site hosted at its primary branch -- and nothing
else. Several sites' states are never merged into one (services.pipeline_state.worse_state is explicitly not for
that); the overview lists every site with its own. Nothing is judged by age.

A library that does not exist is 404 library_not_found. A database failure is the generic 500.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from starlette.responses import JSONResponse, Response

from admin_api.library_schemas import (
    BranchSummary,
    HostBranch,
    LibraryDetailResponse,
    LibraryListResponse,
    LibrarySummary,
    PipelineSummary,
    PrimarySorter,
    SorterSiteSummary,
)
from customer_api import tenant_scope
from customer_api.errors import NO_STORE_HEADERS, CustomerApiError, CustomerApiRoute
from customer_api.organization_schemas import SubscriptionSummary
from customer_api.settings import product_timezone
from services import (
    admin_library_service,
    pipeline_status_service,
    sorter_inventory_service,
)
from services.admin_library_service import LibraryRow
from services.sorter_inventory_service import SorterSite

_NOTHING_REPORTED = PipelineSummary(state="unknown", last_reported_at=None)


def _site_pipeline(org_slug: str, branch_slug: str, now: datetime) -> PipelineSummary:
    tenant = admin_library_service.site_tenant(org_slug, branch_slug)
    if tenant is None:
        return _NOTHING_REPORTED
    with tenant_scope.open_customer_tenant_connection(tenant) as conn:
        status = pipeline_status_service.get_pipeline_status(conn, tenant, now=now)
    return PipelineSummary(state=status.state, last_reported_at=status.last_reported_at)


def _primary_branch(row: LibraryRow) -> BranchSummary | None:
    if row.primary_branch_slug is None:
        return None
    return BranchSummary(
        slug=row.primary_branch_slug,
        name=row.primary_branch_name or "",
        status=row.primary_branch_status or "",
    )


def _subscription(row: LibraryRow) -> SubscriptionSummary | None:
    if row.plan_code is None or row.plan_name is None or row.subscription_status is None:
        return None
    return SubscriptionSummary(plan_code=row.plan_code, plan_name=row.plan_name, status=row.subscription_status)


def _sorter_site(org_slug: str, site: SorterSite, now: datetime) -> SorterSiteSummary:
    return SorterSiteSummary(
        slug=site.slug,
        name=site.name,
        host_branch=HostBranch(slug=site.host_branch_slug, name=site.host_branch_name),
        status=site.status,
        collector_count=site.collector_count,
        onboarding_status=site.onboarding_status,
        pipeline=_site_pipeline(org_slug, site.host_branch_slug, now),
    )


def _summary(row: LibraryRow, now: datetime) -> LibrarySummary:
    sites = sorter_inventory_service.list_sorter_sites(row.slug)
    primary = next((site for site in sites if site.host_branch_slug == row.primary_branch_slug), None)
    return LibrarySummary(
        slug=row.slug,
        name=row.name,
        status=row.status,
        primary_branch=_primary_branch(row),
        subscription=_subscription(row),
        sorter_count=len(sites),
        primary_sorter=None if primary is None else PrimarySorter(
            onboarding_status=primary.onboarding_status,
            pipeline=_site_pipeline(row.slug, primary.host_branch_slug, now),
        ),
    )


def _library_not_found() -> CustomerApiError:
    return CustomerApiError(404, "library_not_found", "Library not found.")


def create_library_router() -> APIRouter:
    router = APIRouter(prefix="/libraries", route_class=CustomerApiRoute)

    @router.get("")
    def list_libraries() -> Response:
        now = datetime.now(UTC)
        body = LibraryListResponse(
            timezone=product_timezone().key,
            libraries=[_summary(row, now) for row in admin_library_service.list_library_rows()],
        )
        return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)

    @router.get("/{org_slug}")
    def get_library(org_slug: str) -> Response:
        row = admin_library_service.get_library_row(org_slug)
        if row is None:
            raise _library_not_found()
        now = datetime.now(UTC)
        body = LibraryDetailResponse(
            timezone=product_timezone().key,
            slug=row.slug,
            name=row.name,
            status=row.status,
            primary_branch=_primary_branch(row),
            operational_identity=row.operational_identity,
            subscription=_subscription(row),
            sorters=[_sorter_site(row.slug, site, now) for site in sorter_inventory_service.list_sorter_sites(row.slug)],
        )
        return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)

    return router
