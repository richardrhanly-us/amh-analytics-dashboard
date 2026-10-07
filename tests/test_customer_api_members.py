"""R8D: an organization's members -- the customer API routes.

    GET  /api/organizations/{org_slug}/members
    POST /api/organizations/{org_slug}/members
    PUT  /api/organizations/{org_slug}/members/role
    POST /api/organizations/{org_slug}/members/remove
    GET  /api/organizations/{org_slug}/members/activity

These tests drive the real production routes through TestClient(main.app), over the REAL services
(services.user_admin_service, access_service, entitlement_service) on the in-memory database of
tests/membership_db.py. Only the session is stood in for (the cookie's value is the user's id), and the audit log's
PostgreSQL-only jsonb is read and written as that helper describes.

What is tested HERE is the API: who may call it and in what order that is decided, the Origin check, what a body may
hold, what an answer looks like and what it never holds, how an address becomes a member, and how each of the service's
answers is mapped. What the service decides -- the role rules, the last owner, two changes at once, the new account and
its membership being one transaction -- is tested where it lives (tests/test_user_admin_service.py,
tests/test_membership_policy.py, tests/test_user_admin_postgres.py) and is not repeated.

The world:

    acme    OWNER (owner)  SECOND (owner)  ADMIN (admin)  MANAGER (manager)  VIEWER (viewer)  BOTH (viewer)
    beta    BETA_OWNER (owner)  BOTH (admin)
    paused  (suspended)  OWNER (owner)
    closed  (cancelled)  OWNER (owner)

STRANGER has an account and no membership; PLATFORM is a platform administrator with no membership.
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from membership_db import MembershipDatabase, record_audit
from werkzeug.security import check_password_hash

import main
from customer_api import member_routes, member_schemas
from services import (
    access_service,
    auth_service,
    entitlement_service,
    session_service,
    user_admin_service,
)

ORIGIN = "https://app.example.invalid"
COOKIE = "__Host-sortview_api_session"
MEMBERS = "/api/organizations/{org}/members"
ACME = MEMBERS.format(org="acme")

ACME_ID, BETA_ID, PAUSED_ID, CLOSED_ID = 1, 2, 3, 4
# Distinctive ids, so none can be in a response by accident. The session cookie's value is the user's id.
OWNER, SECOND, ADMIN, MANAGER, VIEWER, BOTH = 900101, 900102, 900103, 900104, 900105, 900106
BETA_OWNER, STRANGER, PLATFORM = 900201, 900501, 900900
ALL_IDS = (OWNER, SECOND, ADMIN, MANAGER, VIEWER, BOTH, BETA_OWNER, STRANGER, PLATFORM)

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
ORGANIZATION_NOT_FOUND = {"code": "organization_not_found", "message": "Organization not found."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have permission to manage this organization's members."}
OWNER_REQUIRED = {"code": "owner_required", "message": "Only an owner of the organization can do that."}
READ_ONLY = {"code": "organization_read_only", "message": "This organization's members cannot be changed."}
MEMBER_NOT_FOUND = {"code": "member_not_found", "message": "That person is not a member of this organization."}
ALREADY_MEMBER = {"code": "already_member", "message": "That person is already a member of this organization."}
LAST_OWNER = {"code": "last_owner", "message": "An organization must keep at least one active owner."}
ORIGIN_NOT_ALLOWED = {"code": "origin_not_allowed", "message": "Request origin is not allowed."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}


# Addresses carry no id, so an id found in an answer is an id that was returned.
NAMES = {OWNER: "olive", SECOND: "sam", ADMIN: "ada", MANAGER: "max", VIEWER: "vera", BOTH: "bo", BETA_OWNER: "bea",
         STRANGER: "stan", PLATFORM: "pat"}


def _email(user_id: int) -> str:
    return f"{NAMES.get(user_id, 'nobody')}@example.invalid"


@pytest.fixture
def db(monkeypatch):
    database = MembershipDatabase()
    for organization_id, slug, status in ((ACME_ID, "acme", "active"), (BETA_ID, "beta", "trial"), (PAUSED_ID, "paused", "suspended"),
                                          (CLOSED_ID, "closed", "cancelled")):
        database.organization(organization_id, slug, status)
    for user_id in ALL_IDS:
        database.user(user_id, _email(user_id), platform_admin=user_id == PLATFORM)
    database.run("UPDATE app_users SET full_name = 'Olive Owner' WHERE id = :u", u=OWNER)
    for organization_id, user_id, role in (
        (ACME_ID, OWNER, "owner"), (ACME_ID, SECOND, "owner"), (ACME_ID, ADMIN, "admin"), (ACME_ID, MANAGER, "manager"),
        (ACME_ID, VIEWER, "viewer"), (ACME_ID, BOTH, "viewer"), (BETA_ID, BETA_OWNER, "owner"), (BETA_ID, BOTH, "admin"),
        (PAUSED_ID, OWNER, "owner"), (CLOSED_ID, OWNER, "owner"),
    ):
        database.member(organization_id, user_id, role)

    for module in (user_admin_service, access_service, entitlement_service):
        monkeypatch.setattr(module, "get_engine", lambda: database.engine)
    monkeypatch.setattr(auth_service, "log_auth_event_with_connection", record_audit)
    monkeypatch.setattr(
        session_service, "validate_session",
        lambda raw_token: {"id": int(raw_token), "email": _email(int(raw_token)), "full_name": "Test User"} if raw_token.isdigit() else None,
    )
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", ORIGIN)

    # The audit rows as PostgreSQL hands them back: metadata a dict (JSONB), created_at an aware instant. SQLite
    # stores both as text; the query itself is the service's own and runs for real.
    really_list = user_admin_service.list_recent_org_auth_events

    def list_recent_org_auth_events(org_slug, limit=25, **kwargs):
        return [
            {**event, "metadata": json.loads(event["metadata"]),
             "created_at": datetime.fromisoformat(event["created_at"]).replace(tzinfo=UTC)}
            for event in really_list(org_slug, limit=limit, **kwargs)
        ]

    monkeypatch.setattr(user_admin_service, "list_recent_org_auth_events", list_recent_org_auth_events)
    return database


@pytest.fixture
def api(db):
    main.limiter.reset()
    yield TestClient(main.app, base_url="https://testserver")
    main.limiter.reset()


def _as(user: int | None, *, origin: str | None = ORIGIN) -> dict[str, str]:
    headers = {}
    if user is not None:
        headers["Cookie"] = f"{COOKIE}={user}"
    if origin is not None:
        headers["Origin"] = origin
    return headers


def _get(api, path, user=OWNER):
    return api.get(path, headers=_as(user, origin=None))


def _add(api, body, user=OWNER, org="acme", **kwargs):
    return api.post(MEMBERS.format(org=org), json=body, headers=_as(user, **kwargs))


def _role(api, body, user=OWNER, org="acme", **kwargs):
    return api.put(MEMBERS.format(org=org) + "/role", json=body, headers=_as(user, **kwargs))


def _remove(api, body, user=OWNER, org="acme", **kwargs):
    return api.post(MEMBERS.format(org=org) + "/remove", json=body, headers=_as(user, **kwargs))


def _every_write(api, user, org="acme", **kwargs):
    return (
        _add(api, {"email": "new@example.invalid", "role": "viewer"}, user, org, **kwargs),
        _role(api, {"email": _email(VIEWER), "role": "manager"}, user, org, **kwargs),
        _remove(api, {"email": _email(VIEWER)}, user, org, **kwargs),
    )


def _every_request(api, user, org="acme"):
    return (_get(api, MEMBERS.format(org=org), user), _get(api, MEMBERS.format(org=org) + "/activity", user), *_every_write(api, user, org))


def _no_content(response) -> None:
    assert response.status_code == 204, response.text
    assert response.content == b"" and response.headers["cache-control"] == "no-store"


def _refused(response, status: int, body: dict) -> None:
    assert response.status_code == status, response.text
    assert response.json() == body and response.headers["cache-control"] == "no-store"


# =====================================================================================================================
# Who may call these routes
# =====================================================================================================================

def test_without_a_session_every_route_is_401_and_nothing_is_read_or_written(api, db):
    before = db.snapshot()

    for response in (*_every_request(api, None), *(api.get(ACME, headers={"Cookie": f"{COOKIE}=not-a-session"}),)):
        _refused(response, 401, NOT_AUTHENTICATED)

    assert db.snapshot() == before


@pytest.mark.parametrize(("user", "org"), [(STRANGER, "acme"), (BETA_OWNER, "acme"), (PLATFORM, "acme"), (OWNER, "beta"),
                                          (OWNER, "closed"), (OWNER, "nowhere")],
                         ids=["no-membership", "owner-of-another-org", "platform-admin", "not-a-member-there", "cancelled", "unknown"])
def test_an_organization_the_user_cannot_see_is_the_same_404_for_every_route(api, db, user, org):
    before = db.snapshot()

    for response in _every_request(api, user, org):
        _refused(response, 404, ORGANIZATION_NOT_FOUND)

    assert db.snapshot() == before


@pytest.mark.parametrize("user", [MANAGER, VIEWER, BOTH], ids=["manager", "viewer", "viewer-here-admin-elsewhere"])
def test_a_member_who_is_not_an_owner_or_admin_is_forbidden_everything_including_the_list(api, db, user):
    before = db.snapshot()

    for response in _every_request(api, user):
        _refused(response, 403, FORBIDDEN)

    assert db.snapshot() == before


def test_a_removed_admin_is_no_member_on_the_very_next_request(api, db):
    assert _get(api, ACME, ADMIN).status_code == 200
    _no_content(_remove(api, {"email": _email(ADMIN)}, OWNER))
    before = db.snapshot()

    for response in _every_request(api, ADMIN):
        _refused(response, 404, ORGANIZATION_NOT_FOUND)

    assert db.snapshot() == before


def test_a_suspended_organizations_members_can_be_listed_and_not_changed(api, db):
    before = db.snapshot()

    assert _get(api, MEMBERS.format(org="paused"), OWNER).status_code == 200
    assert _get(api, MEMBERS.format(org="paused") + "/activity", OWNER).status_code == 200
    for response in _every_write(api, OWNER, "paused"):
        _refused(response, 403, READ_ONLY)

    assert db.snapshot() == before


def test_the_service_refuses_a_suspended_organization_itself_if_the_routes_check_is_bypassed(api, db, monkeypatch):
    # The route's own check is only an early answer. Without it, the service's locked check gives the same one.
    monkeypatch.setattr(access_service, "get_org_access_mode", lambda org_slug: "full")
    before = db.snapshot()

    for response in (_add(api, {"email": "new@example.invalid", "role": "viewer"}, OWNER, "paused"),
                     _role(api, {"email": _email(OWNER), "role": "admin"}, OWNER, "paused"),
                     _remove(api, {"email": _email(OWNER)}, OWNER, "paused")):
        _refused(response, 403, READ_ONLY)

    assert db.snapshot() == before


def test_the_service_refuses_a_non_admin_itself_if_the_routes_check_is_bypassed(api, db, monkeypatch):
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", lambda user_id, org_slug: "owner")
    before = db.snapshot()

    for response in _every_write(api, VIEWER):
        _refused(response, 403, FORBIDDEN)

    assert db.snapshot() == before


@pytest.mark.parametrize("origin", [None, "https://evil.example.invalid", "null", "https://app.example.invalid/", ""])
def test_every_write_needs_an_allowed_origin_and_it_is_checked_before_anything_else(api, db, origin):
    before = db.snapshot()

    for user in (OWNER, None, STRANGER):  # even before the session: the answer is the same whoever asks
        for response in _every_write(api, user, origin=origin):
            _refused(response, 403, ORIGIN_NOT_ALLOWED)

    assert db.snapshot() == before
    assert _get(api, ACME, OWNER).status_code == 200  # a read needs none


# =====================================================================================================================
# The member list
# =====================================================================================================================

def test_the_list_is_the_active_members_with_exactly_five_fields_each(api, db):
    db.run("UPDATE memberships SET removed_at = '2026-01-01' WHERE organization_id = :o AND user_id = :u", o=ACME_ID, u=MANAGER)
    db.run("UPDATE app_users SET is_active = 0 WHERE id = :u", u=VIEWER)

    response = _get(api, ACME, ADMIN)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"members": [
        {"email": _email(ADMIN), "full_name": "", "role": "admin", "is_self": True, "account_active": True},
        {"email": _email(BOTH), "full_name": "", "role": "viewer", "is_self": False, "account_active": True},
        {"email": _email(OWNER), "full_name": "Olive Owner", "role": "owner", "is_self": False, "account_active": True},
        {"email": _email(SECOND), "full_name": "", "role": "owner", "is_self": False, "account_active": True},
        {"email": _email(VIEWER), "full_name": "", "role": "viewer", "is_self": False, "account_active": False},
    ]}  # in address order


def test_no_answer_holds_an_id_a_hash_or_anything_about_another_organization(api, db):
    _no_content(_role(api, {"email": _email(BOTH), "role": "manager"}, OWNER))
    text = _get(api, ACME, OWNER).text + _get(api, ACME + "/activity", OWNER).text

    for forbidden in (*map(str, ALL_IDS), "CANARY-HASH", "user_id", "actor_user_id", "membership_id", "org_slug", "metadata",
                      "is_platform_admin", "last_login", "created_at", "password", "locked", "beta", _email(BETA_OWNER)):
        assert forbidden not in text, forbidden
    assert set(member_schemas.Member.model_fields) == {"email", "full_name", "role", "is_self", "account_active"}
    assert set(member_schemas.MemberActivity.model_fields) == {"occurred_at", "event_type", "member_email", "actor_email",
                                                                "previous_role", "role"}


# =====================================================================================================================
# Adding
# =====================================================================================================================

def test_adding_a_new_address_creates_an_account_with_a_password_nobody_is_told(api, db, caplog, monkeypatch):
    generated: list[str] = []
    really_generate = member_routes._generated_password
    monkeypatch.setattr(member_routes, "_generated_password", lambda: generated.append(really_generate()) or generated[-1])

    with caplog.at_level(logging.DEBUG):
        response = _add(api, {"email": " New.Person@Example.invalid ", "full_name": "New Person", "role": "manager"}, ADMIN)

    _no_content(response)
    [account] = db.rows("SELECT id, full_name, password_hash, is_active FROM app_users WHERE email = 'new.person@example.invalid'")
    assert account["full_name"] == "New Person" and account["is_active"]
    assert db.active_role(ACME_ID, account["id"]) == "manager"
    # The password: generated here, long and random, hashed by the service -- and nowhere else at all.
    [password] = generated
    assert len(password) >= 64 and check_password_hash(account["password_hash"], password)
    for place in (response.text, repr(dict(response.headers)), caplog.text, repr(db.audit())):
        assert password not in place and account["password_hash"] not in place
    assert [e["event_type"] for e in db.audit()] == ["user_create_success", "membership_added"]
    assert "Organization members changed | change=added org=acme actor_user_id=900103" in caplog.text
    assert "new.person" not in caplog.text.lower() and "New Person" not in caplog.text


def test_every_generated_password_is_different_and_comes_from_the_secrets_module():
    passwords = {member_routes._generated_password() for _ in range(50)}

    assert len(passwords) == 50 and all(len(p) >= 64 for p in passwords)
    source = inspect.getsource(member_routes._generated_password)
    assert "secrets.token_urlsafe(48)" in source and "random." not in source


def test_adding_an_existing_account_changes_nothing_about_it_and_answers_the_same(api, db):
    account = db.rows("SELECT * FROM app_users WHERE id = :u", u=BETA_OWNER)

    existing = _add(api, {"email": _email(BETA_OWNER).upper(), "full_name": "Another Name", "role": "viewer"}, OWNER)
    brand_new = _add(api, {"email": "brand.new@example.invalid", "role": "viewer"}, OWNER)

    _no_content(existing)
    assert (existing.status_code, existing.content, existing.headers["cache-control"]) == (
        brand_new.status_code, brand_new.content, brand_new.headers["cache-control"])
    assert db.rows("SELECT * FROM app_users WHERE id = :u", u=BETA_OWNER) == account  # hash, name, state: untouched
    assert (db.active_role(ACME_ID, BETA_OWNER), db.active_role(BETA_ID, BETA_OWNER)) == ("viewer", "owner")


def test_a_removed_member_can_be_added_back_with_the_role_given_now(api, db):
    _no_content(_remove(api, {"email": _email(SECOND)}, OWNER))
    _refused(_add(api, {"email": _email(SECOND), "role": "owner"}, ADMIN), 403, OWNER_REQUIRED)
    _no_content(_add(api, {"email": _email(SECOND), "role": "viewer"}, ADMIN))

    assert db.active_role(ACME_ID, SECOND) == "viewer"
    assert len(db.rows("SELECT 1 FROM memberships WHERE organization_id = :o AND user_id = :u", o=ACME_ID, u=SECOND)) == 1


@pytest.mark.parametrize("extra", [{"password": "an-administrators-choice"}, {"temporary_password": "x"}, {"user_id": OWNER},
                                   {"actor_user_id": OWNER}, {"actor_role": "owner"}, {"is_active": False}, {"is_platform_admin": True}])
def test_an_add_cannot_carry_a_password_an_actor_or_any_field_it_does_not_take(api, db, extra):
    before = db.snapshot()

    response = _add(api, {"email": "new@example.invalid", "role": "viewer", **extra}, OWNER)

    assert response.status_code == 422 and db.snapshot() == before
    assert set(member_schemas.AddMemberRequest.model_fields) == {"email", "full_name", "role"}


# =====================================================================================================================
# Changing a role, and removing: a member is named by email
# =====================================================================================================================

def test_a_role_change_names_the_member_by_email_in_any_letter_case(api, db):
    _no_content(_role(api, {"email": f"  {_email(VIEWER).upper()} ", "role": "admin"}, OWNER))

    assert db.active_role(ACME_ID, VIEWER) == "admin"
    [event] = db.audit()
    assert (event["event_type"], event["user_id"], event["metadata"]["actor_user_id"]) == ("membership_role_updated", VIEWER, OWNER)


def test_removal_is_from_this_organization_only_and_leaves_the_account_alone(api, db):
    db.session(BOTH, "hash-of-a-session")

    _no_content(_remove(api, {"email": _email(BOTH)}, ADMIN))

    assert db.active_role(ACME_ID, BOTH) is None and db.active_role(BETA_ID, BOTH) == "admin"
    assert db.account_active(BOTH) and db.live_sessions(BOTH) == 1


@pytest.mark.parametrize("email", [_email(STRANGER), _email(BETA_OWNER), _email(PLATFORM), "nobody@example.invalid", "not-an-address", "   "],
                         ids=["no-membership", "member-of-another-org", "platform-admin", "no-account", "not-an-address", "blank"])
def test_an_address_that_is_not_an_active_member_here_is_one_404_whatever_else_is_true_of_it(api, db, email):
    before = db.snapshot()

    _refused(_role(api, {"email": email, "role": "viewer"}, OWNER), 404, MEMBER_NOT_FOUND)
    _refused(_remove(api, {"email": email}, OWNER), 404, MEMBER_NOT_FOUND)

    assert db.snapshot() == before


def test_a_removed_member_is_not_found_by_address(api, db):
    _no_content(_remove(api, {"email": _email(VIEWER)}, OWNER))

    _refused(_role(api, {"email": _email(VIEWER), "role": "admin"}, OWNER), 404, MEMBER_NOT_FOUND)
    _refused(_remove(api, {"email": _email(VIEWER)}, OWNER), 404, MEMBER_NOT_FOUND)
    assert user_admin_service.find_active_member_id("acme", _email(VIEWER)) is None


def test_the_lookup_finds_only_an_active_member_of_that_organization(db):
    find = user_admin_service.find_active_member_id

    assert find("acme", _email(BOTH)) == BOTH and find("beta", _email(BOTH).upper()) == BOTH
    assert find("acme", _email(BETA_OWNER)) is None and find("beta", _email(OWNER)) is None
    assert find("nowhere", _email(OWNER)) is None and find("acme", "") is None and find("acme", None) is None
    source = inspect.getsource(user_admin_service).split("_FIND_ACTIVE_MEMBER_SQL", 1)[1].split('"""', 2)[1]
    assert "m.removed_at IS NULL" in source and "o.slug = :org_slug" in source and "lower(u.email) = :email" in source


