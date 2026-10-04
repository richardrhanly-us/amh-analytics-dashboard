"""The dashboard's st.cache_data layer over entitlement_service.

The cache tests here moved from tests/test_entitlement_service.py when the
caching itself moved out of entitlement_service (Block 2d): entitlement_service
is now the uncached, framework-neutral core, and
services.streamlit_entitlement_adapter holds the cached subscription/plan
lookups and the build_entitlement_context the Streamlit entry scripts import.

Imported the "flat" way (services.streamlit_entitlement_adapter), matching how
Streamlit loads it with src/ as the script root. The adapter reaches the core
as services.entitlement_service, so that -- not
src.services.entitlement_service, a separate module object -- is the module
these tests patch.
"""

import pytest
from db_fakes import FakeEngine, FakeQueryResult

from services import entitlement_service, streamlit_entitlement_adapter


@pytest.fixture(autouse=True)
def _clear_entitlement_adapter_caches():
    # get_org_subscription/get_plan_entitlements are st.cache_data-wrapped
    # (module-level cache, shared across tests in this process). Several
    # tests below call them with identical args against different
    # monkeypatched fakes, so this must be cleared before/after every test
    # or a later test would silently see an earlier test's cached result.
    #
    # build_entitlement_context itself is deliberately NOT cached (role must
    # always be fetched fresh), so there is no cache to clear for it.
    streamlit_entitlement_adapter.get_org_subscription.clear()
    streamlit_entitlement_adapter.get_plan_entitlements.clear()
    yield
    streamlit_entitlement_adapter.get_org_subscription.clear()
    streamlit_entitlement_adapter.get_plan_entitlements.clear()


SUBSCRIPTION = {"id": 10, "status": "active", "started_at": None, "ends_at": None,
                "plan_id": 2, "plan_code": "pro", "plan_name": "Pro"}
NO_PLAN_SUBSCRIPTION = {"id": 1, "status": "active", "started_at": None, "ends_at": None,
                        "plan_id": None, "plan_code": None, "plan_name": None}


def test_cache_ttl_is_120_seconds():
    assert streamlit_entitlement_adapter._ENTITLEMENT_CACHE_TTL_SECONDS == 120


# --- what is cached, and what is not ------------------------------------------

def test_only_the_subscription_and_plan_lookups_are_cached():
    assert hasattr(streamlit_entitlement_adapter.get_org_subscription, "clear")
    assert hasattr(streamlit_entitlement_adapter.get_plan_entitlements, "clear")
    assert not hasattr(streamlit_entitlement_adapter.build_entitlement_context, "clear")
    # No cached (or any) copy of the role lookup exists here at all.
    assert not hasattr(streamlit_entitlement_adapter, "get_org_role_for_user")


# --- get_org_subscription ------------------------------------------------------

def test_get_org_subscription_returns_the_core_row(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=SUBSCRIPTION)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    assert streamlit_entitlement_adapter.get_org_subscription("acme") == SUBSCRIPTION
    assert engine.calls[0]["params"] == {"org_slug": "acme"}


