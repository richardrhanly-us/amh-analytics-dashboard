"""Reports R10E: reading and replacing an organization's SortView Value settings -- the routes.

    GET /api/organizations/{org_slug}/settings/sortview-value
    PUT /api/organizations/{org_slug}/settings/sortview-value

These tests drive the real production routes through TestClient(main.app):
who may call them, in what order that is decided, the Origin check, what a
body may hold, what an answer looks like, which labor rate applies, what is
logged, and which date is "today". The real settings model
(services.sortview_value) runs throughout, and so does the service's own
reading of the two stored blocks (services.sortview_value_service
.stored_sortview_value).

The two functions that touch the database are replaced by a store that keeps
the settings DOCUMENT in memory and applies the same rule to it -- set or
remove the `sortview_value` key, touch nothing else. Their SQL is
PostgreSQL's (JSONB) and runs for real, through these same routes, in
tests/test_sortview_value_settings_postgres.py. What can be said about that
SQL without a server is pinned at the end of this file.

The session and the access services are replaced at the FLAT module identity
the routes use, as in tests/test_customer_api_efficiency_settings.py. The
clock the routes read is the shared controlled clock, which only a test moves.
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
from customer_api import sortview_value_routes, sortview_value_schemas
from customer_api.sortview_value_routes import FIELD_NAME_MAX_LENGTH, safe_field_name
from services import (
    access_service,
    efficiency_settings_service,
    entitlement_service,
    permission_service,
    session_service,
    sortview_value_service,
)
from services.efficiency_settings import OrganizationEfficiencySettings
from services.sortview_value import (
    USED_INPUTS,
    SortViewValueError,
    SortViewValueSettings,
    parse_sortview_value_settings,
    serialize_sortview_value_settings,
)
from services.sortview_value_service import StoredSortViewValue, stored_sortview_value

PATH = "/api/organizations/{org}/settings/sortview-value"
ACME, BETA = PATH.format(org="acme"), PATH.format(org="beta")
EFFICIENCY = "/api/organizations/{org}/settings/efficiency"

ORIGIN = "https://app.example.invalid"
COOKIE = "__Host-sortview_api_session"
# One session per person. The number is the user's id: distinctive, so it cannot be in a response by accident.
OWNER, ADMIN, MANAGER, VIEWER, STRANGER = 900201, 900202, 900203, 900204, 900205
ROLES = {OWNER: "owner", ADMIN: "admin", MANAGER: "manager", VIEWER: "viewer"}

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
ORGANIZATION_NOT_FOUND = {"code": "organization_not_found", "message": "Organization not found."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have permission to manage these settings."}
READ_ONLY = {"code": "organization_read_only", "message": "This organization's settings cannot be changed."}
ORIGIN_NOT_ALLOWED = {"code": "origin_not_allowed", "message": "Request origin is not allowed."}
STORED_INVALID = {"code": "sortview_value_settings_invalid", "message": "The stored SortView Value settings could not be read."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

# Where the clock starts unless a test moves it: 1 PM on Tuesday 6 October 2026 in America/Chicago.
CLOCK = ControlledClock(datetime(2026, 10, 6, 18, 0, tzinfo=UTC))

# What else a settings document holds. None of it is SortView Value's, and none of it may change or be returned.
CANARY_HASH = "CANARY-admin-lock-hash-7b3e"
ORG_DOCUMENT = {
    "library_name": "Acme Library",
    "security": {"admin_enabled": True, "admin_password_hash": CANARY_HASH},
    "transit": {"home_branch_label": "Main", "destinations": [{"key": "b1", "label": "Westside", "enabled": True}]},
}

REPORTING = {
    "tasks_per_month": "12.0",
    "baseline_minutes_per_task": "90.0",
    "current_minutes_per_task": "15.0",
    "basis": "estimate",
    "as_of": "2026-09-01",
}
TROUBLESHOOTING = {
    "incidents_per_month": "4.0",
    "staff_per_incident": "2.0",
    "baseline_minutes": "45.0",
    "current_minutes": "20.0",
    "basis": "measured",
    "as_of": "2026-09-15",
}
BLOCK = {"labor_rate": "32.50", "subscription_annual_cost": "4800.00", "reporting": REPORTING, "troubleshooting": TROUBLESHOOTING}

NO_RATE = {"sortview_value": None, "organization_efficiency": None, "effective": None, "source": None}
NO_REPORTING = dict.fromkeys(REPORTING)
NO_TROUBLESHOOTING = dict.fromkeys(TROUBLESHOOTING)
NOTHING = {
    "sortview_value": {
        "labor_rate": NO_RATE,
        "subscription_annual_cost": None,
        "reporting": NO_REPORTING,
        "troubleshooting": NO_TROUBLESHOOTING,
    }
}
OWN_RATE = {"sortview_value": "32.50", "organization_efficiency": None, "effective": "32.50", "source": "sortview_value"}
# What the routes answer when BLOCK is what is stored and the organization has no Efficiency default.
STORED = {"sortview_value": {"labor_rate": OWN_RATE, "subscription_annual_cost": "4800.00", "reporting": REPORTING, "troubleshooting": TROUBLESHOOTING}}


class Store:
    """The organizations' settings documents, in memory, behind the service's
    two functions -- and a record of every call made to them. What a document
    SAYS is read by the service's own stored_sortview_value."""

    def __init__(self, monkeypatch):
        self.organizations: dict[str, dict] = {"acme": copy.deepcopy(ORG_DOCUMENT), "beta": {}}
        self.calls: list[tuple] = []
        self.gone = False
        monkeypatch.setattr(sortview_value_service, "read_sortview_value", self.read_sortview_value)
        monkeypatch.setattr(sortview_value_service, "replace_sortview_value", self.replace_sortview_value)

    def _stored(self, org_slug: str) -> StoredSortViewValue:
        document = self.organizations[org_slug]
        return stored_sortview_value(document.get("sortview_value"), document.get("efficiency"))

    def read_sortview_value(self, org_slug, *, user_id):
        self.calls.append(("read", org_slug, user_id))
        if self.gone or org_slug not in self.organizations:
            return None
        return self._stored(org_slug)

    def replace_sortview_value(self, org_slug, settings, *, user_id):
        self.calls.append(("replace", org_slug, user_id))
        if self.gone or org_slug not in self.organizations:
            return None
        block = serialize_sortview_value_settings(settings)
        if block:
            self.organizations[org_slug]["sortview_value"] = block
        else:
            self.organizations[org_slug].pop("sortview_value", None)
        return self._stored(org_slug)

    def writes(self) -> list[tuple]:
        return [call for call in self.calls if call[0] == "replace"]


@pytest.fixture
def api():
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


@pytest.fixture(autouse=True)
def _controlled_time():
    CLOCK.reset()
    with CLOCK.controlling(sortview_value_routes):
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


def _get(api, path=ACME, user=OWNER):
    return api.get(path, headers=_as(user, origin=None))


def _put(api, body, path=ACME, user=OWNER, **kwargs):
    return api.put(path, json=body, headers=_as(user, **kwargs))


