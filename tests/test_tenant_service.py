import inspect
import json
from datetime import UTC, datetime

import pytest
from db_fakes import FakeEngine, FakeQueryResult

from src.services import collector_rules, tenant_service

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


# --- list_active_plans -------------------------------------------------------

def test_list_active_plans_returns_the_active_plans_code_and_name_in_id_order(monkeypatch):
    plans = [{"code": "starter", "name": "Starter"}, {"code": "pro", "name": "Pro"}]
    engine = FakeEngine([FakeQueryResult(all_rows=plans)])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    assert tenant_service.list_active_plans() == plans
    (call,) = engine.calls
    sql = " ".join(call["sql"].split())
    # The same rule a plan code is looked up under when an organization is created: active plans only.
    assert sql == "SELECT code, name FROM plans WHERE is_active = TRUE ORDER BY id"
    assert "AND is_active = TRUE" in inspect.getsource(tenant_service.create_organization_with_primary_branch)


def test_list_active_plans_is_empty_when_no_plan_is_active(monkeypatch):
    monkeypatch.setattr(tenant_service, "get_engine", lambda: FakeEngine([FakeQueryResult(all_rows=[])]))

    assert tenant_service.list_active_plans() == []


# --- collector site settings (Contract v2) ------------------------------------

REVIEWED_AT = datetime(2026, 10, 11, 15, 4, 5, tzinfo=UTC)  # passed in; nothing here reads a clock
STORED_DOCUMENT = {
    "library_name": "Acme Library",
    "security": {"admin_enabled": True, "admin_password_hash": "CANARY-admin-lock-hash"},
    "internal_routing": {
        "branch_services_names": ["CANARY-STAFF-NAME-ONE", "CANARY-STAFF-NAME-TWO", "  "],
        "collection_services_names": ["CANARY-ACCOUNT-NAME"],
        "branch_services_da_patterns": [],
    },
    "transit": {
        "home_branch_label": "Central",
        "destinations": [
            {"key": "dest_1", "label": "North Branch", "enabled": True},
            {"key": "old", "label": "Old Depot", "enabled": False},
            {"label": "Bookmobile"},  # enabled unless it says otherwise
        ],
    },
    "collector_timezone": "America/Denver",
    "collector_rules": {"reviewed_at": "2026-01-01T00:00:00+00:00", "reviewed_by_user_id": 3},
}


def _site_engine(status="active", document=STORED_DOCUMENT, written=True):
    return FakeEngine([
        FakeQueryResult(first={"organization_status": status, "settings_json": document}),
        FakeQueryResult(first={"organization_id": 1} if written else None),
    ])


def test_get_collector_site_settings_returns_the_enrollment_keys_labels_and_counts_and_no_list_entry(monkeypatch):
    engine = _site_engine()
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    site = tenant_service.get_collector_site_settings(1)

    assert site == {
        "collector_timezone": "America/Denver",
        "collector_rules": {"reviewed_at": "2026-01-01T00:00:00+00:00", "reviewed_by_user_id": 3},
        "home_branch_label": "Central",
        "transit_destination_labels": ["North Branch", "Bookmobile"],  # enabled ones only
        "internal_routing_counts": {"branch_services_names": 2, "collection_services_names": 1,
                                    "branch_services_da_patterns": 0, "collection_services_da_patterns": 0},
    }
    # Counts only: no patron account name, and nothing else of the document (the admin lock), leaves the service.
    assert "CANARY" not in repr(site)
    assert engine.calls[0]["params"] == {"organization_id": 1}
    # The ORGANIZATION's own row -- the one the enrollment reads -- and never a branch's settings.
    assert "organization_settings" in engine.calls[0]["sql"] and "branch_settings" not in engine.calls[0]["sql"]
    # ... and what it returns is what services.collector_rules itself checks.
    assert collector_rules.collector_timezone(site) == "America/Denver"
    collector_rules.require_rules_reviewed(site)


@pytest.mark.parametrize("document", [None, {}, "{}", {"transit": "not a block", "internal_routing": ["not", "a", "block"]}])
def test_get_collector_site_settings_reads_an_unprepared_organization_as_having_nothing(monkeypatch, document):
    monkeypatch.setattr(tenant_service, "get_engine", lambda: _site_engine(document=document))

    site = tenant_service.get_collector_site_settings(1)

    assert site["collector_timezone"] is None and site["collector_rules"] is None
    assert site["home_branch_label"] == ""
    assert site["transit_destination_labels"] == [] and set(site["internal_routing_counts"].values()) == {0}
    with pytest.raises(collector_rules.CollectorSettingsError):
        collector_rules.collector_timezone(site)


