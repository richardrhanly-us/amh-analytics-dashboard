"""Tests for library deactivation semantics (platform_admin_service.
set_library_active_status) and how it interacts with agent authentication.

Deactivate Library = reversible administrative SUSPENSION:
    organizations.status = 'suspended'
and nothing else. Historical data, operational identity mappings,
collector_installations, subscriptions and agent tokens are preserved, and
branch statuses are left alone. Reactivate = 'suspended' -> 'active' and never
touches a token. 'cancelled' is terminal here.

organizations.status is CHECK-constrained to active/trial/suspended/cancelled,
so 'inactive' is NOT a valid organization status. _FakeTenantDb enforces that
constraint, and it raises on any SQL it does not expect, so an unexpected write
(to a branch, token, installation...) fails a test instead of passing silently.

One fake serves both the admin service (engine.begin()) and
main.authenticate_agent (conn.execute), so token state and tenant state are
exercised together: request allowed = token active AND tenant usable AND
branch usable AND scope matches.
"""

from __future__ import annotations

import copy

import pytest
from db_fakes import FakeQueryResult
from fastapi import HTTPException

import main
from src.services import platform_admin_service

_VALID_ORGANIZATION_STATUSES = ("active", "trial", "suspended", "cancelled")


class _FakeTenantDb:
    def __init__(self, orgs, branches, tokens) -> None:
        self.orgs = {o["id"]: dict(o) for o in orgs}
        self.branches = {b["id"]: dict(b) for b in branches}
        self.tokens = {t["token"]: dict(t) for t in tokens}
        # Data the suspension must never touch.
        self.installations = [{"id": 1, "organization_id": 1, "status": "active"}]
        self.subscriptions = [{"id": 1, "organization_id": 1, "status": "active"}]
        self.history = [{"barcode": "1", "customer_id": 1, "branch_id": 1}]
        self.statements: list[str] = []

    # engine interface (service) ------------------------------------------------
    def begin(self):
        return self

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # statements ----------------------------------------------------------------
    def execute(self, sql, params=None):
        q = " ".join(str(sql).lower().split())
        self.statements.append(q)

        if q.startswith("select status from organizations") and "for update" in q:
            org = self.orgs.get(params["organization_id"])
            return FakeQueryResult(first={"status": org["status"]} if org else None)

        if q.startswith("update organizations set status"):
            new_status = params["new_status"]
            if new_status not in _VALID_ORGANIZATION_STATUSES:
                raise RuntimeError(
                    f'violates check constraint on organizations.status: {new_status!r}'
                )
            self.orgs[params["organization_id"]]["status"] = new_status
            return FakeQueryResult()

        if "from agent_tokens t" in q:
            return FakeQueryResult(first=self._token_row(params["token"]))

        if q.startswith("update agent_tokens set last_used_at"):
            return FakeQueryResult()

        raise AssertionError(f"unexpected SQL: {q}")

    def _token_row(self, bearer):
        token = self.tokens.get(bearer)
        if token is None:
            return None
        org = next(
            (o for o in self.orgs.values()
             if o["operational_customer_id"] == token["customer_id"]),
            None,
        )
        branch = None
        if org is not None:
            branch = next(
                (b for b in self.branches.values()
                 if b["operational_branch_id"] == token["branch_id"]
                 and b["organization_id"] == org["id"]),
                None,
            )
        return {
            "id": token["id"], "customer_id": token["customer_id"],
            "branch_id": token["branch_id"], "is_active": token["is_active"],
            "description": "test",
            "organization_status": org["status"] if org else None,
            "branch_status": branch["status"] if branch else None,
        }

    def writes(self):
        return [s for s in self.statements if s.startswith(("update", "insert", "delete"))]

    def preserved(self):
        """Everything a suspension must leave exactly as it was."""
        return copy.deepcopy((
            {i: {k: v for k, v in o.items() if k != "status"} for i, o in self.orgs.items()},
            self.branches, self.tokens, self.installations, self.subscriptions, self.history,
        ))


def _nbpl_db(**org_overrides):
    org = {"id": 1, "status": "active", "operational_customer_id": 1}
    org.update(org_overrides)
    return _FakeTenantDb(
        orgs=[org],
        branches=[{"id": 1, "organization_id": 1, "status": "active", "operational_branch_id": 1}],
        tokens=[
            {"token": "active-token", "id": 10, "customer_id": 1, "branch_id": 1, "is_active": True},
            {"token": "revoked-token", "id": 11, "customer_id": 1, "branch_id": 1,
             "is_active": False},
        ],
    )


