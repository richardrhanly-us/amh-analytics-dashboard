"""An organization's members: who they are, and adding, changing and removing them.

    GET  /organizations/{org_slug}/members             the organization's active members
    POST /organizations/{org_slug}/members             add someone (or add back someone who was removed)
    PUT  /organizations/{org_slug}/members/role        give a member a different role
    POST /organizations/{org_slug}/members/remove      take a member out of THIS organization
    GET  /organizations/{org_slug}/members/activity    recent changes to the organization's members

WHO. Only the organization's OWNERS and ADMINS, for every route here. Being a
platform administrator grants nothing, as nowhere in this API.

    no session                                        401 not_authenticated
    not a member, or the organization is not visible  404 organization_not_found  (the organization routes' own answer)
    a member who is not an owner or admin             403 forbidden
    changing a suspended organization's members       403 organization_read_only  (they can still be listed)

THOSE ARE FOR A CLEAN ANSWER; THEY ARE NOT WHAT DECIDES. Every change is
decided by services.user_admin_service, inside its own locked transaction,
from the database: whether the person acting may, whether the organization
can be changed, whether only an owner may do this, and whether it would
leave the organization with no active owner. Nothing here repeats those
rules, and nothing a request says about who is acting is believed: the actor
is the session's user and no request has a field for one.

A MEMBER IS NAMED BY EMAIL, in the body. No user id or membership id is
taken or returned. The address is resolved to a member of THIS organization
only (services.user_admin_service.find_active_member_id), so nothing about
anyone's place in another organization can be learnt from an answer.

ADDING SOMEONE TAKES NO PASSWORD. If the address has no SortView account, one
is created with a long random password that is generated here and never
returned, logged or recorded: the person sets their own through the password
reset ("Forgot password") flow. If the address already has an account, that
account -- its password, name and state -- is left exactly as it is. The
answer is the same either way, so it does not say which happened. No email
is sent by these routes.

REMOVING SOMEONE is about this organization alone. Their account stays as it
is, and so do their other organizations. Nothing here can switch an account
off: that is a platform operation and no customer route reaches it.

A write carries the browser's Origin, checked first, exactly as for login
and logout (customer_api.auth_dependencies.require_allowed_origin), and
answers 204 with no body.

A route runs no SQL of its own.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from customer_api.auth_dependencies import require_allowed_origin
from customer_api.errors import (
    NO_STORE_HEADERS,
    CustomerApiError,
    CustomerApiRoute,
    logger,
)
from customer_api.member_schemas import (
    AddMemberRequest,
    ChangeMemberRoleRequest,
    Member,
    MemberActivity,
    MemberActivityResponse,
    MembersResponse,
    RemoveMemberRequest,
)
from customer_api.organization_routes import CurrentUser, require_organization_member
from services import access_service, entitlement_service, user_admin_service
from services.permission_service import ADMIN_ROLES

# The changes to an organization's members, as the audit log names them. Nothing else recorded against an
# organization is shown by the activity route -- and the limit counts these: the service selects by type BEFORE it
# orders and limits, so the answer is the most recent 50 membership changes, whatever else was recorded since.
_ACTIVITY_EVENT_TYPES = user_admin_service.MEMBERSHIP_EVENT_TYPES
_ACTIVITY_LIMIT = 50

# What services.user_admin_service refuses with, as this API answers it: status, code, and this API's OWN fixed
# message. A code that is not here (or under _PROBLEMS) is a server error -- never a guess.
_REFUSALS: dict[str, tuple[int, str, str]] = {
    "not_permitted": (403, "forbidden", "You do not have permission to manage this organization's members."),
    "owner_required": (403, "owner_required", "Only an owner of the organization can do that."),
    "organization_not_found": (404, "organization_not_found", "Organization not found."),
    "organization_not_editable": (403, "organization_read_only", "This organization's members cannot be changed."),
    "member_not_found": (404, "member_not_found", "That person is not a member of this organization."),
    "already_member": (409, "already_member", "That person is already a member of this organization."),
    "last_owner": (409, "last_owner", "An organization must keep at least one active owner."),
}
# What it says is wrong with a value, as the field it is about and a stable word for why.
_PROBLEMS: dict[str, tuple[str, str]] = {
    "invalid_role": ("role", "invalid"),
    "invalid_member_details": ("email", "invalid"),
}


def _refusal(code: str) -> CustomerApiError:
    status_code, api_code, message = _REFUSALS[code]
    return CustomerApiError(status_code, api_code, message)


def require_member_admin(org_slug: str, user: CurrentUser) -> dict[str, Any]:
    """The authenticated user, once it is established that they may see the
    organization in the path AND are an owner or admin of it. Someone who
    cannot see it gets the organization routes' own 404; a member with any
    other role gets 403.

    The role is the uncached lookup every permission check is built on; a
    membership that was removed gives none."""
    require_organization_member(org_slug, user)
    if entitlement_service.get_org_role_for_user(user_id=user["id"], org_slug=org_slug) not in ADMIN_ROLES:
        raise _refusal("not_permitted")
    return user


def require_writable_member_admin(org_slug: str, user: CurrentUser) -> dict[str, Any]:
    """require_member_admin, for a change: the organization must also have
    full access. An early, clean answer only -- the service refuses the same
    thing itself, under its lock."""
    require_member_admin(org_slug, user)
    if access_service.get_org_access_mode(org_slug) != "full":
        raise _refusal("organization_not_editable")
    return user


Admin = Annotated[dict[str, Any], Depends(require_member_admin)]
WritingAdmin = Annotated[dict[str, Any], Depends(require_writable_member_admin)]


def _generated_password() -> str:
    """A password for an account nobody is meant to sign in to with it: 48
    random bytes from the operating system. It goes to the service to be
    hashed and exists nowhere else."""
    return secrets.token_urlsafe(48)


def _answer(result: dict[str, Any]) -> Response:
    """The service's answer, as this API's: 204 for a change that was made,
    the mapped refusal otherwise. The service's own message is never used."""
    if result.get("ok"):
        return Response(status_code=204, headers=NO_STORE_HEADERS)

    code = result.get("code")
    if code in _REFUSALS:
        raise _refusal(code)
    if code in _PROBLEMS:
        field, problem = _PROBLEMS[code]
        return JSONResponse(
            status_code=422,
            content={
                "code": "invalid_member",
                "message": "The member details are not valid.",
                "problems": [{"field": field, "code": problem}],
            },
            headers=NO_STORE_HEADERS,
        )
    # member_not_added, or a code this module does not know: a server error. The code only -- no message.
    raise RuntimeError(f"user_admin_service answered with an unhandled code: {code}")


