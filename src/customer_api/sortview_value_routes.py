"""Reading and replacing an organization's SortView Value settings.

    GET /organizations/{org_slug}/settings/sortview-value
    PUT /organizations/{org_slug}/settings/sortview-value

THE SETTINGS, AND NOTHING WORKED OUT FROM THEM. What an organization has
entered about its reporting and troubleshooting work, what SortView costs it,
and which labor rate applies. No hours, value, return or payback is
calculated or returned here.

WHO. These are an organization's labor and cost figures: only its OWNERS and
ADMINS may read them, and only they may change them -- the Efficiency
settings' own rule, by the Efficiency settings' own dependencies
(customer_api.efficiency_settings_routes). No plan feature is involved.

    no session                                        401 not_authenticated
    not a member, or the organization is not visible  404 organization_not_found
    a member who is not an owner or admin             403 forbidden
    changing a suspended organization's settings      403 organization_read_only  (it can still be read)

The checks run in that order, and a body is looked at only after them: so
someone who may not see the settings learns nothing about them.

A PUT also carries the browser's Origin, checked first, exactly as for login
and logout (customer_api.auth_dependencies.require_allowed_origin).

WHAT A PUT DOES. Its body is the whole SortView Value block: a field that is
left out, or null, is cleared, and a block with nothing set removes what was
stored. Every rule about a value is services.sortview_value's, and a body
that breaks one is answered 422 with each field at fault and a code for why
-- never the value that was sent. (A body that is not a JSON object at all
is the application's ordinary 422.) The answer is what is then stored.

A FIELD'S NAME IN AN ANSWER OR A LOG IS SHORT AND PLAIN. A key that is not a
field is named so that it can be found -- but its name was chosen by whoever
sent it. So every name is cut to FIELD_NAME_MAX_LENGTH characters, and any
character that is not a letter, a digit, `_`, `-` or `.` is written `?`,
before it goes into an answer or a log line.

THE LABOR RATE is the block's own if it has one, else the organization's
Efficiency default, else none; the answer gives all three and says which.
Only the organization's default is ever looked at: never a sorter's.

WHAT IS STORED BUT MALFORMED. A stored SortView Value block that cannot be
read is a fault on this side, not the client's: 500
sortview_value_settings_invalid, naming nothing. A stored EFFICIENCY block
that cannot be read is not this route's fault to report: the settings are
answered all the same, with no Efficiency rate to fall back on.

A route runs no SQL of its own (services.sortview_value_service does) and
takes no identifier but the organization's slug.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from customer_api import settings
from customer_api.auth_dependencies import require_allowed_origin
from customer_api.efficiency_settings_routes import (
    Admin,
    WritingAdmin,
    _organization_not_found,
)
from customer_api.errors import (
    NO_STORE_HEADERS,
    CustomerApiError,
    CustomerApiRoute,
    logger,
)
from customer_api.sortview_value_schemas import (
    ReportingSettings,
    SortViewValue,
    SortViewValueRequest,
    SortViewValueSettingsResponse,
    TroubleshootingSettings,
    ValueLaborRate,
)
from services import sortview_value_service
from services.efficiency_settings import serialize_organization_efficiency_settings
from services.sortview_value import (
    REPORTING,
    TROUBLESHOOTING,
    SortViewValueError,
    SortViewValueProblem,
    resolve_labor_rate,
    serialize_sortview_value_settings,
    validate_sortview_value_settings,
)
from services.sortview_value_service import StoredSortViewValue

# The longest a field's name is in an answer or a log line. Every real field's name is well within it.
FIELD_NAME_MAX_LENGTH = 64

_NOT_PLAIN = re.compile(r"[^A-Za-z0-9_.\-]")


def safe_field_name(name: str) -> str:
    """A field's name as it may go into an answer or a log: cut to
    FIELD_NAME_MAX_LENGTH characters, and nothing in it but letters, digits,
    `_`, `-` and `.` -- anything else is written `?`. A real field's name is
    unchanged."""
    return _NOT_PLAIN.sub("?", name[:FIELD_NAME_MAX_LENGTH])


def _stored_settings_invalid(error: SortViewValueError) -> CustomerApiError:
    # Which fields and why, for whoever has to repair them. Never a stored value: the model's errors carry none.
    logger.error(
        "Stored SortView Value settings are malformed | problems=%s",
        [f"{safe_field_name(problem.field)}:{problem.code}" for problem in error.problems],
    )
    return CustomerApiError(500, "sortview_value_settings_invalid", "The stored SortView Value settings could not be read.")


def _invalid(problems: tuple[SortViewValueProblem, ...]) -> Response:
    """422 for a body that cannot be stored: every field at fault and why.
    The customer API's error shape, with the problems beside it. Nothing
    that was sent is repeated, and a field's name is made safe first."""
    return JSONResponse(
        status_code=422,
        content={
            "code": "invalid_sortview_value_settings",
            "message": "The SortView Value settings are not valid.",
            "problems": [{"field": safe_field_name(problem.field), "code": problem.code} for problem in problems],
        },
        headers=NO_STORE_HEADERS,
    )


