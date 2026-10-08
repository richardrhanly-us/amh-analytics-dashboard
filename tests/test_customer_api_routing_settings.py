"""R8G: reading and replacing an organization's routing settings -- the routes.

    GET /api/organizations/{org_slug}/settings/routing
    PUT /api/organizations/{org_slug}/settings/routing

These tests drive the real production routes through TestClient(main.app): who may call them, in what order that is
decided, the Origin check, what a body may hold, what an answer looks like and never holds, and what is logged. The
real settings model (services.routing_settings) runs throughout.

The two functions that touch the database (services.routing_settings_service) are replaced by a store that keeps
each organization's settings DOCUMENT in memory and applies the same rule to it -- set the `transit` key, touch
nothing else. Their SQL is PostgreSQL's (JSONB) and runs for real in tests/test_routing_settings_postgres.py. What
can be said about that SQL without a server is pinned at the end of this file.

The session and the access services are replaced at the FLAT module identity the routes use, as in
tests/test_customer_api_efficiency_settings.py.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging

import pytest
from entitlement_support import feature, grant
from fastapi.testclient import TestClient

import main
from customer_api import routing_settings_routes, routing_settings_schemas
from services import (
    access_service,
    entitlement_service,
    routing_settings_service,
    session_service,
)
from services.routing_settings import (
    parse_stored_routing_settings,
    serialize_routing_settings,
)

ACME = "/api/organizations/acme/settings/routing"
BETA = "/api/organizations/beta/settings/routing"

ORIGIN = "https://app.example.invalid"
COOKIE = "__Host-sortview_api_session"
# One session per person. The number is the user's id: distinctive, so it cannot be in a response by accident.
OWNER, ADMIN, MANAGER, VIEWER, STRANGER = 900101, 900102, 900103, 900104, 900105
ROLES = {OWNER: "owner", ADMIN: "admin", MANAGER: "manager", VIEWER: "viewer"}

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
ORGANIZATION_NOT_FOUND = {"code": "organization_not_found", "message": "Organization not found."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have permission to manage these settings."}
READ_ONLY = {"code": "organization_read_only", "message": "This organization's settings cannot be changed."}
ORIGIN_NOT_ALLOWED = {"code": "origin_not_allowed", "message": "Request origin is not allowed."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

# What else a settings document holds. None of it is routing's, and none of it may change or be returned.
CANARY_HASH = "CANARY-admin-lock-hash-5f1d"
DOCUMENT = {
    "library_name": "Acme Library",
    "security": {"admin_enabled": True, "admin_password_hash": CANARY_HASH},
    "efficiency": {"labor_rate": "17.56"},
    "internal_routing": {"branch_services_names": ["CANARY-staff-name"], "collection_services_names": []},
    # As the dashboard's form stored it: keys an administrator typed, which say nothing of the labels.
    "transit": {
        "home_branch_label": "Main",
        "destinations": [
            {"key": "branch_1", "label": "Westside", "enabled": True},
            {"key": "lx", "label": "Library Express", "enabled": False},
        ],
    },
}
STORED = {"home_branch_label": "Main", "destinations": [{"label": "Westside", "enabled": True}, {"label": "Library Express", "enabled": False}]}
WANTED = {"home_branch_label": "Central", "destinations": [{"label": "North Annex", "enabled": True}, {"label": "Westside", "enabled": False}]}


class Store:
    """Each organization's settings document, in memory, behind the service's two functions -- and a record of
    every call made to them."""

    def __init__(self, monkeypatch):
        self.documents: dict[str, dict] = {"acme": copy.deepcopy(DOCUMENT), "beta": {}}
        self.calls: list[tuple] = []
        self.gone = False
        for name in ("read_organization_routing", "replace_organization_routing"):
            monkeypatch.setattr(routing_settings_service, name, getattr(self, name))

    def read_organization_routing(self, org_slug, *, user_id):
        self.calls.append(("read", org_slug, user_id))
        if self.gone or org_slug not in self.documents:
            return None
        return parse_stored_routing_settings(self.documents[org_slug].get("transit"))

    def replace_organization_routing(self, org_slug, settings, *, user_id):
        self.calls.append(("replace", org_slug, user_id))
        if self.gone or org_slug not in self.documents:
            return None
        self.documents[org_slug]["transit"] = serialize_routing_settings(settings)
        return parse_stored_routing_settings(self.documents[org_slug]["transit"])

    def writes(self) -> list[tuple]:
        return [call for call in self.calls if call[0] == "replace"]


@pytest.fixture
def api():
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


@pytest.fixture(autouse=True)
def world(monkeypatch):
    """Acme (active) and Beta (active). OWNER, ADMIN, MANAGER and VIEWER are members of Acme with those roles;
    STRANGER is an owner of Beta only. One allowed browser origin."""
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", ORIGIN)
    state = {"modes": {"acme": "full", "beta": "full"}, "roles": dict(ROLES)}

    def validate_session(raw_token):
        return {"id": int(raw_token), "email": f"person{raw_token[-1]}@example.invalid", "full_name": "Test User"} if raw_token.isdigit() else None

    def memberships(user_id):
        if user_id == STRANGER:
            return [{"organization_slug": "beta", "organization_name": "Beta Library", "role": "owner"}]
        if user_id in state["roles"]:
            return [{"organization_slug": "acme", "organization_name": "Acme Library", "role": state["roles"][user_id]}]
        return []

    def role(user_id, org_slug):
        if org_slug == "beta":
            return "owner" if user_id == STRANGER else None
        return state["roles"].get(user_id)

    monkeypatch.setattr(session_service, "validate_session", validate_session)
    monkeypatch.setattr(access_service, "get_user_memberships", memberships)
    monkeypatch.setattr(access_service, "get_org_access_mode", lambda org_slug: state["modes"].get(org_slug, "blocked"))
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", role)
    # Transit routing is on: what its absence does is tested on its own (R9C).
    grant(monkeypatch)
    return state


@pytest.fixture
def store(monkeypatch):
    return Store(monkeypatch)


def _as(user: int | None, *, origin: str | None = ORIGIN) -> dict[str, str]:
    headers = {}
    if user is not None:
        headers["Cookie"] = f"{COOKIE}={user}"
    if origin is not None:
        headers["Origin"] = origin
    return headers


def _get(api, path=ACME, user=OWNER):
    return api.get(path, headers=_as(user, origin=None))


def _put(api, routing, user=OWNER, path=ACME, **kwargs):
    return api.put(path, json={"routing": routing}, headers=_as(user, **kwargs))


def _problems(response) -> list[tuple[str, str]]:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_routing_settings" and body["message"] == "The routing settings are not valid."
    assert set(body) == {"code", "message", "problems"} and response.headers["cache-control"] == "no-store"
    return [(problem["field"], problem["code"]) for problem in body["problems"]]


def _refused(response, status: int, body: dict) -> None:
    assert (response.status_code, response.json()) == (status, body), response.text
    assert response.headers["cache-control"] == "no-store"


# =====================================================================================================================
# Reading and replacing
# =====================================================================================================================

def test_get_answers_with_the_organizations_stored_block_as_labels_and_flags_and_nothing_else(api, store):
    response = _get(api)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"routing": STORED}
    assert store.calls == [("read", "acme", OWNER)]


def test_an_organization_with_nothing_stored_has_a_blank_home_and_no_destinations(api, store):
    assert _get(api, BETA, STRANGER).json() == {"routing": {"home_branch_label": "", "destinations": []}}


def test_put_replaces_the_whole_block_and_answers_with_what_is_then_stored(api, store):
    response = _put(api, {"home_branch_label": "  Central ", "destinations": [{"label": " North Annex ", "enabled": True}, {"label": "Westside", "enabled": False}]})

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"routing": WANTED}
    assert _get(api).json() == {"routing": WANTED}
    # Replaced, not merged: "Library Express" is gone, and the order is the one that was sent.
    assert [d["label"] for d in store.documents["acme"]["transit"]["destinations"]] == ["North Annex", "Westside"]


def test_put_touches_the_transit_key_alone_and_writes_a_key_derived_from_each_label(api, store):
    before = copy.deepcopy(store.documents["acme"])

    assert _put(api, WANTED).status_code == 200

    after = store.documents["acme"]
    assert {key: value for key, value in after.items() if key != "transit"} == {key: value for key, value in before.items() if key != "transit"}
    # The stored form keeps a `key` for the dashboard's form: the one the label gives, never one a caller sent.
    assert after["transit"] == {
        "home_branch_label": "Central",
        "destinations": [
            {"key": "north_annex", "label": "North Annex", "enabled": True},
            {"key": "westside", "label": "Westside", "enabled": False},
        ],
    }


@pytest.mark.parametrize("routing", [
    {"home_branch_label": "", "destinations": []},
    {"home_branch_label": "Main", "destinations": []},
    {"home_branch_label": "", "destinations": [{"label": "Westside", "enabled": False}]},
    {"home_branch_label": "Main", "destinations": [{"label": "Bücherei Süd — Dock #2", "enabled": True}, {"label": "x" * 300, "enabled": False}]},
    {"home_branch_label": "Main", "destinations": [{"label": f"Branch {n}", "enabled": True} for n in range(20)]},
])
def test_what_the_settings_model_allows_is_stored_as_sent(api, store, routing):
    response = _put(api, routing)

    assert response.status_code == 200, response.text
    assert response.json() == {"routing": routing}


def test_get_never_fails_on_what_the_dashboards_form_stored_though_a_put_of_the_same_would(api, store):
    store.documents["acme"]["transit"] = {
        "home_branch_label": " Main ",
        "destinations": [
            {"key": "branch_1", "label": "Westside", "enabled": True},
            {"key": "branch_2", "label": "Westside", "enabled": False},     # the same destination twice
            {"key": "main", "label": "Main", "enabled": True},              # one that means home
            {"key": "lx", "label": "Library Express"},                      # no `enabled`: enabled
            {"key": "branch_5", "label": "  ", "enabled": True},            # a blank row
            "CANARY-not-an-object",
        ],
    }

    response = _get(api)

    assert response.status_code == 200
    assert response.json() == {"routing": {"home_branch_label": "Main", "destinations": [
        {"label": "Westside", "enabled": True}, {"label": "Westside", "enabled": False},
        {"label": "Main", "enabled": True}, {"label": "Library Express", "enabled": True},
    ]}}
    assert "CANARY" not in response.text
    # Sent straight back, it is told what is wrong with it -- and nothing is written.
    assert _problems(_put(api, response.json()["routing"])) == [("destinations.1.label", "duplicate"), ("destinations.2.label", "means_home")]
    assert store.writes() == []


@pytest.mark.parametrize("transit", [None, "not-a-block", [], 7, {"destinations": "Westside"}, {"home_branch_label": 5, "destinations": [None, 3]}])
def test_a_malformed_stored_block_is_answered_as_no_settings_never_as_an_error_or_as_itself(api, store, transit):
    store.documents["acme"]["transit"] = transit

    assert _get(api).json() == {"routing": {"home_branch_label": "", "destinations": []}}


# =====================================================================================================================
# What an answer never holds
# =====================================================================================================================

def test_no_answer_holds_a_stored_key_an_id_or_anything_else_in_the_settings_document(api, store):
    text = _get(api).text + _put(api, WANTED).text + _put(api, {"home_branch_label": "Main", "destinations": [{"label": "", "enabled": True}]}).text

    for forbidden in ("key", "branch_1", "lx", "north_annex", CANARY_HASH, "CANARY", "security", "efficiency", "17.56", "internal_routing",
                      "library_name", "Acme Library", "transit", "settings_json", "organization_id", "user_id", *map(str, ROLES), "acme"):
        assert forbidden not in text, forbidden
    assert set(routing_settings_schemas.RoutingDestination.model_fields) == {"label", "enabled"}
    assert set(routing_settings_schemas.Routing.model_fields) == {"home_branch_label", "destinations"}
    assert set(routing_settings_schemas.RoutingSettingsResponse.model_fields) == {"routing"}


# =====================================================================================================================
# Who may read and write, and in what order that is decided
# =====================================================================================================================

@pytest.mark.parametrize("user", [OWNER, ADMIN])
def test_an_owner_and_an_admin_can_read_and_replace(api, store, user):
    assert _get(api, user=user).json() == {"routing": STORED}
    assert _put(api, WANTED, user).json() == {"routing": WANTED}
    # The acting user, from the session, is who every read and write is made for.
    assert {call[-1] for call in store.calls} == {user}


@pytest.mark.parametrize("user", [MANAGER, VIEWER])
def test_a_member_who_is_not_an_owner_or_admin_can_neither_read_nor_write(api, store, user):
    _refused(_get(api, user=user), 403, FORBIDDEN)
    _refused(_put(api, WANTED, user), 403, FORBIDDEN)
    assert store.calls == []


def test_without_a_session_both_routes_are_401_and_nothing_is_read_or_written(api, store):
    _refused(_get(api, user=None), 401, NOT_AUTHENTICATED)
    _refused(_put(api, WANTED, None), 401, NOT_AUTHENTICATED)
    _refused(api.get(ACME, headers={"Cookie": f"{COOKIE}=not-a-session"}), 401, NOT_AUTHENTICATED)
    assert store.calls == []


@pytest.mark.parametrize(("user", "path"), [
    (STRANGER, ACME),                                             # an owner, of another organization
    (OWNER, BETA),                                                # not a member there
    (OWNER, "/api/organizations/nowhere/settings/routing"),       # no such organization
    (900199, ACME),                                               # a session whose user belongs to nothing
], ids=["another-orgs-owner", "not-a-member", "unknown", "no-membership"])
def test_an_organization_the_user_cannot_see_is_the_same_404_for_a_read_and_a_write(api, store, user, path):
    _refused(_get(api, path, user), 404, ORGANIZATION_NOT_FOUND)
    _refused(_put(api, WANTED, user, path), 404, ORGANIZATION_NOT_FOUND)
    assert store.calls == []


def test_a_removed_member_and_a_cancelled_organization_are_not_found(api, store, world):
    del world["roles"][ADMIN]                       # removed: no membership is listed for them any more
    _refused(_get(api, user=ADMIN), 404, ORGANIZATION_NOT_FOUND)
    _refused(_put(api, WANTED, ADMIN), 404, ORGANIZATION_NOT_FOUND)

    world["modes"]["acme"] = "blocked"              # cancelled
    _refused(_get(api, user=OWNER), 404, ORGANIZATION_NOT_FOUND)
    _refused(_put(api, WANTED, OWNER), 404, ORGANIZATION_NOT_FOUND)
    assert store.calls == []


def test_a_suspended_organizations_routing_can_be_read_and_not_changed(api, store, world):
    world["modes"]["acme"] = "read_only"
    before = copy.deepcopy(store.documents["acme"])

    assert _get(api).json() == {"routing": STORED}
    for user in (OWNER, ADMIN):
        _refused(_put(api, WANTED, user), 403, READ_ONLY)
    assert store.documents["acme"] == before and store.writes() == []


def test_the_scope_that_stops_resolving_after_the_checks_is_the_same_404(api, store):
    # The route's checks pass; the service -- which decides for itself, in its own statement -- finds nothing.
    store.gone = True

    _refused(_get(api), 404, ORGANIZATION_NOT_FOUND)
    _refused(_put(api, WANTED), 404, ORGANIZATION_NOT_FOUND)


@pytest.mark.parametrize("origin", [None, "https://evil.example.invalid", "null", "https://app.example.invalid/", ""])
def test_a_put_needs_an_allowed_origin_and_it_is_checked_before_anything_else(api, store, origin):
    for user in (OWNER, VIEWER, STRANGER, None):   # before the session, the membership and the role
        _refused(_put(api, WANTED, user, origin=origin), 403, ORIGIN_NOT_ALLOWED)
    # ... and before the body is looked at.
    response = api.put(ACME, json={"nonsense": True}, headers=_as(OWNER, origin=origin))
    _refused(response, 403, ORIGIN_NOT_ALLOWED)

    assert store.calls == []
    assert _get(api).status_code == 200   # a read needs none
    assert api.get(ACME, headers=_as(OWNER, origin="https://evil.example.invalid")).status_code == 200


def test_the_checks_run_in_order_so_someone_who_may_not_see_the_settings_learns_nothing_of_the_body(api, store, world):
    bad = {"home_branch_label": "Main", "destinations": [{"label": "", "enabled": True}]}

    _refused(_put(api, bad, None), 401, NOT_AUTHENTICATED)
    _refused(_put(api, bad, STRANGER), 404, ORGANIZATION_NOT_FOUND)
    _refused(_put(api, bad, VIEWER), 403, FORBIDDEN)
    world["modes"]["acme"] = "read_only"
    _refused(_put(api, bad, OWNER), 403, READ_ONLY)
    world["modes"]["acme"] = "full"
    assert _problems(_put(api, bad, OWNER)) == [("destinations.0.label", "required")]


# =====================================================================================================================
# What a body may hold
# =====================================================================================================================

@pytest.mark.parametrize(("routing", "problems"), [
    ({"home_branch_label": "Main", "destinations": [{"label": "   ", "enabled": True}]}, [("destinations.0.label", "required")]),
    ({"home_branch_label": "Main", "destinations": [{"label": "Westside", "enabled": True}, {"label": "WESTSIDE ", "enabled": False}]},
     [("destinations.1.label", "duplicate")]),
    ({"home_branch_label": "Main", "destinations": [{"label": "Library Express", "enabled": True}, {"label": "library_express", "enabled": True}]},
     [("destinations.1.label", "duplicate")]),
    ({"home_branch_label": "Central", "destinations": [{"label": "central", "enabled": True}]}, [("destinations.0.label", "means_home")]),
    ({"home_branch_label": "Central", "destinations": [{"label": "Main", "enabled": True}]}, [("destinations.0.label", "means_home")]),
    ({"home_branch_label": "Main", "destinations": [{"label": f"Branch {n}", "enabled": True} for n in range(21)]}, [("destinations", "too_many")]),
    ({"home_branch_label": "Main", "destinations": [{"label": "", "enabled": True}, {"label": "Main", "enabled": True}, {"label": "A", "enabled": True},
                                                    {"label": "a", "enabled": True}]},
     [("destinations.0.label", "required"), ("destinations.1.label", "means_home"), ("destinations.3.label", "duplicate")]),
], ids=["blank-label", "duplicate", "duplicate-by-slug", "means-own-home", "means-contract-home", "twenty-one", "several"])
def test_a_block_the_settings_model_refuses_is_a_422_naming_each_field_and_nothing_is_written(api, store, routing, problems):
    before = copy.deepcopy(store.documents["acme"])

    response = _put(api, routing)

    assert _problems(response) == problems
    assert store.documents["acme"] == before and store.calls == []


def test_a_refusal_never_repeats_what_was_sent(api, store):
    routing = {"home_branch_label": "CANARY-home", "destinations": [{"label": "CANARY-one", "enabled": True}, {"label": "canary one", "enabled": True}]}

    response = _put(api, routing)

    assert _problems(response) == [("destinations.1.label", "duplicate")]
    assert "canary" not in response.text.lower()


@pytest.mark.parametrize("body", [
    {"routing": {"home_branch_label": "Main", "destinations": [{"label": "Westside", "enabled": True, "key": "westside"}]}},   # a supplied key
    {"routing": {"home_branch_label": "Main", "destinations": [{"label": "Westside", "enabled": True, "id": 4}]}},
    {"routing": {"home_branch_label": "Main", "destinations": [], "internal_routing": {}}},
    {"routing": {"home_branch_label": "Main", "destinations": []}, "efficiency": {"labor_rate": "1.00"}},
    {"routing": {"home_branch_label": "Main", "destinations": []}, "user_id": OWNER},
    {"home_branch_label": "Main", "destinations": []},                                       # not wrapped
    {"routing": {"home_branch_label": "Main"}},                                              # no destinations
    {"routing": {"destinations": []}},                                                       # no home label
    {"routing": {"home_branch_label": "Main", "destinations": {"0": {"label": "Westside", "enabled": True}}}},
    {"routing": {"home_branch_label": "Main", "destinations": ["Westside"]}},
    {"routing": {"home_branch_label": "Main", "destinations": [{"label": "Westside"}]}},      # no `enabled`
    {"routing": {"home_branch_label": "Main", "destinations": [{"label": "Westside", "enabled": "true"}]}},
    {"routing": {"home_branch_label": "Main", "destinations": [{"label": "Westside", "enabled": 1}]}},
    {"routing": {"home_branch_label": "Main", "destinations": [{"label": 7, "enabled": True}]}},
    {"routing": {"home_branch_label": None, "destinations": []}},
    {"routing": {"home_branch_label": ["Main"], "destinations": []}},
    {"routing": None},
    {"routing": "Main"},
    {},
    [],
], ids=["key", "id", "another-block-inside", "another-block-beside", "user-id", "unwrapped", "no-destinations", "no-home", "object-for-list",
        "text-for-destination", "no-enabled", "text-for-flag", "number-for-flag", "number-for-label", "null-home", "list-home", "null-routing",
        "text-routing", "empty", "list"])
def test_a_body_that_is_not_the_contracts_shape_is_refused_and_nothing_is_written(api, store, body):
    before = copy.deepcopy(store.documents["acme"])

    response = api.put(ACME, json=body, headers=_as(OWNER))

    assert response.status_code == 422, response.text
    assert store.documents["acme"] == before and store.calls == []
    assert "CANARY" not in response.text


def test_a_body_that_is_not_json_is_refused(api, store):
    response = api.put(ACME, content=b"home_branch_label=Main", headers={**_as(OWNER), "Content-Type": "application/json"})

    assert response.status_code == 422 and store.calls == []


# =====================================================================================================================
# Failures and the log
# =====================================================================================================================

@pytest.mark.parametrize("failing", ["read_organization_routing", "replace_organization_routing"])
def test_a_database_failure_is_a_generic_500_that_carries_nothing_of_it(api, store, monkeypatch, failing, caplog):
    def fail(*_args, **_kwargs):
        raise RuntimeError("CANARY connection to server at 10.0.0.1 failed for settings_json")

    monkeypatch.setattr(routing_settings_service, failing, fail)

    with caplog.at_level(logging.DEBUG):
        response = _get(api) if failing.startswith("read") else _put(api, WANTED)

    _refused(response, 500, INTERNAL_ERROR)
    assert "CANARY" not in response.text and "CANARY" not in caplog.text and "10.0.0.1" not in caplog.text


def test_a_replacement_is_logged_once_with_who_and_how_many_and_never_a_label(api, store, caplog):
    routing = {"home_branch_label": "CANARY-home", "destinations": [{"label": "CANARY-north", "enabled": True}, {"label": "CANARY-south", "enabled": False}]}

    with caplog.at_level(logging.INFO, logger="sortview.customer_api"):
        assert _put(api, routing, ADMIN).status_code == 200
        _get(api, user=ADMIN)                                                                   # a read logs nothing
        _put(api, {"home_branch_label": "Main", "destinations": [{"label": "", "enabled": True}]}, ADMIN)   # nor a refusal

    messages = [record.getMessage() for record in caplog.records if "Routing" in record.getMessage()]
    assert messages == [f"Routing settings replaced | org=acme user_id={ADMIN} destinations=2"]
    assert "CANARY" not in caplog.text and "person" not in caplog.text


# =====================================================================================================================
# The shape of the module, and of its SQL
# =====================================================================================================================

def test_the_routes_are_exactly_these_two_and_only_the_put_checks_the_origin():
    routes = {(method, route.path): route for route in main.customer_router.routes for method in route.methods
              if route.path.endswith("/settings/routing")}

    assert sorted(routes) == [("GET", "/api/organizations/{org_slug}/settings/routing"), ("PUT", "/api/organizations/{org_slug}/settings/routing")]
    for (method, _path), route in routes.items():
        guarded = routing_settings_routes.require_allowed_origin in [dependency.call for dependency in route.dependant.dependencies]
        assert guarded is (method == "PUT")
        # The organization's slug, and nothing else: no branch, no id, no query.
        assert [param.name for param in route.dependant.path_params] == ["org_slug"]
        assert route.dependant.query_params == []
    for method in ("post", "patch", "delete"):
        assert getattr(TestClient(main.app), method)(ACME, headers=_as(OWNER)).status_code == 405


def test_the_routes_run_no_sql_take_no_branch_and_touch_nothing_but_routing():
    code = inspect.getsource(routing_settings_routes).split('"""', 2)[2]

    for forbidden in ("text(", "get_engine", "branch_slug", "branch_settings", "internal_routing", "efficiency_settings_service", "tenant_scope",
                      "routing_config_service", "auth_audit", "log_auth_event", "destination_key", '"key"'):
        assert forbidden not in code, forbidden
    assert code.count('user_id=user["id"]') == 2


