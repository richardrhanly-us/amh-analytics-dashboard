"""A library's Collector installations and their enrollment codes in the administration API (A2 item 5).

    GET  /api/admin/libraries/{org_slug}/installations                               every installation
    POST /api/admin/libraries/{org_slug}/installations                               add one at the primary branch
    PUT  /api/admin/libraries/{org_slug}/installations/{installation_id}             rename it
    PUT  /api/admin/libraries/{org_slug}/installations/{installation_id}/status      deactivate, return to
                                                                                     provisioning, or retire it
    POST /api/admin/libraries/{org_slug}/installations/{installation_id}/enrollment-codes
                                                                                     issue a one-time code

Included in the admin router (admin_api.router), so every route inherits its platform-administrator check and the
admin Origin guard on the writes. Every answer is no-store. What each operation may do is
services.admin_installation_service's.

    unknown library                                        404 library_not_found
    an installation that is not this library's             404 installation_not_found
    a cancelled library changed                            409 library_cancelled
    no active primary branch to add at                     409 no_active_primary_branch
    contract 2 without the library's Collector settings    409 collector_settings_not_ready
    a status change the installation cannot make           409 transition_not_allowed
    a code the installation cannot have                    409 installation_not_enrollable | library_not_enrollable |
                                                               operational_identity_incomplete | branch_already_enrolled
    a value refused                                        422 with the field and why, never the value

THE RAW ENROLLMENT CODE is in the 201 answer to the request that issued it and nowhere else: it is not logged, audited,
stored or shown again.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from admin_api.dependencies import require_platform_admin
from admin_api.installation_schemas import (
    AddInstallationRequest,
    EnrollmentCodeResponse,
    Installation,
    InstallationBranch,
    InstallationRef,
    InstallationsLibrary,
    InstallationsResponse,
    InstallationStatusRequest,
    PrimaryBranchSummary,
    RenameInstallationRequest,
)
from customer_api.errors import (
    NO_STORE_HEADERS,
    CustomerApiError,
    CustomerApiRoute,
    logger,
)
from services import admin_installation_service as service
from services.admin_installation_service import InstallationAdminError, Library

PlatformAdmin = Annotated[dict[str, Any], Depends(require_platform_admin)]

_MESSAGES = {
    "installation_not_found": (404, "Installation not found."),
    "library_cancelled": (409, "This library is cancelled; its installations cannot be changed."),
    "no_active_primary_branch": (409, "This library has no active primary branch to add an installation at."),
    "collector_settings_not_ready": (
        409, "A Contract 2 installation needs this library's Collector time zone and routing review first.",
    ),
    "transition_not_allowed": (409, "This installation cannot be changed to that status."),
    "installation_not_enrollable": (409, "This installation cannot be given an enrollment code in its current status."),
    "library_not_enrollable": (409, "This library cannot enroll a Collector in its current status."),
    "operational_identity_incomplete": (409, "This library's operational identity is not complete."),
    "branch_already_enrolled": (
        409, "This branch already has an enrolled Contract 2 Collector. Replacing it is not handled here.",
    ),
}


def _refusal(error: InstallationAdminError) -> CustomerApiError:
    status, message = _MESSAGES[error.code]
    return CustomerApiError(status, error.code, message)


def _library(org_slug: str) -> Library:
    library = service.get_library(org_slug)
    if library is None:
        raise CustomerApiError(404, "library_not_found", "Library not found.")
    return library


def _invalid_name(code: str) -> Response:
    return JSONResponse(
        status_code=422,
        content={"code": "invalid_installation", "message": "The installation is not valid.",
                 "problems": [{"field": "name", "code": code}]},
        headers=NO_STORE_HEADERS,
    )


def _installation(found: dict[str, Any]) -> Installation:
    branch = found["branch"]
    return Installation(
        id=found["id"],
        name=found["name"],
        branch=InstallationBranch(slug=branch.slug, name=branch.name, is_primary=branch.is_primary, status=branch.status),
        status=found["status"],
        required_contract=found["required_contract"],
        installed_at=found["installed_at"],
        last_seen_at=found["last_seen_at"],
        created_at=found["created_at"],
        pending_code_expires_at=found["pending_code_expires_at"],
        last_code_redeemed_at=found["last_code_redeemed_at"],
    )


def _json(model: Any, status_code: int = 200) -> Response:
    return JSONResponse(status_code=status_code, content=model.model_dump(mode="json"), headers=NO_STORE_HEADERS)


def create_installation_router() -> APIRouter:
    router = APIRouter(prefix="/libraries/{org_slug}/installations", route_class=CustomerApiRoute)

    @router.get("")
    def list_installations(org_slug: str) -> Response:
        library = _library(org_slug)
        primary = service.get_primary_branch(library)
        body = InstallationsResponse(
            library=InstallationsLibrary(slug=library.slug, name=library.name, status=library.status),
            primary_branch=None if primary is None else PrimaryBranchSummary(
                slug=primary.slug, name=primary.name, status=primary.status,
            ),
            add_problems=service.add_problems(library, primary),
            installations=[_installation(found) for found in service.list_installations(library)],
        )
        return _json(body)

    @router.post("")
    def add_installation(org_slug: str, user: PlatformAdmin, body: AddInstallationRequest) -> Response:
        library = _library(org_slug)
        try:
            name = service.validate_name(body.name)
        except ValueError as invalid:
            return _invalid_name(str(invalid))
        try:
            created = service.create_installation(library, name=name, required_contract=body.required_contract, actor=user)
        except InstallationAdminError as refused:
            raise _refusal(refused) from None
        logger.info("Admin installation added | org=%s user_id=%s installation_id=%s contract=%s",
                    library.slug, user["id"], created["id"], created["required_contract"])
        return _json(_installation(created), 201)

    @router.put("/{installation_id}")
    def rename_installation(org_slug: str, installation_id: int, user: PlatformAdmin, body: RenameInstallationRequest) -> Response:
        library = _library(org_slug)
        try:
            name = service.validate_name(body.name)
        except ValueError as invalid:
            return _invalid_name(str(invalid))
        try:
            renamed = service.rename_installation(library, installation_id, name=name, actor=user)
        except InstallationAdminError as refused:
            raise _refusal(refused) from None
        logger.info("Admin installation renamed | org=%s user_id=%s installation_id=%s",
                    library.slug, user["id"], installation_id)
        return _json(_installation(renamed))

    @router.put("/{installation_id}/status")
    def change_status(org_slug: str, installation_id: int, user: PlatformAdmin, body: InstallationStatusRequest) -> Response:
        library = _library(org_slug)
        try:
            changed = service.change_status(library, installation_id, status=body.status, actor=user)
        except InstallationAdminError as refused:
            raise _refusal(refused) from None
        logger.info("Admin installation status changed | org=%s user_id=%s installation_id=%s status=%s",
                    library.slug, user["id"], installation_id, body.status)
        return _json(_installation(changed))

    @router.post("/{installation_id}/enrollment-codes")
    def issue_enrollment_code(org_slug: str, installation_id: int, user: PlatformAdmin) -> Response:
        library = _library(org_slug)
        try:
            issued = service.issue_enrollment_code(library, installation_id, actor=user)
        except InstallationAdminError as refused:
            raise _refusal(refused) from None
        # Never the code: the enrollment service has already logged the code's row id and timing.
        logger.info("Admin enrollment code issued | org=%s user_id=%s installation_id=%s",
                    library.slug, user["id"], installation_id)
        body = EnrollmentCodeResponse(
            installation=InstallationRef(**issued["installation"]),
            required_contract=issued["required_contract"],
            enrollment_code=issued["enrollment_code"],
            expires_at=issued["expires_at"],
            ttl_minutes=issued["ttl_minutes"],
            revoked_previous_count=issued["revoked_previous_count"],
        )
        return _json(body, 201)

    return router
