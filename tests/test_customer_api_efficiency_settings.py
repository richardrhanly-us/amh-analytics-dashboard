"""Reports R6B: reading and replacing Efficiency settings -- the routes.

    GET /api/organizations/{org_slug}/settings/efficiency
    PUT /api/organizations/{org_slug}/settings/efficiency
    GET /api/organizations/{org_slug}/branches/{branch_slug}/settings/efficiency
    PUT /api/organizations/{org_slug}/branches/{branch_slug}/settings/efficiency

These tests drive the real production routes through TestClient(main.app):
who may call them, in what order that is decided, the Origin check, what a
body may hold, what an answer looks like, what is logged, and which date is
"today". The real settings model (services.efficiency_settings) runs
throughout.

The four functions that touch the database (services
.efficiency_settings_service) are replaced by a store that keeps the two
settings DOCUMENTS in memory and applies the same rule to them -- set or
remove the `efficiency` key, touch nothing else. Their SQL is PostgreSQL's
(JSONB) and runs for real, through these same routes, in
tests/test_efficiency_settings_postgres.py. What can be said about that SQL
without a server is pinned at the end of this file.

The session and the access services are replaced at the FLAT module identity
the routes use, as in tests/test_customer_api_organizations.py. The clock the
routes read is the shared controlled clock, which only a test moves.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import re
from datetime import UTC, datetime

import pytest
from controlled_clock import ControlledClock
from fastapi.testclient import TestClient

import main
from customer_api import efficiency_settings_routes, efficiency_settings_schemas
from services import (
    access_service,
    efficiency_settings_service,
    entitlement_service,
    permission_service,
    session_service,
    sorter_inventory_service,
)
from services.efficiency_settings import (
    parse_organization_efficiency_settings,
    parse_sorter_efficiency_settings,
    serialize_organization_efficiency_settings,
    serialize_sorter_efficiency_settings,
)
from services.efficiency_settings_service import SorterEfficiency

ORG = "/api/organizations/{org}/settings/efficiency"
SORTER = "/api/organizations/{org}/branches/{branch}/settings/efficiency"
ACME, ACME_MAIN, ACME_EAST = ORG.format(org="acme"), SORTER.format(org="acme", branch="main"), SORTER.format(org="acme", branch="east")

ORIGIN = "https://app.example.invalid"
COOKIE = "__Host-sortview_api_session"
# One session per person. The number is the user's id: distinctive, so it cannot be in a response by accident.
OWNER, ADMIN, MANAGER, VIEWER, STRANGER = 900101, 900102, 900103, 900104, 900105
ROLES = {OWNER: "owner", ADMIN: "admin", MANAGER: "manager", VIEWER: "viewer"}

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
ORGANIZATION_NOT_FOUND = {"code": "organization_not_found", "message": "Organization not found."}
SORTER_NOT_FOUND = {"code": "sorter_not_found", "message": "Sorter not found."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have permission to manage these settings."}
READ_ONLY = {"code": "organization_read_only", "message": "This organization's settings cannot be changed."}
ORIGIN_NOT_ALLOWED = {"code": "origin_not_allowed", "message": "Request origin is not allowed."}
STORED_INVALID = {"code": "efficiency_settings_invalid", "message": "The stored efficiency settings could not be read."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

# Where the clock starts unless a test moves it: 1 PM on Tuesday 6 October 2026 in America/Chicago.
CLOCK = ControlledClock(datetime(2026, 10, 6, 18, 0, tzinfo=UTC))

# What else a settings document holds. None of it is Efficiency's, and none of it may change or be returned.
CANARY_HASH = "CANARY-admin-lock-hash-5f1d"
ORG_DOCUMENT = {
    "library_name": "Acme Library",
    "security": {"admin_enabled": True, "admin_password_hash": CANARY_HASH},
    "transit": {"home_branch_label": "Main", "destinations": [{"key": "b1", "label": "Westside", "enabled": True}]},
}
BRANCH_DOCUMENT = {"branch_name": "Main", "transit": {"home_branch_label": "Main Branch"}}

ORG_RATES = {"labor_rate": "17.56", "manual_items_per_hour": "45.0"}
SORTER_BLOCK = {
    "labor_rate": "20.00",
    "manual_items_per_hour": "50.0",
    "one_time_cost": "118003.92",
    "recurring_annual_cost": "8400.00",
    "in_service_date": "2020-11-20",
}
NO_RATE = {"organization": None, "sorter": None, "effective": None, "source": None}


class Store:
    """The two kinds of settings document, in memory, behind the service's
    four functions -- and a record of every call made to them."""

    def __init__(self, monkeypatch):
        self.organizations: dict[str, dict] = {"acme": copy.deepcopy(ORG_DOCUMENT), "beta": {}}
        # The sorter sites of each organization. A missing document is a site with no settings row yet.
        self.sorters: dict[tuple[str, str], dict | None] = {
            ("acme", "main"): copy.deepcopy(BRANCH_DOCUMENT),
            ("acme", "east"): None,
            ("beta", "main"): None,
        }
        self.calls: list[tuple] = []
        self.gone = False
        for name in ("read_organization_efficiency", "replace_organization_efficiency", "read_sorter_efficiency", "replace_sorter_efficiency"):
            monkeypatch.setattr(efficiency_settings_service, name, getattr(self, name))

    @staticmethod
    def _put(document: dict, block: dict) -> None:
        if block:
            document["efficiency"] = block
        else:
            document.pop("efficiency", None)

    def read_organization_efficiency(self, org_slug, *, user_id):
        self.calls.append(("read_organization", org_slug, user_id))
        if self.gone or org_slug not in self.organizations:
            return None
        return parse_organization_efficiency_settings(self.organizations[org_slug])

    def replace_organization_efficiency(self, org_slug, settings, *, user_id):
        self.calls.append(("replace_organization", org_slug, user_id))
        if self.gone or org_slug not in self.organizations:
            return None
        self._put(self.organizations[org_slug], serialize_organization_efficiency_settings(settings))
        return parse_organization_efficiency_settings(self.organizations[org_slug])

    def _sorter(self, org_slug, branch_slug):
        return SorterEfficiency(
            organization=parse_organization_efficiency_settings(self.organizations[org_slug]),
            sorter=parse_sorter_efficiency_settings(self.sorters[org_slug, branch_slug]),
        )

    def read_sorter_efficiency(self, org_slug, branch_slug, *, user_id):
        self.calls.append(("read_sorter", org_slug, branch_slug, user_id))
        if self.gone or (org_slug, branch_slug) not in self.sorters:
            return None
        return self._sorter(org_slug, branch_slug)

    def replace_sorter_efficiency(self, org_slug, branch_slug, settings, *, user_id):
        self.calls.append(("replace_sorter", org_slug, branch_slug, user_id))
        if self.gone or (org_slug, branch_slug) not in self.sorters:
            return None
        parse_organization_efficiency_settings(self.organizations[org_slug])
        document = self.sorters[org_slug, branch_slug] or {}
        self._put(document, serialize_sorter_efficiency_settings(settings))
        self.sorters[org_slug, branch_slug] = document
        return self._sorter(org_slug, branch_slug)

    def writes(self) -> list[tuple]:
        return [call for call in self.calls if call[0].startswith("replace")]


@pytest.fixture
def api():
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


@pytest.fixture(autouse=True)
def _controlled_time():
    CLOCK.reset()
    with CLOCK.controlling(efficiency_settings_routes):
        yield CLOCK


@pytest.fixture(autouse=True)
def world(monkeypatch):
    """Acme (active) and Beta (active). OWNER, ADMIN, MANAGER and VIEWER are members of Acme with those roles;
    STRANGER is an owner of Beta only. One allowed browser origin."""
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    state = {"modes": {"acme": "full", "beta": "full"}, "roles": dict(ROLES), "lookups": []}

    def validate_session(raw_token):
        return {"id": int(raw_token), "email": f"user{raw_token}@example.invalid", "full_name": "Test User"} if raw_token.isdigit() else None

    def memberships(user_id):
        state["lookups"].append(("memberships", user_id))
        if user_id == STRANGER:
            return [{"organization_slug": "beta", "organization_name": "Beta Library", "role": "owner"}]
        if user_id in state["roles"]:
            return [{"organization_slug": "acme", "organization_name": "Acme Library", "role": state["roles"][user_id]}]
        return []

    def role(user_id, org_slug):
        state["lookups"].append(("role", user_id, org_slug))
        if org_slug == "beta":
            return "owner" if user_id == STRANGER else None
        return state["roles"].get(user_id)

    monkeypatch.setattr(session_service, "validate_session", validate_session)
    monkeypatch.setattr(access_service, "get_user_memberships", memberships)
    monkeypatch.setattr(access_service, "get_org_access_mode", lambda org_slug: state["modes"].get(org_slug, "blocked"))
    monkeypatch.setattr(entitlement_service, "get_org_role_for_user", role)
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


def _get(api, path, user=OWNER):
    return api.get(path, headers=_as(user, origin=None))


def _put(api, path, body, user=OWNER, **kwargs):
    return api.put(path, json=body, headers=_as(user, **kwargs))


def _problems(response) -> list[tuple[str, str]]:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_efficiency_settings"
    assert set(body) == {"code", "message", "problems"}
    return [(problem["field"], problem["code"]) for problem in body["problems"]]


# =====================================================================================================================
# Who may read and write
# =====================================================================================================================

@pytest.mark.parametrize("user", [OWNER, ADMIN])
def test_an_owner_and_an_admin_can_read_and_replace_both_levels(api, store, user):
    assert _put(api, ACME, ORG_RATES, user).json() == {"efficiency": ORG_RATES}
    assert _get(api, ACME, user).json() == {"efficiency": ORG_RATES}
    assert _put(api, ACME_MAIN, {"one_time_cost": "0.00"}, user).status_code == 200
    assert _get(api, ACME_MAIN, user).json()["efficiency"]["one_time_cost"] == "0.00"
    # The acting user, from the session, is who every read and write is made for.
    assert {call[-1] for call in store.calls} == {user}


@pytest.mark.parametrize("user", [MANAGER, VIEWER])
@pytest.mark.parametrize("path", [ACME, ACME_MAIN, SORTER.format(org="acme", branch="no-such-sorter")])
def test_a_member_who_is_not_an_owner_or_admin_can_neither_read_nor_write(api, store, user, path):
    store.organizations["acme"]["efficiency"] = dict(ORG_RATES)

    read = _get(api, path, user)
    written = _put(api, path, {"labor_rate": "99.00"}, user)

    assert (read.status_code, read.json()) == (403, FORBIDDEN)
    assert (written.status_code, written.json()) == (403, FORBIDDEN)
    assert read.headers["cache-control"] == "no-store"
    assert "17.56" not in read.text
    # Nothing was read or written for them: not even whether the sorter exists.
    assert store.calls == []


@pytest.mark.parametrize("path", [ACME, ACME_MAIN])
def test_no_session_is_401_before_anything_is_looked_up(api, store, world, path):
    for response in (
        api.get(path),
        api.get(path, headers={"Cookie": f"{COOKIE}=not-a-session"}),
        api.put(path, json=ORG_RATES, headers={"Origin": ORIGIN}),
    ):
        assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert store.calls == []
    assert world["lookups"] == []


@pytest.mark.parametrize("path", [ACME, ACME_MAIN])
def test_another_organizations_owner_gets_the_same_404_as_for_an_organization_that_does_not_exist(api, store, path):
    missing = path.replace("acme", "no-such-org")

    for target in (path, missing):
        read, written = _get(api, target, STRANGER), _put(api, target, {"labor_rate": "99.00"}, STRANGER)
        assert (read.status_code, read.json()) == (404, ORGANIZATION_NOT_FOUND)
        assert (written.status_code, written.json()) == (404, ORGANIZATION_NOT_FOUND)
    assert store.calls == []
    # And their own organization is theirs.
    assert _get(api, ORG.format(org="beta"), STRANGER).status_code == 200


def test_a_cancelled_or_unknown_access_mode_is_not_found_for_its_own_owner(api, store, world):
    world["modes"]["acme"] = "blocked"

    assert _get(api, ACME).json() == ORGANIZATION_NOT_FOUND
    assert _put(api, ACME, ORG_RATES).json() == ORGANIZATION_NOT_FOUND
    assert store.calls == []


def test_the_role_is_looked_up_afresh_for_the_sessions_user_on_every_request(api, store, world):
    assert _get(api, ACME, ADMIN).status_code == 200
    world["roles"][ADMIN] = "viewer"

    assert _get(api, ACME, ADMIN).status_code == 403
    assert ("role", ADMIN, "acme") in world["lookups"]


def test_a_role_this_code_does_not_know_is_not_an_admin(api, store, world):
    world["roles"][ADMIN] = "superuser"

    assert _get(api, ACME, ADMIN).status_code == 403
    assert efficiency_settings_routes.ADMIN_ROLES is permission_service.ADMIN_ROLES == {"owner", "admin"}


def test_a_suspended_organizations_settings_can_be_read_and_not_changed(api, store, world):
    store.organizations["acme"]["efficiency"] = dict(ORG_RATES)
    world["modes"]["acme"] = "read_only"

    assert _get(api, ACME).json() == {"efficiency": ORG_RATES}
    assert _get(api, ACME_MAIN).status_code == 200
    for path in (ACME, ACME_MAIN):
        refused = _put(api, path, {"labor_rate": "99.00"})
        assert (refused.status_code, refused.json()) == (403, READ_ONLY)
    assert store.writes() == []
    assert store.organizations["acme"]["efficiency"] == ORG_RATES
    # A member who could not have changed it anyway is told that, not that it is suspended.
    assert _put(api, ACME, {"labor_rate": "99.00"}, VIEWER).json() == FORBIDDEN


def test_the_answer_is_not_found_if_the_scope_stops_resolving_after_the_checks(api, store):
    store.gone = True

    assert _get(api, ACME).json() == ORGANIZATION_NOT_FOUND
    assert _put(api, ACME, ORG_RATES).json() == ORGANIZATION_NOT_FOUND
    assert _get(api, ACME_MAIN).json() == SORTER_NOT_FOUND
    assert _put(api, ACME_MAIN, {}).json() == SORTER_NOT_FOUND


# =====================================================================================================================
# Origin
# =====================================================================================================================

@pytest.mark.parametrize("path", [ACME, ACME_MAIN])
@pytest.mark.parametrize("origin", [None, "https://evil.example.invalid", "null", "https://app.example.invalid/", "http://app.example.invalid", ""])
def test_a_put_without_an_allowed_origin_is_refused_before_anything_else(api, store, world, path, origin):
    response = api.put(path, json=ORG_RATES, headers=_as(OWNER, origin=origin))

    assert (response.status_code, response.json()) == (403, ORIGIN_NOT_ALLOWED)
    assert store.calls == []
    assert world["lookups"] == []
    # Even with no session at all: the origin is checked first, as for login and logout.
    assert api.put(path, json=ORG_RATES, headers=_as(None, origin=origin)).json() == ORIGIN_NOT_ALLOWED


def test_a_put_is_refused_when_no_origin_is_configured_at_all(api, store, monkeypatch):
    monkeypatch.delenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS")

    assert _put(api, ACME, ORG_RATES).json() == ORIGIN_NOT_ALLOWED


def test_each_configured_origin_is_accepted_and_a_get_needs_none(api, store, monkeypatch):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", f"{ORIGIN}, https://staging.example.invalid")

    assert _put(api, ACME, ORG_RATES, origin="https://staging.example.invalid").status_code == 200
    assert _put(api, ACME, ORG_RATES, origin="HTTPS://APP.EXAMPLE.INVALID").status_code == 200
    assert api.get(ACME, headers=_as(OWNER, origin=None)).status_code == 200
    assert api.get(ACME, headers=_as(OWNER, origin="https://evil.example.invalid")).status_code == 200


def test_the_two_puts_and_only_they_carry_the_origin_guard_and_no_other_method_exists(api, store):
    guarded = {
        (method, route.path)
        for route in main.customer_router.routes
        if route.path.endswith("/settings/efficiency")
        for method in route.methods
        if efficiency_settings_routes.require_allowed_origin in [dependency.call for dependency in route.dependant.dependencies]
    }

    assert guarded == {
        ("PUT", "/api/organizations/{org_slug}/settings/efficiency"),
        ("PUT", "/api/organizations/{org_slug}/branches/{branch_slug}/settings/efficiency"),
    }
    for method in ("post", "patch", "delete"):
        assert getattr(api, method)(ACME, headers=_as(OWNER)).status_code == 405


# =====================================================================================================================
# Organization: GET
# =====================================================================================================================

def test_an_organization_with_nothing_stored_has_two_nulls_and_no_invented_default(api, store):
    for document in ({}, copy.deepcopy(ORG_DOCUMENT), {"efficiency": {}}, {"efficiency": None}):
        store.organizations["acme"] = document

        response = _get(api, ACME)

        assert response.status_code == 200
        assert response.json() == {"efficiency": {"labor_rate": None, "manual_items_per_hour": None}}
    assert response.headers["cache-control"] == "no-store"


def test_an_organizations_stored_rates_are_returned_as_canonical_text(api, store):
    store.organizations["acme"]["efficiency"] = {"labor_rate": "17.5", "manual_items_per_hour": "45"}

    response = _get(api, ACME)

    assert response.json() == {"efficiency": {"labor_rate": "17.50", "manual_items_per_hour": "45.0"}}
    assert '"labor_rate":"17.50"' in response.text  # text, not a JSON number


def test_one_stored_rate_leaves_the_other_null(api, store):
    store.organizations["acme"]["efficiency"] = {"manual_items_per_hour": "47.1"}

    assert _get(api, ACME).json() == {"efficiency": {"labor_rate": None, "manual_items_per_hour": "47.1"}}


def test_nothing_else_of_the_settings_document_is_ever_returned(api, store):
    store.organizations["acme"]["efficiency"] = {**ORG_RATES, "one_time_cost": "5.00", "updated_by": 900101}

    for response in (_get(api, ACME), _put(api, ACME, ORG_RATES), _get(api, ACME_MAIN), _put(api, ACME_MAIN, SORTER_BLOCK)):
        assert response.status_code == 200
        assert set(response.json()) == {"efficiency"}
        for forbidden in (CANARY_HASH, "security", "transit", "library_name", "Westside", "branch_name", "updated_by", "9001"):
            assert forbidden not in response.text
    assert set(_get(api, ACME).json()["efficiency"]) == {"labor_rate", "manual_items_per_hour"}


# =====================================================================================================================
# Organization: PUT
# =====================================================================================================================

def test_an_organization_put_stores_the_block_and_answers_with_what_is_stored(api, store):
    response = _put(api, ACME, {"labor_rate": "17.56", "manual_items_per_hour": "45"})

    assert response.status_code == 200
    assert response.json() == {"efficiency": ORG_RATES}
    assert response.headers["cache-control"] == "no-store"
    assert store.organizations["acme"] == {**ORG_DOCUMENT, "efficiency": ORG_RATES}
    assert store.calls == [("replace_organization", "acme", OWNER)]


def test_an_organization_put_replaces_the_whole_block_so_a_field_left_out_or_null_is_cleared(api, store):
    _put(api, ACME, ORG_RATES)

    assert _put(api, ACME, {"labor_rate": "25.00"}).json() == {"efficiency": {"labor_rate": "25.00", "manual_items_per_hour": None}}
    assert store.organizations["acme"]["efficiency"] == {"labor_rate": "25.00"}
    assert _put(api, ACME, {"labor_rate": None, "manual_items_per_hour": "50.0"}).json()["efficiency"] == {
        "labor_rate": None,
        "manual_items_per_hour": "50.0",
    }


@pytest.mark.parametrize("body", [{}, {"labor_rate": None, "manual_items_per_hour": None}])
def test_an_organization_put_of_nothing_removes_the_block_and_nothing_else(api, store, body):
    _put(api, ACME, ORG_RATES)

    response = _put(api, ACME, body)

    assert response.json() == {"efficiency": {"labor_rate": None, "manual_items_per_hour": None}}
    assert store.organizations["acme"] == ORG_DOCUMENT


@pytest.mark.parametrize("field", ["one_time_cost", "recurring_annual_cost", "in_service_date"])
def test_an_organization_put_refuses_a_sorters_field(api, store, field):
    response = _put(api, ACME, {**ORG_RATES, field: SORTER_BLOCK[field]})

    assert _problems(response) == [(field, "unknown_field")]
    assert store.calls == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"labor_rate": "0"}, [("labor_rate", "out_of_range")]),
        ({"labor_rate": "1000.01"}, [("labor_rate", "out_of_range")]),
        ({"labor_rate": "17.567"}, [("labor_rate", "too_many_decimal_places")]),
        ({"labor_rate": "$17.56"}, [("labor_rate", "not_a_decimal")]),
        ({"labor_rate": " 17.56 "}, [("labor_rate", "not_a_decimal")]),
        ({"labor_rate": "1e2"}, [("labor_rate", "not_a_decimal")]),
        ({"labor_rate": "NaN"}, [("labor_rate", "not_a_decimal")]),
        ({"labor_rate": 17.56}, [("labor_rate", "not_a_string")]),
        ({"labor_rate": 17}, [("labor_rate", "not_a_string")]),
        ({"labor_rate": True}, [("labor_rate", "not_a_string")]),
        ({"labor_rate": ["17.56"]}, [("labor_rate", "not_a_string")]),
        ({"manual_items_per_hour": "0.9"}, [("manual_items_per_hour", "out_of_range")]),
        ({"manual_items_per_hour": "45.25"}, [("manual_items_per_hour", "too_many_decimal_places")]),
        ({"labor_rate": "abc", "manual_items_per_hour": 45, "amh_rate": "130", "currency": "USD"},
         [("labor_rate", "not_a_decimal"), ("manual_items_per_hour", "not_a_string"), ("amh_rate", "unknown_field"), ("currency", "unknown_field")]),
        ({"efficiency": ORG_RATES}, [("efficiency", "unknown_field")]),
        ({"organization_id": 1}, [("organization_id", "unknown_field")]),
    ],
)
def test_an_organization_put_that_cannot_be_stored_is_422_with_every_field_and_why(api, store, body, expected):
    response = _put(api, ACME, body)

    assert _problems(response) == expected
    assert response.json()["message"] == "The efficiency settings are not valid."
    assert response.headers["cache-control"] == "no-store"
    assert store.calls == []
    assert store.organizations["acme"] == ORG_DOCUMENT


def test_a_refused_value_is_never_repeated_in_the_answer_or_the_log(api, store, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "CANARY-9917.4455"

    response = _put(api, ACME, {"labor_rate": secret, secret: "1.00"})

    assert _problems(response)[0] == ("labor_rate", "not_a_decimal")
    # A key the model does not know is named -- it is the client's own field name -- but no VALUE is.
    assert response.text.count(secret) == 1
    assert "1.00" not in response.text
    assert secret not in caplog.text


@pytest.mark.parametrize("body", ["null", "[]", '"17.56"', "17.56", "{not json", ""])
def test_a_body_that_is_not_a_json_object_is_the_applications_ordinary_422_and_stores_nothing(api, store, body):
    response = api.put(ACME, content=body, headers={**_as(OWNER), "Content-Type": "application/json"})

    assert response.status_code == 422
    assert "17.56" not in response.text
    assert store.calls == []
    assert store.organizations["acme"] == ORG_DOCUMENT


# =====================================================================================================================
# Sorter: GET
# =====================================================================================================================

def test_a_sorter_with_nothing_of_its_own_inherits_the_organizations_rates_and_nothing_else(api, store):
    store.organizations["acme"]["efficiency"] = dict(ORG_RATES)

    response = _get(api, ACME_EAST)

    assert response.status_code == 200
    assert response.json() == {
        "efficiency": {
            "labor_rate": {"organization": "17.56", "sorter": None, "effective": "17.56", "source": "organization"},
            "manual_items_per_hour": {"organization": "45.0", "sorter": None, "effective": "45.0", "source": "organization"},
            "one_time_cost": None,
            "recurring_annual_cost": None,
            "in_service_date": None,
        }
    }
    assert response.headers["cache-control"] == "no-store"


def test_a_sorters_own_rates_override_and_say_so_and_its_costs_and_date_are_its_own(api, store):
    store.organizations["acme"]["efficiency"] = dict(ORG_RATES)
    store.sorters["acme", "main"]["efficiency"] = dict(SORTER_BLOCK)

    assert _get(api, ACME_MAIN).json() == {
        "efficiency": {
            "labor_rate": {"organization": "17.56", "sorter": "20.00", "effective": "20.00", "source": "sorter"},
            "manual_items_per_hour": {"organization": "45.0", "sorter": "50.0", "effective": "50.0", "source": "sorter"},
            "one_time_cost": "118003.92",
            "recurring_annual_cost": "8400.00",
            "in_service_date": "2020-11-20",
        }
    }


def test_one_override_leaves_the_other_rate_inherited(api, store):
    store.organizations["acme"]["efficiency"] = dict(ORG_RATES)
    store.sorters["acme", "main"]["efficiency"] = {"manual_items_per_hour": "50"}

    efficiency = _get(api, ACME_MAIN).json()["efficiency"]

    assert efficiency["labor_rate"] == {"organization": "17.56", "sorter": None, "effective": "17.56", "source": "organization"}
    assert efficiency["manual_items_per_hour"] == {"organization": "45.0", "sorter": "50.0", "effective": "50.0", "source": "sorter"}


def test_with_no_rate_anywhere_every_part_is_null(api, store):
    efficiency = _get(api, ACME_MAIN).json()["efficiency"]

    assert efficiency == {
        "labor_rate": NO_RATE,
        "manual_items_per_hour": NO_RATE,
        "one_time_cost": None,
        "recurring_annual_cost": None,
        "in_service_date": None,
    }


def test_a_cost_in_the_organizations_document_is_never_a_sorters(api, store):
    store.organizations["acme"]["efficiency"] = {**ORG_RATES, "one_time_cost": "100000.00", "recurring_annual_cost": "9000.00", "in_service_date": "2019-01-01"}

    efficiency = _get(api, ACME_EAST).json()["efficiency"]

    assert (efficiency["one_time_cost"], efficiency["recurring_annual_cost"], efficiency["in_service_date"]) == (None, None, None)
    assert "100000" not in _get(api, ACME_EAST).text


def test_an_explicit_zero_cost_is_zero_and_a_missing_one_is_null(api, store):
    store.sorters["acme", "main"]["efficiency"] = {"one_time_cost": "0", "recurring_annual_cost": "0.00"}

    main_site = _get(api, ACME_MAIN).json()["efficiency"]
    east_site = _get(api, ACME_EAST).json()["efficiency"]

    assert (main_site["one_time_cost"], main_site["recurring_annual_cost"]) == ("0.00", "0.00")
    assert (east_site["one_time_cost"], east_site["recurring_annual_cost"]) == (None, None)


def test_a_branch_that_is_not_one_of_the_organizations_sorter_sites_is_not_found(api, store):
    # "main" is a sorter site of Acme AND of Beta: each owner reaches their own, and only by their own organization.
    for path in (SORTER.format(org="acme", branch="no-such-branch"), SORTER.format(org="acme", branch="westside")):
        assert (_get(api, path).status_code, _get(api, path).json()) == (404, SORTER_NOT_FOUND)
        assert _put(api, path, {}).json() == SORTER_NOT_FOUND
    assert _get(api, SORTER.format(org="beta", branch="main"), STRANGER).status_code == 200
    assert _get(api, SORTER.format(org="beta", branch="main"), OWNER).json() == ORGANIZATION_NOT_FOUND
    assert ("read_sorter", "beta", "main", OWNER) not in store.calls


# =====================================================================================================================
# Sorter: PUT
# =====================================================================================================================

def test_a_sorter_put_stores_the_block_and_answers_with_how_each_rate_resolves(api, store):
    store.organizations["acme"]["efficiency"] = dict(ORG_RATES)

    response = _put(api, ACME_EAST, {"labor_rate": "20", "one_time_cost": "118003.92", "recurring_annual_cost": "8400", "in_service_date": "2020-11-20"})

    assert response.status_code == 200
    assert response.json()["efficiency"] == {
        "labor_rate": {"organization": "17.56", "sorter": "20.00", "effective": "20.00", "source": "sorter"},
        "manual_items_per_hour": {"organization": "45.0", "sorter": None, "effective": "45.0", "source": "organization"},
        "one_time_cost": "118003.92",
        "recurring_annual_cost": "8400.00",
        "in_service_date": "2020-11-20",
    }
    # The site had no settings row: it has one now, holding the block alone.
    assert store.sorters["acme", "east"] == {
        "efficiency": {"labor_rate": "20.00", "one_time_cost": "118003.92", "recurring_annual_cost": "8400.00", "in_service_date": "2020-11-20"}
    }
    assert store.calls == [("replace_sorter", "acme", "east", OWNER)]


def test_a_sorter_put_touches_no_other_key_and_no_other_document(api, store):
    store.organizations["acme"]["efficiency"] = dict(ORG_RATES)
    organization_before = copy.deepcopy(store.organizations["acme"])

    _put(api, ACME_MAIN, SORTER_BLOCK)

    assert store.sorters["acme", "main"] == {**BRANCH_DOCUMENT, "efficiency": SORTER_BLOCK}
    assert store.organizations["acme"] == organization_before
    assert store.sorters["acme", "east"] is None


def test_clearing_a_sorters_override_goes_back_to_the_organizations_rate(api, store):
    store.organizations["acme"]["efficiency"] = dict(ORG_RATES)
    _put(api, ACME_MAIN, SORTER_BLOCK)

    response = _put(api, ACME_MAIN, {**SORTER_BLOCK, "labor_rate": None})

    assert response.json()["efficiency"]["labor_rate"] == {"organization": "17.56", "sorter": None, "effective": "17.56", "source": "organization"}
    assert "labor_rate" not in store.sorters["acme", "main"]["efficiency"]


def test_a_cost_can_be_set_to_zero_and_cleared_back_to_unknown(api, store):
    zero = _put(api, ACME_MAIN, {"one_time_cost": "0.00", "recurring_annual_cost": "0"}).json()["efficiency"]
    assert (zero["one_time_cost"], zero["recurring_annual_cost"]) == ("0.00", "0.00")
    assert store.sorters["acme", "main"]["efficiency"] == {"one_time_cost": "0.00", "recurring_annual_cost": "0.00"}

    cleared = _put(api, ACME_MAIN, {"recurring_annual_cost": "0.00"}).json()["efficiency"]
    assert (cleared["one_time_cost"], cleared["recurring_annual_cost"]) == (None, "0.00")

    assert _put(api, ACME_MAIN, {}).json()["efficiency"]["recurring_annual_cost"] is None
    assert store.sorters["acme", "main"] == BRANCH_DOCUMENT


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"one_time_cost": "-1.00"}, [("one_time_cost", "not_a_decimal")]),
        ({"one_time_cost": "100000000.01"}, [("one_time_cost", "out_of_range")]),
        ({"recurring_annual_cost": 8400}, [("recurring_annual_cost", "not_a_string")]),
        ({"recurring_annual_cost": "8,400.00"}, [("recurring_annual_cost", "not_a_decimal")]),
        ({"in_service_date": "2026-02-30"}, [("in_service_date", "not_a_date")]),
        ({"in_service_date": "2020-11-20T00:00:00Z"}, [("in_service_date", "not_a_date")]),
        ({"in_service_date": 20201120}, [("in_service_date", "not_a_string")]),
        ({"amh_rate": "130", "useful_life_years": 10}, [("amh_rate", "unknown_field"), ("useful_life_years", "unknown_field")]),
        ({"branch_id": 14, "labor_rate": "20.00"}, [("branch_id", "unknown_field")]),
    ],
)
def test_a_sorter_put_that_cannot_be_stored_is_422_and_stores_nothing(api, store, body, expected):
    assert _problems(_put(api, ACME_MAIN, body)) == expected
    assert store.calls == []
    assert store.sorters["acme", "main"] == BRANCH_DOCUMENT


# --- which day is "today" ------------------------------------------------------------------------------------------

def test_a_sorter_can_have_gone_into_service_today_and_not_tomorrow(api, store):
    assert _put(api, ACME_MAIN, {"in_service_date": "2026-10-06"}).status_code == 200
    assert _problems(_put(api, ACME_MAIN, {"in_service_date": "2026-10-07"})) == [("in_service_date", "in_the_future")]
    assert store.sorters["acme", "main"]["efficiency"] == {"in_service_date": "2026-10-06"}


def test_today_is_the_products_date_not_utcs_and_not_the_servers(api, store, _controlled_time):
    # 11:30 PM on 6 October in Chicago. In UTC it is already the 7th.
    _controlled_time.set(datetime(2026, 10, 7, 4, 30, tzinfo=UTC))

    assert _problems(_put(api, ACME_MAIN, {"in_service_date": "2026-10-07"})) == [("in_service_date", "in_the_future")]
    assert _put(api, ACME_MAIN, {"in_service_date": "2026-10-06"}).status_code == 200

    # Half an hour later it is the 7th in Chicago too.
    _controlled_time.set(datetime(2026, 10, 7, 5, 0, tzinfo=UTC))
    assert _put(api, ACME_MAIN, {"in_service_date": "2026-10-07"}).status_code == 200


def test_today_follows_the_configured_product_zone(api, store, monkeypatch, _controlled_time):
    # 6 October, 18:00 UTC: already the 7th in Auckland.
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Pacific/Auckland")

    assert _put(api, ACME_MAIN, {"in_service_date": "2026-10-07"}).status_code == 200
    assert _problems(_put(api, ACME_MAIN, {"in_service_date": "2026-10-08"})) == [("in_service_date", "in_the_future")]


def test_a_product_zone_that_is_set_but_invalid_is_a_server_error_and_stores_nothing(api, store, monkeypatch):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Central Time")

    response = _put(api, ACME_MAIN, {"in_service_date": "2020-11-20"})

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    assert store.calls == []


def test_the_date_comes_from_the_product_zone_setting_and_one_clock_read():
    source = inspect.getsource(efficiency_settings_routes)

    assert source.count("datetime.now(UTC).astimezone(settings.product_timezone()).date()") == 1
    for other_clock in ("date.today", "datetime.today", "utcnow", "time.time", "pipeline", "tzlocal"):
        assert other_clock not in source


# =====================================================================================================================
# What is stored but malformed
# =====================================================================================================================

def test_a_malformed_stored_organization_block_is_a_contained_error_that_names_nothing(api, store, caplog):
    store.organizations["acme"]["efficiency"] = {"labor_rate": "CANARY-not-a-rate", "manual_items_per_hour": 45}

    with caplog.at_level(logging.ERROR):
        for response in (_get(api, ACME), _get(api, ACME_MAIN), _put(api, ACME_MAIN, SORTER_BLOCK)):
            assert (response.status_code, response.json()) == (500, STORED_INVALID)
            assert response.headers["cache-control"] == "no-store"
            assert "CANARY" not in response.text
            assert "Traceback" not in response.text

    assert "labor_rate:not_a_decimal" in caplog.text
    assert "manual_items_per_hour:not_a_string" in caplog.text
    assert "CANARY-not-a-rate" not in caplog.text
    # The sorter was not written to while its organization's block could not be read.
    assert "efficiency" not in store.sorters["acme", "main"]


def test_a_malformed_stored_block_is_repaired_by_replacing_it(api, store):
    store.organizations["acme"]["efficiency"] = "not an object"
    store.sorters["acme", "main"]["efficiency"] = {"one_time_cost": 5}
    assert _get(api, ACME).json() == STORED_INVALID

    assert _put(api, ACME, ORG_RATES).json() == {"efficiency": ORG_RATES}
    assert _get(api, ACME_MAIN).json() == STORED_INVALID
    assert _put(api, ACME_MAIN, {"one_time_cost": "5.00"}).json()["efficiency"]["one_time_cost"] == "5.00"
    assert _get(api, ACME_MAIN).status_code == 200


def test_a_malformed_efficiency_block_does_not_touch_any_other_route(api, store, monkeypatch):
    store.organizations["acme"]["efficiency"] = {"labor_rate": "bad"}
    monkeypatch.setattr(access_service, "get_org_branches", lambda org_slug: [])
    monkeypatch.setattr(sorter_inventory_service, "list_sorter_sites", lambda org_slug: [])
    monkeypatch.setattr(entitlement_service, "build_entitlement_context", lambda user_id, org_slug: {"role": "owner", "subscription": None, "entitlements": {}})

    assert api.get("/api/organizations/acme", headers=_as(OWNER, origin=None)).status_code == 200
    assert api.get("/api/organizations", headers=_as(OWNER, origin=None)).status_code == 200


def test_a_database_failure_is_the_generic_server_error_and_says_nothing(api, store, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("CANARY connection to 10.0.0.9 failed: password=hunter2")

    monkeypatch.setattr(efficiency_settings_service, "read_organization_efficiency", broken)
    monkeypatch.setattr(efficiency_settings_service, "replace_sorter_efficiency", broken)

    for response in (_get(api, ACME), _put(api, ACME_MAIN, SORTER_BLOCK)):
        assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
        assert "CANARY" not in response.text


# =====================================================================================================================
# What is logged
# =====================================================================================================================

def test_a_successful_put_logs_who_changed_which_fields_of_what_and_no_value(api, store, caplog):
    with caplog.at_level(logging.INFO, logger="sortview.customer_api"):
        _put(api, ACME, ORG_RATES, ADMIN)
        _put(api, ACME_MAIN, SORTER_BLOCK, OWNER)
        _put(api, ACME_EAST, {}, OWNER)

    lines = [record.getMessage() for record in caplog.records if "Efficiency settings replaced" in record.getMessage()]
    assert lines == [
        f"Efficiency settings replaced | scope=organization org=acme sorter=- user_id={ADMIN} fields_set=['labor_rate', 'manual_items_per_hour']",
        (
            f"Efficiency settings replaced | scope=sorter org=acme sorter=main user_id={OWNER} "
            "fields_set=['labor_rate', 'manual_items_per_hour', 'one_time_cost', 'recurring_annual_cost', 'in_service_date']"
        ),
        f"Efficiency settings replaced | scope=sorter org=acme sorter=east user_id={OWNER} fields_set=[]",
    ]
    for value in ("17.56", "45.0", "20.00", "118003", "8400", "2020-11-20", "example.invalid", COOKIE):
        assert value not in caplog.text


def test_nothing_is_logged_as_replaced_when_nothing_was(api, store, caplog):
    with caplog.at_level(logging.INFO, logger="sortview.customer_api"):
        _put(api, ACME, {"labor_rate": "bad"})
        _put(api, ACME, ORG_RATES, VIEWER)
        _put(api, ACME, ORG_RATES, origin="https://evil.example.invalid")
        _get(api, ACME)

    assert "Efficiency settings replaced" not in caplog.text


# =====================================================================================================================
# The contract
# =====================================================================================================================

def test_no_answer_carries_an_internal_identifier_or_a_json_number(api, store):
    store.organizations["acme"]["efficiency"] = dict(ORG_RATES)
    store.sorters["acme", "main"]["efficiency"] = dict(SORTER_BLOCK)

    def leaves(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert not re.search(r"(^|_)id$", key), key
                yield from leaves(item)
        else:
            yield value

    for response in (_get(api, ACME), _get(api, ACME_MAIN), _put(api, ACME, ORG_RATES), _put(api, ACME_MAIN, SORTER_BLOCK)):
        for leaf in leaves(response.json()):
            assert leaf is None or type(leaf) is str, leaf


def test_the_request_and_response_models_have_exactly_the_settings_fields():
    schemas = efficiency_settings_schemas

    assert set(schemas.OrganizationEfficiencyRequest.model_fields) == set(schemas.OrganizationEfficiency.model_fields) == {
        "labor_rate",
        "manual_items_per_hour",
    }
    assert set(schemas.SorterEfficiencyRequest.model_fields) == set(schemas.SorterEfficiency.model_fields) == {
        "labor_rate",
        "manual_items_per_hour",
        "one_time_cost",
        "recurring_annual_cost",
        "in_service_date",
    }
    assert set(schemas.EfficiencyRate.model_fields) == {"organization", "sorter", "effective", "source"}
    # No money or rate is a number anywhere in the schema.
    annotations = {str(field.annotation) for model in vars(schemas).values() if hasattr(model, "model_fields") for field in model.model_fields.values()}
    assert not any("float" in annotation or "Decimal" in annotation or "int" in annotation for annotation in annotations)


def test_the_routes_run_no_sql_and_decide_no_value_rule_of_their_own():
    source = inspect.getsource(efficiency_settings_routes)

    for forbidden in ("sqlalchemy", "get_engine", "text(", "Decimal", "advanced_reports", "feature_enabled", "is_platform_admin", "streamlit"):
        assert forbidden not in source.split('"""', 2)[2], forbidden
    assert "validate_organization_efficiency_settings(" in source
    assert "validate_sorter_efficiency_settings(" in source


