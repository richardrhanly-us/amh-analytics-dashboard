"""R8C: administering an organization's members (services.user_admin_service), on a real in-memory database.

Every statement the service runs is run for real (tests/membership_db.py), account creation included; only the audit
INSERT's jsonb cast is stood in for. What needs a real PostgreSQL -- row locks and two changes at once, the jsonb audit
row, the migrated column -- is in tests/test_user_admin_postgres.py.

The world: two live organizations, a suspended one and a cancelled one.

    acme    OWNER (owner)  SECOND (owner)  ADMIN (admin)  MANAGER (manager)  VIEWER (viewer)  BOTH (viewer)
    beta    BETA_OWNER (owner)  BOTH (admin)
    paused  (suspended)  PAUSED_OWNER (owner)
    closed  (cancelled)  CLOSED_OWNER (owner)

STRANGER has an account and no membership; PLATFORM is a platform administrator with no membership.
"""

from __future__ import annotations

import inspect

import pytest
from membership_db import MembershipDatabase, record_audit
from werkzeug.security import check_password_hash

from services import (
    access_service,
    auth_service,
    entitlement_service,
    membership_policy,
    user_admin_service,
)
from services.user_admin_service import (
    add_organization_member,
    change_organization_member_role,
    list_org_users,
    list_recent_org_auth_events,
    remove_organization_member,
    set_global_account_active,
)

ACME, BETA, PAUSED, CLOSED = 1, 2, 3, 4
OWNER, SECOND, ADMIN, MANAGER, VIEWER, BOTH = 101, 102, 103, 104, 105, 106
BETA_OWNER, PAUSED_OWNER, CLOSED_OWNER, STRANGER, PLATFORM = 201, 301, 401, 501, 900
PASSWORD = "synthetic-Temp-Password-1"


@pytest.fixture
def db(monkeypatch):
    database = MembershipDatabase()
    for organization_id, slug, status in ((ACME, "acme", "active"), (BETA, "beta", "trial"), (PAUSED, "paused", "suspended"),
                                          (CLOSED, "closed", "cancelled")):
        database.organization(organization_id, slug, status)
    for user_id in (OWNER, SECOND, ADMIN, MANAGER, VIEWER, BOTH, BETA_OWNER, PAUSED_OWNER, CLOSED_OWNER, STRANGER):
        database.user(user_id)
    database.user(PLATFORM, platform_admin=True)
    for organization_id, user_id, role in (
        (ACME, OWNER, "owner"), (ACME, SECOND, "owner"), (ACME, ADMIN, "admin"), (ACME, MANAGER, "manager"),
        (ACME, VIEWER, "viewer"), (ACME, BOTH, "viewer"), (BETA, BETA_OWNER, "owner"), (BETA, BOTH, "admin"),
        (PAUSED, PAUSED_OWNER, "owner"), (CLOSED, CLOSED_OWNER, "owner"),
    ):
        database.member(organization_id, user_id, role)

    for module in (user_admin_service, access_service, entitlement_service):
        monkeypatch.setattr(module, "get_engine", lambda: database.engine)
    monkeypatch.setattr(auth_service, "log_auth_event_with_connection", record_audit)

    # The REAL account creation, on the service's own transaction -- watched, not replaced.
    really_create = auth_service.create_user_with_connection

    def create_user_with_connection(conn, email, password, full_name=""):
        database.created.append({"email": email, "password": password, "full_name": full_name})
        return really_create(conn, email, password, full_name)

    database.created = []
    monkeypatch.setattr(auth_service, "create_user_with_connection", create_user_with_connection)
    # The one that opens a transaction of its own is never the way an account is made here.
    monkeypatch.setattr(auth_service, "create_user", lambda *_a, **_k: pytest.fail("create_user must not be used: it commits separately"))
    return database


def _account(db, email):
    found = db.rows("SELECT id, email, password_hash, is_active FROM app_users WHERE email = :e", e=email)
    return found[0] if found else None


def _add(org_slug, email, role, actor, password=PASSWORD):
    return add_organization_member(org_slug, email, password, "Pat Example", role, actor_user_id=actor)


def _email(user_id: int) -> str:
    return f"user{user_id}@example.invalid"


def _sole_owner(db) -> None:
    """Leaves OWNER as acme's only active owner."""
    db.run("UPDATE memberships SET role = 'admin' WHERE organization_id = :o AND user_id = :u", o=ACME, u=SECOND)


# =====================================================================================================================
# Who may administer at all
# =====================================================================================================================

def _every_change(actor):
    return (
        _add("acme", _email(STRANGER), "viewer", actor),
        change_organization_member_role("acme", VIEWER, "manager", actor_user_id=actor),
        remove_organization_member("acme", VIEWER, actor_user_id=actor),
    )


