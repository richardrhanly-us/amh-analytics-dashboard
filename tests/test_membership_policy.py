"""R8C: who may change whose membership -- the pure policy (services.membership_policy).

Nothing here touches a database. What the policy cannot decide from two roles -- whether an organization would be
left with no active owner -- is tested with the service, in tests/test_user_admin_service.py.
"""

from __future__ import annotations

import inspect
import itertools

import pytest

from services import membership_policy, permission_service
from services.membership_policy import (
    ADMIN,
    INVALID_ROLE,
    MANAGER,
    NOT_PERMITTED,
    OWNER,
    OWNER_REQUIRED,
    ROLES,
    VIEWER,
    MembershipPolicyError,
    assignable_roles,
    require_administrator,
    require_may_assign,
    require_may_change_role,
    require_may_manage,
)


def _refusal(check, *args) -> str | None:
    try:
        check(*args)
    except MembershipPolicyError as error:
        return error.code
    return None


def test_the_roles_are_exactly_the_four_the_table_allows_and_nothing_new():
    assert ROLES == ("owner", "admin", "manager", "viewer")
    assert membership_policy.ADMINISTERING_ROLES == permission_service.ADMIN_ROLES == {"owner", "admin"}


@pytest.mark.parametrize("actor", [MANAGER, VIEWER, None, "", "superuser", "platform_admin", "OWNER"])
def test_only_an_owner_or_an_admin_administers_members_at_all(actor):
    assert _refusal(require_administrator, actor) == NOT_PERMITTED
    # Whoever the target and whatever the role: not even on themself, and not even to do nothing.
    for target, new_role in itertools.product([*ROLES, None], ROLES):
        assert _refusal(require_may_manage, actor, target) == NOT_PERMITTED
        assert _refusal(require_may_assign, actor, new_role) == NOT_PERMITTED
        assert _refusal(require_may_change_role, actor, target, new_role) == NOT_PERMITTED
    assert assignable_roles(actor) == ()


def test_an_owner_may_do_everything_the_policy_decides():
    assert _refusal(require_administrator, OWNER) is None
    for target, new_role in itertools.product([*ROLES, None], ROLES):
        assert _refusal(require_may_manage, OWNER, target) is None
        assert _refusal(require_may_assign, OWNER, new_role) is None
        assert _refusal(require_may_change_role, OWNER, target, new_role) is None
    assert assignable_roles(OWNER) == ROLES


@pytest.mark.parametrize("target", [ADMIN, MANAGER, VIEWER, None])
@pytest.mark.parametrize("new_role", [ADMIN, MANAGER, VIEWER])
def test_an_admin_manages_admins_managers_and_viewers(target, new_role):
    assert _refusal(require_may_manage, ADMIN, target) is None
    assert _refusal(require_may_assign, ADMIN, new_role) is None
    assert _refusal(require_may_change_role, ADMIN, target, new_role) is None


@pytest.mark.parametrize("target", [OWNER, ADMIN, MANAGER, VIEWER, None])
def test_an_admin_cannot_give_anyone_ownership(target):
    assert _refusal(require_may_assign, ADMIN, OWNER) == OWNER_REQUIRED
    assert _refusal(require_may_change_role, ADMIN, target, OWNER) == OWNER_REQUIRED
    assert OWNER not in assignable_roles(ADMIN)
    assert assignable_roles(ADMIN) == (ADMIN, MANAGER, VIEWER)


@pytest.mark.parametrize("new_role", ROLES)
def test_an_admin_cannot_change_or_remove_an_owner(new_role):
    assert _refusal(require_may_manage, ADMIN, OWNER) == OWNER_REQUIRED
    assert _refusal(require_may_change_role, ADMIN, OWNER, new_role) == OWNER_REQUIRED


