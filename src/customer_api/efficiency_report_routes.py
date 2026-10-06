"""A sorter site's Efficiency report over a range.

    GET /organizations/{org_slug}/branches/{branch_slug}/reports/efficiency?from=YYYY-MM-DD&to=YYYY-MM-DD

It sits beside the sorter's other range reports (customer_api.report_routes)
and is put together from pieces that already exist, each used as it is:

    who may read it       customer_api.efficiency_settings_routes.require_organization_admin
    the sorter's scope    customer_api.tenant_scope (require_resolved_tenant, open_customer_tenant_connection)
    the range             customer_api.report_routes.require_report_range
    check-ins by day      services.operational_report_service (report_window, get_checkin_counts_by_day)
    the settings          services.efficiency_settings_service.read_sorter_efficiency
    what applies          services.efficiency_settings.resolve_efficiency_settings
    the arithmetic        services.efficiency_report.calculate_efficiency

WHO. The report is made of an organization's labor and cost figures, so it
is for its OWNERS and ADMINS only, exactly as the settings are. Its answers,
in the order they are decided:

    no session                                        401 not_authenticated
    not a member, or the organization is not visible  404 organization_not_found
    a member who is not an owner or admin             403 forbidden
    no such branch, or no data scope for it           404 tenant_not_found       (the other reports' own answer)
    a range that cannot be reported on                422                        (the other reports' own answer)
    a branch that hosts no sorter                     404 sorter_not_found

A suspended organization's report can still be read, like its other reports.

THE CHECK-INS are the ones the sorter's other reports count: the same window
over the same range, read on the same scoped and verified connection, one
count per day. Nothing is counted here.

SETTINGS THAT ARE STORED BUT MALFORMED are a fault on this side: 500
efficiency_settings_invalid, as for the settings routes, before any
operational data is read. Nothing is worked out from half of a broken block.

Read-only: nothing is written. A route runs no SQL of its own and takes no
identifier but the two slugs.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter
from starlette.responses import JSONResponse, Response

from customer_api.efficiency_report_schemas import (
    AssumedRate,
    EfficiencyAssumptions,
    EfficiencyReportResponse,
    EfficiencyResults,
)
from customer_api.efficiency_settings_routes import (
    Admin,
    _sorter_not_found,
    _stored_settings_invalid,
)
from customer_api.errors import NO_STORE_HEADERS, CustomerApiRoute
from customer_api.operational_routes import ResolvedTenant
from customer_api.report_routes import Range
from customer_api.tenant_scope import open_customer_tenant_connection
from services import efficiency_settings_service
from services.efficiency_report import (
    COST_PER_ITEM_PLACES,
    HOURS_PLACES,
    MONEY_PLACES,
    calculate_efficiency,
)
from services.efficiency_settings import (
    EfficiencySettingsError,
    resolve_efficiency_settings,
    serialize_organization_efficiency_settings,
    serialize_sorter_efficiency_settings,
)
from services.operational_report_service import (
    get_checkin_counts_by_day,
    report_window,
)

# The one currency there is. The settings carry none: every amount is the customer's own figure, as entered.
CURRENCY = "USD"


def _text(value: Decimal | None, places: Decimal) -> str | None:
    """A result as text with exactly the places it was rounded to -- never
    in exponent form, and never a float on the way."""
    return None if value is None else f"{value:.{-places.as_tuple().exponent}f}"  # type: ignore[operator]


def create_efficiency_report_router() -> APIRouter:
    router = APIRouter(
        prefix="/organizations/{org_slug}/branches/{branch_slug}/reports",
        route_class=CustomerApiRoute,
    )

    # The dependencies run in the order they are declared: who may read it, then the sorter's scope, then the range.
    @router.get("/efficiency")
    def get_site_efficiency_report(
        org_slug: str, branch_slug: str, user: Admin, tenant: ResolvedTenant, requested: Range
    ) -> Response:
        try:
            stored = efficiency_settings_service.read_sorter_efficiency(org_slug, branch_slug, user_id=user["id"])
        except EfficiencySettingsError as error:
            raise _stored_settings_invalid(error) from None
        if stored is None:
            raise _sorter_not_found()
        effective = resolve_efficiency_settings(stored.organization, stored.sorter)

        with open_customer_tenant_connection(tenant) as conn:
            checkin_days = get_checkin_counts_by_day(conn, tenant, report_window(conn, tenant, requested.local_range))

        # The connection is closed. Counts and the customer's own configured figures only.
        report = calculate_efficiency(requested.local_range.dates, checkin_days, effective)

        # Each assumption as the canonical text it is stored as, from whichever document it came from.
        blocks = {
            "organization": serialize_organization_efficiency_settings(stored.organization),
            "sorter": serialize_sorter_efficiency_settings(stored.sorter),
        }

        def rate(name: str) -> AssumedRate | None:
            resolved = getattr(effective, name)
            return None if resolved is None else AssumedRate(value=blocks[resolved.source][name], source=resolved.source)

        body = EfficiencyReportResponse(
            range=requested.response(),
            currency=CURRENCY,
            checkin_count=report.checkin_count,
            assumptions=EfficiencyAssumptions(
                manual_items_per_hour=rate("manual_items_per_hour"),
                labor_rate=rate("labor_rate"),
                recurring_annual_cost=blocks["sorter"].get("recurring_annual_cost"),
                one_time_cost=blocks["sorter"].get("one_time_cost"),
                in_service_date=blocks["sorter"].get("in_service_date"),
            ),
            results=EfficiencyResults(
                in_service_days=report.in_service_days,
                in_service_checkin_count=report.in_service_checkin_count,
                staff_time_equivalent_hours=_text(report.staff_time_equivalent_hours, HOURS_PLACES),
                labor_value_equivalent=_text(report.labor_value_equivalent, MONEY_PLACES),
                recurring_cost=_text(report.recurring_cost, MONEY_PLACES),
                net_operational_value=_text(report.net_operational_value, MONEY_PLACES),
                recurring_cost_per_item=_text(report.recurring_cost_per_item, COST_PER_ITEM_PLACES),
            ),
            missing=list(report.missing),
        )
        return JSONResponse(content=body.model_dump(mode="json", by_alias=True), headers=NO_STORE_HEADERS)

    return router