@pytest.mark.parametrize("actor", [MANAGER, VIEWER, STRANGER, BETA_OWNER, BOTH, PLATFORM, 424242],
                         ids=["manager", "viewer", "no-membership", "owner-of-another-org", "admin-of-another-org",
                              "platform-admin", "nobody"])
def test_only_an_owner_or_admin_of_this_organization_changes_its_members(db, actor):
    before = db.snapshot()

    for result in _every_change(actor):
        assert result == {"ok": False, "code": "not_permitted", "message": membership_policy.MESSAGES["not_permitted"]}

    assert db.snapshot() == before and db.created == []


def test_a_removed_owner_has_no_authority_left(db):
    db.run("UPDATE memberships SET removed_at = '2026-01-01' WHERE organization_id = :o AND user_id = :u", o=ACME, u=SECOND)
    before = db.snapshot()

    assert [r["code"] for r in _every_change(SECOND)] == ["not_permitted"] * 3
    assert db.snapshot() == before


def test_an_owner_whose_account_is_switched_off_has_no_authority(db):
    db.run("UPDATE app_users SET is_active = 0 WHERE id = :u", u=SECOND)
    before = db.snapshot()

    assert [r["code"] for r in _every_change(SECOND)] == ["not_permitted"] * 3
    assert db.snapshot() == before


def test_the_actor_is_read_from_the_database_and_there_is_nothing_a_caller_can_say_about_their_role():
    for function in (add_organization_member, change_organization_member_role, remove_organization_member, set_global_account_active):
        parameters = inspect.signature(function).parameters
        assert parameters["actor_user_id"].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters["actor_user_id"].default is inspect.Parameter.empty
        assert not {"actor_role", "actor_email", "is_platform_admin", "role_override"} & set(parameters)


@pytest.mark.parametrize(("slug", "code"), [("paused", "organization_not_editable"), ("closed", "organization_not_editable"),
                                            ("nowhere", "organization_not_found")])
def test_a_suspended_cancelled_or_unknown_organization_cannot_be_changed(db, slug, code):
    actor = {"paused": PAUSED_OWNER, "closed": CLOSED_OWNER, "nowhere": OWNER}[slug]
    before = db.snapshot()

    assert _add(slug, _email(STRANGER), "viewer", actor)["code"] == code
    assert change_organization_member_role(slug, actor, "viewer", actor_user_id=actor)["code"] == code
    assert remove_organization_member(slug, actor, actor_user_id=actor)["code"] == code
    assert db.snapshot() == before


@pytest.mark.parametrize("status", ["active", "trial", "suspended", "cancelled", "past_due", "", "ACTIVE"])
def test_members_can_be_changed_exactly_when_the_organization_has_full_access(db, status):
    # The rule the old functions applied through access_service.get_org_access_mode(...) == "full", now decided
    # inside the locked transaction: the two must agree for every status, known or not.
    db.run("UPDATE organizations SET status = :s WHERE id = :o", s=status, o=ACME)
    writable = access_service.get_org_access_mode("acme") == "full"
    assert writable is (status in ("active", "trial"))
    before = db.snapshot()

    results = _every_change(OWNER)

    if writable:
        assert [r["ok"] for r in results] == [True, True, True]
    else:
        assert [r.get("code") for r in results] == ["organization_not_editable"] * 3
        assert db.snapshot() == before and db.created == []


# =====================================================================================================================
# Changing a role
# =====================================================================================================================

@pytest.mark.parametrize(("actor", "target", "new", "code"), [
    (OWNER, SECOND, "admin", None),          # owner -> admin
    (OWNER, SECOND, "viewer", None),         # owner -> viewer
    (OWNER, ADMIN, "owner", None),           # admin -> owner
    (OWNER, VIEWER, "owner", None),          # viewer -> owner
    (OWNER, MANAGER, "viewer", None),
    (ADMIN, VIEWER, "admin", None),          # viewer -> admin
    (ADMIN, MANAGER, "admin", None),
    (ADMIN, VIEWER, "manager", None),
    (ADMIN, MANAGER, "viewer", None),
    (ADMIN, ADMIN, "viewer", None),          # an admin may demote themself
    (ADMIN, ADMIN, "owner", "owner_required"),   # ... and not promote themself
    (ADMIN, VIEWER, "owner", "owner_required"),
    (ADMIN, OWNER, "admin", "owner_required"),
    (ADMIN, OWNER, "viewer", "owner_required"),
    (ADMIN, SECOND, "owner", "owner_required"),  # not even to "change" an owner to what they are
    (OWNER, VIEWER, "superuser", "invalid_role"),
    (OWNER, VIEWER, "", "invalid_role"),
    (OWNER, VIEWER, "Owner", "invalid_role"),
])
def test_role_changes(db, actor, target, new, code):
    was = db.active_role(ACME, target)
    before = db.snapshot()

    result = change_organization_member_role("acme", target, new, actor_user_id=actor)

    assert result.get("code") == code and result["ok"] is (code is None)
    if code is None:
        assert db.active_role(ACME, target) == new
        [event] = db.audit()
        assert (event["event_type"], event["user_id"], event["email"], event["is_success"]) == ("membership_role_updated", target, _email(target), 1)
        assert event["metadata"] == {"org_slug": "acme", "previous_role": was, "role": new,
                                     "actor_user_id": actor, "actor_email": _email(actor)}
    else:
        assert db.snapshot() == before