def _problems(response) -> list[tuple[str, str]]:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_sortview_value_settings"
    assert body["message"] == "The SortView Value settings are not valid."
    assert set(body) == {"code", "message", "problems"}
    assert all(set(problem) == {"field", "code"} for problem in body["problems"])
    return [(problem["field"], problem["code"]) for problem in body["problems"]]


# =====================================================================================================================
# Who may read and write
# =====================================================================================================================

@pytest.mark.parametrize("user", [OWNER, ADMIN])
def test_an_owner_and_an_admin_can_read_and_replace(api, store, user):
    assert _put(api, BLOCK, user=user).json() == STORED
    assert _get(api, user=user).json() == STORED
    # The acting user, from the session, is who every read and write is made for.
    assert {call[-1] for call in store.calls} == {user}
    assert [call[0] for call in store.calls] == ["replace", "read"]


@pytest.mark.parametrize("user", [MANAGER, VIEWER])
def test_a_member_who_is_not_an_owner_or_admin_can_neither_read_nor_write(api, store, user):
    store.organizations["acme"]["sortview_value"] = copy.deepcopy(BLOCK)

    read, written = _get(api, user=user), _put(api, {"labor_rate": "99.00"}, user=user)

    assert (read.status_code, read.json()) == (403, FORBIDDEN)
    assert (written.status_code, written.json()) == (403, FORBIDDEN)
    assert read.headers["cache-control"] == "no-store"
    assert "32.50" not in read.text and "4800" not in read.text
    # Nothing was read or written for them.
    assert store.calls == []
    assert store.organizations["acme"]["sortview_value"] == BLOCK


def test_no_session_is_401_before_anything_is_looked_up(api, store, world):
    for response in (
        api.get(ACME),
        api.get(ACME, headers={"Cookie": f"{COOKIE}=not-a-session"}),
        api.put(ACME, json=BLOCK, headers={"Origin": ORIGIN}),
        api.put(ACME, json={"labor_rate": "not a rate"}, headers={"Origin": ORIGIN}),
    ):
        assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert store.calls == []
    assert world["lookups"] == []


def test_another_organizations_owner_gets_the_same_404_as_for_an_organization_that_does_not_exist(api, store):
    for target in (ACME, PATH.format(org="no-such-org")):
        read, written = _get(api, target, STRANGER), _put(api, {"labor_rate": "99.00"}, target, STRANGER)
        assert (read.status_code, read.json()) == (404, ORGANIZATION_NOT_FOUND)
        assert (written.status_code, written.json()) == (404, ORGANIZATION_NOT_FOUND)
    assert store.calls == []
    # And their own organization is theirs.
    assert _get(api, BETA, STRANGER).json() == NOTHING


def test_a_cancelled_or_unknown_access_mode_is_not_found_for_its_own_owner(api, store, world):
    world["modes"]["acme"] = "blocked"

    assert _get(api).json() == ORGANIZATION_NOT_FOUND
    assert _put(api, BLOCK).json() == ORGANIZATION_NOT_FOUND
    assert store.calls == []


def test_a_suspended_organizations_settings_can_be_read_and_not_changed(api, store, world):
    store.organizations["acme"]["sortview_value"] = copy.deepcopy(BLOCK)
    world["modes"]["acme"] = "read_only"

    assert _get(api).json() == STORED
    for body in (BLOCK, {}, {"labor_rate": "not a rate"}):
        written = _put(api, body)
        assert (written.status_code, written.json()) == (403, READ_ONLY)
    assert store.writes() == []
    assert store.organizations["acme"]["sortview_value"] == BLOCK


def test_an_organization_that_stops_resolving_between_the_check_and_the_read_is_the_same_404(api, store):
    store.gone = True

    assert (_get(api).status_code, _get(api).json()) == (404, ORGANIZATION_NOT_FOUND)
    assert (_put(api, BLOCK).status_code, _put(api, BLOCK).json()) == (404, ORGANIZATION_NOT_FOUND)


@pytest.mark.parametrize(
    "user, mode, body",
    [
        (None, "full", BLOCK),
        (None, "full", {"labor_rate": "bad"}),
        (STRANGER, "full", BLOCK),
        (STRANGER, "full", {"labor_rate": "bad"}),
        (VIEWER, "full", BLOCK),
        (VIEWER, "full", {"labor_rate": "bad"}),
        (MANAGER, "read_only", BLOCK),
        (OWNER, "read_only", BLOCK),
        (OWNER, "read_only", {"labor_rate": "bad"}),
        (OWNER, "blocked", BLOCK),
        (ADMIN, "full", {}),
    ],
)
def test_who_is_answered_what_is_decided_exactly_as_for_the_organizations_efficiency_settings(api, store, world, monkeypatch, user, mode, body):
    """The same person asking the same thing of the two routes gets the same status and the same error code: the
    Value routes use the Efficiency routes' own dependencies, and add no rule of their own."""
    world["modes"]["acme"] = mode
    monkeypatch.setattr(efficiency_settings_service, "read_organization_efficiency", lambda org_slug, *, user_id: OrganizationEfficiencySettings())
    monkeypatch.setattr(efficiency_settings_service, "replace_organization_efficiency", lambda org_slug, settings, *, user_id: OrganizationEfficiencySettings())

    def outcome(response):
        return response.status_code, response.json().get("code") if response.status_code != 200 else None

    def generic(code):
        # Each route names its own 422; everything decided before a body is looked at has one name for both.
        return None if code is None else code.replace("invalid_sortview_value_settings", "invalid_settings").replace("invalid_efficiency_settings", "invalid_settings")

    efficiency_body = {} if body == BLOCK or body == {} else body
    for value, efficiency in (
        (api.get(ACME, headers=_as(user, origin=None)), api.get(EFFICIENCY.format(org="acme"), headers=_as(user, origin=None))),
        (api.put(ACME, json=body, headers=_as(user)), api.put(EFFICIENCY.format(org="acme"), json=efficiency_body, headers=_as(user))),
    ):
        status, code = outcome(value)
        assert (status, generic(code)) == (efficiency.status_code, generic(outcome(efficiency)[1]))


def test_a_body_is_looked_at_only_after_it_is_known_who_is_asking(api, store):
    bad = {"labor_rate": "CANARY-not-a-rate", "surprise": 1}

    assert api.put(ACME, json=bad, headers={"Origin": ORIGIN}).status_code == 401
    assert _put(api, bad, user=STRANGER).status_code == 404
    assert _put(api, bad, user=VIEWER).status_code == 403
    # And for someone who may change them, the same body is what is wrong.
    assert _problems(_put(api, bad, user=ADMIN)) == [("labor_rate", "not_a_decimal"), ("surprise", "unknown_field")]
    assert store.calls == []


# =====================================================================================================================
# The Origin check
# =====================================================================================================================

@pytest.mark.parametrize("origin", [None, "https://evil.example.invalid", "null", "https://app.example.invalid.evil.invalid"])
def test_a_put_from_an_origin_that_is_not_allowed_is_refused_before_anything_else(api, store, world, origin):
    for user in (OWNER, VIEWER, None):
        response = _put(api, BLOCK, user=user, origin=origin)
        assert (response.status_code, response.json()) == (403, ORIGIN_NOT_ALLOWED)
    assert store.calls == []
    assert world["lookups"] == []


