"""Reading and replacing Efficiency settings.

    GET /organizations/{org_slug}/settings/efficiency
    PUT /organizations/{org_slug}/settings/efficiency
    GET /organizations/{org_slug}/branches/{branch_slug}/settings/efficiency
    PUT /organizations/{org_slug}/branches/{branch_slug}/settings/efficiency

The second pair is a SORTER SITE's settings. As everywhere in this API, a
sorter site is named by its host branch's slug -- which is the sorter's own
slug in the organization's `sorters` list -- so the address is the branch
one. A branch that hosts no sorter has no such settings.

WHO. These are an organization's labor and cost figures: only its OWNERS and
ADMINS may read them, and only they may change them (services
.permission_service.ADMIN_ROLES, the dashboard settings page's own rule).
Being a platform administrator grants nothing here, as nowhere in this API.

    no session                                        401 not_authenticated
    not a member, or the organization is not visible  404 organization_not_found  (the organization routes' own answer)
    a member who is not an owner or admin             403 forbidden
    changing a suspended organization's settings      403 organization_read_only  (it can still be read)
    no such sorter site in the organization           404 sorter_not_found

The checks run in that order, so someone who may not see the settings learns
nothing about them -- not even whether a sorter exists.

A PUT also carries the browser's Origin, checked first, exactly as for login
and logout (customer_api.auth_dependencies.require_allowed_origin).

WHAT A PUT DOES. Its body is the whole Efficiency block for that level: a
field that is left out, or null, is cleared. An organization's block has its
two rates and nothing else; a sorter's has its two overrides, its two costs
and its date. Every rule about a value is services.efficiency_settings', and
a body that breaks one is answered 422 with each field at fault and a code
for why -- never the value that was sent. (A body that is not a JSON object
at all is the application's ordinary 422.)

WHAT IS STORED BUT MALFORMED is a fault on this side, not the client's: 500
efficiency_settings_invalid, naming nothing. Only these routes read the
Efficiency block, so nothing else is affected by one.

A route runs no SQL of its own (services.efficiency_settings_service does)
and takes no identifier but the two slugs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from customer_api import settings
from customer_api.auth_dependencies import require_allowed_origin
from customer_api.efficiency_settings_schemas import (
    EfficiencyRate,
    OrganizationEfficiency,
    OrganizationEfficiencyRequest,
    OrganizationEfficiencyResponse,
    SorterEfficiency,
    SorterEfficiencyRequest,
    SorterEfficiencyResponse,
)
from customer_api.errors import (
    NO_STORE_HEADERS,
    CustomerApiError,
    CustomerApiRoute,
    logger,
)
from customer_api.organization_routes import CurrentUser, require_organization_member
from services import access_service, efficiency_settings_service, entitlement_service
from services.efficiency_settings import (
    EfficiencySettingProblem,
    EfficiencySettingsError,
    OrganizationEfficiencySettings,
    resolve_efficiency_settings,
    serialize_organization_efficiency_settings,
    serialize_sorter_efficiency_settings,
    validate_organization_efficiency_settings,
    validate_sorter_efficiency_settings,
)
from services.permission_service import ADMIN_ROLES


def require_organization_admin(org_slug: str, user: CurrentUser) -> dict[str, Any]:
    """The authenticated user, once it is established that they may see the
    organization in the path AND are an owner or admin of it. Someone who
    cannot see it gets the organization routes' own 404; a member with any
    other role gets 403, which tells them nothing they did not know.

    The role is the uncached lookup every permission check is built on."""
    require_organization_member(org_slug, user)
    if entitlement_service.get_org_role_for_user(user_id=user["id"], org_slug=org_slug) not in ADMIN_ROLES:
        raise CustomerApiError(403, "forbidden", "You do not have permission to manage these settings.")
    return user


def require_writable_organization_admin(org_slug: str, user: CurrentUser) -> dict[str, Any]:
    """require_organization_admin, for a change: the organization must also
    have full access. A suspended organization's settings stay readable and
    cannot be changed, as on the dashboard's settings page."""
    require_organization_admin(org_slug, user)
    if access_service.get_org_access_mode(org_slug) != "full":
        raise CustomerApiError(403, "organization_read_only", "This organization's settings cannot be changed.")
    return user


Admin = Annotated[dict[str, Any], Depends(require_organization_admin)]
WritingAdmin = Annotated[dict[str, Any], Depends(require_writable_organization_admin)]


def _organization_not_found() -> CustomerApiError:
    # The scope stopped resolving between the checks above and the read: the same answer as if it never had.
    return CustomerApiError(404, "organization_not_found", "Organization not found.")


def _sorter_not_found() -> CustomerApiError:
    """The one answer for a branch slug that is not one of the organization's
    sorter sites: no such branch, another organization's, an inactive one, or
    one that hosts no sorter."""
    return CustomerApiError(404, "sorter_not_found", "Sorter not found.")


def _stored_settings_invalid(error: EfficiencySettingsError) -> CustomerApiError:
    # Which fields and why, for whoever has to repair them. Never a stored value: the model's errors carry none.
    logger.error(
        "Stored efficiency settings are malformed | problems=%s",
        [f"{problem.field}:{problem.code}" for problem in error.problems],
    )
    return CustomerApiError(500, "efficiency_settings_invalid", "The stored efficiency settings could not be read.")


