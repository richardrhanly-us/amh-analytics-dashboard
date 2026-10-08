"""A sorter site's holds report over a range.

    GET /organizations/{org_slug}/branches/{branch_slug}/reports/holds?from=YYYY-MM-DD&to=YYYY-MM-DD

It sits beside the sorter's other range reports (customer_api.report_routes) and is put together from pieces that
already exist, each used as it is:

    the sorter's scope    customer_api.tenant_scope (require_resolved_tenant, open_customer_tenant_connection)
    the range             customer_api.report_routes.require_report_range
    the eras              services.operational_report_service.report_window
    the counts            services.hold_report_service.get_holds_report

WHO. Every member of the organization, whatever their role, as for the other sorter reports -- WHEN the
organization's plan includes the `internal_workflow` feature, the one the dashboard shows its holds under. Its answers,
in the order they are decided:

    no session                                           401 not_authenticated
    no such organization or site, or not visible to you  404 tenant_not_found   (the other reports' own answer)
    the plan does not include the holds report           403 feature_not_available
    a range that cannot be reported on                   422                    (the other reports' own answer)

A suspended organization's report can still be read, like its other reports. The feature is checked here, by the
server, whatever a page chooses to show; no other report is affected by it.

Read-only: nothing is written. A route runs no SQL of its own, counts nothing itself and takes no identifier but the
two slugs.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from customer_api.auth_dependencies import require_current_user
from customer_api.errors import NO_STORE_HEADERS, CustomerApiError, CustomerApiRoute
from customer_api.holds_report_schemas import HoldsReportResponse
from customer_api.operational_routes import ResolvedTenant
from customer_api.report_routes import Range
from customer_api.tenant_scope import open_customer_tenant_connection
from services import entitlement_service
from services.hold_report_service import get_holds_report
from services.operational_report_service import report_window

# The plan feature the holds report is part of. The dashboard shows its holds under the same one
# (services.permission_service.can_view_internal_workflow); no new feature is defined for it.
HOLDS_FEATURE = "internal_workflow"


def require_holds_feature(org_slug: str, user: Annotated[dict[str, Any], Depends(require_current_user)]) -> None:
    """The organization's plan includes the holds report, or 403. Read for the organization in the path, by the
    same uncached lookup the organization detail is built from. A database failure propagates as a server error."""
    context = entitlement_service.build_entitlement_context(user["id"], org_slug)
    if not entitlement_service.feature_enabled(context, HOLDS_FEATURE):
        raise CustomerApiError(403, "feature_not_available", "This report is not available for this organization.")


# Declared after the tenant and before the range in the route below: 401, then 404, then 403, then 422.
HoldsFeature = Annotated[None, Depends(require_holds_feature)]


def create_holds_report_router() -> APIRouter:
    router = APIRouter(
        prefix="/organizations/{org_slug}/branches/{branch_slug}/reports",
        route_class=CustomerApiRoute,
    )

    @router.get("/holds")
    def get_site_holds_report(tenant: ResolvedTenant, _feature: HoldsFeature, requested: Range) -> Response:
        with open_customer_tenant_connection(tenant) as conn:
            report = get_holds_report(conn, tenant, report_window(conn, tenant, requested.local_range))

        # The connection is closed. Two counts: nothing that was read to work them out.
        body = HoldsReportResponse(
            range=requested.response(),
            public_hold_count=report.public_hold_count,
            ill_hold_count=report.ill_hold_count,
        )
        return JSONResponse(content=body.model_dump(mode="json", by_alias=True), headers=NO_STORE_HEADERS)

    return router