# =====================================================================================================================
# The SQL, as far as it can be read without a server (it RUNS in tests/test_efficiency_settings_postgres.py)
# =====================================================================================================================

def _sql(name: str) -> str:
    return " ".join(str(getattr(efficiency_settings_service, name)).split())


SCOPES = ("_ORGANIZATION_SQL", "_SORTER_SQL")
WRITES = ("_SET_ORGANIZATION_SQL", "_CLEAR_ORGANIZATION_SQL", "_SET_SORTER_SQL", "_CLEAR_SORTER_SQL")
SORTER_STATEMENTS = ("_SORTER_SQL", "_SET_SORTER_SQL", "_CLEAR_SORTER_SQL")


def test_these_are_all_the_statements_the_service_has():
    statements = {name for name, value in vars(efficiency_settings_service).items() if name.endswith("_SQL") and hasattr(value, "text")}

    assert statements == set(SCOPES) | set(WRITES)
    assert "text(f" not in inspect.getsource(efficiency_settings_service)
    assert ".format(" not in inspect.getsource(efficiency_settings_service)


@pytest.mark.parametrize("name", SCOPES + WRITES)
def test_every_statement_starts_from_the_slug_and_the_users_owner_or_admin_membership(name):
    sql = _sql(name)

    assert "o.slug = :org_slug" in sql
    assert "JOIN memberships m ON m.organization_id = o.id" in sql
    assert "m.user_id = :user_id" in sql
    assert "m.role IN ('owner', 'admin')" in sql
    # No id is bound from outside: the only parameters are the slugs, the user, the block and the read/write flag.
    assert set(re.findall(r":(\w+)", sql.replace("::", ""))) <= {"org_slug", "branch_slug", "user_id", "block", "for_write"}
    assert not re.search(r":\w*_id\b", sql.replace(":user_id", ""))


