"""Provisioning a library, and assigning an existing library's operational identity, in the administration API (A2
item 7).

    GET  /api/admin/plans                                    the active plans a library can be provisioned on
    POST /api/admin/libraries                                provision a library (one transaction)
    POST /api/admin/libraries/{org_slug}/operational-identity
                                                             assign a library's missing operational identity

Included in the admin router (admin_api.router), so every route inherits its platform-administrator check and the
admin Origin guard on the writes. Every answer is no-store. What each operation does is services.tenant_service's
(provision_library, assign_library_operational_identity).

    a value refused                                  422 invalid_library   (each field and why, never the value)
    an organization slug already in use              409 library_slug_taken
    a plan that is not an active plan                409 plan_not_available
    unknown library                                  404 library_not_found
    a cancelled library                              409 library_cancelled
    no single primary branch                         409 no_primary_branch
    an existing mapping that contradicts itself      409 operational_identity_inconsistent

Provisioning creates no user, membership, installation, enrollment code, token, ingest key or cutover.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from admin_api.dependencies import require_platform_admin
from admin_api.provisioning_schemas import (
    OperationalIdentityResponse,
    Plan,
    PlansResponse,
    ProvisionLibraryRequest,
    ProvisionLibraryResponse,
)
from customer_api.errors import (
    NO_STORE_HEADERS,
    CustomerApiError,
    CustomerApiRoute,
    logger,
)
from services import tenant_service
from services.tenant_service import AdminProvisioningError

PlatformAdmin = Annotated[dict[str, Any], Depends(require_platform_admin)]

_MESSAGES = {
    "library_slug_taken": (409, "A library with this slug already exists."),
    "plan_not_available": (409, "This plan is not available for a new library."),
    "library_not_found": (404, "Library not found."),
    "library_cancelled": (409, "This library is cancelled; its operational identity cannot be changed."),
    "no_primary_branch": (409, "This library has no single primary branch to assign an operational identity to."),
    "operational_identity_inconsistent": (
        409, "This library's existing operational identity is inconsistent. It was not changed; contact SortView engineering.",
    ),
}


def _refusal(error: AdminProvisioningError) -> CustomerApiError:
    status, message = _MESSAGES[error.code]
    return CustomerApiError(status, error.code, message)


def _json(model: Any, status_code: int = 200) -> Response:
    return JSONResponse(status_code=status_code, content=model.model_dump(mode="json"), headers=NO_STORE_HEADERS)


def create_provisioning_router() -> APIRouter:
    router = APIRouter(route_class=CustomerApiRoute)

    @router.get("/plans")
    def list_plans() -> Response:
        plans = [Plan(code=str(plan["code"]), name=str(plan["name"])) for plan in tenant_service.list_active_plans()]
        return _json(PlansResponse(plans=plans))

    @router.post("/libraries")
    def provision_library(user: PlatformAdmin, body: ProvisionLibraryRequest) -> Response:
        values, problems = tenant_service.validate_provisioning(
            name=body.name, slug=body.slug, branch_name=body.branch_name, branch_slug=body.branch_slug,
            plan_code=body.plan_code,
        )
        if problems:
            return JSONResponse(
                status_code=422,
                content={"code": "invalid_library", "message": "Library details are invalid.", "problems": problems},
                headers=NO_STORE_HEADERS,
            )
        try:
            created = tenant_service.provision_library(**values, actor=user)
        except AdminProvisioningError as refused:
            raise _refusal(refused) from None
        logger.info("Admin library provisioned | org=%s user_id=%s action=provision", created["library"]["slug"], user["id"])
        return _json(ProvisionLibraryResponse.model_validate(created), 201)

    @router.post("/libraries/{org_slug}/operational-identity")
    def assign_operational_identity(org_slug: str, user: PlatformAdmin) -> Response:
        try:
            assigned = tenant_service.assign_library_operational_identity(org_slug, actor=user)
        except AdminProvisioningError as refused:
            raise _refusal(refused) from None
        logger.info("Admin operational identity | org=%s user_id=%s action=assign changed=%s",
                    org_slug, user["id"], assigned["changed"])
        return _json(OperationalIdentityResponse(operational_identity="assigned", changed=assigned["changed"]))

    return router