def test_get_collector_site_settings_raises_for_an_unknown_organization(monkeypatch):
    monkeypatch.setattr(tenant_service, "get_engine", lambda: FakeEngine([FakeQueryResult(first=None)]))

    with pytest.raises(RuntimeError, match="not found"):
        tenant_service.get_collector_site_settings(99)


def test_set_collector_site_settings_saves_the_zone_the_destinations_and_the_reviewers_own_record(monkeypatch):
    engine = _site_engine()
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    stored = tenant_service.set_collector_site_settings(
        1, timezone="  America/New_York ", transit_destination_labels=["Riverside", " ", "riverside", "Old Depot Annex"],
        reviewed_by_user_id=7, reviewed_at=REVIEWED_AT,
    )

    marker = {"reviewed_at": "2026-10-11T15:04:05+00:00", "reviewed_by_user_id": 7}  # the id it was GIVEN, and no name
    assert stored == {"collector_timezone": "America/New_York", "collector_rules": marker}
    read, write = engine.calls
    assert read["params"] == {"organization_id": 1}
    params = write["params"]
    assert params["organization_id"] == 1 and json.loads(params["site"]) == stored
    # The enabled destinations are exactly what was given, each with its label's canonical key; the disabled one and
    # the home label are kept as they were. The old enabled ones -- and the synthetic "dest_1" key -- are gone.
    transit = json.loads(params["transit"])
    assert transit == {
        "home_branch_label": "Central",
        "destinations": [
            {"key": "riverside", "label": "Riverside", "enabled": True},
            {"key": "old_depot_annex", "label": "Old Depot Annex", "enabled": True},
            {"key": "old", "label": "Old Depot", "enabled": False},
        ],
    }
    # What a first write inserts is those three keys and nothing else.
    assert json.loads(params["document"]) == {**stored, "transit": transit}
    # What the enrollment then reads passes its own checks.
    assert collector_rules.collector_timezone(stored) == "America/New_York"
    collector_rules.require_rules_reviewed(stored)


def test_set_collector_site_settings_leaves_every_other_key_of_the_document_to_the_statement(monkeypatch):
    """Nothing of the stored document but its `transit` block is sent back: the statement merges the two enrollment
    keys and that block INTO the stored document, so the admin lock, the classification lists and everything else
    are untouched -- and cannot be overwritten with a stale copy. (tests/test_collector_site_settings_postgres.py runs
    the statement itself.)"""
    engine = _site_engine()
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    tenant_service.set_collector_site_settings(1, timezone="America/New_York", transit_destination_labels=[],
                                               reviewed_by_user_id=7, reviewed_at=REVIEWED_AT)

    write = engine.calls[1]
    sql = " ".join(write["sql"].split())
    assert set(write["params"]) == {"organization_id", "document", "site", "transit"}
    assert "CANARY" not in repr(write["params"]) and "library_name" not in repr(write["params"])
    assert "ON CONFLICT (organization_id) DO UPDATE SET settings_json = jsonb_set( organization_settings.settings_json || CAST(:site AS JSONB), '{transit}', CAST(:transit AS JSONB), true )" in sql
    assert "AND o.status <> 'cancelled'" in sql  # refused by the statement itself, whatever was read a moment before


@pytest.mark.parametrize("written", [True, False], ids=["read as cancelled", "cancelled between the read and the write"])
def test_set_collector_site_settings_refuses_a_cancelled_organization(monkeypatch, written):
    engine = _site_engine(status="cancelled" if written else "active", written=written)
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(RuntimeError, match="cancelled"):
        tenant_service.set_collector_site_settings(1, timezone="America/New_York", transit_destination_labels=["Riverside"],
                                                   reviewed_by_user_id=7, reviewed_at=REVIEWED_AT)

    assert len(engine.calls) == (1 if written else 2)  # read as cancelled: the write is never attempted