def test_giving_someone_the_role_they_have_changes_and_records_nothing(db):
    before = db.snapshot()

    assert change_organization_member_role("acme", VIEWER, "viewer", actor_user_id=ADMIN) == {"ok": True, "message": "User role updated."}
    assert db.snapshot() == before


@pytest.mark.parametrize("target", [STRANGER, BETA_OWNER, 424242], ids=["no-membership", "member-of-another-org", "nobody"])
def test_someone_who_is_not_a_member_here_cannot_be_changed_or_removed(db, target):
    before = db.snapshot()

    assert change_organization_member_role("acme", target, "viewer", actor_user_id=OWNER)["code"] == "member_not_found"
    assert remove_organization_member("acme", target, actor_user_id=OWNER)["code"] == "member_not_found"
    assert db.snapshot() == before


def test_a_removed_member_cannot_be_given_a_role_or_removed_again(db):
    assert remove_organization_member("acme", VIEWER, actor_user_id=ADMIN)["ok"]
    before = db.snapshot()

    assert change_organization_member_role("acme", VIEWER, "admin", actor_user_id=OWNER)["code"] == "member_not_found"
    assert remove_organization_member("acme", VIEWER, actor_user_id=OWNER)["code"] == "member_not_found"
    assert db.snapshot() == before


# =====================================================================================================================
# An organization always keeps an active owner
# =====================================================================================================================

def test_the_only_owner_cannot_be_demoted_or_removed_not_even_by_themself(db):
    _sole_owner(db)
    before = db.snapshot()

    for new_role in ("admin", "manager", "viewer"):
        assert change_organization_member_role("acme", OWNER, new_role, actor_user_id=OWNER)["code"] == "last_owner"
    assert remove_organization_member("acme", OWNER, actor_user_id=OWNER)["code"] == "last_owner"
    assert db.snapshot() == before and db.active_role(ACME, OWNER) == "owner"


def test_one_of_two_owners_may_step_down_or_leave_and_then_the_other_may_not(db):
    assert change_organization_member_role("acme", OWNER, "viewer", actor_user_id=OWNER)["ok"]
    assert db.active_role(ACME, OWNER) == "viewer"

    assert change_organization_member_role("acme", SECOND, "admin", actor_user_id=SECOND)["code"] == "last_owner"
    assert remove_organization_member("acme", SECOND, actor_user_id=SECOND)["code"] == "last_owner"
    assert db.active_role(ACME, SECOND) == "owner"


def test_an_owner_may_remove_another_owner_while_one_remains(db):
    assert remove_organization_member("acme", SECOND, actor_user_id=OWNER)["ok"]
    assert db.active_role(ACME, SECOND) is None and db.active_role(ACME, OWNER) == "owner"
    assert remove_organization_member("acme", OWNER, actor_user_id=OWNER)["code"] == "last_owner"


@pytest.mark.parametrize("how", ["account-switched-off", "membership-removed"])
def test_an_owner_who_is_not_active_does_not_count_as_the_one_who_remains(db, how):
    if how == "account-switched-off":
        db.run("UPDATE app_users SET is_active = 0 WHERE id = :u", u=SECOND)
    else:
        db.run("UPDATE memberships SET removed_at = '2026-01-01' WHERE organization_id = :o AND user_id = :u", o=ACME, u=SECOND)
    before = db.snapshot()

    assert change_organization_member_role("acme", OWNER, "admin", actor_user_id=OWNER)["code"] == "last_owner"
    assert remove_organization_member("acme", OWNER, actor_user_id=OWNER)["code"] == "last_owner"
    assert db.snapshot() == before


def test_an_owner_of_another_organization_does_not_count(db):
    _sole_owner(db)
    db.member(BETA, OWNER, "viewer")  # BETA_OWNER owns beta; that is nothing to acme

    assert remove_organization_member("acme", OWNER, actor_user_id=OWNER)["code"] == "last_owner"