@pytest.mark.parametrize("extra", [{"user_id": VIEWER}, {"id": VIEWER}, {"actor_user_id": OWNER}, {"password": "x"}])
def test_a_role_change_and_a_removal_take_no_id_and_no_other_field(api, db, extra):
    before = db.snapshot()

    assert _role(api, {"email": _email(VIEWER), "role": "admin", **extra}, OWNER).status_code == 422
    assert _remove(api, {"email": _email(VIEWER), **extra}, OWNER).status_code == 422
    assert _role(api, {"user_id": VIEWER, "role": "admin"}, OWNER).status_code == 422  # an id instead of an address
    assert db.snapshot() == before


# =====================================================================================================================
# The service's answers, as this API's
# =====================================================================================================================

def test_the_services_real_refusals_reach_the_client_as_this_apis_own_codes_and_messages(api, db):
    _refused(_add(api, {"email": "new@example.invalid", "role": "owner"}, ADMIN), 403, OWNER_REQUIRED)
    _refused(_role(api, {"email": _email(OWNER), "role": "viewer"}, ADMIN), 403, OWNER_REQUIRED)
    _refused(_remove(api, {"email": _email(OWNER)}, ADMIN), 403, OWNER_REQUIRED)
    _refused(_add(api, {"email": _email(VIEWER), "role": "admin"}, OWNER), 409, ALREADY_MEMBER)

    db.run("UPDATE memberships SET role = 'admin' WHERE organization_id = :o AND user_id = :u", o=ACME_ID, u=SECOND)
    before = db.snapshot()
    _refused(_role(api, {"email": _email(OWNER), "role": "admin"}, OWNER), 409, LAST_OWNER)
    _refused(_remove(api, {"email": _email(OWNER)}, OWNER), 409, LAST_OWNER)
    assert db.snapshot() == before


