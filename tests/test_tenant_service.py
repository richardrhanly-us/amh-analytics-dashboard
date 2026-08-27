import pytest

import src.services.tenant_service as tenant_service
from db_fakes import FakeEngine, FakeQueryResult


# --- create_organization_with_primary_branch --------------------------------

def test_create_organization_with_primary_branch_happy_path(monkeypatch):
    plan = {"id": 5, "code": "pro", "name": "Pro"}
    org = {"id": 1, "name": "Acme", "slug": "acme", "status": "active", "created_at": None}
    branch = {"id": 10, "organization_id": 1, "name": "Main", "slug": "main",
              "is_primary": True, "status": "active", "created_at": None}
    subscription = {"id": 100, "organization_id": 1, "plan_id": 5, "status": "trial", "started_at": None}

    engine = FakeEngine([
        FakeQueryResult(first=plan),          # sql_find_plan
        FakeQueryResult(first=org),           # sql_insert_org
        FakeQueryResult(first=branch),        # sql_insert_branch
        FakeQueryResult(first=subscription),  # sql_insert_subscription
        FakeQueryResult(first=None),          # sql_insert_org_settings
        FakeQueryResult(first=None),          # sql_insert_branch_settings
    ])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    result = tenant_service.create_organization_with_primary_branch(
        org_name="Acme", org_slug="acme",
        branch_name="Main", branch_slug="main",
        plan_code="pro",
    )

    assert result["organization"] == org
    assert result["branch"] == branch
    assert result["plan"] == plan
    assert result["subscription"] == subscription


def test_create_organization_with_primary_branch_raises_for_unknown_plan(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=None)])  # sql_find_plan finds nothing
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(RuntimeError, match="Plan not found"):
        tenant_service.create_organization_with_primary_branch(
            org_name="Acme", org_slug="acme",
            branch_name="Main", branch_slug="main",
            plan_code="nonexistent-plan",
        )


# --- get_organization_by_slug / get_branch_by_slug --------------------------

def test_get_organization_by_slug_returns_matching_org(monkeypatch):
    org = {"id": 1, "name": "Acme", "slug": "acme", "status": "active",
           "created_at": None, "updated_at": None}
    engine = FakeEngine([FakeQueryResult(first=org)])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    assert tenant_service.get_organization_by_slug("acme") == org


def test_get_organization_by_slug_returns_none_for_unknown_slug(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    assert tenant_service.get_organization_by_slug("no-such-org") is None


def test_get_branch_by_slug_scopes_lookup_to_the_given_org(monkeypatch):
    branch = {"id": 10, "organization_id": 1, "name": "Main", "slug": "main",
              "is_primary": True, "status": "active"}
    engine = FakeEngine([FakeQueryResult(first=branch)])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    result = tenant_service.get_branch_by_slug(org_slug="acme", branch_slug="main")

    assert result == branch
    assert engine.calls[0]["params"] == {"org_slug": "acme", "branch_slug": "main"}


def test_get_branch_by_slug_none_when_branch_belongs_to_a_different_org(monkeypatch):
    # The join requires both org_slug and branch_slug to match the same row,
    # so a branch slug that only exists under a different org must not resolve.
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    result = tenant_service.get_branch_by_slug(org_slug="acme", branch_slug="main")

    assert result is None


# --- _deep_merge_settings ----------------------------------------------------

def test_deep_merge_settings_merges_nested_dicts_without_dropping_siblings():
    base = {"theme": {"color": "blue", "font": "arial"}, "locale": "en-US"}
    override = {"theme": {"color": "red"}}

    merged = tenant_service._deep_merge_settings(base, override)

    assert merged == {"theme": {"color": "red", "font": "arial"}, "locale": "en-US"}


def test_deep_merge_settings_non_dict_override_replaces_value_outright():
    base = {"feature_flags": {"beta": True}}
    override = {"feature_flags": "disabled"}

    merged = tenant_service._deep_merge_settings(base, override)

    assert merged == {"feature_flags": "disabled"}


# --- get_effective_settings --------------------------------------------------

def _effective_settings_engine(org_settings, branch_settings, subscription=None, entitlement_rows=None):
    org_row = {"id": 1, "name": "Acme", "slug": "acme", "org_settings": org_settings}
    branch_row = {"id": 10, "name": "Main", "slug": "main", "is_primary": True,
                  "branch_settings": branch_settings}
    return FakeEngine([
        FakeQueryResult(first=org_row),
        FakeQueryResult(first=branch_row),
        FakeQueryResult(first=subscription),
        FakeQueryResult(all_rows=entitlement_rows or []),
    ])


def test_get_effective_settings_deep_merges_branch_over_org_settings(monkeypatch):
    # Regression test: get_effective_settings previously had a duplicate
    # definition further down the file that silently won and did a *shallow*
    # dict merge, discarding sibling keys of any nested dict the branch
    # overrode. This asserts the deep-merge behavior is what actually runs.
    org_settings = {"theme": {"color": "blue", "font": "arial"}, "locale": "en-US"}
    branch_settings = {"theme": {"color": "red"}}
    engine = _effective_settings_engine(org_settings, branch_settings)
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    result = tenant_service.get_effective_settings(org_slug="acme", branch_slug="main")

    assert result["settings"] == {
        "theme": {"color": "red", "font": "arial"},
        "locale": "en-US",
    }


def test_get_effective_settings_includes_subscription_and_entitlements(monkeypatch):
    subscription = {"id": 1, "status": "active", "plan_code": "pro", "plan_name": "Pro"}
    entitlement_rows = [{"feature_key": "exports", "enabled": True, "limit_value": None}]
    engine = _effective_settings_engine({}, {}, subscription=subscription, entitlement_rows=entitlement_rows)
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    result = tenant_service.get_effective_settings(org_slug="acme")

    assert result["subscription"] == subscription
    assert result["entitlements"] == {"exports": {"enabled": True, "limit_value": None}}


def test_get_effective_settings_raises_when_org_not_found(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(RuntimeError, match="Organization not found"):
        tenant_service.get_effective_settings(org_slug="no-such-org")


def test_get_effective_settings_raises_when_branch_not_found(monkeypatch):
    org_row = {"id": 1, "name": "Acme", "slug": "acme", "org_settings": {}}
    engine = FakeEngine([
        FakeQueryResult(first=org_row),
        FakeQueryResult(first=None),  # no matching branch
    ])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(RuntimeError, match="Branch not found"):
        tenant_service.get_effective_settings(org_slug="acme", branch_slug="ghost-branch")