def test_each_configured_origin_is_accepted_and_a_get_needs_none(api, store, monkeypatch):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", f"{ORIGIN},https://second.example.invalid")

    assert _put(api, BLOCK, origin=ORIGIN).status_code == 200
    assert _put(api, BLOCK, origin="https://second.example.invalid").status_code == 200
    assert _get(api).status_code == 200
    assert api.get(ACME, headers=_as(OWNER, origin="https://evil.example.invalid")).status_code == 200


def test_the_put_and_only_it_carries_the_origin_guard_and_no_other_method_exists(api, store):
    routes = {
        (method, route.path): route
        for route in main.customer_router.routes
        for method in route.methods
        if route.path.endswith("/settings/sortview-value")
    }

    assert sorted(routes) == [("GET", "/api/organizations/{org_slug}/settings/sortview-value"), ("PUT", "/api/organizations/{org_slug}/settings/sortview-value")]
    guarded = {key: any(dependency.call.__name__ == "require_allowed_origin" for dependency in route.dependant.dependencies) for key, route in routes.items()}
    assert guarded == {("GET", "/api/organizations/{org_slug}/settings/sortview-value"): False, ("PUT", "/api/organizations/{org_slug}/settings/sortview-value"): True}
    for method in ("post", "patch", "delete"):
        assert getattr(api, method)(ACME, headers=_as(OWNER)).status_code == 405
    # No other spelling of the address exists.
    for alias in ("/api/organizations/acme/settings/value", "/api/organizations/acme/settings/sortview_value", "/api/organizations/acme/sortview-value"):
        assert api.get(alias, headers=_as(OWNER, origin=None)).status_code == 404


# =====================================================================================================================
# GET
# =====================================================================================================================

def test_an_organization_with_nothing_stored_has_every_field_null_and_no_invented_default(api, store):
    response = _get(api)

    assert response.status_code == 200
    assert response.json() == NOTHING
    assert response.headers["cache-control"] == "no-store"
    assert _get(api, BETA, STRANGER).json() == NOTHING
    assert store.writes() == []


def test_what_is_stored_is_returned_as_canonical_text_in_a_fixed_shape(api, store):
    store.organizations["acme"]["sortview_value"] = {
        "labor_rate": "32.5",
        "subscription_annual_cost": "4800",
        "reporting": {"tasks_per_month": "12", "baseline_minutes_per_task": "90", "current_minutes_per_task": "15", "basis": "estimate", "as_of": "2026-09-01"},
        "troubleshooting": {"incidents_per_month": "4", "staff_per_incident": "2", "baseline_minutes": "45", "current_minutes": "20", "basis": "measured", "as_of": "2026-09-15"},
    }

    body = _get(api).json()

    assert body == STORED
    assert list(body) == ["sortview_value"]
    assert list(body["sortview_value"]) == ["labor_rate", "subscription_annual_cost", "reporting", "troubleshooting"]
    assert list(body["sortview_value"]["labor_rate"]) == ["sortview_value", "organization_efficiency", "effective", "source"]
    assert list(body["sortview_value"]["reporting"]) == list(REPORTING)
    assert list(body["sortview_value"]["troubleshooting"]) == list(TROUBLESHOOTING)


def test_a_partly_filled_block_leaves_the_rest_null_and_a_zero_is_a_zero(api, store):
    store.organizations["acme"]["sortview_value"] = {"subscription_annual_cost": "0", "reporting": {"tasks_per_month": "0.0", "basis": "measured"}}

    body = _get(api).json()["sortview_value"]

    assert body["subscription_annual_cost"] == "0.00"
    assert body["reporting"] == {**NO_REPORTING, "tasks_per_month": "0.0", "basis": "measured"}
    assert body["troubleshooting"] == NO_TROUBLESHOOTING
    assert body["labor_rate"] == NO_RATE


def test_nothing_else_of_the_settings_document_is_ever_returned(api, store):
    store.organizations["acme"]["sortview_value"] = copy.deepcopy(BLOCK)
    store.organizations["acme"]["efficiency"] = {"labor_rate": "17.56", "manual_items_per_hour": "46.5"}

    for response in (_get(api), _put(api, BLOCK)):
        text = response.text
        for kept_out in (CANARY_HASH, "library_name", "Acme Library", "security", "transit", "Westside", "manual_items_per_hour", "46.5"):
            assert kept_out not in text, kept_out


def test_no_figure_is_worked_out_or_returned(api, store):
    """The settings, and which rate applies. Hours, value, return and payback are another route's."""
    store.organizations["acme"]["sortview_value"] = copy.deepcopy(BLOCK)

    for response in (_get(api), _put(api, BLOCK)):
        text = response.text
        # What services.sortview_value would make of this block: 18.33 hours, 595.83 a month, 7,150.00 a year...
        for figure in ("18.33", "595.83", "7150", "2350", "49.0", "8.1", "487.50", "108.33"):
            assert figure not in text, figure
        for name in ("recovered", "monthly_value", "annual_value", "net_annual_value", "roi", "payback", "missing"):
            assert name not in text, name
    assert "calculate_sortview_value" not in inspect.getsource(sortview_value_routes)
    assert "calculate_sortview_value" not in inspect.getsource(sortview_value_service)


# =====================================================================================================================
# The labor rate
# =====================================================================================================================

def test_the_blocks_own_rate_applies_and_says_it_is_its_own(api, store):
    store.organizations["acme"]["sortview_value"] = {"labor_rate": "32.5"}

    assert _get(api).json()["sortview_value"]["labor_rate"] == OWN_RATE


def test_without_its_own_the_organizations_efficiency_default_applies_and_says_so(api, store):
    store.organizations["acme"]["efficiency"] = {"labor_rate": "17.56", "manual_items_per_hour": "45.0"}

    assert _get(api).json()["sortview_value"]["labor_rate"] == {
        "sortview_value": None,
        "organization_efficiency": "17.56",
        "effective": "17.56",
        "source": "organization_efficiency",
    }


def test_with_both_the_blocks_own_wins_and_both_are_shown(api, store):
    store.organizations["acme"]["efficiency"] = {"labor_rate": "17.56"}
    store.organizations["acme"]["sortview_value"] = {"labor_rate": "32.50"}

    assert _get(api).json()["sortview_value"]["labor_rate"] == {
        "sortview_value": "32.50",
        "organization_efficiency": "17.56",
        "effective": "32.50",
        "source": "sortview_value",
    }


def test_with_neither_there_is_no_rate_and_none_is_made_up(api, store):
    store.organizations["acme"]["efficiency"] = {"manual_items_per_hour": "45.0"}
    store.organizations["acme"]["sortview_value"] = {"subscription_annual_cost": "4800.00"}

    assert _get(api).json()["sortview_value"]["labor_rate"] == NO_RATE