@pytest.fixture
def db(monkeypatch):
    fake = _nbpl_db()
    monkeypatch.setattr(platform_admin_service, "get_engine", lambda: fake)
    return fake


def _authenticate(db, token, customer_id=1, branch_id=1):
    return main.authenticate_agent(db, f"Bearer {token}", customer_id, branch_id)


def _status_of_attempt(db, token, customer_id=1, branch_id=1):
    try:
        _authenticate(db, token, customer_id, branch_id)
    except HTTPException as exc:
        return exc.status_code, exc.detail
    return 200, None


# --- deactivate / reactivate ---------------------------------------------------

def test_deactivate_writes_suspended_never_inactive(db):
    result = platform_admin_service.set_library_active_status(1, is_active=False)

    assert db.orgs[1]["status"] == "suspended"
    assert result == {"organization_id": 1, "status": "suspended", "changed": True}
    assert not any("inactive" in s for s in db.statements)


def test_inactive_is_not_a_valid_organization_status_in_the_fake_schema(db):
    # Guards the fake itself: it does reproduce the CHECK constraint, so the
    # test above would fail if the service ever wrote 'inactive' again.
    with pytest.raises(RuntimeError, match="violates check constraint"):
        db.execute("UPDATE organizations SET status = :new_status WHERE id = :organization_id",
                   {"new_status": "inactive", "organization_id": 1})


def test_reactivate_writes_active(db):
    db.orgs[1]["status"] = "suspended"

    result = platform_admin_service.set_library_active_status(1, is_active=True)

    assert db.orgs[1]["status"] == "active"
    assert result == {"organization_id": 1, "status": "active", "changed": True}


def test_suspend_then_reactivate_round_trips_to_active(db):
    platform_admin_service.set_library_active_status(1, is_active=False)
    platform_admin_service.set_library_active_status(1, is_active=True)

    assert db.orgs[1]["status"] == "active"


# --- what suspension must preserve ----------------------------------------------

def test_suspension_writes_only_the_organization_status(db):
    platform_admin_service.set_library_active_status(1, is_active=False)

    assert len(db.writes()) == 1
    assert db.writes()[0].startswith("update organizations set status")
    for statement in db.writes():
        for other in ("branches", "agent_tokens", "collector_installations",
                      "subscriptions", "customers", "checkins", "operational_"):
            assert other not in statement
        assert not statement.startswith("delete")


def test_suspension_preserves_mappings_installations_subscriptions_tokens_and_history(db):
    before = db.preserved()

    platform_admin_service.set_library_active_status(1, is_active=False)
    assert db.preserved() == before
    assert db.orgs[1]["operational_customer_id"] == 1
    assert db.branches[1]["operational_branch_id"] == 1

    platform_admin_service.set_library_active_status(1, is_active=True)
    assert db.preserved() == before


def test_reactivation_does_not_reactivate_an_individually_inactive_branch(db):
    db.branches[1]["status"] = "inactive"

    platform_admin_service.set_library_active_status(1, is_active=False)
    platform_admin_service.set_library_active_status(1, is_active=True)

    assert db.branches[1]["status"] == "inactive"
    # ...so the tenant is still not usable, even with the library active.
    assert _status_of_attempt(db, "active-token")[0] == 403


def test_suspension_never_touches_tokens(db):
    platform_admin_service.set_library_active_status(1, is_active=False)
    platform_admin_service.set_library_active_status(1, is_active=True)

    assert db.tokens["active-token"]["is_active"] is True
    assert db.tokens["revoked-token"]["is_active"] is False
    assert not any("agent_tokens" in s for s in db.statements)


# --- cancelled / unknown / idempotent ---------------------------------------------

@pytest.mark.parametrize("is_active", [True, False])
def test_cancelled_organization_cannot_be_suspended_or_reactivated(db, is_active):
    db.orgs[1]["status"] = "cancelled"

    with pytest.raises(RuntimeError, match="is cancelled"):
        platform_admin_service.set_library_active_status(1, is_active=is_active)

    assert db.orgs[1]["status"] == "cancelled"
    assert db.writes() == []


def test_unknown_organization_is_rejected(db):
    with pytest.raises(RuntimeError, match="Organization 999 not found"):
        platform_admin_service.set_library_active_status(999, is_active=False)

    assert db.writes() == []


