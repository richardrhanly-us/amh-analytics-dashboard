import pytest
from db_fakes import FakeEngine, FakeQueryResult

from src.services import access_service


@pytest.fixture(autouse=True)
def _clear_access_service_caches():
    # get_org_branches/get_user_memberships are now st.cache_data-wrapped
    # (dashboard performance pass); its cache is process-global, so clear
    # it before/after every test to keep these tests isolated from each
    # other and from other test modules, matching the existing convention
    # in tests/test_data_loader_refresh.py.
    access_service.get_org_branches.clear()
    access_service.get_user_memberships.clear()
    yield
    access_service.get_org_branches.clear()
    access_service.get_user_memberships.clear()


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


def test_get_user_memberships_sql_excludes_cancelled_organizations(monkeypatch):
    # Lifecycle policy: a cancelled organization disappears from a user's
    # org list entirely (reusing app.py's existing "selected org not in
    # the allowed list" clamp), rather than needing new UI to represent a
    # visible-but-blocked option. Suspended organizations are NOT excluded
    # here -- suspended customers retain read-only access, enforced by
    # get_org_access_mode, not by hiding the org from this list.
    engine = FakeEngine([FakeQueryResult(all_rows=[])])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    access_service.get_user_memberships(user_id=1)

    sql = engine.calls[0]["sql"]
    assert "o.status != 'cancelled'" in sql
    assert "suspended" not in sql.lower()


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


def test_user_can_access_org_never_cached(monkeypatch):
    # The tenant-isolation gate must re-check the database every call --
    # unlike get_org_branches/get_user_memberships, it is deliberately not
    # st.cache_data-wrapped, so a revoked membership is never masked by a
    # stale cached "True" for the rest of the cache's TTL.
    engine = FakeEngine([FakeQueryResult(first=(1,)), FakeQueryResult(first=None)])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    first = access_service.user_can_access_org(user_id=1, org_slug="acme")
    second = access_service.user_can_access_org(user_id=1, org_slug="acme")

    assert first is True
    assert second is False
    assert len(engine.calls) == 2


# --- cache scoping (dashboard performance pass) ------------------------------


def test_get_org_branches_cache_hits_on_repeated_call_same_org(monkeypatch):
    engine = FakeEngine([FakeQueryResult(all_rows=[{"id": 1, "branch_id": 100, "branch_slug": "main",
                                                      "branch_name": "Main", "is_primary": True, "status": "active"}])])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    for _ in range(5):
        access_service.get_org_branches(org_slug="acme")

    assert len(engine.calls) == 1


def test_get_org_branches_cache_is_scoped_per_org(monkeypatch):
    # A different org_slug must be a genuine cache miss -- caching must
    # never collapse two different tenants' branch lists together.
    engine = FakeEngine([
        FakeQueryResult(all_rows=[{"id": 1, "branch_id": 100, "branch_slug": "main",
                                    "branch_name": "Main", "is_primary": True, "status": "active"}]),
        FakeQueryResult(all_rows=[{"id": 2, "branch_id": 200, "branch_slug": "north",
                                    "branch_name": "North", "is_primary": True, "status": "active"}]),
    ])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    acme_branches = access_service.get_org_branches(org_slug="acme")
    other_branches = access_service.get_org_branches(org_slug="other-org")

    assert len(engine.calls) == 2
    assert acme_branches != other_branches


def test_get_user_memberships_cache_hits_on_repeated_call_same_user(monkeypatch):
    engine = FakeEngine([FakeQueryResult(all_rows=[{"organization_id": 1, "customer_id": 10, "role": "owner",
                                                     "organization_slug": "acme", "organization_name": "Acme"}])])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    for _ in range(5):
        access_service.get_user_memberships(user_id=1)

    assert len(engine.calls) == 1


def test_get_user_memberships_cache_is_scoped_per_user(monkeypatch):
    # A different user_id must be a genuine cache miss -- caching must
    # never leak one user's memberships into another user's lookup.
    engine = FakeEngine([
        FakeQueryResult(all_rows=[{"organization_id": 1, "customer_id": 10, "role": "owner",
                                    "organization_slug": "acme", "organization_name": "Acme"}]),
        FakeQueryResult(all_rows=[]),
    ])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    user_1_memberships = access_service.get_user_memberships(user_id=1)
    user_2_memberships = access_service.get_user_memberships(user_id=2)

    assert len(engine.calls) == 2
    assert user_1_memberships != user_2_memberships


# --- get_org_access_mode (organization lifecycle policy) ---------------------

@pytest.mark.parametrize("status", ["active", "trial"])
def test_get_org_access_mode_full_for_active_or_trial(monkeypatch, status):
    engine = FakeEngine([FakeQueryResult(first=(status,))])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    assert access_service.get_org_access_mode(org_slug="acme") == "full"


def test_get_org_access_mode_read_only_for_suspended(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=("suspended",))])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    assert access_service.get_org_access_mode(org_slug="acme") == "read_only"


def test_get_org_access_mode_blocked_for_cancelled(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=("cancelled",))])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    assert access_service.get_org_access_mode(org_slug="acme") == "blocked"


def test_get_org_access_mode_blocked_for_unknown_org(monkeypatch):
    # No matching organizations row at all -- fail closed, not "full".
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    assert access_service.get_org_access_mode(org_slug="does-not-exist") == "blocked"


def test_get_org_access_mode_blocked_for_unrecognised_status(monkeypatch):
    # A status outside the known four (e.g. a future/typo'd value) must
    # fail closed to "blocked", never silently default to "full".
    engine = FakeEngine([FakeQueryResult(first=("inactive",))])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    assert access_service.get_org_access_mode(org_slug="acme") == "blocked"


def test_get_org_access_mode_is_never_cached(monkeypatch):
    # Security gate, same reasoning as user_can_access_org: an
    # already-open session must see a cancellation take effect on the
    # very next call, not after a cache TTL.
    engine = FakeEngine([
        FakeQueryResult(first=("active",)),
        FakeQueryResult(first=("cancelled",)),
    ])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    first = access_service.get_org_access_mode(org_slug="acme")
    second = access_service.get_org_access_mode(org_slug="acme")

    assert first == "full"
    assert second == "blocked"
    assert len(engine.calls) == 2
