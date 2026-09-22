"""Organization lifecycle policy enforcement for user_admin_service's mutating
functions (create_or_add_org_user, update_org_user_role, set_user_active).

A suspended or cancelled organization must never be able to add/change/
deactivate a user through this service, regardless of what the calling page
already checked -- this is the service-level enforcement layer, independent
of any page-level gate. Each mutating function is expected to call
access_service.get_org_access_mode(org_slug) itself and refuse before
touching the database when the result isn't "full".

set_user_active additionally verifies the target user is actually a member
of the calling org_slug before mutating anything -- but the mutation itself
(app_users.is_active) remains a GLOBAL account flag, exactly as before this
change: this file does not test, and the function does not implement, any
per-organization scoping of account access. That is documented in
set_user_active's own docstring as a separate, not-yet-made decision.
"""

import pytest
from db_fakes import FakeEngine, FakeQueryResult

from src.services import user_admin_service


class _UpdateResult:
    """A minimal stand-in for a SQLAlchemy CursorResult's .rowcount, which
    db_fakes.FakeQueryResult does not model (its existing callers never
    needed it). Scoped to this file only -- not a change to shared test
    infrastructure."""

    def __init__(self, rowcount: int):
        self.rowcount = rowcount


@pytest.fixture(autouse=True)
def _stub_audit_log(monkeypatch):
    # log_auth_event writes to the database; not under test here except in
    # the one test that specifically asserts on its call.
    monkeypatch.setattr(user_admin_service.auth_service, "log_auth_event", lambda **_kwargs: None)


# --- create_or_add_org_user ---------------------------------------------------

@pytest.mark.parametrize("access_mode", ["read_only", "blocked"])
def test_create_or_add_org_user_blocked_when_access_mode_not_full(monkeypatch, access_mode):
    monkeypatch.setattr(user_admin_service.access_service, "get_org_access_mode", lambda org_slug: access_mode)
    engine = FakeEngine([])  # no query may run
    monkeypatch.setattr(user_admin_service, "get_engine", lambda: engine)

    result = user_admin_service.create_or_add_org_user(
        org_slug="acme", email="new@example.com", password="x", full_name="New User", role="viewer",
    )

    assert result == {
        "ok": False,
        "message": "This organization does not currently allow administrative changes.",
    }
    assert engine.calls == []


@pytest.mark.parametrize("status", ["active", "trial"])
def test_create_or_add_org_user_succeeds_when_access_mode_full(monkeypatch, status):
    monkeypatch.setattr(user_admin_service.access_service, "get_org_access_mode", lambda org_slug: "full")
    monkeypatch.setattr(
        user_admin_service.auth_service, "get_user_by_email",
        lambda email: {"id": 5, "email": email},
    )
    engine = FakeEngine([
        FakeQueryResult(first={"id": 1, "slug": "acme", "name": "Acme"}),  # _get_org_row
        FakeQueryResult(first=None),  # membership_check_sql: not already a member
        _UpdateResult(rowcount=1),    # insert_membership_sql
    ])
    monkeypatch.setattr(user_admin_service, "get_engine", lambda: engine)

    result = user_admin_service.create_or_add_org_user(
        org_slug="acme", email="existing@example.com", password="x", full_name="Existing User", role="viewer",
    )

    assert result["ok"] is True


# --- update_org_user_role ------------------------------------------------------

@pytest.mark.parametrize("access_mode", ["read_only", "blocked"])
def test_update_org_user_role_blocked_when_access_mode_not_full(monkeypatch, access_mode):
    monkeypatch.setattr(user_admin_service.access_service, "get_org_access_mode", lambda org_slug: access_mode)
    engine = FakeEngine([])  # no query may run
    monkeypatch.setattr(user_admin_service, "get_engine", lambda: engine)

    result = user_admin_service.update_org_user_role(org_slug="acme", user_id=1, role="admin")

    assert result == {
        "ok": False,
        "message": "This organization does not currently allow administrative changes.",
    }
    assert engine.calls == []


