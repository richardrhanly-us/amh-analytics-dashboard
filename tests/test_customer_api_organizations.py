"""Block 3c: the authenticated user's organization and branch context.

    GET /api/organizations
    GET /api/organizations/{org_slug}

Both describe what the user may reach in SaaS terms only (slugs). The service
rows they are built from also carry internal and OPERATIONAL identifiers --
organization_id, customer_id, the branch's database id and its operational
branch_id -- and a large part of this file is proving none of them can reach
a response.

The services are replaced at the FLAT module identity the routes use
(services.access_service, services.entitlement_service,
services.session_service). The session cookie is sent with an explicit Cookie
header, as in tests/test_customer_api_auth.py.
"""

from __future__ import annotations

import json

import pytest
from db_fakes import FakeEngine, FakeQueryResult
from fastapi.testclient import TestClient

import main
from services import (
    access_service,
    entitlement_service,
    session_service,
    sorter_inventory_service,
)
from services.sorter_inventory_service import SorterSite

COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 7, "email": "user@example.invalid", "full_name": "Test User"}

# Synthetic identifiers chosen so that none of them can appear in a response
# by accident: every internal/operational id is a distinctive number.
ACME = {"organization_id": 900001, "customer_id": 800001, "role": "admin",
        "organization_slug": "acme", "organization_name": "Acme Library"}
BETA = {"organization_id": 900002, "customer_id": 800002, "role": "viewer",
        "organization_slug": "beta", "organization_name": "Beta Library"}
BRANCHES = [
    {"id": 700001, "branch_id": 600001, "branch_slug": "main", "branch_name": "Main",
     "is_primary": True, "status": "active"},
    {"id": 700002, "branch_id": 600002, "branch_slug": "east", "branch_name": "East",
     "is_primary": False, "status": "active"},
]
# One registered sorter, at the main branch. East has none: it is a branch, not a machine.
SORTERS = [SorterSite(slug="main", name="Main Library AMH", host_branch_slug="main", host_branch_name="Main",
                      status="active", collector_count=1)]
SORTERS_JSON = [{"slug": "main", "name": "Main Library AMH", "host_branch": {"slug": "main", "name": "Main"},
                 "status": "active", "collector_count": 1}]
SUBSCRIPTION = {"id": 500001, "status": "active", "started_at": None, "ends_at": None,
                "plan_id": 400001, "plan_code": "pro", "plan_name": "Pro"}
ENTITLEMENTS = {"exports": {"enabled": True, "limit_value": None},
                "max_branches": {"enabled": True, "limit_value": 3}}
FORBIDDEN_NUMBERS = ("900001", "900002", "800001", "800002", "700001", "700002", "600001", "600002",
                     "500001", "400001")
FORBIDDEN_KEYS = {"organization_id", "customer_id", "operational_customer_id", "id", "branch_id",
                  "operational_branch_id", "plan_id", "status_id", "user_id"}

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
NOT_FOUND = {"code": "organization_not_found", "message": "Organization not found."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}


@pytest.fixture
def api():
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