def test_the_roles_in_the_sql_are_the_admin_roles_and_the_sorter_rule_is_the_inventorys():
    for name in SCOPES + WRITES:
        assert set(re.findall(r"m\.role IN \(([^)]*)\)", _sql(name))[0].replace("'", "").split(", ")) == permission_service.ADMIN_ROLES
    for name in SORTER_STATEMENTS:
        sql = _sql(name)
        assert "JOIN branches b ON b.organization_id = o.id" in sql
        assert "b.slug = :branch_slug" in sql
        assert "b.status = 'active'" in sql
        assert "ci.organization_id = o.id AND ci.branch_id = b.id" in sql
        statuses = re.findall(r"ci\.status IN \(([^)]*)\)", sql)[0].replace("'", "").split(", ")
        assert tuple(statuses) == sorter_inventory_service.VISIBLE_STATUSES
        # A sorter site needs no operational data scope to be configured.
        assert "operational" not in sql


@pytest.mark.parametrize("name", WRITES)
def test_a_write_needs_full_access_and_a_read_allows_a_suspended_organization(name):
    assert "o.status IN ('active', 'trial')" in _sql(name)
    assert "suspended" not in _sql(name)
    for scope in SCOPES:
        assert "(o.status IN ('active', 'trial') OR (o.status = 'suspended' AND :for_write = 0))" in _sql(scope)
        assert "cancelled" not in _sql(scope)


