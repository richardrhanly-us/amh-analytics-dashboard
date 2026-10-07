"""Who may change whose membership of an organization. Pure: no database,
no clock, no request.

THE ROLES are the four an organization's membership can hold -- owner,
admin, manager, viewer -- and nothing else is one. Being a PLATFORM
administrator is not a role in an organization and is not looked at here: it
gives nobody authority over an organization's members.

WHO MAY ADMINISTER MEMBERS. An owner and an admin. A manager and a viewer
may not, whatever they are asked to do and whoever to.

WHAT AN ADMIN MAY NOT TOUCH. Ownership. An admin may add, change and remove
admins, managers and viewers -- themself included -- but may not

    give anyone the owner role          (add as owner, change to owner, add back as owner)
    change or remove a member who is an owner

An owner may do all of that. Acting on one's own membership changes none of
it: the same two checks are made whoever the target is.

A REMOVED MEMBERSHIP HAS NO ROLE. The role left on a removed membership's
row is history. Adding that person back is decided by the role they are
being GIVEN and by nothing they once held -- so the caller passes
`target_role=None` for someone who is not currently a member.

One thing is NOT decided here, because it cannot be known from two roles:
whether a change would leave an organization with no active owner. That
needs the organization's other members, read under a lock
(services.user_admin_service).

Every refusal is a MembershipPolicyError with a stable `code`; nothing here
knows what a web framework is.
"""

from __future__ import annotations

OWNER = "owner"
ADMIN = "admin"
MANAGER = "manager"
VIEWER = "viewer"

# Every role a membership can hold, most authority first. The memberships table's CHECK constraint is the same set.
ROLES: tuple[str, ...] = (OWNER, ADMIN, MANAGER, VIEWER)

# The roles that may administer an organization's members at all (services.permission_service.ADMIN_ROLES).
ADMINISTERING_ROLES: frozenset[str] = frozenset({OWNER, ADMIN})

# Why a change was refused.
NOT_PERMITTED = "not_permitted"
OWNER_REQUIRED = "owner_required"
INVALID_ROLE = "invalid_role"

MESSAGES: dict[str, str] = {
    NOT_PERMITTED: "You do not have permission to manage this organization's users.",
    OWNER_REQUIRED: "Only an owner of the organization can do that.",
    INVALID_ROLE: "That is not a role a user can be given.",
}


class MembershipPolicyError(Exception):
    """A change the policy does not allow. `code` is stable and safe to hand
    to a client; `message` is fixed text for a person."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = MESSAGES[code]


def is_role(value: object) -> bool:
    return isinstance(value, str) and value in ROLES


def require_administrator(actor_role: str | None) -> None:
    """The actor may administer members at all: they hold an ACTIVE
    membership as an owner or an admin. `actor_role` is None for someone who
    is not currently a member."""
    if actor_role not in ADMINISTERING_ROLES:
        raise MembershipPolicyError(NOT_PERMITTED)


def require_may_assign(actor_role: str | None, new_role: object) -> None:
    """The actor may give someone `new_role`: it is a role, and only an
    owner gives ownership."""
    require_administrator(actor_role)
    if not is_role(new_role):
        raise MembershipPolicyError(INVALID_ROLE)
    if new_role == OWNER and actor_role != OWNER:
        raise MembershipPolicyError(OWNER_REQUIRED)


def require_may_manage(actor_role: str | None, target_role: str | None) -> None:
    """The actor may change or remove a member who currently holds
    `target_role`: only an owner acts on an owner. `target_role` is None for
    someone with no active membership, whom anyone who may administer may
    act on (subject to the role they are being given)."""
    require_administrator(actor_role)
    if target_role == OWNER and actor_role != OWNER:
        raise MembershipPolicyError(OWNER_REQUIRED)


def require_may_change_role(actor_role: str | None, target_role: str | None, new_role: object) -> None:
    """The actor may move a member from `target_role` to `new_role`: both of
    the above. Whether it would leave no active owner is the caller's to
    check."""
    require_may_manage(actor_role, target_role)
    require_may_assign(actor_role, new_role)


def assignable_roles(actor_role: str | None) -> tuple[str, ...]:
    """The roles the actor could give someone, most authority first; none
    for someone who may not administer members. For a form's choices: the
    checks above are what decides."""
    if actor_role == OWNER:
        return ROLES
    return tuple(role for role in ROLES if role != OWNER) if actor_role == ADMIN else ()