def test_an_admin_cannot_remove_an_owner_and_an_admin_may_remove_themself(db):
    before = db.snapshot()
    assert remove_organization_member("acme", OWNER, actor_user_id=ADMIN)["code"] == "owner_required"
    assert db.snapshot() == before

    assert remove_organization_member("acme", ADMIN, actor_user_id=ADMIN) == {"ok": True, "message": "User removed from this organization."}
    assert db.active_role(ACME, ADMIN) is None


# =====================================================================================================================
# Removal is one membership of one organization
# =====================================================================================================================

def test_removal_marks_one_membership_and_touches_nothing_else(db):
    db.session(BOTH, "hash-of-a-session")
    accounts = db.rows("SELECT * FROM app_users ORDER BY id")
    others = db.rows("SELECT * FROM memberships WHERE NOT (organization_id = :o AND user_id = :u) ORDER BY id", o=ACME, u=BOTH)

    assert remove_organization_member("acme", BOTH, actor_user_id=ADMIN)["ok"]

    removed = db.membership(ACME, BOTH)
    assert removed["role"] == "viewer" and removed["removed_at"] is not None  # the row stays: history, not a DELETE
    assert db.rows("SELECT * FROM app_users ORDER BY id") == accounts          # is_active, password hash: as they were
    assert db.account_active(BOTH) and db.live_sessions(BOTH) == 1             # no session is revoked
    assert db.rows("SELECT * FROM memberships WHERE NOT (organization_id = :o AND user_id = :u) ORDER BY id", o=ACME, u=BOTH) == others
    assert db.active_role(BETA, BOTH) == "admin"
    [event] = db.audit()
    assert event["event_type"] == "membership_removed"
    assert event["metadata"] == {"org_slug": "acme", "previous_role": "viewer", "role": None,
                                 "actor_user_id": ADMIN, "actor_email": _email(ADMIN)}


def test_a_role_change_in_one_organization_changes_no_other(db):
    assert change_organization_member_role("acme", BOTH, "manager", actor_user_id=ADMIN)["ok"]

    assert (db.active_role(ACME, BOTH), db.active_role(BETA, BOTH)) == ("manager", "admin")


def test_an_admin_of_one_organization_cannot_touch_another(db):
    # BOTH is an admin in beta and only a viewer in acme.
    before = db.snapshot()

    assert remove_organization_member("acme", VIEWER, actor_user_id=BOTH)["code"] == "not_permitted"
    assert change_organization_member_role("acme", BOTH, "admin", actor_user_id=BOTH)["code"] == "not_permitted"
    assert db.snapshot() == before
    # ... and in beta, where they are one, they can.
    assert _add("beta", _email(STRANGER), "viewer", BOTH)["ok"]


def test_a_removed_membership_is_no_membership_to_everything_that_decides_access(db):
    assert access_service.user_can_access_org(BOTH, "acme") is True
    assert entitlement_service.get_org_role_for_user(BOTH, "acme") == "viewer"

    assert remove_organization_member("acme", BOTH, actor_user_id=OWNER)["ok"]

    assert access_service.user_can_access_org(BOTH, "acme") is False
    assert entitlement_service.get_org_role_for_user(BOTH, "acme") is None
    assert [m["organization_slug"] for m in access_service.get_user_memberships(BOTH)] == ["beta"]
    assert BOTH not in [u["user_id"] for u in list_org_users("acme")]
    # The other organization is exactly as reachable as before.
    assert access_service.user_can_access_org(BOTH, "beta") is True
    assert entitlement_service.get_org_role_for_user(BOTH, "beta") == "admin"
    assert BOTH in [u["user_id"] for u in list_org_users("beta")]


def test_the_member_list_is_the_active_members_and_selects_no_secret(db):
    db.run("UPDATE memberships SET removed_at = '2026-01-01' WHERE organization_id = :o AND user_id = :u", o=ACME, u=MANAGER)

    users = list_org_users("acme")

    assert sorted(u["user_id"] for u in users) == [OWNER, SECOND, ADMIN, VIEWER, BOTH]
    assert set(users[0]) == {"user_id", "email", "full_name", "is_active", "role", "last_login_at", "last_password_changed_at", "created_at"}
    assert "CANARY-HASH" not in repr(users)


def test_no_organization_level_function_can_reach_the_account_switch():
    source = inspect.getsource(user_admin_service)
    organization_level, platform_level = source.split("def set_global_account_active", 1)
    organization_level = organization_level.split('"""', 2)[2]  # past the module docstring

    assert organization_level.count("_SET_ACCOUNT_ACTIVE_SQL") == 1  # its definition; never executed above
    assert "text(_SET_ACCOUNT_ACTIVE_SQL)" in platform_level and "text(_SET_ACCOUNT_ACTIVE_SQL)" not in organization_level
    assert "revoke_all_sessions" not in organization_level and "DELETE" not in source.replace("no DELETE", "")
    for name in ("set_user_active", "create_or_add_org_user", "update_org_user_role"):
        assert not hasattr(user_admin_service, name)