def _invalid(problems: tuple[EfficiencySettingProblem, ...]) -> Response:
    """422 for a body that cannot be stored: every field at fault and why.
    The customer API's error shape, with the problems beside it. Nothing
    that was sent is repeated."""
    return JSONResponse(
        status_code=422,
        content={
            "code": "invalid_efficiency_settings",
            "message": "The efficiency settings are not valid.",
            "problems": [{"field": problem.field, "code": problem.code} for problem in problems],
        },
        headers=NO_STORE_HEADERS,
    )


def _organization_body(stored: OrganizationEfficiencySettings) -> Response:
    block = serialize_organization_efficiency_settings(stored)
    body = OrganizationEfficiencyResponse(
        efficiency=OrganizationEfficiency(
            labor_rate=block.get("labor_rate"), manual_items_per_hour=block.get("manual_items_per_hour")
        )
    )
    return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)


def _sorter_body(stored: efficiency_settings_service.SorterEfficiency) -> Response:
    organization = serialize_organization_efficiency_settings(stored.organization)
    sorter = serialize_sorter_efficiency_settings(stored.sorter)
    effective = resolve_efficiency_settings(stored.organization, stored.sorter)

    def rate(name: str) -> EfficiencyRate:
        resolved = getattr(effective, name)
        source = None if resolved is None else resolved.source
        return EfficiencyRate(
            organization=organization.get(name),
            sorter=sorter.get(name),
            effective={"organization": organization, "sorter": sorter}[source].get(name) if source else None,
            source=source,
        )

    body = SorterEfficiencyResponse(
        efficiency=SorterEfficiency(
            labor_rate=rate("labor_rate"),
            manual_items_per_hour=rate("manual_items_per_hour"),
            one_time_cost=sorter.get("one_time_cost"),
            recurring_annual_cost=sorter.get("recurring_annual_cost"),
            in_service_date=sorter.get("in_service_date"),
        )
    )
    return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)


def _log_replaced(scope: str, org_slug: str, branch_slug: str | None, user: dict[str, Any], fields: list[str]) -> None:
    # Who changed which settings of what. Field NAMES only: never a rate, a cost, a date or the request body.
    logger.info(
        "Efficiency settings replaced | scope=%s org=%s sorter=%s user_id=%s fields_set=%s",
        scope,
        org_slug,
        branch_slug or "-",
        user["id"],
        fields,
    )


def create_efficiency_settings_router() -> APIRouter:
    router = APIRouter(prefix="/organizations/{org_slug}", route_class=CustomerApiRoute)

    @router.get("/settings/efficiency")
    def get_organization_efficiency_settings(org_slug: str, user: Admin) -> Response:
        try:
            stored = efficiency_settings_service.read_organization_efficiency(org_slug, user_id=user["id"])
        except EfficiencySettingsError as error:
            raise _stored_settings_invalid(error) from None
        if stored is None:
            raise _organization_not_found()
        return _organization_body(stored)

    @router.put("/settings/efficiency", dependencies=[Depends(require_allowed_origin)])
    def put_organization_efficiency_settings(org_slug: str, user: WritingAdmin, body: OrganizationEfficiencyRequest) -> Response:
        try:
            wanted = validate_organization_efficiency_settings(body.block())
        except EfficiencySettingsError as error:
            return _invalid(error.problems)

        try:
            stored = efficiency_settings_service.replace_organization_efficiency(org_slug, wanted, user_id=user["id"])
        except EfficiencySettingsError as error:
            raise _stored_settings_invalid(error) from None
        if stored is None:
            raise _organization_not_found()

        _log_replaced("organization", org_slug, None, user, list(serialize_organization_efficiency_settings(wanted)))
        return _organization_body(stored)

    @router.get("/branches/{branch_slug}/settings/efficiency")
    def get_sorter_efficiency_settings(org_slug: str, branch_slug: str, user: Admin) -> Response:
        try:
            stored = efficiency_settings_service.read_sorter_efficiency(org_slug, branch_slug, user_id=user["id"])
        except EfficiencySettingsError as error:
            raise _stored_settings_invalid(error) from None
        if stored is None:
            raise _sorter_not_found()
        return _sorter_body(stored)

    @router.put("/branches/{branch_slug}/settings/efficiency", dependencies=[Depends(require_allowed_origin)])
    def put_sorter_efficiency_settings(
        org_slug: str, branch_slug: str, user: WritingAdmin, body: SorterEfficiencyRequest
    ) -> Response:
        # The product's current date, in the product's zone: the latest date a sorter can have gone into service.
        # Read once and here -- the model is told what today is and reads no clock. Never the browser's date.
        today = datetime.now(UTC).astimezone(settings.product_timezone()).date()

        try:
            wanted = validate_sorter_efficiency_settings(body.block(), today=today)
        except EfficiencySettingsError as error:
            return _invalid(error.problems)

        try:
            stored = efficiency_settings_service.replace_sorter_efficiency(org_slug, branch_slug, wanted, user_id=user["id"])
        except EfficiencySettingsError as error:
            raise _stored_settings_invalid(error) from None
        if stored is None:
            raise _sorter_not_found()

        _log_replaced("sorter", org_slug, branch_slug, user, list(serialize_sorter_efficiency_settings(wanted)))
        return _sorter_body(stored)

    return router
