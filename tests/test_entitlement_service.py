from db_fakes import FakeEngine, FakeQueryResult

from src.services import entitlement_service

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


def test_get_org_subscription_queries_the_database_on_every_call(monkeypatch):
    # The core is uncached (Block 2d): the dashboard's st.cache_data layer
    # lives in services.streamlit_entitlement_adapter and is tested in
    # tests/test_streamlit_entitlement_adapter.py.
    row = {"id": 10, "status": "active", "started_at": None, "ends_at": None,
           "plan_id": 2, "plan_code": "pro", "plan_name": "Pro"}
    engine = FakeEngine([FakeQueryResult(first=row), FakeQueryResult(first=None)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    first = entitlement_service.get_org_subscription(org_slug="acme")
    second = entitlement_service.get_org_subscription(org_slug="acme")

    assert first == row
    assert second is None
    assert len(engine.calls) == 2


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


def test_get_plan_entitlements_queries_the_database_on_every_call(monkeypatch):
    rows = [{"feature_key": "exports", "enabled": True, "limit_value": None}]
    engine = FakeEngine([FakeQueryResult(all_rows=rows), FakeQueryResult(all_rows=[])])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)

    first = entitlement_service.get_plan_entitlements(plan_id=2)
    second = entitlement_service.get_plan_entitlements(plan_id=2)

    assert first == {"exports": {"enabled": True, "limit_value": None}}
    assert second == {}
    assert len(engine.calls) == 2


def test_the_core_lookups_carry_no_streamlit_cache():
    for name in ("get_org_role_for_user", "get_org_subscription", "get_plan_entitlements",
                 "build_entitlement_context", "build_entitlement_context_with"):
        assert not hasattr(getattr(entitlement_service, name), "clear"), name


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


def test_build_entitlement_context_queries_the_subscription_on_every_call(monkeypatch):
    # The core never caches: two context builds for the same org are two
    # subscription queries. (Sharing one cached lookup across users of the
    # same org is the Streamlit adapter's job.)
    row = {"id": 1, "status": "active", "started_at": None, "ends_at": None,
           "plan_id": None, "plan_code": None, "plan_name": None}
    engine = FakeEngine([FakeQueryResult(first=row), FakeQueryResult(first=row)])
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", lambda user_id, org_slug: "viewer")

    entitlement_service.build_entitlement_context(user_id=1, org_slug="acme")
    entitlement_service.build_entitlement_context(user_id=2, org_slug="acme")

    assert len(engine.calls) == 2


# --- build_entitlement_context_with (the one assembly point) ------------------

def test_build_entitlement_context_with_uses_the_supplied_lookups_and_the_cores_own_role(monkeypatch):
    calls = []

    def role(user_id, org_slug):
        calls.append(("role", user_id, org_slug))
        return "manager"

    def subscription(org_slug):
        calls.append(("subscription", org_slug))
        return {"id": 1, "plan_id": 5, "status": "active"}

    def plan(plan_id):
        calls.append(("plan", plan_id))
        return {"exports": {"enabled": True, "limit_value": None}}

    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", role)

    context = entitlement_service.build_entitlement_context_with(
        1, "acme", load_subscription=subscription, load_plan_entitlements=plan,
    )

    assert calls == [("role", 1, "acme"), ("subscription", "acme"), ("plan", 5)]
    assert context == {
        "role": "manager",
        "subscription": {"id": 1, "plan_id": 5, "status": "active"},
        "entitlements": {"exports": {"enabled": True, "limit_value": None}},
    }


def test_build_entitlement_context_with_skips_the_plan_lookup_without_a_plan(monkeypatch):
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", lambda user_id, org_slug: None)
    plan_calls = []

    for subscription in (None, {"id": 1, "plan_id": None}):
        context = entitlement_service.build_entitlement_context_with(
            1, "acme",
            load_subscription=lambda org_slug, subscription=subscription: subscription,
            load_plan_entitlements=lambda plan_id: plan_calls.append(plan_id) or {},
        )
        assert context == {"role": None, "subscription": subscription, "entitlements": {}}

    assert plan_calls == []


def test_build_entitlement_context_with_does_not_accept_a_role_lookup():
    # The role lookup is not injectable: a caller (the Streamlit adapter)
    # can supply cached subscription/plan lookups, but never a cached role.
    import inspect

    parameters = inspect.signature(entitlement_service.build_entitlement_context_with).parameters
    assert list(parameters) == ["user_id", "org_slug", "load_subscription", "load_plan_entitlements"]


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