# =====================================================================================================================
# Adding, and adding back
# =====================================================================================================================

def test_a_new_address_gets_an_account_and_a_membership(db):
    result = _add("acme", "  New.Person@Example.invalid ", "manager", ADMIN)

    assert result == {"ok": True, "message": "The user was added to this organization. If they already had a SortView account, "
                                             "their existing password is unchanged."}
    assert db.created == [{"email": "new.person@example.invalid", "password": PASSWORD, "full_name": "Pat Example"}]
    account = _account(db, "new.person@example.invalid")
    assert account["is_active"] and check_password_hash(account["password_hash"], PASSWORD)
    assert db.active_role(ACME, account["id"]) == "manager"
    created, added = db.audit()
    assert (created["event_type"], created["user_id"], created["metadata"]) == ("user_create_success", account["id"], {})
    assert (added["event_type"], added["user_id"], added["email"]) == ("membership_added", account["id"], "new.person@example.invalid")
    assert added["metadata"] == {"org_slug": "acme", "previous_role": None, "role": "manager",
                                 "actor_user_id": ADMIN, "actor_email": _email(ADMIN)}
    assert PASSWORD not in repr(db.audit()) and account["password_hash"] not in repr(db.audit())
    # Creating the account is the account's event; only the membership is the organization's.
    assert [e["event_type"] for e in list_recent_org_auth_events("acme")] == ["membership_added"]


def test_a_new_account_never_outlives_a_membership_that_could_not_be_completed(db, monkeypatch):
    # The account is created, and THEN something fails before the change is complete: here, its record.
    def fail_on_the_membership(conn, event_type, *args, **kwargs):
        if event_type == "membership_added":
            raise RuntimeError("the membership could not be completed")
        record_audit(conn, event_type, *args, **kwargs)

    monkeypatch.setattr(auth_service, "log_auth_event_with_connection", fail_on_the_membership)
    before = db.snapshot()

    with pytest.raises(RuntimeError):
        _add("acme", "pat@example.invalid", "viewer", OWNER)

    assert len(db.created) == 1                       # the creation really ran ...
    assert _account(db, "pat@example.invalid") is None   # ... and was rolled back with everything else
    assert db.snapshot() == before


def test_creating_an_account_that_already_exists_writes_and_records_nothing(db, monkeypatch):
    # What add_organization_member relies on when someone else creates the address in the same instant.
    monkeypatch.undo()
    monkeypatch.setattr(auth_service, "log_auth_event_with_connection", record_audit)
    before = db.snapshot()

    with db.engine.begin() as conn:
        again = auth_service.create_user_with_connection(conn, _email(STRANGER).upper(), "another-password", "Another Name")

    assert again is None and db.snapshot() == before


def test_an_account_that_appears_between_the_lookup_and_the_insert_is_attached_as_it_is(db, monkeypatch):
    # The lookup finds nobody; by the time the INSERT runs, the account exists (STRANGER's).
    find = user_admin_service._FIND_ACCOUNT_SQL
    monkeypatch.setattr(user_admin_service, "_FIND_ACCOUNT_SQL", find.replace(":email", "'nobody-yet' || :email"))
    really_create = auth_service.create_user_with_connection

    def create_then_reveal(conn, **kwargs):
        monkeypatch.setattr(user_admin_service, "_FIND_ACCOUNT_SQL", find)
        return really_create(conn, **kwargs)

    monkeypatch.setattr(auth_service, "create_user_with_connection", create_then_reveal)
    account = db.rows("SELECT * FROM app_users WHERE id = :u", u=STRANGER)

    assert _add("acme", _email(STRANGER), "viewer", OWNER)["ok"]

    assert db.rows("SELECT * FROM app_users WHERE id = :u", u=STRANGER) == account
    assert db.active_role(ACME, STRANGER) == "viewer"
    assert [e["event_type"] for e in db.audit()] == ["membership_added"]  # no creation is claimed


def test_an_inactive_existing_account_is_added_as_it_is_and_not_switched_back_on(db):
    db.run("UPDATE app_users SET is_active = 0 WHERE id = :u", u=STRANGER)
    account = db.rows("SELECT * FROM app_users WHERE id = :u", u=STRANGER)

    assert _add("acme", _email(STRANGER), "viewer", OWNER, password="a-password-that-must-be-ignored")["ok"]

    assert db.rows("SELECT * FROM app_users WHERE id = :u", u=STRANGER) == account and not db.account_active(STRANGER)
    assert db.active_role(ACME, STRANGER) == "viewer" and db.created == []