def _member_id(org_slug: str, email: str) -> int:
    """The active member of THIS organization with that address, for the
    service to act on -- or the one 404 for anyone else, whether they have
    no account, belong only to other organizations, or were removed."""
    user_id = user_admin_service.find_active_member_id(org_slug, email)
    if user_id is None:
        raise _refusal("member_not_found")
    return user_id


def _utc(instant: datetime) -> datetime:
    # The audit log's created_at is a TIMESTAMPTZ: an aware instant in the database session's zone. Written in UTC
    # here; a naive value is a fault and is refused, not guessed at (as customer_api.account_routes does).
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("an activity timestamp has no time zone")
    return instant.astimezone(UTC)


def _log_changed(change: str, org_slug: str, user: dict[str, Any]) -> None:
    # Who changed an organization's members, and how. Never an address, a name, a role's holder or a password.
    logger.info("Organization members changed | change=%s org=%s actor_user_id=%s", change, org_slug, user["id"])


def create_member_router() -> APIRouter:
    router = APIRouter(prefix="/organizations/{org_slug}/members", route_class=CustomerApiRoute)

    @router.get("")
    def list_members(org_slug: str, user: Admin) -> Response:
        # Built field by field through the response model, so nothing else in a user row can reach the browser.
        body = MembersResponse(
            members=[
                Member(
                    email=row["email"],
                    full_name=row["full_name"] or "",
                    role=row["role"],
                    is_self=row["user_id"] == user["id"],
                    account_active=bool(row["is_active"]),
                )
                for row in user_admin_service.list_org_users(org_slug)
            ]
        )
        return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)

    @router.post("", dependencies=[Depends(require_allowed_origin)])
    def add_member(org_slug: str, user: WritingAdmin, data: AddMemberRequest) -> Response:
        response = _answer(
            user_admin_service.add_organization_member(
                org_slug,
                data.email,
                _generated_password(),
                data.full_name,
                data.role,
                actor_user_id=user["id"],
            )
        )
        if response.status_code == 204:
            _log_changed("added", org_slug, user)
        return response

    @router.put("/role", dependencies=[Depends(require_allowed_origin)])
    def change_member_role(org_slug: str, user: WritingAdmin, data: ChangeMemberRoleRequest) -> Response:
        response = _answer(
            user_admin_service.change_organization_member_role(
                org_slug, _member_id(org_slug, data.email), data.role, actor_user_id=user["id"]
            )
        )
        if response.status_code == 204:
            _log_changed("role_changed", org_slug, user)
        return response

    @router.post("/remove", dependencies=[Depends(require_allowed_origin)])
    def remove_member(org_slug: str, user: WritingAdmin, data: RemoveMemberRequest) -> Response:
        response = _answer(
            user_admin_service.remove_organization_member(
                org_slug, _member_id(org_slug, data.email), actor_user_id=user["id"]
            )
        )
        if response.status_code == 204:
            _log_changed("removed", org_slug, user)
        return response

    @router.get("/activity")
    def list_member_activity(org_slug: str, user: Admin) -> Response:
        activity = []
        events = user_admin_service.list_recent_org_auth_events(
            org_slug, limit=_ACTIVITY_LIMIT, event_types=_ACTIVITY_EVENT_TYPES
        )
        for event in events:
            if event["event_type"] not in _ACTIVITY_EVENT_TYPES:  # the query's own condition, never relied on alone
                continue
            details = event["metadata"] if isinstance(event["metadata"], dict) else {}
            activity.append(
                MemberActivity(
                    occurred_at=_utc(event["created_at"]),
                    event_type=event["event_type"],
                    member_email=event["email"],
                    actor_email=details.get("actor_email"),
                    previous_role=details.get("previous_role"),
                    role=details.get("role"),
                )
            )
        return JSONResponse(content=MemberActivityResponse(activity=activity).model_dump(mode="json"), headers=NO_STORE_HEADERS)

    return router
