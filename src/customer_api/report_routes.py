"""Read-only range reports over one sorter site's OPERATIONAL data.

    GET .../reports/overview?from=YYYY-MM-DD&to=YYYY-MM-DD
    GET .../reports/volume?from=...&to=...
    GET .../reports/routing?from=...&to=...
    GET .../reports/reliability?from=...&to=...
    GET .../reports/bins?from=...&to=...

Each is nested under one organization and one branch, named by slug, exactly
like the single-day reads in customer_api.operational_routes, and follows the
same steps: customer_api.tenant_scope resolves the request to the operational
tenant the user may read (or answers 404) and opens a connection carrying
that tenant's verified row level security context; a service runs the reads.
The branch in the address is, operationally, a SORTER SITE: the scope one
sorter's collector uploads under.

A RANGE is two calendar dates, `from` and `to`, both included, in the
product's zone. It is checked before any connection is opened -- each date
by the same rule as the single-day reads' `date`, then the pair together --
and a range that cannot be reported on is the application's ordinary 422,
saying which bound and what kind of problem and never the value. A range may
also start no earlier than the organization's plan allows
(services.entitlement_service.earliest_report_date): one that does is a 422
with its own code, range_before_history. The two rules are independent -- the
plan says how far back a range may start; MAX_REPORT_RANGE_DAYS, an
engineering guard and not a history limit, how long one request may be.

A route never takes an operational identifier from the request and never
counts anything itself: the tenant comes only from the two slugs in its path,
the counts only from services.operational_report_service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BeforeValidator
from starlette.responses import JSONResponse, Response

from customer_api import settings
from customer_api.entitlement_dependencies import OrganizationEntitlements, Transits
from customer_api.errors import NO_STORE_HEADERS, CustomerApiError, CustomerApiRoute
from customer_api.operational_routes import ResolvedTenant, _calendar_date
from customer_api.operational_schemas import RoutingDestination, RoutingHome
from customer_api.report_schemas import (
    BinVolumeBin,
    BinVolumeReportResponse,
    OverviewDay,
    OverviewReportResponse,
    ReliabilityDay,
    ReliabilityReason,
    ReliabilityReportResponse,
    ReportRange,
    RoutingDay,
    RoutingReportResponse,
    VolumeDay,
    VolumeHour,
    VolumeReportResponse,
)
from customer_api.tenant_scope import open_customer_tenant_connection
from services import entitlement_service
from services.operational_report_service import (
    LocalRange,
    ReportRangeError,
    get_bin_volume_report,
    get_overview_report,
    get_reliability_report,
    get_routing_report,
    get_volume_report,
    local_range,
    report_window,
    validate_report_range,
)
from services.reject_reason import REJECT_REASONS
from services.routing_config_service import get_routing_config

# Each bound is a calendar date and nothing else, by the single-day reads' own rule.
FromDate = Annotated[date, BeforeValidator(_calendar_date), Query(alias="from")]
ToDate = Annotated[date, BeforeValidator(_calendar_date), Query(alias="to")]


@dataclass(frozen=True, slots=True)
class RequestedRange:
    """A range that may be reported on, as it was asked for."""

    local_range: LocalRange
    includes_today: bool

    def response(self) -> ReportRange:
        return ReportRange(
            from_date=self.local_range.from_date,
            to_date=self.local_range.to_date,
            days=self.local_range.days,
            timezone=self.local_range.timezone_name,
            includes_today=self.includes_today,
        )


def require_report_range(from_date: FromDate, to_date: ToDate, entitlements: OrganizationEntitlements) -> RequestedRange:
    """The range in the request's `from` and `to`, or the ordinary 422 -- or, for a range that starts before the
    organization's plan allows, the 422 range_before_history.

    The product's current date is read once and here -- the services are
    told what today is and never read a clock of their own. A zone that is
    set but invalid raises, and is answered as a server error.
    """
    zone = settings.product_timezone()
    today = datetime.now(UTC).astimezone(zone).date()

    try:
        validate_report_range(from_date, to_date, today=today)
    except ReportRangeError as error:
        # Where and what kind only, like every validation failure: the hardened handler never echoes a value.
        raise RequestValidationError([{
            "type": f"report_{error.problem}",
            "loc": ("query", error.field),
            "msg": "The date range cannot be reported on.",
            "input": None,
        }]) from None

    earliest = entitlement_service.earliest_report_date(entitlements, today)
    if earliest is not None and from_date < earliest:
        raise CustomerApiError(
            422, "range_before_history", "The selected range starts before this organization's available reporting window."
        )

    return RequestedRange(local_range=local_range(from_date, to_date, zone), includes_today=to_date == today)


# Declared AFTER the tenant (and after a route's own plan feature) in every route below, so a request is
# authenticated and resolved first, exactly as for the single-day reads: 401, then 404, then 403, then 422.
Range = Annotated[RequestedRange, Depends(require_report_range)]


def _json(body) -> Response:
    return JSONResponse(content=body.model_dump(mode="json", by_alias=True), headers=NO_STORE_HEADERS)


def create_report_router() -> APIRouter:
    router = APIRouter(
        prefix="/organizations/{org_slug}/branches/{branch_slug}/reports",
        route_class=CustomerApiRoute,
    )

    @router.get("/overview")
    def get_site_overview_report(tenant: ResolvedTenant, requested: Range) -> Response:
        dates = requested.local_range.dates

        with open_customer_tenant_connection(tenant) as conn:
            routing = get_routing_config(conn, tenant)
            report = get_overview_report(conn, tenant, report_window(conn, tenant, requested.local_range), routing)

        # The connection is closed. Counts only.
        return _json(OverviewReportResponse(
            range=requested.response(),
            checkin_count=report.checkin_count,
            active_days=report.active_days,
            home_count=report.routing.home_count,
            transit_count=report.routing.transit_count,
            other_count=report.routing.other_count,
            reject_count=report.reject_count,
            days=[
                OverviewDay(date=day, checkin_count=checkins, reject_count=rejects)
                for day, checkins, rejects in zip(dates, report.checkin_days, report.reject_days, strict=True)
            ],
        ))

    @router.get("/volume")
    def get_site_volume_report(tenant: ResolvedTenant, requested: Range) -> Response:
        dates = requested.local_range.dates

        with open_customer_tenant_connection(tenant) as conn:
            report = get_volume_report(conn, tenant, report_window(conn, tenant, requested.local_range))

        return _json(VolumeReportResponse(
            range=requested.response(),
            checkin_count=report.checkin_count,
            days=[
                VolumeDay(date=day, checkin_count=count)
                for day, count in zip(dates, report.checkin_days, strict=True)
            ],
            hours=[VolumeHour(hour=hour, checkin_count=count) for hour, count in enumerate(report.checkin_hours)],
        ))

    @router.get("/routing")
    def get_site_routing_report(tenant: ResolvedTenant, _transits: Transits, requested: Range) -> Response:
        dates = requested.local_range.dates

        with open_customer_tenant_connection(tenant) as conn:
            # Which destinations this sorter site has is its own configuration,
            # read for the resolved tenant on the same scoped connection.
            routing = get_routing_config(conn, tenant)
            report = get_routing_report(conn, tenant, report_window(conn, tenant, requested.local_range), routing)

        # The connection is closed. Configured labels and counts only: no stored destination value is returned.
        return _json(RoutingReportResponse(
            range=requested.response(),
            checkin_count=report.total.total,
            home=RoutingHome(label=routing.home_label, checkin_count=report.total.home_count),
            transit=[
                RoutingDestination(key=destination.key, label=destination.label, checkin_count=count)
                for destination, count in zip(routing.transit, report.total.transit_counts, strict=True)
            ],
            transit_count=report.total.transit_count,
            other_count=report.total.other_count,
            days=[
                RoutingDay(
                    date=day,
                    checkin_count=counts.total,
                    home_count=counts.home_count,
                    transit_counts=list(counts.transit_counts),
                    other_count=counts.other_count,
                )
                for day, counts in zip(dates, report.days, strict=True)
            ],
        ))

    @router.get("/reliability")
    def get_site_reliability_report(tenant: ResolvedTenant, requested: Range) -> Response:
        dates = requested.local_range.dates

        with open_customer_tenant_connection(tenant) as conn:
            report = get_reliability_report(conn, tenant, report_window(conn, tenant, requested.local_range))

        # The connection is closed. Reason codes and counts only: nothing a stored row said beyond its reason.
        return _json(ReliabilityReportResponse(
            range=requested.response(),
            checkin_count=report.checkin_count,
            reject_count=report.reject_count,
            reasons=[
                ReliabilityReason(reason=reason, reject_count=count)
                for reason, count in zip(REJECT_REASONS, report.rejects.reason_counts, strict=True)
            ],
            days=[
                ReliabilityDay(date=day, checkin_count=checkins, reject_count=rejects)
                for day, checkins, rejects in zip(dates, report.checkin_days, report.rejects.day_counts, strict=True)
            ],
        ))

    @router.get("/bins")
    def get_site_bin_volume_report(tenant: ResolvedTenant, requested: Range) -> Response:
        with open_customer_tenant_connection(tenant) as conn:
            report = get_bin_volume_report(conn, tenant, report_window(conn, tenant, requested.local_range))

        # The connection is closed. Bin numbers and counts only: only bins that were observed, in numeric order.
        return _json(BinVolumeReportResponse(
            range=requested.response(),
            checkin_count=report.checkin_count,
            known_bin_count=report.known_count,
            unknown_bin_count=report.unknown_count,
            bins=[
                BinVolumeBin(key=counts.key, checkin_count=counts.checkin_count, hours=list(counts.hour_counts))
                for counts in report.bins
            ],
        ))

    return router