def test_a_write_changes_the_efficiency_key_of_the_stored_document_and_never_replaces_the_document():
    for name, table, key in (("_SET_ORGANIZATION_SQL", "organization_settings", "organization_id"), ("_SET_SORTER_SQL", "branch_settings", "branch_id")):
        sql = _sql(name)
        assert f"INSERT INTO {table} ({key}, settings_json) SELECT" in sql
        assert "jsonb_build_object('efficiency', CAST(:block AS JSONB))" in sql
        assert f"ON CONFLICT ({key}) DO UPDATE SET settings_json = jsonb_set({table}.settings_json, '{{efficiency}}', CAST(:block AS JSONB), true)" in sql
    for name, table in (("_CLEAR_ORGANIZATION_SQL", "organization_settings"), ("_CLEAR_SORTER_SQL", "branch_settings")):
        assert f"UPDATE {table} SET settings_json = settings_json - 'efficiency'" in _sql(name)
    for name in WRITES:
        # The stored document is only ever an INPUT to its own new value: no whole document is bound in.
        assert "settings_json = CAST(" not in _sql(name)
        assert ":settings_json" not in _sql(name)
        assert "DELETE" not in _sql(name).upper()


def test_the_only_thing_bound_into_a_write_is_the_serialized_efficiency_block():
    source = inspect.getsource(efficiency_settings_service)

    assert source.count('"block": json.dumps(block)') == 2
    assert source.count("json.dumps(") == 2
    assert "serialize_organization_efficiency_settings(settings)" in source
    assert "serialize_sorter_efficiency_settings(settings)" in source
    # Nothing that reads a document out writes one back.
    assert "_deep_merge_settings" not in source
    assert "get_effective_settings" not in source


def test_a_serialized_block_is_what_json_dumps_binds():
    block = serialize_sorter_efficiency_settings(parse_sorter_efficiency_settings({"efficiency": SORTER_BLOCK}))

    assert json.loads(json.dumps(block)) == SORTER_BLOCK
    assert all(type(value) is str for value in block.values())
