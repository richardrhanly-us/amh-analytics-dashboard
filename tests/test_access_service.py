from db_fakes import FakeEngine, FakeQueryResult

from src.services import access_service

# --- get_org_branches ---------------------------------------------------

def test_get_org_branches_returns_active_branches_for_requested_org(monkeypatch):
    rows = [
        {"id": 1, "branch_id": 100, "branch_slug": "main", "branch_name": "Main",
         "is_primary": True, "status": "active"},
        {"id": 2, "branch_id": 101, "branch_slug": "east", "branch_name": "East",
         "is_primary": False, "status": "active"},
    ]
    engine = FakeEngine([FakeQueryResult(all_rows=rows)])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    branches = access_service.get_org_branches(org_slug="acme")

    assert branches == rows
    assert engine.calls[0]["params"] == {"org_slug": "acme"}


def test_get_org_branches_scoped_to_a_single_org_returns_nothing_for_another(monkeypatch):
    # The SQL join+WHERE scopes results to the requested org_slug; a org with
    # no active branches (e.g. a different tenant entirely) gets an empty list,
    # never another tenant's rows.
    engine = FakeEngine([FakeQueryResult(all_rows=[])])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    branches = access_service.get_org_branches(org_slug="someone-elses-org")

    assert branches == []


# --- get_user_memberships ---------------------------------------------------

def test_get_user_memberships_returns_only_this_users_orgs(monkeypatch):
    rows = [
        {"organization_id": 1, "customer_id": 10, "role": "owner",
         "organization_slug": "acme", "organization_name": "Acme"},
    ]
    engine = FakeEngine([FakeQueryResult(all_rows=rows)])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    memberships = access_service.get_user_memberships(user_id=1)

    assert memberships == rows
    assert engine.calls[0]["params"] == {"user_id": 1}


def test_get_user_memberships_empty_for_user_with_no_orgs(monkeypatch):
    engine = FakeEngine([FakeQueryResult(all_rows=[])])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    assert access_service.get_user_memberships(user_id=999) == []


# --- user_can_access_org (the tenant-isolation gate) ------------------------

def test_user_can_access_org_true_when_membership_row_exists(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=(1,))])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    assert access_service.user_can_access_org(user_id=1, org_slug="acme") is True
    assert engine.calls[0]["params"] == {"user_id": 1, "org_slug": "acme"}


def test_user_can_access_org_false_when_no_membership_row(monkeypatch):
    # This is the core tenant-isolation check used before rendering any
    # org-scoped dashboard data: a user who is not a member of an org must
    # be denied even if they know or guess that org's slug.
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    assert access_service.user_can_access_org(user_id=1, org_slug="not-mine") is False


def test_user_can_access_org_checks_the_specific_org_requested(monkeypatch):
    # A user who belongs to org "acme" but is being checked against org
    # "other-co" must not be granted access just because *some* membership
    # exists for them elsewhere -- the query is scoped by org_slug, and this
    # fake simulates that scoped query returning nothing for the mismatched org.
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    result = access_service.user_can_access_org(user_id=1, org_slug="other-co")

    assert result is False
    assert engine.calls[0]["params"]["org_slug"] == "other-co"