@pytest.mark.parametrize(("write", "body", "problem"), [
    (_add, {"email": "new@example.invalid", "role": "superuser"}, {"field": "role", "code": "invalid"}),
    (_role, {"email": _email(VIEWER), "role": "Owner"}, {"field": "role", "code": "invalid"}),
    (_add, {"email": "not-an-address", "role": "viewer"}, {"field": "email", "code": "invalid"}),
])
def test_a_value_the_service_will_not_accept_is_a_422_naming_the_field_and_never_the_value(api, db, write, body, problem):
    before = db.snapshot()

    response = write(api, body, OWNER)

    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": "invalid_member", "message": "The member details are not valid.", "problems": [problem]}
    assert body["role"] not in response.text and body["email"] not in response.text
    assert db.snapshot() == before


SERVICE_MESSAGE = "CANARY a service message that must never reach a client"


@pytest.mark.parametrize(("code", "status", "body"), [
    ("not_permitted", 403, FORBIDDEN),
    ("owner_required", 403, OWNER_REQUIRED),
    ("organization_not_found", 404, ORGANIZATION_NOT_FOUND),
    ("organization_not_editable", 403, READ_ONLY),
    ("member_not_found", 404, MEMBER_NOT_FOUND),
    ("already_member", 409, ALREADY_MEMBER),
    ("last_owner", 409, LAST_OWNER),
    ("member_not_added", 500, INTERNAL_ERROR),
    ("concurrent_change", 500, INTERNAL_ERROR),
    ("platform_admin_required", 500, INTERNAL_ERROR),
    ("a_code_nobody_has_heard_of", 500, INTERNAL_ERROR),
    (None, 500, INTERNAL_ERROR),
])
def test_every_service_code_is_mapped_and_the_services_own_message_is_never_relayed(api, db, monkeypatch, code, status, body):
    for name in ("add_organization_member", "change_organization_member_role", "remove_organization_member"):
        monkeypatch.setattr(user_admin_service, name, lambda *_a, **_k: {"ok": False, "code": code, "message": SERVICE_MESSAGE})

    for response in _every_write(api, OWNER):
        _refused(response, status, body)
        assert "CANARY" not in response.text