def test_an_existing_account_gains_only_the_membership_and_the_answer_does_not_say_it_existed(db):
    account = db.rows("SELECT * FROM app_users WHERE id = :u", u=BETA_OWNER)

    result = _add("acme", _email(BETA_OWNER).upper(), "viewer", ADMIN, password="a-password-that-must-be-ignored")

    assert result == _add("beta", "brand.new@example.invalid", "viewer", BETA_OWNER)  # the same words as for a new account
    assert db.created == [{"email": "brand.new@example.invalid", "password": PASSWORD, "full_name": "Pat Example"}]
    assert db.rows("SELECT * FROM app_users WHERE id = :u", u=BETA_OWNER) == account  # hash, name, active: untouched
    assert (db.active_role(ACME, BETA_OWNER), db.active_role(BETA, BETA_OWNER)) == ("viewer", "owner")


def test_someone_already_here_is_not_added_twice_or_given_a_role_by_the_back_door(db):
    before = db.snapshot()

    assert _add("acme", _email(VIEWER), "admin", OWNER)["code"] == "already_member"
    assert db.snapshot() == before and db.active_role(ACME, VIEWER) == "viewer"


@pytest.mark.parametrize(("actor", "role", "code"), [(ADMIN, "owner", "owner_required"), (OWNER, "owner", None),
                                                     (ADMIN, "admin", None), (OWNER, "root", "invalid_role")])
def test_who_may_add_as_what(db, actor, role, code):
    result = _add("acme", _email(STRANGER), role, actor)

    assert result.get("code") == code
    assert db.active_role(ACME, STRANGER) == (role if code is None else None)
    if code is not None:
        assert db.created == [] and db.audit() == []


@pytest.mark.parametrize(("email", "password"), [("", PASSWORD), ("   ", PASSWORD), ("not-an-address", PASSWORD),
                                                 (None, PASSWORD), ("pat@example.invalid", ""), ("pat@example.invalid", None)])
def test_an_add_without_an_address_and_a_password_is_refused_before_anything_is_read(db, email, password, monkeypatch):
    monkeypatch.setattr(user_admin_service, "get_engine", lambda: pytest.fail("the database must not be opened"))

    assert add_organization_member("acme", email, password, "Pat", "viewer", actor_user_id=OWNER)["code"] == "invalid_member_details"


@pytest.mark.parametrize(("actor", "role", "code"), [(ADMIN, "viewer", None), (ADMIN, "admin", None), (ADMIN, "owner", "owner_required"),
                                                     (OWNER, "owner", None)])
def test_adding_back_a_removed_owner_is_decided_by_the_role_given_now_not_the_one_they_had(db, actor, role, code):
    assert remove_organization_member("acme", SECOND, actor_user_id=OWNER)["ok"]
    assert db.membership(ACME, SECOND)["role"] == "owner"  # what the removed row still says

    result = _add("acme", _email(SECOND), role, actor)

    assert result.get("code") == code
    assert db.active_role(ACME, SECOND) == (role if code is None else None)
    assert len(db.rows("SELECT 1 FROM memberships WHERE organization_id = :o AND user_id = :u", o=ACME, u=SECOND)) == 1
    assert db.created == []
    if code is None:
        assert db.audit()[-1]["metadata"]["previous_role"] is None  # nothing is carried over from before


def test_a_refused_account_creation_adds_nobody_and_relays_no_text(db, monkeypatch, caplog):
    def refuse(_conn, **_kwargs):
        raise ValueError("CANARY password=hunter2 for pat@example.invalid")

    monkeypatch.setattr(auth_service, "create_user_with_connection", refuse)

    result = _add("acme", "pat@example.invalid", "viewer", OWNER)

    assert result == {"ok": False, "code": "member_not_added", "message": user_admin_service.USER_CREATE_FAILED_MESSAGE}
    assert db.audit() == [] and "CANARY" not in repr(result) and "CANARY" not in caplog.text and "hunter2" not in caplog.text


# =====================================================================================================================
# What happened in this organization
# =====================================================================================================================