@pytest.mark.parametrize(
    ("actor", "current", "new", "expected"),
    [
        # owner -> ...
        (OWNER, OWNER, ADMIN, None),
        (OWNER, OWNER, VIEWER, None),
        (ADMIN, OWNER, ADMIN, OWNER_REQUIRED),
        (ADMIN, OWNER, VIEWER, OWNER_REQUIRED),
        # ... -> owner
        (OWNER, ADMIN, OWNER, None),
        (OWNER, VIEWER, OWNER, None),
        (OWNER, MANAGER, OWNER, None),
        (ADMIN, ADMIN, OWNER, OWNER_REQUIRED),
        (ADMIN, VIEWER, OWNER, OWNER_REQUIRED),
        # below ownership, either may
        (ADMIN, ADMIN, VIEWER, None),
        (ADMIN, VIEWER, ADMIN, None),
        (ADMIN, MANAGER, ADMIN, None),
        (ADMIN, ADMIN, MANAGER, None),
        (ADMIN, VIEWER, MANAGER, None),
        (ADMIN, MANAGER, VIEWER, None),
        (OWNER, ADMIN, VIEWER, None),
        (OWNER, VIEWER, ADMIN, None),
        (OWNER, MANAGER, VIEWER, None),
        # a manager and a viewer change nothing
        (MANAGER, VIEWER, MANAGER, NOT_PERMITTED),
        (MANAGER, MANAGER, VIEWER, NOT_PERMITTED),
        (VIEWER, VIEWER, ADMIN, NOT_PERMITTED),
    ],
)
def test_role_transitions(actor, current, new, expected):
    assert _refusal(require_may_change_role, actor, current, new) == expected


@pytest.mark.parametrize("bad", ["", "Owner", "ADMIN", "superuser", "platform_admin", "root", None, 1, True, ["owner"], "owner "])
def test_a_role_that_is_not_one_of_the_four_cannot_be_given(bad):
    for actor in (OWNER, ADMIN):
        assert _refusal(require_may_assign, actor, bad) == INVALID_ROLE
        assert _refusal(require_may_change_role, actor, VIEWER, bad) == INVALID_ROLE


def test_who_the_target_is_changes_nothing_so_acting_on_oneself_is_no_way_round():
    # The checks take roles, not people: an admin "targeting themself" is an admin acting on an admin, and an admin
    # cannot make themself an owner any more than anyone else.
    assert _refusal(require_may_change_role, ADMIN, ADMIN, OWNER) == OWNER_REQUIRED
    assert _refusal(require_may_change_role, ADMIN, ADMIN, VIEWER) is None
    assert _refusal(require_may_manage, ADMIN, ADMIN) is None
    assert _refusal(require_may_change_role, OWNER, OWNER, VIEWER) is None  # the last-owner rule is the service's
    for check in (require_administrator, require_may_assign, require_may_manage, require_may_change_role):
        assert not {"actor_user_id", "target_user_id", "user_id"} & set(inspect.signature(check).parameters)


def test_a_removed_membership_is_no_membership_whatever_role_its_row_still_says():
    # The caller passes None for someone who is not currently a member. An admin may then add them back below
    # ownership, and not as an owner -- even if "owner" is what they once were.
    assert _refusal(require_may_manage, ADMIN, None) is None
    assert _refusal(require_may_change_role, ADMIN, None, ADMIN) is None
    assert _refusal(require_may_change_role, ADMIN, None, OWNER) == OWNER_REQUIRED
    assert _refusal(require_may_change_role, OWNER, None, OWNER) is None


def test_a_refusal_carries_a_stable_code_and_fixed_text_and_knows_no_web_framework():
    error = MembershipPolicyError(OWNER_REQUIRED)

    assert (error.code, error.message) == ("owner_required", "Only an owner of the organization can do that.")
    assert set(membership_policy.MESSAGES) == {NOT_PERMITTED, OWNER_REQUIRED, INVALID_ROLE}
    source = inspect.getsource(membership_policy)
    for forbidden in ("fastapi", "starlette", "streamlit", "sqlalchemy", "get_engine", "is_platform_admin", "import os"):
        assert forbidden not in source.split('"""', 2)[2], forbidden
    assert [line for line in source.splitlines() if line.startswith(("import ", "from "))] == ["from __future__ import annotations"]