def test_get_org_subscription_is_cached_across_repeated_calls(monkeypatch):
    # This is the piece that's still safe/useful to cache: subscription
    # data only changes on a rare admin action.
    engine = FakeEngine([FakeQueryResult(first=SUBSCRIPTION)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    for _ in range(5):
        assert streamlit_entitlement_adapter.get_org_subscription(org_slug="acme") == SUBSCRIPTION

    assert len(engine.calls) == 1


def test_get_org_subscription_cache_is_scoped_per_org(monkeypatch):
    # A different org_slug must be a genuine cache miss -- one organization's
    # subscription must never be served for another.
    engine = FakeEngine([FakeQueryResult(first=SUBSCRIPTION), FakeQueryResult(first=None)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    acme = streamlit_entitlement_adapter.get_org_subscription(org_slug="acme")
    other = streamlit_entitlement_adapter.get_org_subscription(org_slug="other-org")

    assert len(engine.calls) == 2
    assert acme == SUBSCRIPTION
    assert other is None


# --- get_plan_entitlements -----------------------------------------------------

def test_get_plan_entitlements_is_cached_across_repeated_calls(monkeypatch):
    rows = [{"feature_key": "exports", "enabled": True, "limit_value": None}]
    engine = FakeEngine([FakeQueryResult(all_rows=rows)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    expected = {"exports": {"enabled": True, "limit_value": None}}
    for _ in range(5):
        assert streamlit_entitlement_adapter.get_plan_entitlements(plan_id=2) == expected

    assert len(engine.calls) == 1


def test_get_plan_entitlements_cache_is_scoped_per_plan(monkeypatch):
    engine = FakeEngine([
        FakeQueryResult(all_rows=[{"feature_key": "exports", "enabled": True, "limit_value": None}]),
        FakeQueryResult(all_rows=[]),
    ])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    pro = streamlit_entitlement_adapter.get_plan_entitlements(plan_id=2)
    basic = streamlit_entitlement_adapter.get_plan_entitlements(plan_id=1)

    assert len(engine.calls) == 2
    assert pro == {"exports": {"enabled": True, "limit_value": None}}
    assert basic == {}


def test_clear_forces_the_next_call_back_to_the_core(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=None), FakeQueryResult(first=None)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    streamlit_entitlement_adapter.get_org_subscription(org_slug="acme")
    streamlit_entitlement_adapter.get_org_subscription.clear()
    streamlit_entitlement_adapter.get_org_subscription(org_slug="acme")

    assert len(engine.calls) == 2


# --- build_entitlement_context --------------------------------------------------

def test_build_entitlement_context_returns_the_same_context_as_the_core(monkeypatch):
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", lambda user_id, org_slug: "manager")
    monkeypatch.setattr(entitlement_service, "get_org_subscription", lambda org_slug: dict(SUBSCRIPTION))
    monkeypatch.setattr(
        entitlement_service, "get_plan_entitlements",
        lambda plan_id: {"exports": {"enabled": True, "limit_value": None}},
    )

    cached_path = streamlit_entitlement_adapter.build_entitlement_context(user_id=1, org_slug="acme")
    core_path = entitlement_service.build_entitlement_context(user_id=1, org_slug="acme")

    assert cached_path == core_path == {
        "role": "manager",
        "subscription": SUBSCRIPTION,
        "entitlements": {"exports": {"enabled": True, "limit_value": None}},
    }


def test_build_entitlement_context_without_a_subscription_never_looks_up_a_plan(monkeypatch):
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", lambda user_id, org_slug: "viewer")
    monkeypatch.setattr(entitlement_service, "get_org_subscription", lambda org_slug: None)
    plan_calls = []
    monkeypatch.setattr(entitlement_service, "get_plan_entitlements", lambda plan_id: plan_calls.append(plan_id) or {})

    context = streamlit_entitlement_adapter.build_entitlement_context(user_id=1, org_slug="acme")

    assert context == {"role": "viewer", "subscription": None, "entitlements": {}}
    assert plan_calls == []


def test_build_entitlement_context_no_role_means_no_membership(monkeypatch):
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", lambda user_id, org_slug: None)
    monkeypatch.setattr(entitlement_service, "get_org_subscription", lambda org_slug: None)

    context = streamlit_entitlement_adapter.build_entitlement_context(user_id=99, org_slug="someone-elses-org")

    assert context["role"] is None


def test_build_entitlement_context_looks_up_the_role_on_every_call_while_the_rest_is_cached(monkeypatch):
    # The security invariant of this adapter: the subscription and plan
    # lookups are served from cache, the role lookup never is.
    role_calls = []

    def counting_role(user_id, org_slug):
        role_calls.append((user_id, org_slug))
        return "manager"

    engine = FakeEngine([
        FakeQueryResult(first=SUBSCRIPTION),
        FakeQueryResult(all_rows=[{"feature_key": "exports", "enabled": True, "limit_value": None}]),
    ])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", counting_role)

    for _ in range(5):
        context = streamlit_entitlement_adapter.build_entitlement_context(user_id=1, org_slug="acme")

    assert role_calls == [(1, "acme")] * 5
    assert len(engine.calls) == 2  # one subscription query + one plan query, total
    assert context["entitlements"] == {"exports": {"enabled": True, "limit_value": None}}


def test_build_entitlement_context_reflects_role_change_on_next_call_with_no_clear(monkeypatch):
    # An admin is demoted: the very next call with identical args must see
    # it -- no .clear() call, no TTL wait -- even though the subscription
    # lookup for the same org is being served from cache.
    current_role = {"value": "admin"}
    engine = FakeEngine([FakeQueryResult(first=NO_PLAN_SUBSCRIPTION)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)
    monkeypatch.setattr(
        entitlement_service, "get_org_role_for_user",
        lambda user_id, org_slug: current_role["value"],
    )

    first = streamlit_entitlement_adapter.build_entitlement_context(user_id=1, org_slug="acme")
    current_role["value"] = "viewer"
    second = streamlit_entitlement_adapter.build_entitlement_context(user_id=1, org_slug="acme")

    assert first["role"] == "admin"
    assert second["role"] == "viewer"
    assert len(engine.calls) == 1


def test_build_entitlement_context_shares_subscription_cache_across_users_same_org(monkeypatch):
    # Subscription/entitlements caching never depended on user_id -- two
    # different users in the same org correctly share one cached lookup
    # rather than each re-querying.
    engine = FakeEngine([FakeQueryResult(first=NO_PLAN_SUBSCRIPTION)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", lambda user_id, org_slug: "viewer")

    streamlit_entitlement_adapter.build_entitlement_context(user_id=1, org_slug="acme")
    streamlit_entitlement_adapter.build_entitlement_context(user_id=2, org_slug="acme")

    assert len(engine.calls) == 1
