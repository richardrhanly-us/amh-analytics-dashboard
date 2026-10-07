"""Administering who belongs to an organization, and -- separately -- whether
an account can be used at all.

    list_org_users(org_slug)                                              the organization's ACTIVE members
    add_organization_member(org_slug, email, password, full_name, role, actor_user_id=...)
    change_organization_member_role(org_slug, user_id, role, actor_user_id=...)
    remove_organization_member(org_slug, user_id, actor_user_id=...)
    list_recent_org_auth_events(org_slug, limit)                          what happened IN this organization

    set_global_account_active(user_id, is_active, actor_user_id=...)      platform administrators only

TWO DIFFERENT THINGS, KEPT APART.

    a MEMBERSHIP   one person's place in ONE organization: a role, and whether it has been removed
                   (memberships.removed_at). Everything an organization's own administrators do is about
                   memberships of THEIR organization, and touches nothing else.
    an ACCOUNT     the person's sign-in (app_users). app_users.is_active is account-wide: switching it off locks
                   the person out of EVERY organization and ends all their sessions. That is a platform
                   operation, and no organization-level function here does it or can be made to.

So removing someone from an organization is an UPDATE of one membership row. Their account, their password, their
sessions and their memberships of other organizations are exactly as they were. No session is revoked, and none
needs to be: every request decides access from the membership as it is in the database at that moment
(services.access_service, services.entitlement_service, services.tenant_resolution_service), and a removed
membership is, to all of them, no membership.

EVERY FUNCTION THAT CHANGES SOMETHING DECIDES FOR ITSELF WHETHER THE ACTOR MAY. The caller says WHO is acting
(`actor_user_id`); what that person is in the organization is read here, from the database, inside the same
transaction as the change. Nothing a caller says about the actor's role is believed, so these are as safe called
from a page, an API route, a script or a test. The rules are services.membership_policy's.

AN ORGANIZATION ALWAYS HAS AN ACTIVE OWNER. An active owner is a membership with role 'owner' that has not been
removed, of an account that is active. Anything that would leave an organization with none is refused: taking the
role away, removing the membership, or deactivating the account.

HOW THAT IS MADE SAFE AGAINST TWO CHANGES AT ONCE. Every change that could reduce an organization's owners first
locks that organization's row (SELECT ... FOR UPDATE on PostgreSQL -- the convention of
services.platform_admin_service), then counts, then writes, in one transaction. Two such changes to one organization
therefore happen one after the other, and the second counts what the first left. Deactivating an account can affect
several organizations at once, so it locks every organization the account actively owns, in ascending id order --
always the same order, so two of them cannot deadlock on each other.

THE RECORD of a change -- who acted, in which organization, on whom, from what to what -- is written to the auth
audit log in the same transaction as the change (auth_service.log_auth_event_with_connection): there is no change
without its record and no record of a change that did not happen. It holds ids, e-mail addresses and roles: never
a password, a hash or a token.

Every function returns a dict: {"ok": True, "message": ...} or {"ok": False, "code": ..., "message": ...}. `code`
is stable and is what a caller should branch on; `message` is fixed text for a person. Nothing here knows what a
web framework is. Database errors propagate.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from database import get_engine
from services import auth_service, membership_policy, session_service
from services.membership_policy import MembershipPolicyError
from services.privacy_hardening import log_safe_exception

logger = logging.getLogger("sortview.user_admin")

ALLOWED_MEMBERSHIP_ROLES = list(membership_policy.ROLES)

# Why something was refused, beyond the policy's own codes (membership_policy.NOT_PERMITTED, OWNER_REQUIRED,
# INVALID_ROLE).
ORGANIZATION_NOT_FOUND = "organization_not_found"
ORGANIZATION_NOT_EDITABLE = "organization_not_editable"
MEMBER_NOT_FOUND = "member_not_found"
ALREADY_MEMBER = "already_member"
LAST_OWNER = "last_owner"
INVALID_MEMBER_DETAILS = "invalid_member_details"
MEMBER_NOT_ADDED = "member_not_added"
PLATFORM_ADMIN_REQUIRED = "platform_admin_required"
USER_NOT_FOUND = "user_not_found"
PLATFORM_ADMIN_ACCOUNT = "platform_admin_account"
CONCURRENT_CHANGE = "concurrent_change"

# Shown by the admin Users page; fixed text, never an exception's own message.
USER_CREATE_FAILED_MESSAGE = (
    "The user could not be created. Please check the details and try again. "
    "If this keeps happening, contact SortView support."
)

_MESSAGES: dict[str, str] = {
    **membership_policy.MESSAGES,
    ORGANIZATION_NOT_FOUND: "Organization not found.",
    ORGANIZATION_NOT_EDITABLE: "This organization does not currently allow administrative changes.",
    MEMBER_NOT_FOUND: "That user is not a member of this organization.",
    ALREADY_MEMBER: "That user already belongs to this organization.",
    LAST_OWNER: "An organization must keep at least one active owner. Make someone else an owner first.",
    INVALID_MEMBER_DETAILS: "Enter an email address and a temporary password.",
    MEMBER_NOT_ADDED: USER_CREATE_FAILED_MESSAGE,
    PLATFORM_ADMIN_REQUIRED: "Only a platform administrator can change whether an account is active.",
    USER_NOT_FOUND: "User not found.",
    PLATFORM_ADMIN_ACCOUNT: "Platform administrator accounts cannot be changed here.",
    CONCURRENT_CHANGE: "This account's organizations changed while it was being updated. Nothing was changed; try again.",
}

# The organization statuses under which its members may be administered: the ones access_service.get_org_access_mode
# calls "full". A suspended organization can be read and not changed; a cancelled one is gone.
_EDITABLE_STATUSES = ("active", "trial")


def _refused(code: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": _MESSAGES[code]}


def _is_postgresql(conn: Any) -> bool:
    # Row locks are PostgreSQL's; SQLite (unit tests) has none and serializes writers itself.
    return getattr(getattr(conn, "dialect", None), "name", "") == "postgresql"


class _Refused(Exception):
    """Raised inside a transaction to abandon it: nothing is written, and the code is what the caller is told."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- reading what a decision needs, on the transaction that will make the change ---------------------------------