def test_clearing_the_blocks_own_rate_goes_back_to_the_efficiency_default(api, store):
    store.organizations["acme"]["efficiency"] = {"labor_rate": "17.56"}

    assert _put(api, {"labor_rate": "32.50"}).json()["sortview_value"]["labor_rate"]["source"] == "sortview_value"
    cleared = _put(api, {"subscription_annual_cost": "4800"}).json()["sortview_value"]["labor_rate"]
    assert cleared == {"sortview_value": None, "organization_efficiency": "17.56", "effective": "17.56", "source": "organization_efficiency"}
    # The Efficiency default itself is not this route's to change.
    assert store.organizations["acme"]["efficiency"] == {"labor_rate": "17.56"}


def test_only_the_organizations_efficiency_default_is_ever_looked_at_never_a_sorters():
    """There is no sorter in the address, the service reads no branch_settings row, and what it reads of Efficiency
    is parsed as the ORGANIZATION's block -- which has no sorter fields to take."""
    service = inspect.getsource(sortview_value_service)
    routes = inspect.getsource(sortview_value_routes)

    for sorter in ("branch_slug", "read_sorter_efficiency", "SorterEfficiency", "parse_sorter_efficiency_settings", "resolve_efficiency_settings"):
        assert sorter not in service.split('"""', 2)[2], sorter
        assert sorter not in routes.split('"""', 2)[2], sorter
    assert "branch_settings" not in " ".join(str(getattr(sortview_value_service, name)) for name in ("_ORGANIZATION_SQL", "_SET_SQL", "_CLEAR_SQL"))
    # A sorter's override written into the organization's Efficiency block by mistake is not a rate: only the
    # organization's own two fields are read from it.
    stored = stored_sortview_value(None, {"labor_rate": "17.56", "one_time_cost": "99", "in_service_date": "2020-01-01", "sorter": {"labor_rate": "99.00"}})
    assert stored.organization_efficiency == OrganizationEfficiencySettings(labor_rate=stored.organization_efficiency.labor_rate)
    assert str(stored.organization_efficiency.labor_rate) == "17.56"


# =====================================================================================================================
# A stored Efficiency block that cannot be read
# =====================================================================================================================

@pytest.mark.parametrize(
    "efficiency",
    [
        {"labor_rate": "CANARY-not-a-rate"},
        {"labor_rate": 17.56},
        {"labor_rate": "17.56", "manual_items_per_hour": "CANARY-bad"},
        "CANARY not an object",
        ["labor_rate", "17.56"],
        17,
    ],
)
def test_a_malformed_stored_efficiency_block_does_not_break_the_value_settings(api, store, caplog, efficiency):
    store.organizations["acme"]["sortview_value"] = {"subscription_annual_cost": "4800.00", "reporting": REPORTING}
    store.organizations["acme"]["efficiency"] = efficiency

    with caplog.at_level(logging.WARNING, logger="sortview.customer_api"):
        response = _get(api)

    assert response.status_code == 200
    body = response.json()["sortview_value"]
    # The settings are all there; there is simply no Efficiency rate to fall back on.
    assert body["subscription_annual_cost"] == "4800.00"
    assert body["reporting"] == REPORTING
    assert body["labor_rate"] == NO_RATE
    assert "CANARY" not in response.text
    # It is noted, by organization, and nothing of the block is repeated.
    assert "Stored efficiency settings are malformed; no fallback labor rate | org=acme" in caplog.text
    assert "CANARY" not in caplog.text and "17.56" not in caplog.text


def test_with_a_malformed_efficiency_block_the_blocks_own_rate_still_applies_and_a_put_still_works(api, store):
    store.organizations["acme"]["efficiency"] = {"labor_rate": "bad"}

    written = _put(api, BLOCK)

    assert written.status_code == 200
    assert written.json() == STORED
    assert _get(api).json() == STORED
    # Not this route's to repair, or to touch.
    assert store.organizations["acme"]["efficiency"] == {"labor_rate": "bad"}


def test_the_service_says_the_efficiency_block_was_unreadable_and_never_raises_for_it():
    readable = stored_sortview_value(BLOCK, {"labor_rate": "17.56"})
    unreadable = stored_sortview_value(BLOCK, {"labor_rate": "bad"})
    absent = stored_sortview_value(BLOCK, None)

    assert (readable.efficiency_unreadable, unreadable.efficiency_unreadable, absent.efficiency_unreadable) == (False, True, False)
    assert unreadable.organization_efficiency == OrganizationEfficiencySettings() == absent.organization_efficiency
    assert unreadable.settings == readable.settings == parse_sortview_value_settings({"sortview_value": BLOCK})
    # While its own block that cannot be read is raised, whatever the Efficiency block is.
    for efficiency in (None, {"labor_rate": "17.56"}, {"labor_rate": "bad"}):
        with pytest.raises(SortViewValueError):
            stored_sortview_value({"labor_rate": "bad"}, efficiency)


# =====================================================================================================================
# PUT
# =====================================================================================================================

def test_a_put_stores_the_block_and_answers_with_what_is_stored(api, store):
    response = _put(api, BLOCK)

    assert response.status_code == 200
    assert response.json() == STORED
    assert response.headers["cache-control"] == "no-store"
    assert store.organizations["acme"]["sortview_value"] == BLOCK
    assert _get(api).json() == STORED


def test_a_put_canonicalizes_what_was_sent(api, store):
    loose = {
        "labor_rate": "32.5",
        "subscription_annual_cost": "4800",
        "reporting": {"tasks_per_month": "12", "baseline_minutes_per_task": "90", "current_minutes_per_task": "15", "basis": "estimate", "as_of": "2026-09-01"},
        "troubleshooting": {"incidents_per_month": "4", "staff_per_incident": "2", "baseline_minutes": "45", "current_minutes": "20", "basis": "measured", "as_of": "2026-09-15"},
    }

    assert _put(api, loose).json() == STORED
    assert store.organizations["acme"]["sortview_value"] == BLOCK


def test_a_put_replaces_the_whole_block_so_a_field_left_out_or_null_is_cleared(api, store):
    _put(api, BLOCK)

    response = _put(api, {"labor_rate": None, "reporting": {"tasks_per_month": "3", "basis": None}})

    assert response.json() == {
        "sortview_value": {
            "labor_rate": NO_RATE,
            "subscription_annual_cost": None,
            "reporting": {**NO_REPORTING, "tasks_per_month": "3.0"},
            "troubleshooting": NO_TROUBLESHOOTING,
        }
    }
    assert store.organizations["acme"]["sortview_value"] == {"reporting": {"tasks_per_month": "3.0"}}


@pytest.mark.parametrize(
    "body",
    [{}, {"labor_rate": None}, {"reporting": None, "troubleshooting": None}, {"reporting": {}, "troubleshooting": {}}, {"reporting": {"tasks_per_month": None, "basis": None}}],
)
def test_a_put_of_nothing_removes_the_block_and_nothing_else(api, store, body):
    _put(api, BLOCK)

    response = _put(api, body)

    assert response.status_code == 200
    assert response.json() == NOTHING
    assert store.organizations["acme"] == ORG_DOCUMENT
    assert "sortview_value" not in store.organizations["acme"]