class Services:
    """Recording stand-ins for the session and the core access/entitlement
    services, with per-organization answers."""

    def __init__(self, monkeypatch, *, user=USER, memberships=(ACME, BETA), modes=None, roles=None,
                 subscription=SUBSCRIPTION, entitlements=ENTITLEMENTS, branches=BRANCHES, sorters=SORTERS):
        self.calls: list[tuple] = []
        self.user = user
        self.memberships = [dict(m) for m in memberships]
        self.modes = {"acme": "full", "beta": "full", **(modes or {})}
        self.roles = {m["organization_slug"]: m["role"] for m in memberships} | (roles or {})
        self.subscription = subscription
        self.entitlements = entitlements
        self.branches = branches
        self.sorters = list(sorters)
        self.raise_on: dict[str, Exception] = {}

        monkeypatch.setattr(session_service, "validate_session", self._validate_session)
        monkeypatch.setattr(access_service, "get_user_memberships", self._get_user_memberships)
        monkeypatch.setattr(access_service, "get_org_access_mode", self._get_org_access_mode)
        monkeypatch.setattr(access_service, "get_org_branches", self._get_org_branches)
        monkeypatch.setattr(entitlement_service, "build_entitlement_context", self._build_entitlement_context)
        monkeypatch.setattr(sorter_inventory_service, "list_sorter_sites", self._list_sorter_sites)

    def _record(self, name, *args):
        self.calls.append((name, *args))
        if name in self.raise_on:
            raise self.raise_on[name]

    def _validate_session(self, raw_token):
        self._record("validate_session")
        return dict(self.user) if self.user else None

    def _get_user_memberships(self, user_id):
        self._record("get_user_memberships", user_id)
        return [dict(m) for m in self.memberships]

    def _get_org_access_mode(self, org_slug):
        self._record("get_org_access_mode", org_slug)
        return self.modes.get(org_slug, "blocked")

    def _get_org_branches(self, org_slug):
        self._record("get_org_branches", org_slug)
        return [dict(b) for b in self.branches]

    def _list_sorter_sites(self, org_slug):
        self._record("list_sorter_sites", org_slug)
        return list(self.sorters)

    def _build_entitlement_context(self, user_id, org_slug):
        self._record("build_entitlement_context", user_id, org_slug)
        return {
            "role": self.roles.get(org_slug),
            "subscription": dict(self.subscription) if self.subscription else None,
            "entitlements": {key: dict(value) for key, value in self.entitlements.items()},
        }

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def _keys(value) -> set[str]:
    """Every dictionary key anywhere in a JSON value."""
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def _assert_no_internal_or_operational_identifier(response) -> None:
    assert _keys(response.json()).isdisjoint(FORBIDDEN_KEYS)
    for number in FORBIDDEN_NUMBERS:
        assert number not in response.text


# =====================================================================================================================
# GET /api/organizations
# =====================================================================================================================

def test_the_list_returns_each_membership_with_its_role_and_access_mode(api, monkeypatch):
    services = Services(monkeypatch, modes={"beta": "read_only"})

    response = api.get("/api/organizations", headers=COOKIE)

    assert response.status_code == 200
    assert response.json() == [
        {"slug": "acme", "name": "Acme Library", "role": "admin", "access_mode": "full"},
        {"slug": "beta", "name": "Beta Library", "role": "viewer", "access_mode": "read_only"},
    ]
    assert response.headers["cache-control"] == "no-store"
    assert services.calls == [
        ("validate_session",),
        ("get_user_memberships", 7),              # the session's user, never a request value
        ("get_org_access_mode", "acme"),
        ("get_org_access_mode", "beta"),
    ]


def test_the_list_carries_only_the_four_summary_fields(api, monkeypatch):
    Services(monkeypatch)

    response = api.get("/api/organizations", headers=COOKIE)

    assert all(set(item) == {"slug", "name", "role", "access_mode"} for item in response.json())
    _assert_no_internal_or_operational_identifier(response)


def test_the_list_keeps_the_order_the_service_returns(api, monkeypatch):
    Services(monkeypatch, memberships=(BETA, ACME))

    response = api.get("/api/organizations", headers=COOKIE)

    assert [item["slug"] for item in response.json()] == ["beta", "acme"]


def test_a_user_with_no_memberships_gets_an_empty_list(api, monkeypatch):
    services = Services(monkeypatch, memberships=())

    response = api.get("/api/organizations", headers=COOKIE)

    assert response.status_code == 200
    assert response.json() == []
    assert services.names() == ["validate_session", "get_user_memberships"]


@pytest.mark.parametrize("mode", ["blocked", "some-future-mode", "", None])
def test_an_organization_whose_access_mode_is_not_visible_is_left_out_of_the_list(api, monkeypatch, mode):
    # get_user_memberships already excludes a cancelled organization. If one
    # still reports "blocked" here (cancelled between the two lookups, or a
    # status the membership query does not exclude), it is dropped -- never
    # shown as full or read_only.
    Services(monkeypatch, modes={"beta": mode})

    response = api.get("/api/organizations", headers=COOKIE)

    assert response.status_code == 200
    assert [item["slug"] for item in response.json()] == ["acme"]


@pytest.mark.parametrize("failing", ["get_user_memberships", "get_org_access_mode"])
def test_a_database_failure_in_the_list_is_500_never_an_empty_success(api, monkeypatch, failing):
    services = Services(monkeypatch)
    services.raise_on[failing] = RuntimeError("synthetic database failure")

    response = api.get("/api/organizations", headers=COOKIE)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR


def test_the_list_requires_a_session(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.get("/api/organizations")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert services.calls == []


def test_the_list_with_a_session_that_does_not_validate_is_401_and_reads_no_memberships(api, monkeypatch):
    services = Services(monkeypatch, user=None)

    response = api.get("/api/organizations", headers=COOKIE)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert services.names() == ["validate_session"]


# =====================================================================================================================
# GET /api/organizations/{org_slug}
# =====================================================================================================================

def test_the_detail_returns_the_organization_its_branches_and_its_entitlements(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.get("/api/organizations/acme", headers=COOKIE)

    assert response.status_code == 200
    assert response.json() == {
        "slug": "acme",
        "name": "Acme Library",
        "role": "admin",
        "access_mode": "full",
        "branches": [
            {"slug": "main", "name": "Main", "is_primary": True},
            {"slug": "east", "name": "East", "is_primary": False},
        ],
        "sorters": SORTERS_JSON,
        "subscription": {"plan_code": "pro", "plan_name": "Pro", "status": "active"},
        "entitlements": {
            "exports": {"enabled": True, "limit_value": None},
            "max_branches": {"enabled": True, "limit_value": 3},
        },
    }
    assert response.headers["cache-control"] == "no-store"
    assert services.calls == [
        ("validate_session",),
        ("get_user_memberships", 7),
        ("get_org_access_mode", "acme"),
        ("build_entitlement_context", 7, "acme"),
        ("get_org_branches", "acme"),
        ("list_sorter_sites", "acme"),
    ]


def test_the_detail_exposes_no_internal_or_operational_identifier(api, monkeypatch):
    Services(monkeypatch)

    response = api.get("/api/organizations/acme", headers=COOKIE)

    _assert_no_internal_or_operational_identifier(response)
    assert all(set(branch) == {"slug", "name", "is_primary"} for branch in response.json()["branches"])
    assert set(response.json()["subscription"]) == {"plan_code", "plan_name", "status"}


def test_the_detail_role_comes_from_the_entitlement_lookup_not_the_membership_row(api, monkeypatch):
    # The membership row says "admin"; the entitlement context's own uncached
    # role lookup says "viewer". The response reports the latter.
    Services(monkeypatch, roles={"acme": "viewer"})

    response = api.get("/api/organizations/acme", headers=COOKIE)

    assert response.json()["role"] == "viewer"


def test_a_suspended_organization_is_readable_as_read_only(api, monkeypatch):
    Services(monkeypatch, modes={"acme": "read_only"})

    response = api.get("/api/organizations/acme", headers=COOKIE)

    assert response.status_code == 200
    assert response.json()["access_mode"] == "read_only"
    assert [branch["slug"] for branch in response.json()["branches"]] == ["main", "east"]


def test_an_organization_without_a_subscription_has_none_and_no_entitlements(api, monkeypatch):
    Services(monkeypatch, subscription=None, entitlements={})

    response = api.get("/api/organizations/acme", headers=COOKIE)

    assert response.status_code == 200
    assert response.json()["subscription"] is None
    assert response.json()["entitlements"] == {}


def test_an_organization_with_no_active_branch_has_an_empty_branch_list(api, monkeypatch):
    Services(monkeypatch, branches=[])

    response = api.get("/api/organizations/acme", headers=COOKIE)

    assert response.status_code == 200
    assert response.json()["branches"] == []


# One helper per way an organization can be unreachable. Each returns the
# response and the calls made, so the tests below can compare them.
def _unknown_org(api, monkeypatch):
    services = Services(monkeypatch)
    return api.get("/api/organizations/no-such-org", headers=COOKIE), services


def _not_a_member(api, monkeypatch):
    services = Services(monkeypatch, memberships=(BETA,))  # "acme" exists and is usable, for someone else
    return api.get("/api/organizations/acme", headers=COOKIE), services


def _cancelled_and_excluded_by_the_membership_query(api, monkeypatch):
    services = Services(monkeypatch, memberships=(BETA,), modes={"acme": "blocked"})
    return api.get("/api/organizations/acme", headers=COOKIE), services


def _blocked_although_still_listed(api, monkeypatch):
    services = Services(monkeypatch, modes={"acme": "blocked"})
    return api.get("/api/organizations/acme", headers=COOKIE), services


def _membership_removed_before_the_role_lookup(api, monkeypatch):
    services = Services(monkeypatch, roles={"acme": None})
    return api.get("/api/organizations/acme", headers=COOKIE), services


UNREACHABLE = [
    _unknown_org,
    _not_a_member,
    _cancelled_and_excluded_by_the_membership_query,
    _blocked_although_still_listed,
    _membership_removed_before_the_role_lookup,
]


@pytest.mark.parametrize("case", UNREACHABLE, ids=lambda case: case.__name__.strip("_"))
def test_every_unreachable_organization_gets_the_same_404(api, monkeypatch, case):
    response, services = case(api, monkeypatch)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers  # not being a member is not a reason to end the session
    # Nothing about the organization is read once it is known to be unreachable.
    assert "get_org_branches" not in services.names()


def test_the_unreachable_responses_are_byte_for_byte_identical(api, monkeypatch):
    seen = set()
    for case in UNREACHABLE:
        with monkeypatch.context() as scoped:
            response, _ = case(api, scoped)
        seen.add((response.status_code, response.content, json.dumps(sorted(response.headers.items()))))

    assert len(seen) == 1


def test_a_blocked_organization_is_refused_before_its_entitlements_are_read(api, monkeypatch):
    response, services = _blocked_although_still_listed(api, monkeypatch)

    assert response.status_code == 404
    assert services.names() == ["validate_session", "get_user_memberships", "get_org_access_mode"]


@pytest.mark.parametrize(
    "failing",
    ["get_user_memberships", "get_org_access_mode", "build_entitlement_context", "get_org_branches", "list_sorter_sites"],
)
def test_a_database_failure_in_the_detail_is_500_never_404(api, monkeypatch, failing):
    services = Services(monkeypatch)
    services.raise_on[failing] = RuntimeError("synthetic database failure")

    response = api.get("/api/organizations/acme", headers=COOKIE)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR


def test_the_detail_requires_a_session(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.get("/api/organizations/acme")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert services.calls == []


def test_the_detail_with_a_session_that_does_not_validate_is_401(api, monkeypatch):
    services = Services(monkeypatch, user=None)

    response = api.get("/api/organizations/acme", headers=COOKIE)

    assert response.status_code == 401
    assert services.names() == ["validate_session"]


# =====================================================================================================================
# The client cannot supply identity, role or access
# =====================================================================================================================

@pytest.mark.parametrize("path", ["/api/organizations", "/api/organizations/acme"])
def test_query_parameters_naming_a_tenant_role_or_user_change_nothing(api, monkeypatch, path):
    services = Services(monkeypatch, modes={"acme": "read_only"})
    plain = api.get(path, headers=COOKIE)
    plain_calls = list(services.calls)
    services.calls.clear()

    tampered = api.get(
        path,
        headers=COOKIE,
        params={"customer_id": 800002, "branch_id": 600002, "user_id": 1, "role": "owner",
                "access_mode": "full", "org_slug": "beta"},
    )

    assert tampered.status_code == plain.status_code == 200
    assert tampered.json() == plain.json()
    assert services.calls == plain_calls  # the same lookups, for the session's user and the path's organization


def test_the_organization_routes_take_only_the_slug_from_the_request():
    routes = {route.path: route for route in main.customer_router.routes}

    for path in ("/api/organizations", "/api/organizations/{org_slug}"):
        dependant = routes[path].dependant
        assert dependant.query_params == []
        assert dependant.body_params == []
        assert dependant.header_params == []
        assert dependant.cookie_params == []
    assert [p.name for p in routes["/api/organizations"].dependant.path_params] == []
    assert [p.name for p in routes["/api/organizations/{org_slug}"].dependant.path_params] == ["org_slug"]


def test_the_organization_routes_are_read_only():
    for path in ("/api/organizations", "/api/organizations/acme"):
        for method in ("post", "put", "patch", "delete"):
            assert getattr(TestClient(main.app), method)(path).status_code == 405


# =====================================================================================================================
# The uncached core services, for real
# =====================================================================================================================

MEMBERSHIP_ROWS = [dict(ACME), dict(BETA)]
INSTALLATION_ROWS = [{"name": "Main Library AMH", "status": "active", "branch_slug": "main", "branch_name": "Main"}]


def _real_core(monkeypatch, results) -> FakeEngine:
    """The REAL access_service / entitlement_service against one recording
    fake engine; only the session is stubbed."""
    engine = FakeEngine(results)
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: dict(USER))
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)
    monkeypatch.setattr(entitlement_service, "get_engine", lambda: engine)
    monkeypatch.setattr(sorter_inventory_service, "get_engine", lambda: engine)
    return engine


def _detail_results():
    return [
        FakeQueryResult(all_rows=MEMBERSHIP_ROWS),                                   # get_user_memberships
        FakeQueryResult(first=("suspended",)),                                       # get_org_access_mode
        FakeQueryResult(first=("manager",)),                                         # get_org_role_for_user
        FakeQueryResult(first=dict(SUBSCRIPTION)),                                   # get_org_subscription
        FakeQueryResult(all_rows=[{"feature_key": "exports", "enabled": True, "limit_value": None}]),
        FakeQueryResult(all_rows=[dict(b) for b in BRANCHES]),                       # get_org_branches
        FakeQueryResult(all_rows=[dict(row) for row in INSTALLATION_ROWS]),          # list_sorter_sites
    ]


def test_the_detail_runs_the_core_queries_scoped_to_the_session_user_and_the_path_slug(api, monkeypatch):
    engine = _real_core(monkeypatch, _detail_results())

    response = api.get("/api/organizations/acme", headers=COOKIE)

    assert response.status_code == 200
    assert response.json()["role"] == "manager"            # from the role query, not the membership row's "admin"
    assert response.json()["access_mode"] == "read_only"   # suspended
    assert response.json()["entitlements"] == {"exports": {"enabled": True, "limit_value": None}}
    _assert_no_internal_or_operational_identifier(response)
    assert [call["params"] for call in engine.calls] == [
        {"user_id": 7},
        {"org_slug": "acme"},
        {"user_id": 7, "org_slug": "acme"},
        {"org_slug": "acme"},
        {"plan_id": 400001},
        {"org_slug": "acme"},
        {"org_slug": "acme"},
    ]
    assert "b.status = 'active'" in engine.calls[5]["sql"]  # only active branches, as the dashboard offers
    assert "FROM collector_installations" in engine.calls[6]["sql"]
    assert response.json()["sorters"] == SORTERS_JSON


def test_nothing_is_cached_between_requests(api, monkeypatch):
    engine = _real_core(monkeypatch, _detail_results() + _detail_results())

    first = api.get("/api/organizations/acme", headers=COOKIE)
    second = api.get("/api/organizations/acme", headers=COOKIE)

    assert first.status_code == second.status_code == 200
    assert len(engine.calls) == 14  # every lookup ran again for the second request


def test_the_list_reads_each_organizations_status_fresh(api, monkeypatch):
    engine = _real_core(monkeypatch, [
        FakeQueryResult(all_rows=MEMBERSHIP_ROWS),
        FakeQueryResult(first=("active",)),
        FakeQueryResult(first=("cancelled",)),   # cancelled after the membership query ran
    ])

    response = api.get("/api/organizations", headers=COOKIE)

    assert response.json() == [{"slug": "acme", "name": "Acme Library", "role": "admin", "access_mode": "full"}]
    assert "o.status != 'cancelled'" in engine.calls[0]["sql"]
    _assert_no_internal_or_operational_identifier(response)


def test_the_routes_use_the_core_services_and_never_a_streamlit_adapter(api, monkeypatch):
    import sys

    from customer_api import organization_routes

    assert organization_routes.access_service is access_service
    assert organization_routes.entitlement_service is entitlement_service
    assert not hasattr(access_service.get_user_memberships, "clear")
    assert not hasattr(entitlement_service.build_entitlement_context, "clear")
    # If an adapter is loaded in this process (other tests import them), make
    # any use of it by the routes fail loudly.
    for name in ("services.streamlit_access_adapter", "services.streamlit_entitlement_adapter"):
        adapter = sys.modules.get(name)
        for attribute in ("get_user_memberships", "get_org_branches", "build_entitlement_context"):
            if adapter is not None and hasattr(adapter, attribute):
                monkeypatch.setattr(adapter, attribute, lambda *a, **k: pytest.fail("a Streamlit adapter was called"))
    Services(monkeypatch)

    assert api.get("/api/organizations", headers=COOKIE).status_code == 200
    assert api.get("/api/organizations/acme", headers=COOKIE).status_code == 200


# =====================================================================================================================
# Sorting machines: the organization's registered sorter sites
# =====================================================================================================================

def _site(slug, name, branch_name, status="active", collectors=1) -> SorterSite:
    return SorterSite(slug=slug, name=name, host_branch_slug=slug, host_branch_name=branch_name, status=status,
                      collector_count=collectors)


def test_the_detail_lists_the_organizations_sorters_separately_from_its_branches(api, monkeypatch):
    Services(monkeypatch)

    body = api.get("/api/organizations/acme", headers=COOKIE).json()

    assert body["sorters"] == SORTERS_JSON
    # East is still a branch of the organization. It is not a sorter, and nothing made it one.
    assert [branch["slug"] for branch in body["branches"]] == ["main", "east"]
    assert [sorter["slug"] for sorter in body["sorters"]] == ["main"]


def test_a_sorter_carries_exactly_its_five_public_fields(api, monkeypatch):
    Services(monkeypatch)

    response = api.get("/api/organizations/acme", headers=COOKIE)

    for sorter in response.json()["sorters"]:
        assert list(sorter) == ["slug", "name", "host_branch", "status", "collector_count"]
        assert list(sorter["host_branch"]) == ["slug", "name"]
    _assert_no_internal_or_operational_identifier(response)
    for leaked in ("hostname", "collector_version", "installation_id", "token", "enrollment", "last_seen", "installed_at"):
        assert leaked not in response.text


def test_an_organization_with_no_registered_sorter_has_an_empty_list_whatever_its_branches(api, monkeypatch):
    Services(monkeypatch, sorters=[])

    body = api.get("/api/organizations/acme", headers=COOKIE).json()

    assert body["sorters"] == []
    assert len(body["branches"]) == 2


def test_an_organization_with_several_sorter_sites_lists_each_in_the_order_given(api, monkeypatch):
    Services(monkeypatch, sorters=[
        _site("central", "Central Library AMH", "Central Library"),
        _site("east", "East Branch AMH", "East Branch", status="provisioning"),
        _site("depot", "Old Depot Sorter", "Depot", status="inactive", collectors=0),
    ])

    sorters = api.get("/api/organizations/acme", headers=COOKIE).json()["sorters"]

    assert [(s["slug"], s["name"], s["host_branch"]["name"], s["status"], s["collector_count"]) for s in sorters] == [
        ("central", "Central Library AMH", "Central Library", "active", 1),
        ("east", "East Branch AMH", "East Branch", "provisioning", 1),
        ("depot", "Old Depot Sorter", "Depot", "inactive", 0),
    ]


def test_a_site_with_two_collectors_is_one_sorter_marked_as_combined(api, monkeypatch):
    Services(monkeypatch, sorters=[_site("main", "Main Library AMH", "Main", collectors=2)])

    sorters = api.get("/api/organizations/acme", headers=COOKIE).json()["sorters"]

    assert len(sorters) == 1
    assert sorters[0]["collector_count"] == 2


def test_a_status_the_contract_does_not_have_is_a_500_never_a_sorter(api, monkeypatch):
    Services(monkeypatch, sorters=[_site("main", "Main Library AMH", "Main", status="retired")])

    response = TestClient(main.app, raise_server_exceptions=False).get("/api/organizations/acme", headers=COOKIE)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR


def test_the_sorters_asked_for_are_the_path_organizations_and_no_other(api, monkeypatch):
    services = Services(monkeypatch)

    api.get("/api/organizations/beta", headers=COOKIE, params={"org_slug": "acme", "organization_id": 900001})

    assert ("list_sorter_sites", "beta") in services.calls
    assert ("list_sorter_sites", "acme") not in services.calls


@pytest.mark.parametrize("mode", ["blocked", "unknown-mode"])
def test_no_sorter_is_read_for_an_organization_that_is_not_visible(api, monkeypatch, mode):
    services = Services(monkeypatch, modes={"acme": mode})

    response = api.get("/api/organizations/acme", headers=COOKIE)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert "list_sorter_sites" not in services.names()


def test_no_sorter_is_read_for_an_organization_the_user_is_not_a_member_of(api, monkeypatch):
    services = Services(monkeypatch, memberships=(BETA,))

    response = api.get("/api/organizations/acme", headers=COOKIE)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert "list_sorter_sites" not in services.names()


def test_a_suspended_organization_still_lists_its_sorters(api, monkeypatch):
    Services(monkeypatch, modes={"acme": "read_only"})

    body = api.get("/api/organizations/acme", headers=COOKIE).json()

    assert body["access_mode"] == "read_only"
    assert body["sorters"] == SORTERS_JSON


def test_the_organization_list_carries_no_sorters(api, monkeypatch):
    services = Services(monkeypatch)

    response = api.get("/api/organizations", headers=COOKIE)

    assert "sorters" not in response.text
    assert "list_sorter_sites" not in services.names()