_ORGANIZATION_BY_SLUG_SQL = """
    SELECT id, slug, status
    FROM organizations
    WHERE slug = :org_slug
"""

_ORGANIZATION_BY_ID_SQL = """
    SELECT id
    FROM organizations
    WHERE id = :organization_id
"""

# A person's membership of one organization, removed or not, and the state of their account.
_MEMBERSHIP_SQL = """
    SELECT
        m.id AS membership_id,
        m.role,
        m.removed_at,
        u.email,
        u.is_active
    FROM memberships m
    JOIN app_users u
      ON u.id = m.user_id
    WHERE m.organization_id = :organization_id
      AND m.user_id = :user_id
"""

# An organization's ACTIVE owners: the role, a membership that has not been removed, an account that is active.
_ACTIVE_OWNER_COUNT_SQL = """
    SELECT COUNT(*)
    FROM memberships m
    JOIN app_users u
      ON u.id = m.user_id
    WHERE m.organization_id = :organization_id
      AND m.role = 'owner'
      AND m.removed_at IS NULL
      AND u.is_active = TRUE
"""


def _lock_organization(conn: Any, org_slug: str) -> dict[str, Any]:
    """The organization, with its row locked for the rest of the transaction: every owner-sensitive change to one
    organization goes through here first, so they happen one at a time."""
    lock = " FOR UPDATE" if _is_postgresql(conn) else ""
    organization = conn.execute(text(_ORGANIZATION_BY_SLUG_SQL + lock), {"org_slug": org_slug}).mappings().first()
    if organization is None:
        raise _Refused(ORGANIZATION_NOT_FOUND)
    if organization["status"] not in _EDITABLE_STATUSES:
        raise _Refused(ORGANIZATION_NOT_EDITABLE)
    return dict(organization)