def test_a_put_touches_no_other_key_and_no_other_organization(api, store):
    store.organizations["acme"]["efficiency"] = {"labor_rate": "17.56", "manual_items_per_hour": "45.0"}
    before = copy.deepcopy(store.organizations)

    _put(api, BLOCK)

    assert store.organizations["acme"] == {**before["acme"], "sortview_value": BLOCK}
    assert store.organizations["beta"] == before["beta"] == {}


def test_an_explicit_zero_is_stored_as_zero_and_cleared_back_to_unknown(api, store):
    zero = _put(api, {"subscription_annual_cost": "0", "reporting": {"current_minutes_per_task": "0"}}).json()["sortview_value"]
    assert zero["subscription_annual_cost"] == "0.00" and zero["reporting"]["current_minutes_per_task"] == "0.0"
    assert store.organizations["acme"]["sortview_value"] == {"subscription_annual_cost": "0.00", "reporting": {"current_minutes_per_task": "0.0"}}

    cleared = _put(api, {"reporting": {"basis": "estimate"}}).json()["sortview_value"]
    assert cleared["subscription_annual_cost"] is None and cleared["reporting"]["current_minutes_per_task"] is None


@pytest.mark.parametrize(
    "body, expected",
    [
        ({"labor_rate": 32.5}, [("labor_rate", "not_a_string")]),
        ({"labor_rate": "0"}, [("labor_rate", "out_of_range")]),
        ({"labor_rate": "-5"}, [("labor_rate", "not_a_decimal")]),
        ({"labor_rate": "32.555"}, [("labor_rate", "too_many_decimal_places")]),
        ({"subscription_annual_cost": "10000000.01"}, [("subscription_annual_cost", "out_of_range")]),
        ({"reporting": "twelve a month"}, [("reporting", "not_an_object")]),
        ({"troubleshooting": ["4", "2"]}, [("troubleshooting", "not_an_object")]),
        ({"reporting": {"tasks_per_month": 12}}, [("reporting.tasks_per_month", "not_a_string")]),
        ({"reporting": {"baseline_minutes_per_task": "10000.1"}}, [("reporting.baseline_minutes_per_task", "out_of_range")]),
        ({"reporting": {"basis": "guess"}}, [("reporting.basis", "not_a_basis")]),
        ({"reporting": {"as_of": "09/01/2026"}}, [("reporting.as_of", "not_a_date")]),
        ({"troubleshooting": {"staff_per_incident": "0"}}, [("troubleshooting.staff_per_incident", "out_of_range")]),
        ({"troubleshooting": {"incidents_per_month": "1e3", "basis": 3}}, [("troubleshooting.incidents_per_month", "not_a_decimal"), ("troubleshooting.basis", "not_a_string")]),
        (
            {"labor_rate": "x", "subscription_annual_cost": "y", "reporting": {"tasks_per_month": "z"}, "troubleshooting": {"current_minutes": "w"}},
            [
                ("labor_rate", "not_a_decimal"),
                ("subscription_annual_cost", "not_a_decimal"),
                ("reporting.tasks_per_month", "not_a_decimal"),
                ("troubleshooting.current_minutes", "not_a_decimal"),
            ],
        ),
    ],
)
def test_a_put_that_cannot_be_stored_is_422_with_every_field_and_why_and_stores_nothing(api, store, body, expected):
    _put(api, BLOCK)

    response = _put(api, body)

    assert _problems(response) == expected
    assert response.headers["cache-control"] == "no-store"
    assert store.organizations["acme"]["sortview_value"] == BLOCK
    assert len(store.writes()) == 1


def test_a_key_that_is_not_a_field_is_refused_by_name_at_the_top_and_inside_a_kind_of_work(api, store):
    body = {**BLOCK, "category_c": {"hours": "3"}, "reporting": {**REPORTING, "notes": "x"}, "troubleshooting": {**TROUBLESHOOTING, "tasks_per_month": "1"}}

    assert _problems(_put(api, body)) == [
        ("reporting.notes", "unknown_field"),
        ("troubleshooting.tasks_per_month", "unknown_field"),
        ("category_c", "unknown_field"),
    ]
    assert store.calls == []


@pytest.mark.parametrize("body", [[], [BLOCK], "32.50", 7, None, True])
def test_a_body_that_is_not_a_json_object_is_the_applications_ordinary_422_and_stores_nothing(api, store, body):
    response = _put(api, body)

    assert response.status_code == 422
    assert response.json().get("code") != "invalid_sortview_value_settings"
    assert store.calls == []


# =====================================================================================================================
# A field's name in an answer or a log
# =====================================================================================================================

def test_a_long_unknown_key_is_named_by_its_first_sixty_four_characters_and_no_more(api, store, caplog):
    long_key = "k" * 5000
    nested_key = "n" * 5000

    with caplog.at_level(logging.DEBUG):
        response = _put(api, {long_key: "1", "reporting": {nested_key: "1"}})

    assert FIELD_NAME_MAX_LENGTH == 64
    assert _problems(response) == [
        # The kind of work a nested key is under is still in front: it is what the name starts with.
        (("reporting." + nested_key)[:64], "unknown_field"),
        (long_key[:64], "unknown_field"),
    ]
    assert all(len(problem["field"]) <= 64 for problem in response.json()["problems"])
    # However long the key, the answer is short, and none of it is in a log line at all.
    assert len(response.text) < 500
    assert long_key[:65] not in response.text and nested_key[:60] not in caplog.text
    assert "k" * 65 not in caplog.text
    assert store.calls == []


def test_many_long_unknown_keys_do_not_make_a_long_answer_each(api, store):
    body = {f"{index:03d}{'x' * 2000}": "1" for index in range(20)}

    response = _put(api, body)

    assert [code for _, code in _problems(response)] == ["unknown_field"] * 20
    assert all(len(problem["field"]) == 64 for problem in response.json()["problems"])
    assert len(response.text) < 20 * 120


@pytest.mark.parametrize(
    "key, shown",
    [
        ("line\nbreak", "line?break"),
        ("carriage\rreturn\x00nul", "carriage?return?nul"),
        ('quote"and<tag>', "quote?and?tag?"),
        ("tab\tand space", "tab?and?space"),
        ("unicode-é-ключ", "unicode-?-????"),
        ("| org=other user_id=1", "??org?other?user_id?1"),
        ("plain_name-1.two", "plain_name-1.two"),
    ],
)
def test_an_unknown_keys_name_is_made_plain_before_it_is_returned(api, store, key, shown):
    assert _problems(_put(api, {key: "1"})) == [(shown, "unknown_field")]
    assert _problems(_put(api, {"reporting": {key: "1"}})) == [(f"reporting.{shown}", "unknown_field")]