def test_the_service_reads_and_writes_the_organizations_transit_key_alone():
    service = routing_settings_service
    read, write = str(service._ORGANIZATION_SQL), str(service._SET_ORGANIZATION_SQL)
    source = inspect.getsource(service)

    # Only that key leaves the database: never the document it is in.
    assert "os.settings_json -> 'transit' AS routing" in read and "SELECT os.settings_json AS" not in read
    # One key is set, in place, on the document as it is at that moment.
    assert "jsonb_set(organization_settings.settings_json, '{transit}', CAST(:block AS JSONB), true)" in write
    assert "jsonb_build_object('transit', CAST(:block AS JSONB))" in write
    assert "SET settings_json = CAST(" not in write
    # Each statement decides for itself whose row it is: an active owner or admin, of a writable organization.
    for statement in (read, write):
        for condition in ("o.slug = :org_slug", "m.user_id = :user_id", "m.role IN ('owner', 'admin')", "m.removed_at IS NULL"):
            assert condition in statement, condition
    assert "o.status IN ('active', 'trial')" in write and "suspended" not in write
    assert "(o.status = 'suspended' AND :for_write = 0)" in read
    # The organization's own block: no branch's settings, no other key, no statement that deletes or reads the rest.
    code = source.split('"""', 2)[2]
    for forbidden in ("branch_settings", "branches", "DELETE", "internal_routing", "efficiency", "security", "_deep_merge", "tenant"):
        assert forbidden not in code, forbidden
    assert "THE ORGANIZATION'S OWN BLOCK, AND ONLY THAT." in source and "nothing here looks at branch_settings at all" in source
    assert json.loads(json.dumps(serialize_routing_settings(parse_stored_routing_settings(DOCUMENT["transit"]))))["destinations"][0]["key"] == "westside"