def test_unrecognised_status_fails_closed(db):
    db.orgs[1]["status"] = "inactive"  # not a valid status; must not be "fixed" silently

    with pytest.raises(RuntimeError, match="unrecognised status"):
        platform_admin_service.set_library_active_status(1, is_active=True)

    assert db.orgs[1]["status"] == "inactive"
    assert db.writes() == []


def test_suspending_an_already_suspended_library_is_a_no_op(db):
    db.orgs[1]["status"] = "suspended"

    result = platform_admin_service.set_library_active_status(1, is_active=False)

    assert result["changed"] is False
    assert db.writes() == []


@pytest.mark.parametrize("status", ["active", "trial"])
def test_reactivating_a_usable_library_is_a_no_op_and_does_not_promote_trial(db, status):
    db.orgs[1]["status"] = status

    result = platform_admin_service.set_library_active_status(1, is_active=True)

    assert result == {"organization_id": 1, "status": status, "changed": False}
    assert db.orgs[1]["status"] == status
    assert db.writes() == []


def test_a_trial_library_can_be_suspended(db):
    db.orgs[1]["status"] = "trial"

    platform_admin_service.set_library_active_status(1, is_active=False)

    assert db.orgs[1]["status"] == "suspended"


# --- suspension enforced at the API, token and tenant state independent ---------------

def test_nbpl_style_mapping_authenticates_when_active(db):
    assert _status_of_attempt(db, "active-token") == (200, None)


def test_active_token_of_a_suspended_organization_is_rejected(db):
    platform_admin_service.set_library_active_status(1, is_active=False)

    status_code, detail = _status_of_attempt(db, "active-token")

    assert status_code == 403
    assert detail == "Agent is not currently authorized to upload data"
    # The token itself is untouched: rejection comes from the tenant gate.
    assert db.tokens["active-token"]["is_active"] is True


def test_active_token_works_again_after_the_library_is_reactivated(db):
    platform_admin_service.set_library_active_status(1, is_active=False)
    assert _status_of_attempt(db, "active-token")[0] == 403

    platform_admin_service.set_library_active_status(1, is_active=True)

    assert _status_of_attempt(db, "active-token") == (200, None)


def test_inactive_token_stays_rejected_after_the_organization_is_reactivated(db):
    assert _status_of_attempt(db, "revoked-token") == (403, "Agent token is inactive")

    platform_admin_service.set_library_active_status(1, is_active=False)
    platform_admin_service.set_library_active_status(1, is_active=True)

    assert db.orgs[1]["status"] == "active"
    assert _status_of_attempt(db, "revoked-token") == (403, "Agent token is inactive")
    assert db.tokens["revoked-token"]["is_active"] is False


def test_cancelled_organization_rejects_an_active_token(db):
    db.orgs[1]["status"] = "cancelled"

    assert _status_of_attempt(db, "active-token")[0] == 403


def test_cross_tenant_customer_branch_pair_is_rejected(db):
    # A second, fully active tenant; a token pairing tenant 1's customer with
    # tenant 2's branch spans two tenants and must not authenticate.
    db.orgs[2] = {"id": 2, "status": "active", "operational_customer_id": 50}
    db.branches[5] = {"id": 5, "organization_id": 2, "status": "active",
                      "operational_branch_id": 5}
    db.tokens["cross"] = {"token": "cross", "id": 12, "customer_id": 1, "branch_id": 5,
                          "is_active": True}

    assert _status_of_attempt(db, "cross", customer_id=1, branch_id=5)[0] == 403
    # ...while each tenant's own pair still works.
    db.tokens["tenant-2"] = {"token": "tenant-2", "id": 13, "customer_id": 50, "branch_id": 5,
                             "is_active": True}
    assert _status_of_attempt(db, "tenant-2", customer_id=50, branch_id=5) == (200, None)


def test_suspending_one_library_does_not_affect_another(db):
    db.orgs[2] = {"id": 2, "status": "active", "operational_customer_id": 50}
    db.branches[5] = {"id": 5, "organization_id": 2, "status": "active",
                      "operational_branch_id": 5}
    db.tokens["tenant-2"] = {"token": "tenant-2", "id": 13, "customer_id": 50, "branch_id": 5,
                             "is_active": True}

    platform_admin_service.set_library_active_status(1, is_active=False)

    assert _status_of_attempt(db, "active-token")[0] == 403
    assert _status_of_attempt(db, "tenant-2", customer_id=50, branch_id=5) == (200, None)