def test_a_real_fields_name_is_never_changed_by_being_made_safe():
    assert all(safe_field_name(name) == name for name in USED_INPUTS)
    for name in ("reporting", "troubleshooting", "sortview_value", "reporting.basis", "troubleshooting.as_of"):
        assert safe_field_name(name) == name
    assert max(len(name) for name in USED_INPUTS) == 35 < FIELD_NAME_MAX_LENGTH
    assert safe_field_name("") == ""
    assert safe_field_name("a" * 64) == "a" * 64 and safe_field_name("a" * 65) == "a" * 64
    # Cut first, then made plain: the result is never longer than the limit.
    assert len(safe_field_name("é" * 1000)) == 64 and set(safe_field_name("é" * 1000)) == {"?"}


def test_a_refused_value_is_never_repeated_in_the_answer_or_the_log(api, store, caplog):
    body = {
        "labor_rate": "CANARY-rate-9981",
        "subscription_annual_cost": 99817766,
        "secret_key_name": "CANARY-value-of-unknown-key",
        "reporting": {"tasks_per_month": "CANARY-tasks", "basis": "CANARY-basis", "as_of": "CANARY-date", "extra": {"deep": "CANARY-deep"}},
        "troubleshooting": "CANARY-not-an-object",
    }

    with caplog.at_level(logging.DEBUG):
        response = _put(api, body)

    assert response.status_code == 422
    assert "CANARY" not in response.text and "99817766" not in response.text
    assert "CANARY" not in caplog.text and "99817766" not in caplog.text
    # The names of what was wrong are all that comes back.
    assert [field for field, _ in _problems(response)] == [
        "labor_rate", "subscription_annual_cost", "reporting.tasks_per_month", "reporting.basis", "reporting.as_of",
        "reporting.extra", "troubleshooting", "secret_key_name",
    ]


# =====================================================================================================================
# Today
# =====================================================================================================================

def test_a_figure_can_be_as_of_today_and_not_tomorrow(api, store):
    assert _put(api, {"reporting": {"as_of": "2026-10-06"}}).status_code == 200
    assert _problems(_put(api, {"reporting": {"as_of": "2026-10-07"}})) == [("reporting.as_of", "in_the_future")]
    assert _problems(_put(api, {"troubleshooting": {"as_of": "2026-10-07"}})) == [("troubleshooting.as_of", "in_the_future")]
    assert store.organizations["acme"]["sortview_value"] == {"reporting": {"as_of": "2026-10-06"}}


def test_today_is_the_products_date_not_utcs_and_not_the_servers(api, store, _controlled_time):
    # 11:30 PM on 6 October in Chicago. In UTC it is already the 7th.
    _controlled_time.set(datetime(2026, 10, 7, 4, 30, tzinfo=UTC))

    assert _problems(_put(api, {"reporting": {"as_of": "2026-10-07"}})) == [("reporting.as_of", "in_the_future")]
    assert _put(api, {"reporting": {"as_of": "2026-10-06"}}).status_code == 200

    # Half an hour later it is the 7th in Chicago too.
    _controlled_time.set(datetime(2026, 10, 7, 5, 0, tzinfo=UTC))
    assert _put(api, {"reporting": {"as_of": "2026-10-07"}}).status_code == 200


def test_today_follows_the_configured_product_zone(api, store, monkeypatch):
    # 6 October, 18:00 UTC: already the 7th in Auckland.
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Pacific/Auckland")

    assert _put(api, {"troubleshooting": {"as_of": "2026-10-07"}}).status_code == 200
    assert _problems(_put(api, {"troubleshooting": {"as_of": "2026-10-08"}})) == [("troubleshooting.as_of", "in_the_future")]


def test_a_product_zone_that_is_set_but_invalid_is_a_server_error_and_stores_nothing(api, store, monkeypatch):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Central Time")

    response = _put(api, BLOCK)

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    assert store.calls == []


def test_a_stored_as_of_date_is_read_whatever_today_is(api, store, _controlled_time):
    store.organizations["acme"]["sortview_value"] = {"reporting": {"as_of": "2026-10-06"}}
    _controlled_time.set(datetime(2020, 1, 1, 12, 0, tzinfo=UTC))

    assert _get(api).json()["sortview_value"]["reporting"]["as_of"] == "2026-10-06"


def test_the_date_comes_from_the_product_zone_setting_and_one_clock_read():
    source = inspect.getsource(sortview_value_routes)

    assert source.count("datetime.now(UTC).astimezone(settings.product_timezone()).date()") == 1
    for other_clock in ("date.today", "datetime.today", "utcnow", "time.time", "pipeline", "tzlocal"):
        assert other_clock not in source


# =====================================================================================================================
# What is stored but malformed
# =====================================================================================================================

def test_a_malformed_stored_block_is_a_contained_error_that_names_nothing(api, store, caplog):
    store.organizations["acme"]["sortview_value"] = {"labor_rate": "CANARY-not-a-rate", "reporting": {"tasks_per_month": 12, "basis": "CANARY-basis"}}

    with caplog.at_level(logging.ERROR):
        response = _get(api)

    assert (response.status_code, response.json()) == (500, STORED_INVALID)
    assert response.headers["cache-control"] == "no-store"
    assert "CANARY" not in response.text and "Traceback" not in response.text
    assert "labor_rate:not_a_decimal" in caplog.text
    assert "reporting.tasks_per_month:not_a_string" in caplog.text
    assert "reporting.basis:not_a_basis" in caplog.text
    assert "CANARY" not in caplog.text


@pytest.mark.parametrize("stored", ["not an object", ["labor_rate"], 7, {"reporting": "not an object"}, {"troubleshooting": {"as_of": "soon"}}])
def test_a_malformed_stored_block_is_repaired_by_replacing_it(api, store, stored):
    store.organizations["acme"]["sortview_value"] = stored
    assert _get(api).json() == STORED_INVALID

    assert _put(api, BLOCK).json() == STORED
    assert _get(api).json() == STORED


def test_a_malformed_stored_block_can_be_cleared(api, store):
    store.organizations["acme"]["sortview_value"] = {"labor_rate": "bad"}

    assert _put(api, {}).json() == NOTHING
    assert store.organizations["acme"] == ORG_DOCUMENT


def test_a_malformed_value_block_does_not_touch_the_efficiency_settings_route(api, store, monkeypatch):
    store.organizations["acme"]["sortview_value"] = {"labor_rate": "bad"}
    monkeypatch.setattr(efficiency_settings_service, "read_organization_efficiency", lambda org_slug, *, user_id: OrganizationEfficiencySettings())

    assert api.get(EFFICIENCY.format(org="acme"), headers=_as(OWNER, origin=None)).status_code == 200