def _membership(conn: Any, organization_id: int, user_id: int) -> dict[str, Any] | None:
    row = conn.execute(text(_MEMBERSHIP_SQL), {"organization_id": organization_id, "user_id": user_id}).mappings().first()
    return dict(row) if row else None


def _active_role(membership: dict[str, Any] | None) -> str | None:
    """The role a membership gives NOW: none if there is no membership, or it was removed. The role on a removed
    row is history and gives nothing."""
    return None if membership is None or membership["removed_at"] is not None else membership["role"]


def _actor(conn: Any, organization_id: int, actor_user_id: int) -> dict[str, Any]:
    """Who is acting, as the database has them at this moment: their active role in this organization (None if
    they have none, or their account is not active) and their e-mail, for the record."""
    membership = _membership(conn, organization_id, actor_user_id)
    usable = membership is not None and bool(membership["is_active"])
    return {
        "user_id": actor_user_id,
        "email": membership["email"] if membership else None,
        "role": _active_role(membership) if usable else None,
    }


def _is_active_owner(membership: dict[str, Any] | None) -> bool:
    return _active_role(membership) == membership_policy.OWNER and bool(membership and membership["is_active"])


def _require_another_active_owner(conn: Any, organization_id: int) -> None:
    """Refuses unless the organization has an active owner besides the one about to stop being one. Called with
    the organization locked, for a target that IS an active owner: so "another" means a count of at least two."""
    owners = conn.execute(text(_ACTIVE_OWNER_COUNT_SQL), {"organization_id": organization_id}).scalar_one()
    if int(owners) < 2:
        raise _Refused(LAST_OWNER)


def _record(conn: Any, event_type: str, message: str, *, org_slug: str, actor: dict[str, Any], user_id: int, email: str | None, **details: Any) -> None:
    # Ids, e-mail addresses and roles only. `org_slug` is what attributes the event to this organization.
    auth_service.log_auth_event_with_connection(
        conn,
        event_type=event_type,
        is_success=True,
        user_id=user_id,
        email=email,
        message=message,
        metadata={"org_slug": org_slug, **details, "actor_user_id": actor["user_id"], "actor_email": actor["email"]},
    )


# =====================================================================================================================
# An organization's members
# =====================================================================================================================

def list_org_users(org_slug: str) -> list[dict[str, Any]]:
    """The organization's ACTIVE members: who they are, their role, and whether their account can be used. Someone
    who was removed is not a member and is not listed. No password hash, lockout state or flag is selected."""
    sql = text("""
        SELECT
            u.id AS user_id,
            u.email,
            u.full_name,
            u.is_active,
            m.role,
            u.last_login_at,
            u.last_password_changed_at,
            u.created_at
        FROM memberships m
        JOIN organizations o
          ON o.id = m.organization_id
        JOIN app_users u
          ON u.id = m.user_id
        WHERE o.slug = :org_slug
          AND m.removed_at IS NULL
        ORDER BY lower(u.email)
    """)

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"org_slug": org_slug}).mappings().all()
        return [dict(row) for row in rows]


_FIND_ACCOUNT_SQL = """
    SELECT id, email
    FROM app_users
    WHERE lower(email) = :email
    LIMIT 1
"""

_INSERT_MEMBERSHIP_SQL = """
    INSERT INTO memberships (organization_id, user_id, role)
    VALUES (:organization_id, :user_id, :role)
"""

# Adding back someone who was removed: the same row, active again, with the role being given NOW.
_RESTORE_MEMBERSHIP_SQL = """
    UPDATE memberships
    SET role = :role,
        removed_at = NULL
    WHERE id = :membership_id
      AND organization_id = :organization_id
      AND removed_at IS NOT NULL
"""