def _body(org_slug: str, stored: StoredSortViewValue) -> Response:
    if stored.efficiency_unreadable:
        # Not this route's fault to report, and not a reason to refuse the settings: there is no rate to fall back on.
        logger.warning("Stored efficiency settings are malformed; no fallback labor rate | org=%s", org_slug)

    block = serialize_sortview_value_settings(stored.settings)
    efficiency_rate = serialize_organization_efficiency_settings(stored.organization_efficiency).get("labor_rate")
    resolved = resolve_labor_rate(stored.settings, stored.organization_efficiency)
    own_rate = block.get("labor_rate")

    body = SortViewValueSettingsResponse(
        sortview_value=SortViewValue(
            labor_rate=ValueLaborRate(
                sortview_value=own_rate,
                organization_efficiency=efficiency_rate,
                effective=None if resolved is None else {"sortview_value": own_rate, "organization_efficiency": efficiency_rate}[resolved.source],
                source=None if resolved is None else resolved.source,
            ),
            subscription_annual_cost=block.get("subscription_annual_cost"),
            reporting=ReportingSettings(**{name: block.get(REPORTING, {}).get(name) for name in ReportingSettings.model_fields}),
            troubleshooting=TroubleshootingSettings(
                **{name: block.get(TROUBLESHOOTING, {}).get(name) for name in TroubleshootingSettings.model_fields}
            ),
        )
    )
    return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)


def _fields_set(block: dict[str, Any]) -> list[str]:
    """The names of the fields a stored block sets, a kind of work's with it
    in front. Names only: every one is the settings model's own."""
    names: list[str] = []
    for name, value in block.items():
        if isinstance(value, dict):
            names.extend(f"{name}.{field}" for field in value)
        else:
            names.append(name)
    return names


def create_sortview_value_router() -> APIRouter:
    router = APIRouter(prefix="/organizations/{org_slug}", route_class=CustomerApiRoute)

    @router.get("/settings/sortview-value")
    def get_sortview_value_settings(org_slug: str, user: Admin) -> Response:
        try:
            stored = sortview_value_service.read_sortview_value(org_slug, user_id=user["id"])
        except SortViewValueError as error:
            raise _stored_settings_invalid(error) from None
        if stored is None:
            raise _organization_not_found()
        return _body(org_slug, stored)

    @router.put("/settings/sortview-value", dependencies=[Depends(require_allowed_origin)])
    def put_sortview_value_settings(org_slug: str, user: WritingAdmin, body: SortViewValueRequest) -> Response:
        # The product's current date, in the product's zone: the latest date a figure can be as of. Read once and
        # here -- the model is told what today is and reads no clock. Never the browser's date.
        today = datetime.now(UTC).astimezone(settings.product_timezone()).date()

        try:
            wanted = validate_sortview_value_settings(body.block(), today=today)
        except SortViewValueError as error:
            return _invalid(error.problems)

        try:
            stored = sortview_value_service.replace_sortview_value(org_slug, wanted, user_id=user["id"])
        except SortViewValueError as error:
            raise _stored_settings_invalid(error) from None
        if stored is None:
            raise _organization_not_found()

        # Who changed which settings of what. Field NAMES only: never a rate, a cost, a count, a date or the body.
        logger.info(
            "SortView Value settings replaced | org=%s user_id=%s fields_set=%s",
            org_slug,
            user["id"],
            _fields_set(serialize_sortview_value_settings(wanted)),
        )
        return _body(org_slug, stored)

    return router