def test_activity_is_only_what_was_recorded_as_happening_in_this_organization(db):
    def log(event_type, user_id, **metadata):
        with db.engine.begin() as conn:
            record_audit(conn, event_type, True, user_id=user_id, email=_email(user_id), message=event_type, metadata=metadata)

    log("login_success", BOTH)                                      # a sign-in: the account's, no organization's
    log("password_reset_completed", BOTH)
    log("password_changed", OWNER)
    log("user_status_updated", BOTH, scope="account", is_active=False)
    log("membership_role_updated", BOTH, org_slug="beta", role="admin")   # in the OTHER organization BOTH belongs to
    log("membership_removed", VIEWER, org_slug="acme-archive")          # a different slug that merely starts the same
    assert change_organization_member_role("acme", BOTH, "manager", actor_user_id=ADMIN)["ok"]

    acme, beta = list_recent_org_auth_events("acme"), list_recent_org_auth_events("beta")

    assert [(e["event_type"], e["email"]) for e in acme] == [("membership_role_updated", _email(BOTH))]
    assert [(e["event_type"], e["email"]) for e in beta] == [("membership_role_updated", _email(BOTH))]
    assert '"beta"' not in repr(acme[0]["metadata"]) and '"acme"' not in repr(beta[0]["metadata"])
    assert list_recent_org_auth_events("paused") == [] and list_recent_org_auth_events("") == []


def test_activity_does_not_follow_a_person_into_an_organization_they_join_later(db):
    assert change_organization_member_role("beta", BOTH, "viewer", actor_user_id=BETA_OWNER)["ok"]
    assert _add("acme", _email(BETA_OWNER), "viewer", OWNER)["ok"]  # beta's owner now belongs to acme too

    assert [e["event_type"] for e in list_recent_org_auth_events("acme")] == ["membership_added"]


def test_activity_of_given_kinds_is_selected_before_it_is_limited(db):
    def log(event_type, **metadata):
        with db.engine.begin() as conn:
            record_audit(conn, event_type, True, user_id=VIEWER, email=_email(VIEWER), message=event_type, metadata=metadata)

    for role in ("manager", "admin", "viewer"):                       # the oldest three: membership changes
        assert change_organization_member_role("acme", VIEWER, role, actor_user_id=OWNER)["ok"]
    for _ in range(10):                                               # ten newer events of another kind
        log("user_status_updated", org_slug="acme")
    log("membership_removed", org_slug="beta")                        # another organization's
    log("membership_removed")                                         # attributed to no organization

    kinds = user_admin_service.MEMBERSHIP_EVENT_TYPES
    assert kinds == ("membership_added", "membership_role_updated", "membership_removed")

    # Unfiltered, as the dashboard asks: the newest of everything attributed to acme.
    assert {e["event_type"] for e in list_recent_org_auth_events("acme", limit=5)} == {"user_status_updated"}
    # Of the membership kinds: the limit counts THOSE, newest first, and still only acme's.
    filtered = list_recent_org_auth_events("acme", limit=2, event_types=kinds)
    assert [e["event_type"] for e in filtered] == ["membership_role_updated"] * 2
    assert ['"role": "viewer"' in e["metadata"] for e in filtered] == [True, False]
    assert len(list_recent_org_auth_events("acme", limit=50, event_types=kinds)) == 3
    assert list_recent_org_auth_events("acme", limit=50, event_types=("membership_removed",)) == []
    assert list_recent_org_auth_events("acme", limit=50, event_types=()) == []
    assert len(list_recent_org_auth_events("acme", limit=50)) == 13


def test_activity_is_newest_first_and_limited(db):
    for role in ("manager", "admin", "viewer"):
        assert change_organization_member_role("acme", VIEWER, role, actor_user_id=OWNER)["ok"]

    assert [e["metadata"] for e in list_recent_org_auth_events("acme", limit=2)] != []
    assert len(list_recent_org_auth_events("acme", limit=2)) == 2
    assert '"role": "viewer"' in list_recent_org_auth_events("acme", limit=1)[0]["metadata"]


# =====================================================================================================================
# The whole account: a platform operation
# =====================================================================================================================

@pytest.mark.parametrize("actor", [OWNER, ADMIN, BETA_OWNER, VIEWER, STRANGER, 424242], ids=["owner", "admin", "other-owner", "viewer", "stranger", "nobody"])
def test_no_organization_role_can_switch_an_account_off_or_on(db, actor):
    db.session(VIEWER, "hash-of-a-session")
    before = db.snapshot()

    for desired in (False, True):
        assert set_global_account_active(VIEWER, desired, actor_user_id=actor)["code"] == "platform_admin_required"
    assert db.snapshot() == before


def test_a_platform_administrator_whose_own_account_is_off_cannot(db):
    db.run("UPDATE app_users SET is_active = 0 WHERE id = :u", u=PLATFORM)

    assert set_global_account_active(VIEWER, False, actor_user_id=PLATFORM)["code"] == "platform_admin_required"
    assert db.account_active(VIEWER)


