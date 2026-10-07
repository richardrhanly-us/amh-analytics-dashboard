"""Request and response models for the customer API's organization member routes.

A MEMBER IS IDENTIFIED BY EMAIL, in a request body -- never in a path, so an
address is not written to a URL or an access log. No user id and no
membership id has a field in any model here, in either direction.

Every request model refuses a field it does not name: a request cannot carry
a password, a user id, the actor's own role or anything else these routes do
not take. The length limits only bound what is read. Whether an address or a
role is acceptable is decided by services.user_admin_service, whose answer
the routes turn into the 422 problem list.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddMemberRequest(_Request):
    """Body of POST /api/organizations/{org_slug}/members.

    There is NO password field. An account created by this request gets a
    random password nobody is ever told; the person sets their own through
    the password reset ("Forgot password") flow. An address that already has
    an account keeps its password, its name and everything else."""

    email: str = Field(min_length=1, max_length=320)
    full_name: str = Field(default="", max_length=2000)
    role: str = Field(min_length=1, max_length=64)


class ChangeMemberRoleRequest(_Request):
    """Body of PUT /api/organizations/{org_slug}/members/role."""

    email: str = Field(min_length=1, max_length=320)
    role: str = Field(min_length=1, max_length=64)


class RemoveMemberRequest(_Request):
    """Body of POST /api/organizations/{org_slug}/members/remove."""

    email: str = Field(min_length=1, max_length=320)


class Member(_ResponseModel):
    """One ACTIVE member of the organization.

    `is_self` marks the signed-in user's own row. `account_active` is false
    for someone whose SortView account has been switched off: they cannot
    sign in, and do not count as an owner of anything.

    No user id, membership id, administrator flag, lockout state, sign-in
    time or creation date has a field here."""

    email: str
    full_name: str
    role: str
    is_self: bool
    account_active: bool


class MembersResponse(_ResponseModel):
    members: list[Member]


class MemberActivity(_ResponseModel):
    """One change to the organization's members. `occurred_at` is an instant
    written in UTC. `previous_role` is null for someone being added, `role`
    is null for someone being removed. `actor_email` is who made the change.

    No audit id, user id, message or raw metadata has a field here."""

    occurred_at: dt.datetime
    event_type: str
    member_email: str | None
    actor_email: str | None
    previous_role: str | None
    role: str | None


class MemberActivityResponse(_ResponseModel):
    activity: list[MemberActivity]