# =====================================================================================================================
# R9C: routing settings are part of transit routing, a plan feature
# =====================================================================================================================

NOT_AVAILABLE = {"code": "feature_not_available", "message": "This feature is not available for this organization."}


@pytest.mark.parametrize("transits", [None, feature(False)])
def test_without_transit_routing_the_settings_can_be_neither_read_nor_replaced(api, store, monkeypatch, transits):
    grant(monkeypatch, transits=transits)

    _refused(_get(api), 403, NOT_AVAILABLE)
    _refused(_put(api, WANTED), 403, NOT_AVAILABLE)
    # Nothing was read or written: the gate is before the service.
    assert store.calls == []


def test_with_transit_routing_they_are_read_and_replaced_as_before(api, store, monkeypatch):
    grant(monkeypatch, transits=feature(True))

    assert _get(api).json() == {"routing": STORED}
    assert _put(api, WANTED).json() == {"routing": WANTED}
    assert len(store.writes()) == 1


def test_the_transit_gate_comes_after_who_may_manage_them_and_the_write_rules_still_hold(api, store, monkeypatch, world):
    grant(monkeypatch, transits=None)

    _refused(_get(api, user=None), 401, NOT_AUTHENTICATED)
    _refused(_get(api, BETA), 404, ORGANIZATION_NOT_FOUND)          # not a member of beta
    _refused(_get(api, user=VIEWER), 403, FORBIDDEN)                  # a member who does not manage settings
    _refused(_put(api, WANTED, origin="https://elsewhere.invalid"), 403, ORIGIN_NOT_ALLOWED)
    _refused(_put(api, WANTED, origin=None), 403, ORIGIN_NOT_ALLOWED)
    # A body that could not be stored is not looked at: the feature is refused first.
    _refused(_put(api, {"home_branch_label": "", "destinations": "nope"}), 403, NOT_AVAILABLE)

    world["modes"]["acme"] = "read_only"                               # suspended
    _refused(_put(api, WANTED), 403, READ_ONLY)
    _refused(_get(api), 403, NOT_AVAILABLE)
    assert store.calls == []

    grant(monkeypatch, transits=feature(True))
    assert _get(api).status_code == 200                                # suspended: still readable
    _refused(_put(api, WANTED), 403, READ_ONLY)
    world["modes"]["acme"] = "full"
    assert _put(api, {"home_branch_label": "", "destinations": "nope"}).status_code == 422   # reached validation
