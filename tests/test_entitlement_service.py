import pytest
from db_fakes import FakeEngine, FakeQueryResult

from src.services import entitlement_service


@pytest.fixture(autouse=True)
def _clear_entitlement_caches():
    # get_org_subscription/get_plan_entitlements are st.cache_data-wrapped
    # (module-level cache, shared across tests in this process). Several
    # tests below call them with identical args against different
    # monkeypatched fakes, so this must be cleared before/after every test
    # or a later test would silently see an earlier test's cached result.
    #
    # build_entitlement_context itself is deliberately NOT cached anymore
    # (PRE-PILOT fix: role must always be fetched fresh so a role change or
    # deactivation-driven demotion takes effect on the very next call, not
    # after a TTL) -- so there is no cache to clear for it.
    entitlement_service.get_org_subscription.clear()
    entitlement_service.get_plan_entitlements.clear()
    yield
    entitlement_service.get_org_subscription.clear()
    entitlement_service.get_plan_entitlements.clear()


# --- get_org_role_for_user ---------------------------------------------------

def test_get_org_role_for_user_returns_role_when_membership_exists(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=("admin",))])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    role = entitlement_service.get_org_role_for_user(user_id=1, org_slug="acme")

    assert role == "admin"
    assert engine.calls[0]["params"] == {"user_id": 1, "org_slug": "acme"}