def add_organization_member(
    org_slug: str,
    email: str,
    password: str,
    full_name: str,
    role: str,
    *,
    actor_user_id: int,
) -> dict[str, Any]:
    """Gives the person with this e-mail address a membership of the organization, with `role`.

    If nobody has an account with that address, one is created with `password` -- in this same transaction, so a
    new account never exists without the membership it was created for. If somebody does, the account is
    left exactly as it is -- its password, its name, whether it is active, and every other membership it has -- and
    only gains this membership. Someone who was removed from this organization earlier gets their one membership row
    back, with the role given now; what they held before counts for nothing.

    THE ANSWER IS THE SAME EITHER WAY. Whether the address already had an account -- in another organization -- is
    not this organization's to know, so success says only that the person was added.
    """
    normalized_email = email.strip().lower() if isinstance(email, str) else ""
    if "@" not in normalized_email or not isinstance(password, str) or not password:
        return _refused(INVALID_MEMBER_DETAILS)

    engine = get_engine()
    try:
        with engine.begin() as conn:
            organization = _lock_organization(conn, org_slug)
            actor = _actor(conn, organization["id"], actor_user_id)
            # Decided before anything is looked up about the address: by who is acting and the role being given.
            membership_policy.require_may_assign(actor["role"], role)

            account = conn.execute(text(_FIND_ACCOUNT_SQL), {"email": normalized_email}).mappings().first()
            if account is None:
                # ON THIS TRANSACTION: the new account and its membership commit together or not at all. Whatever
                # fails from here on -- the membership, the record -- leaves no account behind.
                try:
                    created = auth_service.create_user_with_connection(
                        conn, email=normalized_email, password=password, full_name=full_name or ""
                    )
                except ValueError as exc:
                    log_safe_exception(logger, "User creation refused", exc)
                    raise _Refused(MEMBER_NOT_ADDED) from None
                if created is None:
                    # Someone else created it in this same instant. It is an existing account like any other: it
                    # is attached as it is, and this transaction does not claim to have created it.
                    account = conn.execute(text(_FIND_ACCOUNT_SQL), {"email": normalized_email}).mappings().first()
                    if account is None:
                        raise _Refused(MEMBER_NOT_ADDED)
                else:
                    account = {"id": created["id"], "email": created["email"]}

            parameters = {"organization_id": organization["id"], "user_id": account["id"], "role": role}
            existing = _membership(conn, organization["id"], account["id"])
            if existing is None:
                conn.execute(text(_INSERT_MEMBERSHIP_SQL), parameters)
            elif existing["removed_at"] is None:
                raise _Refused(ALREADY_MEMBER)
            else:
                conn.execute(text(_RESTORE_MEMBERSHIP_SQL), {**parameters, "membership_id": existing["membership_id"]})

            _record(
                conn, "membership_added", "User added to organization.",
                org_slug=organization["slug"], actor=actor, user_id=account["id"], email=account["email"],
                previous_role=None, role=role,
            )
    except MembershipPolicyError as error:
        return _refused(error.code)
    except _Refused as refused:
        return _refused(refused.code)

    return {
        "ok": True,
        "message": "The user was added to this organization. If they already had a SortView account, their existing password is unchanged.",
    }


_SET_ROLE_SQL = """
    UPDATE memberships
    SET role = :role
    WHERE id = :membership_id
      AND organization_id = :organization_id
      AND removed_at IS NULL
"""