def test_set_collector_site_settings_raises_for_an_unknown_organization(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(RuntimeError, match="not found"):
        tenant_service.set_collector_site_settings(99, timezone="America/New_York", transit_destination_labels=[],
                                                   reviewed_by_user_id=7, reviewed_at=REVIEWED_AT)

    assert len(engine.calls) == 1


@pytest.mark.parametrize("timezone", ["", "   ", "Central", "CST", "America/Not_A_City", "UTC-6", None])
def test_set_collector_site_settings_refuses_a_time_zone_that_is_not_iana_before_touching_the_database(monkeypatch, timezone):
    engine = _site_engine()
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(collector_rules.CollectorSettingsError):
        tenant_service.set_collector_site_settings(1, timezone=timezone, transit_destination_labels=[],
                                                   reviewed_by_user_id=7, reviewed_at=REVIEWED_AT)

    assert engine.calls == []


@pytest.mark.parametrize("labels", [
    ["North Branch", "North-Branch"],
    ["The Very Long Named Regional Library Annex", "The Very Long Named Regional Library Bookmobile"],
])
def test_set_collector_site_settings_refuses_labels_that_share_a_canonical_key_before_touching_the_database(monkeypatch, labels):
    engine = _site_engine()
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(ValueError, match="share a canonical key"):
        tenant_service.set_collector_site_settings(1, timezone="America/New_York", transit_destination_labels=labels,
                                                   reviewed_by_user_id=7, reviewed_at=REVIEWED_AT)

    assert engine.calls == []  # never collapsed into one destination, and nothing read or written


@pytest.mark.parametrize("labels", [
    ["Riverside", "Main"],      # the collector contract's home
    ["local"],
    ["1"],
    ["Riverside", "CENTRAL"],   # this organization's own STORED home label ("Central")
], ids=["Main", "local", "1", "the stored home label"])
def test_set_collector_site_settings_refuses_a_label_that_means_home_and_writes_nothing(monkeypatch, labels):
    engine = _site_engine()
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(ValueError, match="means home"):
        tenant_service.set_collector_site_settings(1, timezone="America/New_York", transit_destination_labels=labels,
                                                   reviewed_by_user_id=7, reviewed_at=REVIEWED_AT)

    assert len(engine.calls) == 1  # the read that says what home is; the write is never attempted


def test_the_home_rule_is_the_customer_apis_own_and_the_messages_only_name_what_it_refused():
    from src.services import routing_settings

    labels = ["North Branch", " Main ", "Riverside", "central library", "Local"]

    messages = tenant_service.transit_destination_home_messages("Central Library", labels)

    assert [message.split('"')[1] for message in messages] == ["Main", "central library", "Local"]
    assert all("means this library's own (home) branch" in message for message in messages)
    # Exactly the labels validate_routing_settings refuses as `means_home` -- no rule of this module's own.
    block = {"home_branch_label": "Central Library", "destinations": [{"label": label.strip(), "enabled": True} for label in labels]}
    with pytest.raises(routing_settings.RoutingSettingsError) as refused:
        routing_settings.validate_routing_settings(block)
    assert [problem.field for problem in refused.value.problems if problem.code == routing_settings.MEANS_HOME] == [
        "destinations.1.label", "destinations.3.label", "destinations.4.label"]
    assert tenant_service.transit_destination_home_messages("", ["North Branch", "Riverside"]) == []
    assert tenant_service.transit_destination_home_messages(None, ["North Branch"]) == []


def test_transit_destination_problems_are_the_home_labels_and_the_key_collisions_together():
    problems = tenant_service.transit_destination_problems("Central", ["Main", "North Branch", "North-Branch"])

    assert len(problems) == 2
    assert problems[0].startswith('"Main" means this library')
    assert '"North Branch", "North-Branch"' in problems[1] and "share one key" in problems[1]
    assert tenant_service.transit_destination_problems("Central", ["North Branch", "3rd Street Branch"]) == []
    assert tenant_service.transit_destination_problems("Central", []) == []


@pytest.mark.parametrize("reviewer", [0, -1, None, "7", True, 7.0])
def test_set_collector_site_settings_never_invents_a_reviewer(monkeypatch, reviewer):
    engine = _site_engine()
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(ValueError, match="reviewed_by_user_id"):
        tenant_service.set_collector_site_settings(1, timezone="America/New_York", transit_destination_labels=[],
                                                   reviewed_by_user_id=reviewer, reviewed_at=REVIEWED_AT)

    assert engine.calls == []


def test_set_collector_site_settings_stamps_the_review_now_when_no_time_is_given(monkeypatch):
    monkeypatch.setattr(tenant_service, "get_engine", lambda: _site_engine())

    stored = tenant_service.set_collector_site_settings(1, timezone="America/New_York", transit_destination_labels=[],
                                                        reviewed_by_user_id=7)

    assert datetime.fromisoformat(stored["collector_rules"]["reviewed_at"]).tzinfo is not None  # an aware instant
    collector_rules.require_rules_reviewed(stored)


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