def test_update_org_user_role_succeeds_when_access_mode_full(monkeypatch):
    monkeypatch.setattr(user_admin_service.access_service, "get_org_access_mode", lambda org_slug: "full")
    monkeypatch.setattr(user_admin_service.auth_service, "get_user_by_id", lambda uid: {"email": "u@example.com"})
    engine = FakeEngine([_UpdateResult(rowcount=1)])
    monkeypatch.setattr(user_admin_service, "get_engine", lambda: engine)

    result = user_admin_service.update_org_user_role(org_slug="acme", user_id=1, role="admin")

    assert result["ok"] is True


# --- set_user_active -----------------------------------------------------------

@pytest.mark.parametrize("access_mode", ["read_only", "blocked"])
def test_set_user_active_blocked_when_access_mode_not_full(monkeypatch, access_mode):
    monkeypatch.setattr(user_admin_service.access_service, "get_org_access_mode", lambda org_slug: access_mode)
    engine = FakeEngine([])  # no query may run: not even the membership check
    monkeypatch.setattr(user_admin_service, "get_engine", lambda: engine)

    result = user_admin_service.set_user_active(org_slug="acme", user_id=1, is_active=False)

    assert result == {
        "ok": False,
        "message": "This organization does not currently allow administrative changes.",
    }
    assert engine.calls == []


def test_set_user_active_blocked_when_target_not_a_member(monkeypatch):
    monkeypatch.setattr(user_admin_service.access_service, "get_org_access_mode", lambda org_slug: "full")
    # Only the membership-check query is queued: if the code proceeded to
    # the UPDATE anyway, FakeConn would raise "more execute() calls than
    # results were queued", failing this test.
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(user_admin_service, "get_engine", lambda: engine)

    result = user_admin_service.set_user_active(org_slug="acme", user_id=999, is_active=False)

    assert result == {
        "ok": False,
        "message": "That user is not a member of this organization.",
    }
    assert len(engine.calls) == 1
    assert "memberships" in engine.calls[0]["sql"].lower()


def test_set_user_active_succeeds_and_preserves_global_mutation_when_member(monkeypatch):
    monkeypatch.setattr(user_admin_service.access_service, "get_org_access_mode", lambda org_slug: "full")
    monkeypatch.setattr(user_admin_service.auth_service, "get_user_by_id", lambda uid: {"email": "u@example.com"})
    engine = FakeEngine([
        FakeQueryResult(first=(1,)),  # membership check: is a member
        _UpdateResult(rowcount=1),    # UPDATE app_users
    ])
    monkeypatch.setattr(user_admin_service, "get_engine", lambda: engine)

    result = user_admin_service.set_user_active(org_slug="acme", user_id=42, is_active=False)

    assert result == {"ok": True, "message": "User status updated."}
    assert len(engine.calls) == 2

    update_call = engine.calls[1]
    # The mutation itself stays a GLOBAL flag, unscoped by organization --
    # only WHERE id = :user_id, exactly as before this change. Org scoping
    # is enforced entirely by the separate membership check above, not by
    # narrowing this statement's WHERE clause.
    assert "update app_users" in update_call["sql"].lower()
    assert "memberships" not in update_call["sql"].lower()
    assert update_call["params"] == {"user_id": 42, "is_active": False}


def test_set_user_active_logs_org_slug_in_audit_metadata(monkeypatch):
    monkeypatch.setattr(user_admin_service.access_service, "get_org_access_mode", lambda org_slug: "full")
    monkeypatch.setattr(user_admin_service.auth_service, "get_user_by_id", lambda uid: {"email": "u@example.com"})
    engine = FakeEngine([FakeQueryResult(first=(1,)), _UpdateResult(rowcount=1)])
    monkeypatch.setattr(user_admin_service, "get_engine", lambda: engine)

    log_calls = []
    monkeypatch.setattr(
        user_admin_service.auth_service, "log_auth_event",
        lambda **kwargs: log_calls.append(kwargs),
    )

    user_admin_service.set_user_active(org_slug="acme", user_id=42, is_active=False)

    assert len(log_calls) == 1
    assert log_calls[0]["metadata"] == {"org_slug": "acme", "is_active": False}