def change_organization_member_role(
    org_slug: str,
    user_id: int,
    role: str,
    *,
    actor_user_id: int,
) -> dict[str, Any]:
    """Gives one of the organization's active members a different role -- in this organization and no other.

    Refused if the actor may not (membership_policy), if the person is not an active member, or if it would leave
    the organization with no active owner. Giving someone the role they already have changes and records nothing.
    """
    engine = get_engine()
    try:
        with engine.begin() as conn:
            organization = _lock_organization(conn, org_slug)
            actor = _actor(conn, organization["id"], actor_user_id)
            membership_policy.require_administrator(actor["role"])

            target = _membership(conn, organization["id"], user_id)
            previous_role = _active_role(target)
            if target is None or previous_role is None:
                raise _Refused(MEMBER_NOT_FOUND)
            membership_policy.require_may_change_role(actor["role"], previous_role, role)

            if role == previous_role:
                return {"ok": True, "message": "User role updated."}
            if _is_active_owner(target):
                _require_another_active_owner(conn, organization["id"])

            conn.execute(
                text(_SET_ROLE_SQL),
                {"role": role, "membership_id": target["membership_id"], "organization_id": organization["id"]},
            )
            _record(
                conn, "membership_role_updated", "Organization role updated.",
                org_slug=organization["slug"], actor=actor, user_id=user_id, email=target["email"],
                previous_role=previous_role, role=role,
            )
    except MembershipPolicyError as error:
        return _refused(error.code)
    except _Refused as refused:
        return _refused(refused.code)

    return {"ok": True, "message": "User role updated."}


_REMOVE_MEMBERSHIP_SQL = """
    UPDATE memberships
    SET removed_at = :removed_at
    WHERE id = :membership_id
      AND organization_id = :organization_id
      AND removed_at IS NULL
"""


def remove_organization_member(
    org_slug: str,
    user_id: int,
    *,
    actor_user_id: int,
) -> dict[str, Any]:
    """Takes away one person's access to THIS organization, by marking their one membership of it removed.

    Their account stays active, their sessions stay valid and their memberships of other organizations are not
    looked at: from the next request on, this organization is simply not one of theirs. Refused if the actor may
    not, if the person is not an active member, or if it would leave the organization with no active owner.
    """
    engine = get_engine()
    try:
        with engine.begin() as conn:
            organization = _lock_organization(conn, org_slug)
            actor = _actor(conn, organization["id"], actor_user_id)
            membership_policy.require_administrator(actor["role"])

            target = _membership(conn, organization["id"], user_id)
            previous_role = _active_role(target)
            if target is None or previous_role is None:
                raise _Refused(MEMBER_NOT_FOUND)
            membership_policy.require_may_manage(actor["role"], previous_role)
            if _is_active_owner(target):
                _require_another_active_owner(conn, organization["id"])

            conn.execute(
                text(_REMOVE_MEMBERSHIP_SQL),
                {"removed_at": datetime.now(UTC), "membership_id": target["membership_id"], "organization_id": organization["id"]},
            )
            _record(
                conn, "membership_removed", "User removed from organization.",
                org_slug=organization["slug"], actor=actor, user_id=user_id, email=target["email"],
                previous_role=previous_role, role=None,
            )
    except MembershipPolicyError as error:
        return _refused(error.code)
    except _Refused as refused:
        return _refused(refused.code)

    return {"ok": True, "message": "User removed from this organization."}


def list_recent_org_auth_events(org_slug: str, limit: int = 25) -> list[dict[str, Any]]:
    """The most recent audit events that happened IN this organization: the ones recorded with this
    organization's slug -- members added, removed and given roles.

    An event is not this organization's merely because it is about someone who is a member of it. A sign-in, a
    password change or reset, or anything done in another organization the person also belongs to, has no place
    here: none of it is attributed to this organization, and nothing is inferred from who its members are now.
    """
    sql = text("""
        SELECT
            aal.id,
            aal.created_at,
            aal.email,
            aal.event_type,
            aal.is_success,
            aal.message,
            aal.metadata
        FROM auth_audit_log aal
        WHERE aal.metadata ->> 'org_slug' = :org_slug
        ORDER BY aal.created_at DESC, aal.id DESC
        LIMIT :limit
    """)

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"org_slug": org_slug, "limit": limit}).mappings().all()
        return [dict(row) for row in rows]


# =====================================================================================================================
# An account, everywhere: a platform operation
# =====================================================================================================================

_ACCOUNT_SQL = """
    SELECT id, email, is_active, is_platform_admin
    FROM app_users
    WHERE id = :user_id
"""

