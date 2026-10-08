"""Reading and replacing an organization's routing settings.

    GET /organizations/{org_slug}/settings/routing
    PUT /organizations/{org_slug}/settings/routing

WHAT THEY ARE. What the organization calls a sorter site's own shelves, and
the other places its items are routed to -- each a label and whether it is
enabled (services.routing_settings).

THE ORGANIZATION'S OWN SETTINGS. These routes answer with, and replace, the
organization's stored block. They do not answer with what applies at a
sorter site, which the reports work out for themselves and which a site's own
settings can add to. No route here takes a branch.

WHO. Only the organization's OWNERS and ADMINS may read them, and only they
may change them -- the rule of the Efficiency settings beside them, and its
own checks (customer_api.efficiency_settings_routes):

    no session                                        401 not_authenticated
    not a member, or the organization is not visible  404 organization_not_found
    a member who is not an owner or admin             403 forbidden
    changing a suspended organization's settings      403 organization_read_only  (they can still be read)

A PUT also carries the browser's Origin, checked first, exactly as for login
and logout (customer_api.auth_dependencies.require_allowed_origin).

WHAT A PUT DOES. Its body is the whole block, and replaces what is stored:
nothing is merged with what was there. A value the settings model refuses is
answered 422 with each field at fault and a code for why -- never the value
that was sent. (A body that is not this shape at all -- an unknown field, a
`key`, a label that is not text -- is the application's ordinary 422.)

A CHANGE IS FELT IN EVERY REPORT AT ONCE, FOR PAST DAYS TOO. Which
destination a check-in counts under is worked out when a report is read, from
the settings as they then are. Nothing recorded is rewritten, and nothing
keeps the settings as they were.

WHAT IS STORED IS NEVER REFUSED ON THE WAY OUT. The dashboard's settings form
has stored blocks a PUT here would be refused for; a GET returns what is
there, read as the reports read it.

A route runs no SQL of its own (services.routing_settings_service does) and
takes no identifier but the organization's slug.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from customer_api.auth_dependencies import require_allowed_origin
from customer_api.efficiency_settings_routes import Admin, WritingAdmin
from customer_api.errors import (
    NO_STORE_HEADERS,
    CustomerApiError,
    CustomerApiRoute,
    logger,
)
from customer_api.routing_settings_schemas import (
    Routing,
    RoutingDestination,
    RoutingSettingsRequest,
    RoutingSettingsResponse,
)
from services import routing_settings_service
from services.routing_settings import (
    RoutingSettingProblem,
    RoutingSettings,
    RoutingSettingsError,
    validate_routing_settings,
)


def _organization_not_found() -> CustomerApiError:
    # The scope stopped resolving between the checks above and the read: the same answer as if it never had.
    return CustomerApiError(404, "organization_not_found", "Organization not found.")


def _invalid(problems: tuple[RoutingSettingProblem, ...]) -> Response:
    """422 for a block that cannot be stored: every field at fault and why.
    The customer API's error shape, with the problems beside it. Nothing
    that was sent is repeated."""
    return JSONResponse(
        status_code=422,
        content={
            "code": "invalid_routing_settings",
            "message": "The routing settings are not valid.",
            "problems": [{"field": problem.field, "code": problem.code} for problem in problems],
        },
        headers=NO_STORE_HEADERS,
    )


def _body(stored: RoutingSettings) -> Response:
    # Built field by field through the response model: a label and whether it is enabled, and nothing else that
    # is stored beside them.
    body = RoutingSettingsResponse(
        routing=Routing(
            home_branch_label=stored.home_branch_label,
            destinations=[
                RoutingDestination(label=destination.label, enabled=destination.enabled)
                for destination in stored.destinations
            ],
        )
    )
    return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)


def create_routing_settings_router() -> APIRouter:
    router = APIRouter(prefix="/organizations/{org_slug}", route_class=CustomerApiRoute)

    @router.get("/settings/routing")
    def get_organization_routing_settings(org_slug: str, user: Admin) -> Response:
        stored = routing_settings_service.read_organization_routing(org_slug, user_id=user["id"])
        if stored is None:
            raise _organization_not_found()
        return _body(stored)

    @router.put("/settings/routing", dependencies=[Depends(require_allowed_origin)])
    def put_organization_routing_settings(org_slug: str, user: WritingAdmin, body: RoutingSettingsRequest) -> Response:
        try:
            wanted = validate_routing_settings(body.routing.model_dump())
        except RoutingSettingsError as error:
            return _invalid(error.problems)

        stored = routing_settings_service.replace_organization_routing(org_slug, wanted, user_id=user["id"])
        if stored is None:
            raise _organization_not_found()

        # Who replaced which organization's routing, and how many destinations it now has. Never a label, the
        # home label or the request body.
        logger.info(
            "Routing settings replaced | org=%s user_id=%s destinations=%s",
            org_slug,
            user["id"],
            len(wanted.destinations),
        )
        return _body(stored)

    return router