def test_the_mapping_covers_exactly_the_organization_level_codes_the_service_has():
    organization_level = set(user_admin_service._MESSAGES) - {"platform_admin_required", "user_not_found", "platform_admin_account",
                                                             "concurrent_change", "member_not_added"}

    assert set(member_routes._REFUSALS) | set(member_routes._PROBLEMS) == organization_level


def test_a_database_failure_is_a_generic_500_and_not_a_refusal(api, db, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("CANARY connection to server at 10.0.0.1 failed")

    monkeypatch.setattr(user_admin_service, "find_active_member_id", fail)
    monkeypatch.setattr(user_admin_service, "list_org_users", fail)

    for response in (_get(api, ACME, OWNER), _role(api, {"email": _email(VIEWER), "role": "admin"}, OWNER)):
        _refused(response, 500, INTERNAL_ERROR)
        assert "CANARY" not in response.text


# =====================================================================================================================
# Activity
# =====================================================================================================================

def test_activity_is_this_organizations_membership_changes_newest_first_with_six_fields(api, db):
    _no_content(_role(api, {"email": _email(VIEWER), "role": "manager"}, OWNER))
    _no_content(_remove(api, {"email": _email(MANAGER)}, ADMIN))
    _no_content(_role(api, {"email": _email(BOTH), "role": "viewer"}, BETA_OWNER, org="beta"))  # another organization's

    def log(event_type, user_id, **metadata):
        with db.engine.begin() as conn:
            record_audit(conn, event_type, True, user_id=user_id, email=_email(user_id), message=event_type, metadata=metadata)

    log("login_success", VIEWER)                                        # the account's: no organization's
    log("user_status_updated", VIEWER, org_slug="acme", is_active=False)   # an old, org-tagged account event
    log("something_else_entirely", VIEWER, org_slug="acme")

    response = _get(api, ACME + "/activity", ADMIN)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    activity = response.json()["activity"]
    assert [{k: v for k, v in event.items() if k != "occurred_at"} for event in activity] == [
        {"event_type": "membership_removed", "member_email": _email(MANAGER), "actor_email": _email(ADMIN),
         "previous_role": "manager", "role": None},
        {"event_type": "membership_role_updated", "member_email": _email(VIEWER), "actor_email": _email(OWNER),
         "previous_role": "viewer", "role": "manager"},
    ]
    for event in activity:
        assert set(event) == {"occurred_at", "event_type", "member_email", "actor_email", "previous_role", "role"}
        assert event["occurred_at"].endswith(("Z", "+00:00"))
    assert member_routes._ACTIVITY_EVENT_TYPES == ("membership_added", "membership_role_updated", "membership_removed")


def test_the_limit_counts_membership_changes_and_newer_events_of_other_kinds_do_not_crowd_them_out(api, db):
    limit = member_routes._ACTIVITY_LIMIT

    def log(event_type, user_id, **metadata):
        with db.engine.begin() as conn:
            record_audit(conn, event_type, True, user_id=user_id, email=_email(user_id), message=event_type, metadata=metadata)

    # OLDEST: more membership changes than the limit ...
    roles = ["manager", "viewer"] * (limit // 2 + 2)
    for role in roles:
        _no_content(_role(api, {"email": _email(VIEWER), "role": role}, OWNER))
    # ... then, NEWER than all of them, more than the limit of other events attributed to this organization.
    for _ in range(limit + 10):
        log("user_status_updated", VIEWER, org_slug="acme", is_active=True)
    assert len(user_admin_service.list_recent_org_auth_events("acme", limit=limit)) == limit  # all of the other kind

    activity = _get(api, ACME + "/activity").json()["activity"]

    # A full page of membership changes, the most recent of them, newest first: not an empty answer.
    assert len(activity) == limit
    assert {event["event_type"] for event in activity} == {"membership_role_updated"}
    assert [event["role"] for event in activity] == list(reversed(roles))[:limit]


def test_the_route_asks_the_service_for_the_three_kinds_and_the_limit(api, db, monkeypatch):
    asked: list = []
    monkeypatch.setattr(user_admin_service, "list_recent_org_auth_events",
                        lambda org_slug, limit=25, **kwargs: asked.append((org_slug, limit, kwargs)) or [])

    assert _get(api, ACME + "/activity").json() == {"activity": []}
    assert asked == [("acme", 50, {"event_types": ("membership_added", "membership_role_updated", "membership_removed")})]


def test_an_activity_instant_is_written_in_utc_and_a_naive_one_is_refused(api, db, monkeypatch):
    from datetime import timedelta, timezone

    central = timezone(timedelta(hours=-5))
    event = {"event_type": "membership_added", "email": "a@example.invalid", "metadata": {"role": "viewer"},
             "created_at": datetime(2026, 10, 6, 13, 0, tzinfo=central)}
    monkeypatch.setattr(user_admin_service, "list_recent_org_auth_events", lambda org_slug, limit=25, **_kwargs: [event])
    assert _get(api, ACME + "/activity").json()["activity"][0]["occurred_at"] in ("2026-10-06T18:00:00Z", "2026-10-06T18:00:00+00:00")

    monkeypatch.setattr(user_admin_service, "list_recent_org_auth_events",
                        lambda org_slug, limit=25, **_kwargs: [{**event, "created_at": event["created_at"].replace(tzinfo=None)}])
    _refused(_get(api, ACME + "/activity"), 500, INTERNAL_ERROR)


# =====================================================================================================================
# The shape of the module
# =====================================================================================================================

def test_the_member_routes_are_exactly_these_five_and_every_write_checks_the_origin():
    routes = {(method, route.path): route for route in main.customer_router.routes for method in route.methods
              if "/members" in route.path}

    assert sorted(routes) == [
        ("GET", "/api/organizations/{org_slug}/members"),
        ("GET", "/api/organizations/{org_slug}/members/activity"),
        ("POST", "/api/organizations/{org_slug}/members"),
        ("POST", "/api/organizations/{org_slug}/members/remove"),
        ("PUT", "/api/organizations/{org_slug}/members/role"),
    ]
    for (method, _path), route in routes.items():
        guarded = member_routes.require_allowed_origin in [d.call for d in route.dependant.dependencies]
        assert guarded is (method != "GET"), route.path
        # Only the organization is in the path: never an address or an id.
        assert [p.name for p in route.dependant.path_params] == ["org_slug"]
        assert [p.name for p in route.dependant.query_params] == []


def test_the_routes_cannot_switch_an_account_off_and_decide_no_role_rule_themselves():
    source = inspect.getsource(member_routes)
    code = source.split('"""', 2)[2]

    for forbidden in ("set_global_account_active", "is_active =", "app_users", "membership_policy", "text(", "get_engine",
                      "session_service", "revoke", "email_service", "send_", "create_user", '"owner"', "'owner'"):
        assert forbidden not in code, forbidden
    # Who is acting is always the session's user, and nothing else.
    assert code.count('actor_user_id=user["id"]') == 3 and "actor_user_id=data" not in code
    assert not hasattr(member_routes, "set_global_account_active")
