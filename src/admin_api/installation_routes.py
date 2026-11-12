"""A library's Collector installations and their enrollment codes in the administration API (A2 item 5).

    GET  /api/admin/libraries/{org_slug}/installations                               every installation
    POST /api/admin/libraries/{org_slug}/installations                               add one at the primary branch
    PUT  /api/admin/libraries/{org_slug}/installations/{installation_id}             rename it
    PUT  /api/admin/libraries/{org_slug}/installations/{installation_id}/status      deactivate, return to
                                                                                     provisioning, or retire it
    POST /api/admin/libraries/{org_slug}/installations/{installation_id}/enrollment-codes
                                                                                     issue a one-time code
    POST /api/admin/libraries/{org_slug}/installations/{installation_id}/recovery-token
                                                                                     replace its agent token, once
    GET  /api/admin/libraries/{org_slug}/installations/unbound-tokens                the legacy unbound tokens
    POST /api/admin/libraries/{org_slug}/installations/unbound-tokens/{token_id}/revoke
                                                                                     revoke one of them
    POST /api/admin/libraries/{org_slug}/installations/{installation_id}/replacements
                                                                                     authorize replacing it (C6a R1)
    POST /api/admin/libraries/{org_slug}/installations/{installation_id}/cancel-replacement
                                                                                     cancel this pending replacement

Included in the admin router (admin_api.router), so every route inherits its platform-administrator check and the
admin Origin guard on the writes. Every answer is no-store. What each operation may do is
services.admin_installation_service's.

    unknown library                                        404 library_not_found
    an installation that is not this library's             404 installation_not_found
    a cancelled library changed                            409 library_cancelled
    no active primary branch to add at                     409 no_active_primary_branch
    contract 2 without the library's Collector settings    409 collector_settings_not_ready
    a status change the installation cannot make           409 transition_not_allowed
    retiring an installation that owns an active key       409 installation_owns_active_key
    a code the installation cannot have                    409 installation_not_enrollable | library_not_enrollable |
                                                               operational_identity_incomplete | branch_already_enrolled
    a recovery token the installation cannot have          409 installation_not_recoverable
    a token that is not an active unbound one of this
    library                                                404 token_not_found
    retiring a pending replacement by status               409 use_cancel_replacement
    a replacement that cannot be authorized                409 replacement_source_retired | replacement_source_not_contract_2 |
                                                               replacement_source_not_at_primary_branch |
                                                               replacement_source_not_key_owner |
                                                               replacement_branch_not_enrolled | unbound_tokens_active |
                                                               replacement_already_pending |
                                                               lost_response_confirmation_required |
                                                               operational_identity_incomplete | no_active_primary_branch |
                                                               collector_settings_not_ready
    a code for a replacement whose source is no longer
    eligible                                               409 the same replacement_* | unbound_tokens_active codes
    cancelling what is not a pending replacement           409 not_a_pending_replacement
    a value refused                                        422 with the field and why, never the value

A REPLACEMENT IS ONLY AUTHORIZED HERE (C6a R1). Its enrollment code cannot be redeemed until R2: the enrollment refuses it
with enrollment_not_ready and consumes nothing. No old credential is retired and no replacement machine is activated.

THE RAW ENROLLMENT CODE AND THE RAW RECOVERY TOKEN are each in the 201 answer to the request that issued them and nowhere
else: they are not logged, audited, stored or shown again.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from admin_api.dependencies import require_platform_admin
from admin_api.installation_schemas import (
    AddInstallationRequest,
    AuthorizeReplacementRequest,
    EnrollmentCodeResponse,
    Installation,
    InstallationBranch,
    InstallationRef,
    InstallationsLibrary,
    InstallationsResponse,
    InstallationStatusRequest,
    PrimaryBranchSummary,
    RecoveryTokenResponse,
    RenameInstallationRequest,
    ReplacementResponse,
    RevokedUnboundToken,
    UnboundToken,
    UnboundTokensResponse,
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
    "installation_owns_active_key": (
        409, "This installation owns its branch's active ingest key, so it cannot be retired here. Deactivate it instead.",
    ),
    "installation_not_recoverable": (
        409,
        "A recovery token needs a Provisioning or Active Contract 2 installation that owns its branch's only active ingest key.",
    ),
    "token_not_found": (404, "Unbound token not found."),
    "use_cancel_replacement": (409, "This installation is a pending replacement. Use Cancel replacement instead."),
    "replacement_source_retired": (409, "A retired installation cannot be replaced."),
    "replacement_source_not_contract_2": (409, "Only a Contract 2 installation can be replaced."),
    "replacement_source_not_at_primary_branch": (
        409, "Only an installation at the library's active primary branch can be replaced.",
    ),
    "replacement_source_not_key_owner": (
        409, "Only the installation that owns its branch's only active ingest key can be replaced.",
    ),
    "replacement_branch_not_enrolled": (409, "This branch has no Contract 2 enrollment to replace."),
    "unbound_tokens_active": (
        409, "This branch still has an active legacy unbound token. Revoke it before authorizing a replacement.",
    ),
    "replacement_already_pending": (409, "This branch already has a pending replacement. Cancel it first."),
    "lost_response_confirmation_required": (
        409,
        (
            "This installation is still Provisioning. Confirm that its enrollment response was lost and setup cannot "
            "resume (try Resume setup and the recovery token first)."
        ),
    ),
    "not_a_pending_replacement": (409, "This installation is not a pending replacement."),
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

    @router.post("/{installation_id}/recovery-token")
    def issue_recovery_token(org_slug: str, installation_id: int, user: PlatformAdmin) -> Response:
        library = _library(org_slug)
        try:
            issued = service.issue_recovery_token(library, installation_id, actor=user)
        except InstallationAdminError as refused:
            raise _refusal(refused) from None
        # Never the token: the enrollment service has already logged the token's row id.
        logger.info("Admin recovery token issued | org=%s user_id=%s installation_id=%s",
                    library.slug, user["id"], installation_id)
        body = RecoveryTokenResponse(
            installation=InstallationRef(**issued["installation"]),
            agent_token=issued["agent_token"],
            revoked_previous_count=issued["revoked_previous_count"],
        )
        return _json(body, 201)

    @router.get("/unbound-tokens")
    def list_unbound_tokens(org_slug: str) -> Response:
        library = _library(org_slug)
        return _json(UnboundTokensResponse(tokens=[UnboundToken(**row) for row in service.list_unbound_tokens(library)]))

    @router.post("/unbound-tokens/{token_id}/revoke")
    def revoke_unbound_token(org_slug: str, token_id: int, user: PlatformAdmin) -> Response:
        library = _library(org_slug)
        try:
            service.revoke_unbound_token(library, token_id, actor=user)
        except InstallationAdminError as refused:
            raise _refusal(refused) from None
        logger.info("Admin unbound token revoked | org=%s user_id=%s token_id=%s", library.slug, user["id"], token_id)
        return _json(RevokedUnboundToken(id=token_id))

    @router.post("/{installation_id}/replacements")
    def authorize_replacement(
        org_slug: str, installation_id: int, user: PlatformAdmin, body: AuthorizeReplacementRequest
    ) -> Response:
        library = _library(org_slug)
        try:
            name = service.validate_name(body.name)
        except ValueError as invalid:
            return _invalid_name(str(invalid))
        try:
            authorized = service.authorize_replacement(
                library, installation_id, name=name, confirm_lost_response=body.confirm_lost_response, actor=user,
            )
        except InstallationAdminError as refused:
            raise _refusal(refused) from None
        logger.info("Admin replacement authorized | org=%s user_id=%s installation_id=%s replaces_installation_id=%s",
                    library.slug, user["id"], authorized["installation"]["id"], installation_id)
        return _json(ReplacementResponse(installation=_installation(authorized["installation"]),
                                         replaces_installation_id=authorized["replaces_installation_id"]), 201)

    @router.post("/{installation_id}/cancel-replacement")
    def cancel_replacement(org_slug: str, installation_id: int, user: PlatformAdmin) -> Response:
        library = _library(org_slug)
        try:
            cancelled = service.cancel_replacement(library, installation_id, actor=user)
        except InstallationAdminError as refused:
            raise _refusal(refused) from None
        logger.info("Admin replacement cancelled | org=%s user_id=%s installation_id=%s",
                    library.slug, user["id"], installation_id)
        return _json(ReplacementResponse(installation=_installation(cancelled["installation"]),
                                         replaces_installation_id=cancelled["replaces_installation_id"]))

    return router
