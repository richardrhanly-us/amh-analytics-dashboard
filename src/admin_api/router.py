"""The administration API's router: everything under /api/admin.

    GET /api/admin/session                 the signed-in platform administrator
    GET /api/admin/libraries               every library (admin_api.library_routes)
    GET /api/admin/libraries/{org_slug}    one library's read-only overview
    GET /api/admin/libraries/{org_slug}/sorters/{sorter_slug}/diagnostics
                                           one sorter site's collector facts (admin_api.diagnostics_routes)

WHO. Platform administrators only, decided on every request by the router's own dependencies (so a route added
later cannot leave them out):

    require_admin_origin     a state-changing request must come from SORTVIEW_ADMIN_ALLOWED_ORIGINS   403 origin_not_allowed
    require_platform_admin   no valid session                                                         401 not_authenticated
                             signed in, not a platform administrator                                   403 forbidden

Errors are the customer API's own {"code", "message"} shape (CustomerApiRoute): an unexpected failure is one generic
500, never reported as an authorization answer.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from admin_api.dependencies import require_admin_origin, require_platform_admin
from admin_api.diagnostics_routes import create_diagnostics_router
from admin_api.library_routes import create_library_router
from customer_api.auth_schemas import CurrentUserResponse
from customer_api.errors import NO_STORE_HEADERS, CustomerApiRoute

ADMIN_API_PREFIX = "/api/admin"

PlatformAdmin = Annotated[dict[str, Any], Depends(require_platform_admin)]


def create_admin_router() -> APIRouter:
    """Builds the admin router. Call once per application."""
    router = APIRouter(
        prefix=ADMIN_API_PREFIX,
        route_class=CustomerApiRoute,
        # Origin first, as on the customer API's write routes: a refused write never reaches the database.
        dependencies=[Depends(require_admin_origin), Depends(require_platform_admin)],
    )

    @router.get("/session")
    def admin_session(user: PlatformAdmin) -> Response:
        # The same three fields as GET /api/auth/session, built through the same model. That this answered at all
        # is what says the user is a platform administrator: the flag itself is never sent.
        body = CurrentUserResponse(id=user["id"], email=user["email"], full_name=user["full_name"])
        return JSONResponse(content=body.model_dump(), headers=NO_STORE_HEADERS)

    # Included, so its routes carry this router's dependencies as well as their own.
    router.include_router(create_library_router())
    router.include_router(create_diagnostics_router())

    return router