def test_a_database_failure_is_the_generic_server_error_and_says_nothing(api, store, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("CANARY connection to 10.0.0.9 failed: password=hunter2")

    monkeypatch.setattr(sortview_value_service, "read_sortview_value", broken)
    monkeypatch.setattr(sortview_value_service, "replace_sortview_value", broken)

    for response in (_get(api), _put(api, BLOCK)):
        assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
        assert "CANARY" not in response.text


# =====================================================================================================================
# What is logged
# =====================================================================================================================

def test_a_successful_put_logs_who_changed_which_fields_of_what_and_no_value(api, store, caplog):
    with caplog.at_level(logging.INFO, logger="sortview.customer_api"):
        _put(api, BLOCK, user=ADMIN)
        _put(api, {"subscription_annual_cost": "0", "troubleshooting": {"basis": "measured"}}, user=OWNER)
        _put(api, {}, user=OWNER)

    lines = [record.getMessage() for record in caplog.records if "SortView Value settings replaced" in record.getMessage()]
    assert lines == [
        (
            f"SortView Value settings replaced | org=acme user_id={ADMIN} fields_set=['labor_rate', 'subscription_annual_cost', "
            "'reporting.tasks_per_month', 'reporting.baseline_minutes_per_task', 'reporting.current_minutes_per_task', 'reporting.basis', "
            "'reporting.as_of', 'troubleshooting.incidents_per_month', 'troubleshooting.staff_per_incident', "
            "'troubleshooting.baseline_minutes', 'troubleshooting.current_minutes', 'troubleshooting.basis', 'troubleshooting.as_of']"
        ),
        f"SortView Value settings replaced | org=acme user_id={OWNER} fields_set=['subscription_annual_cost', 'troubleshooting.basis']",
        f"SortView Value settings replaced | org=acme user_id={OWNER} fields_set=[]",
    ]
    for value in ("32.50", "4800", "12.0", "90.0", "2026-09-01", "estimate", "example.invalid", COOKIE):
        assert value not in caplog.text, value


def test_nothing_is_logged_as_replaced_when_nothing_was(api, store, caplog):
    with caplog.at_level(logging.INFO, logger="sortview.customer_api"):
        _put(api, {"labor_rate": "bad"})
        _put(api, BLOCK, user=VIEWER)
        _put(api, BLOCK, origin="https://evil.example.invalid")
        _get(api)

    assert "SortView Value settings replaced" not in caplog.text


# =====================================================================================================================
# The contract
# =====================================================================================================================

def _walk(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _walk(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _walk(inner)
    else:
        yield value


def test_no_answer_carries_an_internal_identifier_or_a_json_number(api, store):
    store.organizations["acme"]["efficiency"] = {"labor_rate": "17.56"}

    for response in (_get(api), _put(api, BLOCK), _put(api, {"labor_rate": "bad"}), _put(api, {})):
        for leaf in _walk(response.json()):
            assert not isinstance(leaf, (int, float)) or isinstance(leaf, bool), leaf
        text = response.text
        for user_id in (OWNER, ADMIN):
            assert str(user_id) not in text
        for identifier in ("organization_id", "user_id", "customer_id", "branch_id", '"id"'):
            assert identifier not in text


def test_the_request_and_response_models_have_exactly_the_settings_fields():
    schemas = sortview_value_schemas

    assert list(schemas.SortViewValueRequest.model_fields) == ["labor_rate", "subscription_annual_cost", "reporting", "troubleshooting"]
    assert schemas.SortViewValueRequest.model_config["extra"] == "allow"
    assert list(schemas.SortViewValueSettingsResponse.model_fields) == ["sortview_value"]
    assert list(schemas.SortViewValue.model_fields) == ["labor_rate", "subscription_annual_cost", "reporting", "troubleshooting"]
    assert list(schemas.ValueLaborRate.model_fields) == ["sortview_value", "organization_efficiency", "effective", "source"]
    assert list(schemas.ReportingSettings.model_fields) == list(REPORTING)
    assert list(schemas.TroubleshootingSettings.model_fields) == list(TROUBLESHOOTING)
    for model in (schemas.SortViewValueSettingsResponse, schemas.SortViewValue, schemas.ValueLaborRate, schemas.ReportingSettings, schemas.TroubleshootingSettings):
        assert model.model_config["extra"] == "forbid"
    # What is in a response is the settings model's own fields: nothing is left out, and nothing is added.
    settings_fields = [f"reporting.{name}" for name in REPORTING if name not in ("basis", "as_of")] + [
        f"troubleshooting.{name}" for name in TROUBLESHOOTING if name not in ("basis", "as_of")
    ]
    assert ["labor_rate", "subscription_annual_cost", *settings_fields] == list(USED_INPUTS)


def test_a_request_is_passed_to_the_settings_model_exactly_as_it_arrived():
    request = sortview_value_schemas.SortViewValueRequest.model_validate({"labor_rate": 32.5, "reporting": ["x"], "surprise": {"a": 1}})

    assert request.block() == {"labor_rate": 32.5, "subscription_annual_cost": None, "reporting": ["x"], "troubleshooting": None, "surprise": {"a": 1}}


def test_the_routes_run_no_sql_decide_no_value_rule_and_read_no_plan_feature():
    source = inspect.getsource(sortview_value_routes)

    for forbidden in ("sqlalchemy", "get_engine", "text(", "Decimal", "feature_enabled", "entitlements", "advanced_reports", "is_platform_admin", "streamlit"):
        assert forbidden not in source, forbidden
    # Who may call them is the Efficiency settings' own two dependencies, used as they are.
    from customer_api import efficiency_settings_routes

    assert sortview_value_routes.Admin is efficiency_settings_routes.Admin
    assert sortview_value_routes.WritingAdmin is efficiency_settings_routes.WritingAdmin


def test_a_stored_key_is_read_as_the_database_decoded_it_and_is_never_decoded_again():
    """PostgreSQL hands a JSONB key back decoded. A key that holds a JSON STRING arrives as a str -- and is a malformed
    block, even when the text inside it would itself be a valid block. (Found on a real server:
    tests/test_sortview_value_settings_postgres.py.)"""
    as_text = json.dumps(BLOCK)

    for efficiency in ("not an object", '{"labor_rate": "17.56"}', "17.56", ""):
        stored = sortview_value_service._stored({"sortview_value_block": BLOCK, "efficiency_block": efficiency})
        assert stored.efficiency_unreadable is True
        assert stored.organization_efficiency == OrganizationEfficiencySettings()
        assert stored.settings == parse_sortview_value_settings({"sortview_value": BLOCK})
    for value in ("not an object", as_text, "32.50", ""):
        with pytest.raises(SortViewValueError) as error:
            sortview_value_service._stored({"sortview_value_block": value, "efficiency_block": None})
        assert [(problem.field, problem.code) for problem in error.value.problems] == [("sortview_value", "not_an_object")]
    # What the database decoded to an object, or found no key for, is read as it is.
    whole = sortview_value_service._stored({"sortview_value_block": BLOCK, "efficiency_block": {"labor_rate": "17.56"}})
    assert whole.settings == parse_sortview_value_settings({"sortview_value": BLOCK}) and str(whole.organization_efficiency.labor_rate) == "17.56"
    assert sortview_value_service._stored({"sortview_value_block": None, "efficiency_block": None}) == StoredSortViewValue(SortViewValueSettings(), OrganizationEfficiencySettings())
    source = inspect.getsource(sortview_value_service)
    assert "json.loads" not in source and "_stored_block" not in source


@pytest.mark.parametrize("stored", ["not an object", json.dumps(BLOCK), "32.50", ""])
def test_a_stored_value_block_that_is_a_json_string_is_the_contained_error_and_never_a_decoding_failure(api, store, caplog, stored):
    store.organizations["acme"]["sortview_value"] = stored

    with caplog.at_level(logging.ERROR):
        response = _get(api)

    assert (response.status_code, response.json()) == (500, STORED_INVALID)
    assert "sortview_value:not_an_object" in caplog.text
    assert "JSONDecodeError" not in caplog.text and "Traceback" not in response.text
    assert _put(api, BLOCK).json() == STORED


@pytest.mark.parametrize("efficiency", ['{"labor_rate": "17.56"}', "17.56", ""])
def test_a_stored_efficiency_block_that_is_a_json_string_is_no_fallback_rate_even_if_its_text_holds_one(api, store, efficiency):
    store.organizations["acme"]["sortview_value"] = {"subscription_annual_cost": "4800.00"}
    store.organizations["acme"]["efficiency"] = efficiency

    response = _get(api)

    assert response.status_code == 200
    assert response.json()["sortview_value"]["labor_rate"] == NO_RATE
    assert "17.56" not in response.text


def test_the_service_is_framework_neutral_and_works_nothing_out():
    source = inspect.getsource(sortview_value_service).split('"""', 2)[2]

    for forbidden in ("fastapi", "starlette", "streamlit", "calculate_sortview_value", "resolve_labor_rate(", "Decimal", "datetime", "logger"):
        assert forbidden not in source, forbidden
    assert sortview_value_service.StoredSortViewValue(SortViewValueSettings(), OrganizationEfficiencySettings()).efficiency_unreadable is False


# =====================================================================================================================
# The SQL, as far as it can be read without a server (it RUNS in tests/test_sortview_value_settings_postgres.py)
# =====================================================================================================================

def _sql(name: str) -> str:
    return " ".join(str(getattr(sortview_value_service, name)).split())


SCOPES = ("_ORGANIZATION_SQL",)
WRITES = ("_SET_SQL", "_CLEAR_SQL")


def test_these_are_all_the_statements_the_service_has():
    statements = {name for name, value in vars(sortview_value_service).items() if name.endswith("_SQL") and hasattr(value, "text")}

    assert statements == set(SCOPES) | set(WRITES)
    assert "text(f" not in inspect.getsource(sortview_value_service)
    assert ".format(" not in inspect.getsource(sortview_value_service)


@pytest.mark.parametrize("name", SCOPES + WRITES)
def test_every_statement_starts_from_the_slug_and_the_users_active_owner_or_admin_membership(name):
    sql = _sql(name)

    assert "o.slug = :org_slug" in sql
    assert "JOIN memberships m ON m.organization_id = o.id" in sql
    assert "m.user_id = :user_id" in sql
    assert "m.role IN ('owner', 'admin')" in sql
    assert "m.removed_at IS NULL" in sql
    assert set(re.findall(r"m\.role IN \(([^)]*)\)", sql)[0].replace("'", "").split(", ")) == permission_service.ADMIN_ROLES
    # No id is bound from outside: the only parameters are the slug, the user, the block and the read/write flag.
    assert set(re.findall(r":(\w+)", sql.replace("::", ""))) <= {"org_slug", "user_id", "block", "for_write"}
    assert not re.search(r":\w*_id\b", sql.replace(":user_id", ""))


def test_the_statements_scope_themselves_exactly_as_the_efficiency_settings_do():
    """The same WHERE, word for word, as the organization statements of services.efficiency_settings_service."""
    def scope(sql: str) -> str:
        return sql[sql.index("WHERE o.slug"):].split(" LIMIT ")[0].split(" ON CONFLICT ")[0].rstrip(" )")

    ours = {name: scope(_sql(name)) for name in SCOPES + WRITES}
    theirs = {
        name: scope(" ".join(str(getattr(efficiency_settings_service, name)).split()))
        for name in ("_ORGANIZATION_SQL", "_SET_ORGANIZATION_SQL", "_CLEAR_ORGANIZATION_SQL")
    }

    assert ours["_ORGANIZATION_SQL"] == theirs["_ORGANIZATION_SQL"]
    assert ours["_SET_SQL"] == theirs["_SET_ORGANIZATION_SQL"]
    assert ours["_CLEAR_SQL"] == theirs["_CLEAR_ORGANIZATION_SQL"]


@pytest.mark.parametrize("name", WRITES)
def test_a_write_needs_full_access_and_a_read_allows_a_suspended_organization(name):
    assert "o.status IN ('active', 'trial')" in _sql(name)
    assert "suspended" not in _sql(name)
    assert "(o.status IN ('active', 'trial') OR (o.status = 'suspended' AND :for_write = 0))" in _sql("_ORGANIZATION_SQL")
    assert "cancelled" not in _sql("_ORGANIZATION_SQL")


def test_a_read_selects_two_keys_of_the_document_and_never_the_document():
    sql = _sql("_ORGANIZATION_SQL")

    assert "os.settings_json -> 'sortview_value' AS sortview_value_block" in sql
    assert "os.settings_json -> 'efficiency' AS efficiency_block" in sql
    assert sql.count("settings_json") == 2
    assert "SELECT os.settings_json ->" in sql and "settings_json AS" not in sql and "SELECT *" not in sql
    for other_table in ("branch_settings", "branches", "collector_installations"):
        assert other_table not in sql


def test_a_write_changes_the_sortview_value_key_of_the_stored_document_and_never_replaces_the_document():
    sql = _sql("_SET_SQL")
    assert "INSERT INTO organization_settings (organization_id, settings_json) SELECT" in sql
    assert "jsonb_build_object('sortview_value', CAST(:block AS JSONB))" in sql
    assert (
        "ON CONFLICT (organization_id) DO UPDATE SET settings_json = "
        "jsonb_set(organization_settings.settings_json, '{sortview_value}', CAST(:block AS JSONB), true)" in sql
    )
    assert "UPDATE organization_settings SET settings_json = settings_json - 'sortview_value'" in _sql("_CLEAR_SQL")
    for name in WRITES:
        # The stored document is only ever an INPUT to its own new value: no whole document is bound in.
        assert "settings_json = CAST(" not in _sql(name)
        assert ":settings_json" not in _sql(name)
        assert "DELETE" not in _sql(name).upper()
        # And no other key of it is named.
        assert "efficiency" not in _sql(name) and "transit" not in _sql(name)
        assert "branch" not in _sql(name)


def test_the_only_thing_bound_into_a_write_is_the_serialized_block():
    source = inspect.getsource(sortview_value_service)

    assert source.count('"block": json.dumps(block)') == 1
    assert source.count("json.dumps(") == 1
    assert "block = serialize_sortview_value_settings(settings)" in source
    # Nothing that reads a document out writes one back.
    assert "_deep_merge_settings" not in source
    assert "get_effective_settings" not in source
    assert "engine.begin()" in source


def test_a_serialized_block_is_what_json_dumps_binds():
    block = serialize_sortview_value_settings(parse_sortview_value_settings({"sortview_value": BLOCK}))

    assert json.loads(json.dumps(block)) == BLOCK
    assert all(type(value) is str for value in (block["labor_rate"], block["subscription_annual_cost"], *block["reporting"].values(), *block["troubleshooting"].values()))
