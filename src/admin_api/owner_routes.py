"""Assigning a library's first owner in the administration API (G6).

    POST /api/admin/libraries/{org_slug}/first-owner     make this person the library's first owner

Included in the admin router (admin_api.router), so it inherits the platform-administrator check and the admin Origin
guard. What it may do is services.user_admin_service.assign_first_owner's, decided in its own locked transaction: only
while the library has no active owner, never for a platform administrator's or a switched-off account, and with the
audit record written in the same transaction.

    the two addresses differ, or neither is an address  422 invalid_owner  (the field and why, never the value)
    unknown library                                      404 library_not_found
    a suspended or cancelled library                     409 library_not_editable
    the library already has an active owner              409 owner_exists
    a platform administrator's account                   409 platform_admin_account
    a switched-off account                               409 account_inactive
    someone already a member of the library              409 already_member

A success answers 204 with no body. It says nothing about whether the address already had an account. No password is
taken, returned or logged: a new account gets a random one nobody is told, and the owner sets their own with "Forgot
password?" in the customer app. No email is sent here.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from admin_api.dependencies import require_platform_admin
from admin_api.owner_schemas import AssignFirstOwnerRequest
from customer_api.errors import (
    NO_STORE_HEADERS,
    CustomerApiError,
    CustomerApiRoute,
    logger,
)
from services import user_admin_service

PlatformAdmin = Annotated[dict[str, Any], Depends(require_platform_admin)]

# What the service refuses with, as this API answers it. A code that is not here is a server error -- never a guess.
_REFUSALS: dict[str, tuple[int, str, str]] = {
    "platform_admin_required": (403, "forbidden", "You do not have access to SortView administration."),
    "organization_not_found": (404, "library_not_found", "Library not found."),
    "organization_not_editable": (409, "library_not_editable", "This library is suspended or cancelled; its owner cannot be assigned."),
    "owner_exists": (409, "owner_exists", "This library already has an owner. Its owners add everyone else."),
    "platform_admin_account": (409, "platform_admin_account", "A platform administrator's account cannot be a library's owner."),
    "account_inactive": (409, "account_inactive", "That account is switched off, so it cannot be made an owner."),
    "already_member": (409, "already_member", "That person already belongs to this library."),
}


def _generated_password() -> str:
    """A password for an account nobody is meant to sign in to with it: 48 random bytes from the operating system. It
    goes to the service to be hashed and exists nowhere else."""
    return secrets.token_urlsafe(48)


def _invalid(field: str, code: str) -> Response:
    return JSONResponse(
        status_code=422,
        content={"code": "invalid_owner", "message": "The owner's details are not valid.",
                 "problems": [{"field": field, "code": code}]},
        headers=NO_STORE_HEADERS,
    )


def create_owner_router() -> APIRouter:
    router = APIRouter(route_class=CustomerApiRoute)

    @router.post("/libraries/{org_slug}/first-owner")
    def assign_first_owner(org_slug: str, user: PlatformAdmin, body: AssignFirstOwnerRequest) -> Response:
        email = body.email.strip().lower()
        if "@" not in email:
            return _invalid("email", "invalid")
        if body.email_confirmation.strip().lower() != email:
            return _invalid("email_confirmation", "mismatch")
        result = user_admin_service.assign_first_owner(
            org_slug, email, _generated_password(), body.full_name.strip(), actor_user_id=user["id"],
        )
        if not result["ok"]:
            if result["code"] == "invalid_member_details":
                return _invalid("email", "invalid")
            status_code, code, message = _REFUSALS[result["code"]]
            raise CustomerApiError(status_code, code, message)
        # Never the address: the audit record holds it, with who acted.
        logger.info("Admin first owner assigned | org=%s user_id=%s action=assign_first_owner", org_slug, user["id"])
        return Response(status_code=204, headers=NO_STORE_HEADERS)

    return router