# The organizations in which this account is an active owner right now, lowest id first. A cancelled organization
# has no members to answer to and is not one of them.
_ACTIVELY_OWNED_ORGANIZATIONS_SQL = """
    SELECT o.id
    FROM memberships m
    JOIN organizations o
      ON o.id = m.organization_id
    JOIN app_users u
      ON u.id = m.user_id
    WHERE m.user_id = :user_id
      AND m.role = 'owner'
      AND m.removed_at IS NULL
      AND u.is_active = TRUE
      AND o.status <> 'cancelled'
    ORDER BY o.id
"""

_SET_ACCOUNT_ACTIVE_SQL = """
    UPDATE app_users
    SET is_active = :is_active
    WHERE id = :user_id
"""


def _actively_owned(conn: Any, user_id: int) -> list[int]:
    return [int(row[0]) for row in conn.execute(text(_ACTIVELY_OWNED_ORGANIZATIONS_SQL), {"user_id": user_id})]


def set_global_account_active(
    user_id: int,
    is_active: bool,
    *,
    actor_user_id: int,
) -> dict[str, Any]:
    """Switches a person's whole ACCOUNT off or on: app_users.is_active, which every organization they belong to
    depends on. Switching it off ends every session they have.

    A PLATFORM operation. The actor must be an active platform administrator -- read here, from the database --
    and no organization role, an owner's included, is enough. It is not how someone is taken out of an
    organization: that is remove_organization_member, which leaves the account alone.

    Switching an account off is refused if it would leave ANY organization with no active owner. Every
    organization the account actively owns is locked first, lowest id first, and each must have another active
    owner. If the set of organizations it owns changed while they were being locked, nothing is done and the
    caller is told to try again (CONCURRENT_CHANGE) rather than decide on a picture that is already out of date.
    """
    engine = get_engine()
    try:
        with engine.begin() as conn:
            actor = conn.execute(text(_ACCOUNT_SQL), {"user_id": actor_user_id}).mappings().first()
            if actor is None or not actor["is_active"] or not actor["is_platform_admin"]:
                raise _Refused(PLATFORM_ADMIN_REQUIRED)

            target = conn.execute(text(_ACCOUNT_SQL), {"user_id": user_id}).mappings().first()
            if target is None:
                raise _Refused(USER_NOT_FOUND)
            if target["is_platform_admin"]:
                raise _Refused(PLATFORM_ADMIN_ACCOUNT)

            if not is_active:
                owned = _actively_owned(conn, user_id)
                lock = " FOR UPDATE" if _is_postgresql(conn) else ""
                for organization_id in owned:
                    conn.execute(text(_ORGANIZATION_BY_ID_SQL + lock), {"organization_id": organization_id})
                # With those locked nobody can change who owns them. If the account came to own another
                # organization meanwhile, that one is not locked: do nothing rather than guess.
                if _actively_owned(conn, user_id) != owned:
                    raise _Refused(CONCURRENT_CHANGE)
                for organization_id in owned:
                    _require_another_active_owner(conn, organization_id)

            conn.execute(text(_SET_ACCOUNT_ACTIVE_SQL), {"user_id": user_id, "is_active": bool(is_active)})
            if not is_active:
                session_service.revoke_all_sessions_for_user_with_connection(conn, user_id)

            # Account-wide, so it carries no organization: it is not an event IN any organization.
            auth_service.log_auth_event_with_connection(
                conn,
                event_type="user_status_updated",
                is_success=True,
                user_id=user_id,
                email=target["email"],
                message="Account status updated.",
                metadata={
                    "scope": "account",
                    "previous_is_active": bool(target["is_active"]),
                    "is_active": bool(is_active),
                    "actor_user_id": actor_user_id,
                    "actor_email": actor["email"],
                },
            )
    except _Refused as refused:
        return _refused(refused.code)

    return {"ok": True, "message": "Account status updated."}
