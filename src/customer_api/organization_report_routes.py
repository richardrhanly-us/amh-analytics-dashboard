"""Read-only range reports over a whole organization.

    GET /organizations/{org_slug}/reports/overview?from=YYYY-MM-DD&to=YYYY-MM-DD
    GET /organizations/{org_slug}/reports/routing-network?from=...&to=...
    GET /organizations/{org_slug}/reports/reliability?from=...&to=...

WHO MAY READ ONE is who may see the organization: a member of an organization
that is not cancelled (customer_api.organization_routes
.require_organization_member, the organization detail's own rule). Anyone
else gets the same organization_not_found 404, so a slug cannot be probed. A
suspended organization stays readable.

THE RANGE is the sorter-site reports' range, by the same dependency
(customer_api.report_routes.require_report_range): the same rules, the same
422, checked before anything operational is read.

NO SCOPE WIDER THAN ONE SORTER SITE IS EVER OPENED. Seeing the organization
does not open its data. services.organization_report_service reads the
organization's sorter sites one at a time, and for each one this module hands
it the two things the sorter-site reports themselves use:

    customer_api.tenant_scope.resolve_site_tenant         the user, the organization and that site's host
                                                          branch -> the operational tenant that user may read
    customer_api.tenant_scope.open_customer_tenant_connection
                                                          a connection scoped to exactly that tenant by row
                                                          level security, with the scope verified

so an organization report can count only rows a sorter-site report of the
same user could have counted.

ALL OR NOTHING. A response is built only from a finished report. If any site
cannot be read the request fails -- as a server error, or as the ordinary 404
if the user stopped being able to see the organization while it was being
read -- and no partial total is ever returned.

A route counts nothing itself and takes no identifier but the organization's
slug.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from customer_api.entitlement_dependencies import Transits
from customer_api.errors import NO_STORE_HEADERS, CustomerApiRoute
from customer_api.operational_schemas import RoutingDestination, RoutingHome
from customer_api.organization_report_schemas import (
    OrganizationOverviewResponse,
    OrganizationOverviewTotals,
    OrganizationReliabilityResponse,
    OrganizationReliabilityTotals,
    OrganizationSorterOverview,
    OrganizationSorterReliability,
    RoutingNetworkDestination,
    RoutingNetworkResponse,
    RoutingNetworkSource,
    RoutingNetworkTotals,
    SorterIdentity,
)
from customer_api.organization_routes import require_organization_member
from customer_api.organization_schemas import SorterHostBranch
from customer_api.report_routes import Range
from customer_api.report_schemas import OverviewDay, ReliabilityDay, ReliabilityReason
from customer_api.tenant_scope import (
    open_customer_tenant_connection,
    resolve_site_tenant,
)
from services.organization_report_service import (
    SiteNotResolvedError,
    SiteOpener,
    SiteResolver,
    get_organization_overview,
    get_organization_reliability,
    get_organization_routing_network,
)
from services.reject_reason import REJECT_REASONS
from services.sorter_inventory_service import SorterSite

# Declared BEFORE the range in every route below, so a request is authenticated and the organization checked
# first, as for the sorter-site reports: 401, then 404, then 422.
Member = Annotated[dict[str, Any], Depends(require_organization_member)]

T = TypeVar("T")


def _json(body) -> Response:
    return JSONResponse(content=body.model_dump(mode="json", by_alias=True), headers=NO_STORE_HEADERS)


def _read(org_slug: str, user: dict[str, Any], report: Callable[[SiteResolver, SiteOpener], T]) -> T:
    """Runs `report` with the one way this API turns a user and a site into a
    scope, and the one way it opens that scope.

    A listed site that did not resolve is a fault -- unless the user can no
    longer see the organization at all (membership or the organization's
    status changed while the sites were being read), which is the ordinary
    404. That is checked again, by the same rule, rather than assumed.
    """

    def resolve_site(branch_slug: str):
        return resolve_site_tenant(user["id"], org_slug, branch_slug)

    try:
        return report(resolve_site, open_customer_tenant_connection)
    except SiteNotResolvedError:
        require_organization_member(org_slug, user)
        raise


def _host_branch(site: SorterSite) -> SorterHostBranch:
    return SorterHostBranch(slug=site.host_branch_slug, name=site.host_branch_name)


def _identity(site: SorterSite) -> SorterIdentity:
    return SorterIdentity(slug=site.slug, name=site.name, host_branch=_host_branch(site))


def create_organization_report_router() -> APIRouter:
    router = APIRouter(prefix="/organizations/{org_slug}/reports", route_class=CustomerApiRoute)

    @router.get("/overview")
    def get_organization_overview_report(org_slug: str, user: Member, requested: Range) -> Response:
        local_range = requested.local_range
        report = _read(org_slug, user, lambda resolve_site, open_site: get_organization_overview(
            org_slug, local_range, resolve_site=resolve_site, open_site=open_site,
        ))

        # Every connection is closed. Counts only.
        return _json(OrganizationOverviewResponse(
            range=requested.response(),
            totals=OrganizationOverviewTotals(
                checkin_count=report.checkin_count,
                home_count=report.home_count,
                transit_count=report.transit_count,
                other_count=report.other_count,
                reject_count=report.reject_count,
            ),
            sorters=[
                OrganizationSorterOverview(
                    slug=sorter.site.slug,
                    name=sorter.site.name,
                    host_branch=_host_branch(sorter.site),
                    status=sorter.site.status,
                    collector_count=sorter.site.collector_count,
                    available=sorter.available,
                    checkin_count=sorter.checkin_count,
                    active_days=sorter.active_days,
                    transit_count=sorter.transit_count,
                    reject_count=sorter.reject_count,
                )
                for sorter in report.sorters
            ],
            days=[
                OverviewDay(date=day, checkin_count=checkins, reject_count=rejects)
                for day, checkins, rejects in zip(local_range.dates, report.checkin_days, report.reject_days, strict=True)
            ],
        ))

    @router.get("/routing-network")
    def get_organization_routing_network_report(org_slug: str, user: Member, _transits: Transits, requested: Range) -> Response:
        local_range = requested.local_range
        report = _read(org_slug, user, lambda resolve_site, open_site: get_organization_routing_network(
            org_slug, local_range, resolve_site=resolve_site, open_site=open_site,
        ))

        # Every connection is closed. Configured labels and counts only: no stored destination value is returned.
        return _json(RoutingNetworkResponse(
            range=requested.response(),
            totals=RoutingNetworkTotals(checkin_count=report.checkin_count, transit_count=report.transit_count),
            sources=[
                RoutingNetworkSource(
                    sorter=_identity(source.site),
                    checkin_count=source.counts.total,
                    home=RoutingHome(label=source.home_label, checkin_count=source.counts.home_count),
                    transit_count=source.counts.transit_count,
                    other_count=source.counts.other_count,
                    transit=[
                        RoutingDestination(key=destination.key, label=destination.label, checkin_count=count)
                        for destination, count in zip(source.transit, source.counts.transit_counts, strict=True)
                    ],
                )
                for source in report.sources
            ],
            destinations=[
                RoutingNetworkDestination(
                    key=destination.key,
                    label=destination.label,
                    checkin_count=destination.checkin_count,
                    source_count=destination.source_count,
                )
                for destination in report.destinations
            ],
        ))

    @router.get("/reliability")
    def get_organization_reliability_report(org_slug: str, user: Member, requested: Range) -> Response:
        local_range = requested.local_range
        report = _read(org_slug, user, lambda resolve_site, open_site: get_organization_reliability(
            org_slug, local_range, resolve_site=resolve_site, open_site=open_site,
        ))

        def reasons(counts) -> list[ReliabilityReason]:
            return [
                ReliabilityReason(reason=reason, reject_count=count)
                for reason, count in zip(REJECT_REASONS, counts, strict=True)
            ]

        # Every connection is closed. Reason codes and counts only: nothing a stored row said beyond its reason.
        return _json(OrganizationReliabilityResponse(
            range=requested.response(),
            totals=OrganizationReliabilityTotals(
                checkin_count=report.checkin_count,
                reject_count=report.reject_count,
                reasons=reasons(report.reason_counts),
            ),
            sorters=[
                OrganizationSorterReliability(
                    sorter=_identity(sorter.site),
                    available=sorter.available,
                    checkin_count=sorter.checkin_count,
                    reject_count=sorter.reject_count,
                    reasons=reasons(sorter.reason_counts),
                )
                for sorter in report.sorters
            ],
            days=[
                ReliabilityDay(date=day, checkin_count=checkins, reject_count=rejects)
                for day, checkins, rejects in zip(local_range.dates, report.checkin_days, report.reject_days, strict=True)
            ],
        ))

    return router
