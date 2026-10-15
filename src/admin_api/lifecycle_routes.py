"""A library's lifecycle in the administration API (A2 item 6).

    POST /api/admin/libraries/{org_slug}/lifecycle/suspend      active/trial -> suspended (reversible)
    POST /api/admin/libraries/{org_slug}/lifecycle/reactivate   suspended -> active (a trial label is not restored)
    POST /api/admin/libraries/{org_slug}/lifecycle/offboard     -> cancelled, every access artifact revoked (permanent)

Included in the admin router (admin_api.router), so every route inherits its platform-administrator check and the
admin Origin guard. Every answer is no-store. What each action does is services.platform_admin_service's -- the same
two functions Super Admin calls -- run with the administrator as `admin_actor`, so the change and its auth_audit_log row
commit together. There is no generic status write.

    unknown library                                  404 library_not_found
    a cancelled library suspended, reactivated or
    offboarded again                                 409 library_cancelled
    a status the service does not recognise          409 invalid_transition
    an offboarding confirmation that is not the
    library's slug (or is blank)                     422 confirmation_mismatch   (the typed value is never echoed)

Suspending a suspended library, or reactivating an active or trial one, is 200 with `changed: false` and writes nothing.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from admin_api.dependencies import require_platform_admin
from admin_api.lifecycle_schemas import (
    LifecycleLibrary,
    LifecycleResponse,
    OffboardCounts,
    OffboardRequest,
    OffboardResponse,
)
from customer_api.errors import (
    NO_STORE_HEADERS,
    CustomerApiError,
    CustomerApiRoute,
    logger,
)
from services import admin_installation_service, platform_admin_service
from services.admin_installation_service import Library
from services.platform_admin_service import LibraryLifecycleError

PlatformAdmin = Annotated[dict[str, Any], Depends(require_platform_admin)]

_MESSAGES = {
    "library_not_found": (404, "Library not found."),
    "library_cancelled": (409, "This library is cancelled; its lifecycle cannot be changed."),
    "invalid_transition": (409, "This library cannot be changed from its current status."),
}


def _refusal(code: str) -> CustomerApiError:
    status, message = _MESSAGES[code]
    return CustomerApiError(status, code, message)


def _library(org_slug: str) -> Library:
    library = admin_installation_service.get_library(org_slug)
    if library is None:
        raise _refusal("library_not_found")
    return library


def _json(model: Any) -> Response:
    return JSONResponse(content=model.model_dump(mode="json"), headers=NO_STORE_HEADERS)


def create_lifecycle_router() -> APIRouter:
    router = APIRouter(prefix="/libraries/{org_slug}/lifecycle", route_class=CustomerApiRoute)

    def _set_active(org_slug: str, user: dict[str, Any], *, is_active: bool) -> Response:
        library = _library(org_slug)
        try:
            result = platform_admin_service.set_library_active_status(library.id, is_active, admin_actor=user)
        except LibraryLifecycleError as refused:
            raise _refusal(refused.code) from None
        logger.info("Admin library lifecycle | org=%s user_id=%s action=%s changed=%s",
                    library.slug, user["id"], "reactivate" if is_active else "suspend", result["changed"])
        return _json(LifecycleResponse(
            library=LifecycleLibrary(slug=library.slug, name=library.name, status=result["status"]),
            previous_status=result["previous_status"],
            changed=result["changed"],
        ))

    @router.post("/suspend")
    def suspend(org_slug: str, user: PlatformAdmin) -> Response:
        return _set_active(org_slug, user, is_active=False)

    @router.post("/reactivate")
    def reactivate(org_slug: str, user: PlatformAdmin) -> Response:
        return _set_active(org_slug, user, is_active=True)

    @router.post("/offboard")
    def offboard(org_slug: str, user: PlatformAdmin, body: OffboardRequest) -> Response:
        library = _library(org_slug)
        if library.status == "cancelled":
            raise _refusal("library_cancelled")
        # Trimmed, as Super Admin and the service compare it. The service checks it again under its lock.
        if body.confirm_slug.strip() != library.slug:
            raise CustomerApiError(422, "confirmation_mismatch", "The confirmation does not match this library's slug.")
        try:
            result = platform_admin_service.offboard_library(
                library.id,
                body.confirm_slug,
                actor_user_id=int(user["id"]),
                actor_label=f"platform_admin:{int(user['id'])}",
                admin_actor=user,
            )
        except LibraryLifecycleError as refused:
            raise _refusal(refused.code) from None
        counts = result["counts"]
        logger.info("Admin library lifecycle | org=%s user_id=%s action=offboard changed=%s tokens=%s installations=%s "
                    "codes=%s keys=%s sessions=%s",
                    library.slug, user["id"], result["changed"], counts["agent_tokens_deactivated"],
                    counts["installations_retired"], counts["enrollment_codes_revoked"], counts["ingest_keys_retired"],
                    counts["sessions_revoked"])
        return _json(OffboardResponse(
            library=LifecycleLibrary(slug=library.slug, name=library.name, status="cancelled"),
            previous_status=result["status_before"],
            changed=result["changed"],
            counts=OffboardCounts(**counts),
        ))

    return router
