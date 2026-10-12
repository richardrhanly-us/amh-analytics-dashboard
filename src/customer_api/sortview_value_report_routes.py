"""An organization's SortView Value report.

    GET /organizations/{org_slug}/reports/sortview-value

It is put together from pieces that already exist, each used as it is:

    who may read it       customer_api.efficiency_settings_routes.require_organization_admin
    the settings          services.sortview_value_service.read_sortview_value
    the labor rate        services.sortview_value.resolve_labor_rate
    the arithmetic        services.sortview_value.calculate_sortview_value
    the assumptions       customer_api.sortview_value_routes.sortview_value_view

NO RANGE, AND NO EVENTS. Every figure is worked out from what the
organization has entered about a month of its own work. Nothing is counted
from a sorter, so there is no `from` and `to`, no sorter site in the address,
and nothing operational is read: the settings service's one read is all the
database is asked.

WHO. The report is made of an organization's labor and cost figures, so it
is for its OWNERS and ADMINS only, exactly as the settings are. Its answers,
in the order they are decided:

    no session                                        401 not_authenticated
    not a member, or the organization is not visible  404 organization_not_found
    a member who is not an owner or admin             403 forbidden

A suspended organization's report can still be read, like its settings. No
plan feature is involved.

SETTINGS THAT ARE NOT ALL THERE ARE A REPORT, NOT AN ERROR: 200, with null
for each figure that cannot be worked out and `missing` naming what is not
set. A cost or a count that was entered as zero is zero, and is worked
through. Nothing is clipped: a result may be negative.

SETTINGS THAT ARE STORED BUT MALFORMED are a fault on this side: 500
sortview_value_settings_invalid, as for the settings routes, and nothing is
worked out from half of a broken block. A stored EFFICIENCY block that
cannot be read is not: the report is answered, with no Efficiency rate to
fall back on.

Read-only: nothing is written. A route runs no SQL of its own, works out no
figure of its own and takes no identifier but the organization's slug.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter
from starlette.responses import JSONResponse, Response

from customer_api.efficiency_settings_routes import Admin, _organization_not_found
from customer_api.errors import NO_STORE_HEADERS, CustomerApiRoute
from customer_api.sortview_value_report_schemas import (
    CategoryResults,
    SortViewValueReportResponse,
    SortViewValueResults,
)
from customer_api.sortview_value_routes import (
    _stored_settings_invalid,
    sortview_value_view,
)
from services import sortview_value_service
from services.sortview_value import (
    HOURS_PLACES,
    MONEY_PLACES,
    PAYBACK_PLACES,
    ROI_PLACES,
    CategoryValue,
    SortViewValueError,
    calculate_sortview_value,
    resolve_labor_rate,
)

# The one currency there is. The settings carry none: every amount is the customer's own figure, as entered.
CURRENCY = "USD"


def _text(value: Decimal | None, places: Decimal) -> str | None:
    """A result as text with exactly the places it was rounded to -- never
    in exponent form, and never a float on the way."""
    return None if value is None else f"{value:.{-places.as_tuple().exponent}f}"  # type: ignore[operator]


def _category(value: CategoryValue) -> CategoryResults:
    return CategoryResults(
        baseline_hours_per_month=_text(value.baseline_hours_per_month, HOURS_PLACES),
        current_hours_per_month=_text(value.current_hours_per_month, HOURS_PLACES),
        recovered_hours_per_month=_text(value.recovered_hours_per_month, HOURS_PLACES),
        monthly_value=_text(value.monthly_value, MONEY_PLACES),
        annual_value=_text(value.annual_value, MONEY_PLACES),
    )


def create_sortview_value_report_router() -> APIRouter:
    router = APIRouter(prefix="/organizations/{org_slug}/reports", route_class=CustomerApiRoute)

    @router.get("/sortview-value")
    def get_sortview_value_report(org_slug: str, user: Admin) -> Response:
        try:
            stored = sortview_value_service.read_sortview_value(org_slug, user_id=user["id"])
        except SortViewValueError as error:
            raise _stored_settings_invalid(error) from None
        if stored is None:
            raise _organization_not_found()

        # The connection is closed. What was entered, the rate that applies, and the arithmetic -- nothing else.
        report = calculate_sortview_value(stored.settings, resolve_labor_rate(stored.settings, stored.organization_efficiency))

        body = SortViewValueReportResponse(
            currency=CURRENCY,
            assumptions=sortview_value_view(org_slug, stored),
            results=SortViewValueResults(
                reporting=_category(report.reporting),
                troubleshooting=_category(report.troubleshooting),
                recovered_hours_per_month=_text(report.recovered_hours_per_month, HOURS_PLACES),
                monthly_value=_text(report.monthly_value, MONEY_PLACES),
                annual_value=_text(report.annual_value, MONEY_PLACES),
                net_annual_value=_text(report.net_annual_value, MONEY_PLACES),
                roi_percent=_text(report.roi_percent, ROI_PLACES),
                payback_months=_text(report.payback_months, PAYBACK_PLACES),
            ),
            missing=list(report.missing),
        )
        return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)

    return router