def test_get_org_role_for_user_returns_none_without_membership(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    role = entitlement_service.get_org_role_for_user(user_id=1, org_slug="other-org")

    assert role is None


def test_get_org_role_for_user_scopes_lookup_to_requested_org(monkeypatch):
    # A user with a role in one org must not leak a role for a different org:
    # the query itself is scoped, so a lookup against an org with no
    # membership row returns nothing regardless of the user's other orgs.
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    role = entitlement_service.get_org_role_for_user(user_id=1, org_slug="not-my-org")

    assert role is None
    assert engine.calls[0]["params"]["org_slug"] == "not-my-org"


# --- get_org_subscription ----------------------------------------------------

def test_get_org_subscription_returns_latest_plan(monkeypatch):
    row = {"id": 10, "status": "active", "started_at": None, "ends_at": None,
           "plan_id": 2, "plan_code": "pro", "plan_name": "Pro"}
    engine = FakeEngine([FakeQueryResult(first=row)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    subscription = entitlement_service.get_org_subscription(org_slug="acme")

    assert subscription == row


def test_get_org_subscription_returns_none_when_no_subscription(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    assert entitlement_service.get_org_subscription(org_slug="acme") is None


def test_get_org_subscription_is_cached_across_repeated_calls(monkeypatch):
    # This is the piece that's still safe/useful to cache: subscription
    # data only changes on a rare admin action. Proves the @st.cache_data
    # decorator that moved onto this function (off of
    # build_entitlement_context) actually caches repeated calls.
    row = {"id": 10, "status": "active", "started_at": None, "ends_at": None,
           "plan_id": 2, "plan_code": "pro", "plan_name": "Pro"}
    engine = FakeEngine([FakeQueryResult(first=row)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    for _ in range(5):
        assert entitlement_service.get_org_subscription(org_slug="acme") == row

    assert len(engine.calls) == 1


# --- get_plan_entitlements ----------------------------------------------------

def test_get_plan_entitlements_builds_feature_keyed_dict(monkeypatch):
    rows = [
        {"feature_key": "exports", "enabled": True, "limit_value": None},
        {"feature_key": "max_branches", "enabled": True, "limit_value": 3},
    ]
    engine = FakeEngine([FakeQueryResult(all_rows=rows)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    entitlements = entitlement_service.get_plan_entitlements(plan_id=2)

    assert entitlements == {
        "exports": {"enabled": True, "limit_value": None},
        "max_branches": {"enabled": True, "limit_value": 3},
    }


def test_get_plan_entitlements_coerces_enabled_to_bool(monkeypatch):
    rows = [{"feature_key": "alerts", "enabled": 1, "limit_value": None}]
    engine = FakeEngine([FakeQueryResult(all_rows=rows)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    entitlements = entitlement_service.get_plan_entitlements(plan_id=1)

    assert entitlements["alerts"]["enabled"] is True


def test_get_plan_entitlements_is_cached_across_repeated_calls(monkeypatch):
    # Same reasoning as get_org_subscription: plan entitlements are safe
    # and useful to keep cached, moved here off build_entitlement_context.
    rows = [{"feature_key": "exports", "enabled": True, "limit_value": None}]
    engine = FakeEngine([FakeQueryResult(all_rows=rows)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    expected = {"exports": {"enabled": True, "limit_value": None}}
    for _ in range(5):
        assert entitlement_service.get_plan_entitlements(plan_id=2) == expected

    assert len(engine.calls) == 1


# --- build_entitlement_context -----------------------------------------------

def test_build_entitlement_context_combines_role_subscription_and_entitlements(monkeypatch):
    monkeypatch.setattr(
        entitlement_service, "get_org_role_for_user",
        lambda user_id, org_slug: "manager",
    )
    monkeypatch.setattr(
        entitlement_service, "get_org_subscription",
        lambda org_slug: {"id": 1, "plan_id": 5, "status": "active"},
    )
    monkeypatch.setattr(
        entitlement_service, "get_plan_entitlements",
        lambda plan_id: {"exports": {"enabled": True, "limit_value": None}},
    )

    context = entitlement_service.build_entitlement_context(user_id=1, org_slug="acme")

    # Return shape is unchanged by the PRE-PILOT caching fix.
    assert set(context.keys()) == {"role", "subscription", "entitlements"}
    assert context["role"] == "manager"
    assert context["subscription"]["plan_id"] == 5
    assert context["entitlements"] == {"exports": {"enabled": True, "limit_value": None}}


def test_build_entitlement_context_skips_entitlement_lookup_without_subscription(monkeypatch):
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", lambda user_id, org_slug: "viewer")
    monkeypatch.setattr(entitlement_service, "get_org_subscription", lambda org_slug: None)

    calls = []
    monkeypatch.setattr(
        entitlement_service, "get_plan_entitlements",
        lambda plan_id: calls.append(plan_id) or {},
    )

    context = entitlement_service.build_entitlement_context(user_id=1, org_slug="acme")

    assert context["subscription"] is None
    assert context["entitlements"] == {}
    assert calls == []  # never looked up entitlements with no plan to look up


def test_build_entitlement_context_no_role_means_no_membership(monkeypatch):
    # A user with no membership row for this org gets role=None; callers
    # (permission_service.has_role) already treat None as "no access."
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", lambda user_id, org_slug: None)
    monkeypatch.setattr(entitlement_service, "get_org_subscription", lambda org_slug: None)

    context = entitlement_service.build_entitlement_context(user_id=99, org_slug="someone-elses-org")

    assert context["role"] is None


# --- PRE-PILOT fix: role must never be cached --------------------------------

def test_build_entitlement_context_calls_role_lookup_on_every_call(monkeypatch):
    # Direct proof the @st.cache_data decorator is gone from
    # build_entitlement_context: role lookup must run on every call, not
    # just the first, even with identical (user_id, org_slug) args.
    calls = []

    def counting_role(user_id, org_slug):
        calls.append((user_id, org_slug))
        return "manager"

    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", counting_role)
    monkeypatch.setattr(entitlement_service, "get_org_subscription", lambda org_slug: None)

    for _ in range(5):
        entitlement_service.build_entitlement_context(user_id=1, org_slug="acme")

    assert len(calls) == 5


def test_build_entitlement_context_reflects_role_change_on_next_call_with_no_clear(monkeypatch):
    # This is the scenario the fix exists for: an admin is demoted (or
    # deactivated, which downstream shows up as no usable role), and the
    # very next call with identical args must see it -- no .clear() call,
    # no TTL wait.
    current_role = {"value": "admin"}

    monkeypatch.setattr(
        entitlement_service, "get_org_role_for_user",
        lambda user_id, org_slug: current_role["value"],
    )
    monkeypatch.setattr(entitlement_service, "get_org_subscription", lambda org_slug: None)

    first = entitlement_service.build_entitlement_context(user_id=1, org_slug="acme")
    assert first["role"] == "admin"

    current_role["value"] = "viewer"

    second = entitlement_service.build_entitlement_context(user_id=1, org_slug="acme")
    assert second["role"] == "viewer"


def test_build_entitlement_context_shares_subscription_cache_across_users_same_org(monkeypatch):
    # Subscription/entitlements caching (still on, and still useful) never
    # depended on user_id -- two different users in the same org correctly
    # share one cached lookup via the real get_org_subscription cache
    # rather than each re-querying.
    row = {"id": 1, "status": "active", "started_at": None, "ends_at": None,
           "plan_id": None, "plan_code": None, "plan_name": None}
    engine = FakeEngine([FakeQueryResult(first=row)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", lambda user_id, org_slug: "viewer")

    entitlement_service.build_entitlement_context(user_id=1, org_slug="acme")
    entitlement_service.build_entitlement_context(user_id=2, org_slug="acme")

    assert len(engine.calls) == 1


# --- feature_enabled / feature_limit (module-local copies) --------------------

def test_feature_enabled_true_when_entitlement_present_and_on():
    context = {"entitlements": {"exports": {"enabled": True, "limit_value": None}}}
    assert entitlement_service.feature_enabled(context, "exports") is True


def test_feature_enabled_false_when_entitlement_absent():
    context = {"entitlements": {}}
    assert entitlement_service.feature_enabled(context, "exports") is False


def test_feature_limit_returns_configured_value():
    context = {"entitlements": {"max_branches": {"enabled": True, "limit_value": 5}}}
    assert entitlement_service.feature_limit(context, "max_branches") == 5


def test_feature_limit_none_when_entitlement_absent():
    context = {"entitlements": {}}
    assert entitlement_service.feature_limit(context, "max_branches") is None