def test_switching_an_account_off_is_account_wide_and_ends_every_session(db):
    db.session(BOTH, "hash-one")
    db.session(BOTH, "hash-two")
    db.session(VIEWER, "hash-of-someone-else")
    memberships = db.rows("SELECT * FROM memberships ORDER BY id")

    assert set_global_account_active(BOTH, False, actor_user_id=PLATFORM) == {"ok": True, "message": "Account status updated."}

    assert not db.account_active(BOTH) and db.live_sessions(BOTH) == 0
    assert db.live_sessions(VIEWER) == 1 and db.account_active(VIEWER)
    assert db.rows("SELECT * FROM memberships ORDER BY id") == memberships  # no membership is removed by it
    [event] = db.audit()
    assert (event["event_type"], event["user_id"]) == ("user_status_updated", BOTH)
    assert event["metadata"] == {"scope": "account", "previous_is_active": True, "is_active": False,
                                 "actor_user_id": PLATFORM, "actor_email": _email(PLATFORM)}
    # Account-wide, so it is not an event IN either organization the person belongs to.
    assert list_recent_org_auth_events("acme") == [] and list_recent_org_auth_events("beta") == []


def test_switching_an_account_back_on_restores_no_session(db):
    db.session(VIEWER, "hash-one")
    assert set_global_account_active(VIEWER, False, actor_user_id=PLATFORM)["ok"]

    assert set_global_account_active(VIEWER, True, actor_user_id=PLATFORM)["ok"]

    assert db.account_active(VIEWER) and db.live_sessions(VIEWER) == 0
    assert [e["metadata"]["is_active"] for e in db.audit()] == [False, True]


def test_an_account_that_is_any_organizations_only_active_owner_cannot_be_switched_off(db):
    db.member(BETA, OWNER, "owner")           # OWNER co-owns beta with BETA_OWNER ...
    db.session(OWNER, "hash-one")
    assert set_global_account_active(OWNER, False, actor_user_id=PLATFORM)["ok"]  # ... and acme with SECOND: fine

    assert set_global_account_active(OWNER, True, actor_user_id=PLATFORM)["ok"]
    db.run("UPDATE memberships SET removed_at = '2026-01-01' WHERE organization_id = :o AND user_id = :u", o=BETA, u=BETA_OWNER)
    before = db.snapshot()

    # acme still has SECOND, but beta would be left with nobody: refused, and NOTHING is changed anywhere.
    assert set_global_account_active(OWNER, False, actor_user_id=PLATFORM)["code"] == "last_owner"
    assert db.snapshot() == before and db.account_active(OWNER)


def test_a_suspended_organization_keeps_its_owner_and_a_cancelled_one_holds_nobody(db):
    assert set_global_account_active(PAUSED_OWNER, False, actor_user_id=PLATFORM)["code"] == "last_owner"
    assert set_global_account_active(CLOSED_OWNER, False, actor_user_id=PLATFORM)["ok"]


def test_someone_who_owns_nothing_actively_can_be_switched_off(db):
    db.run("UPDATE memberships SET removed_at = '2026-01-01' WHERE organization_id = :o AND user_id = :u", o=BETA, u=BETA_OWNER)

    for user_id in (ADMIN, BETA_OWNER, STRANGER):   # an admin; an owner who was removed; no membership at all
        assert set_global_account_active(user_id, False, actor_user_id=PLATFORM)["ok"]


def test_unknown_and_platform_administrator_accounts_are_refused(db):
    db.user(901, platform_admin=True)
    before = db.snapshot()

    assert set_global_account_active(424242, False, actor_user_id=PLATFORM)["code"] == "user_not_found"
    assert set_global_account_active(901, False, actor_user_id=PLATFORM)["code"] == "platform_admin_account"
    assert set_global_account_active(PLATFORM, False, actor_user_id=PLATFORM)["code"] == "platform_admin_account"
    assert db.snapshot() == before


def test_after_an_owners_account_is_switched_off_the_remaining_owner_is_the_last(db):
    assert set_global_account_active(SECOND, False, actor_user_id=PLATFORM)["ok"]

    assert remove_organization_member("acme", OWNER, actor_user_id=OWNER)["code"] == "last_owner"
    assert change_organization_member_role("acme", OWNER, "admin", actor_user_id=OWNER)["code"] == "last_owner"


def test_every_refusal_has_fixed_text_and_a_stable_code():
    codes = {"not_permitted", "owner_required", "invalid_role", "organization_not_found", "organization_not_editable",
             "member_not_found", "already_member", "last_owner", "invalid_member_details", "member_not_added",
             "platform_admin_required", "user_not_found", "platform_admin_account", "concurrent_change"}

    assert set(user_admin_service._MESSAGES) == codes
    assert all(isinstance(m, str) and m for m in user_admin_service._MESSAGES.values())
    assert user_admin_service.ALLOWED_MEMBERSHIP_ROLES == ["owner", "admin", "manager", "viewer"]
