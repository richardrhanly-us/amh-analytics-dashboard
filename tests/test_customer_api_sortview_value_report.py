"""Reports R10F: an organization's SortView Value report -- the route.

    GET /api/organizations/{org_slug}/reports/sortview-value

These tests drive the real production route through TestClient(main.app): who
may read it, in what order that is decided, what an answer looks like, what
becomes of settings that are not all there or cannot be read, and that
nothing is read but the stored settings. The real settings model and the real
arithmetic (services.sortview_value) run throughout, and so does the
service's own reading of the two stored blocks.

The one function that touches the database (services.sortview_value_service
.read_sortview_value) is replaced by a store that keeps the settings
DOCUMENT in memory. It runs for real, through this same route, in
tests/test_sortview_value_report_postgres.py.

The session and the access services are replaced at the FLAT module identity
the routes use, as in tests/test_customer_api_sortview_value_settings.py.
"""

from __future__ import annotations

import copy
import inspect
import logging
import re

import pytest
from fastapi.testclient import TestClient

import main
from customer_api import (
    sortview_value_report_routes,
    sortview_value_report_schemas,
    sortview_value_routes,
    sortview_value_schemas,
)
from services import (
    access_service,
    entitlement_service,
    session_service,
    sortview_value_service,
)
from services.sortview_value import (
    USED_INPUTS,
    calculate_sortview_value,
    parse_sortview_value_settings,
    resolve_labor_rate,
    serialize_sortview_value_settings,
)
from services.sortview_value_service import stored_sortview_value

REPORT = "/api/organizations/{org}/reports/sortview-value"
SETTINGS = "/api/organizations/{org}/settings/sortview-value"
ACME, BETA = REPORT.format(org="acme"), REPORT.format(org="beta")

ORIGIN = "https://app.example.invalid"
COOKIE = "__Host-sortview_api_session"
# One session per person. The number is the user's id: distinctive, so it cannot be in a response by accident.
OWNER, ADMIN, MANAGER, VIEWER, STRANGER = 900301, 900302, 900303, 900304, 900305
ROLES = {OWNER: "owner", ADMIN: "admin", MANAGER: "manager", VIEWER: "viewer"}

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
ORGANIZATION_NOT_FOUND = {"code": "organization_not_found", "message": "Organization not found."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have permission to manage these settings."}
STORED_INVALID = {"code": "sortview_value_settings_invalid", "message": "The stored SortView Value settings could not be read."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

CANARY_HASH = "CANARY-admin-lock-hash-2c9a"
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
# Reporting: 12 tasks, 90 minutes each before and 15 now -> 18.00 h and 3.00 h a month, 15.00 recovered.
# Troubleshooting: 4 incidents of 2 staff, 45 minutes each before and 20 now -> 6.00 h and 2.666... h, 3.333... recovered.
# At 32.50 an hour: 487.50 and 108.333... a month; 595.8333... together, 7,150.00 a year, against 4,800.00.
BLOCK = {"labor_rate": "32.50", "subscription_annual_cost": "4800.00", "reporting": REPORTING, "troubleshooting": TROUBLESHOOTING}

OWN_RATE = {"sortview_value": "32.50", "organization_efficiency": None, "effective": "32.50", "source": "sortview_value"}
NO_RATE = {"sortview_value": None, "organization_efficiency": None, "effective": None, "source": None}
NO_CATEGORY = {
    "baseline_hours_per_month": None,
    "current_hours_per_month": None,
    "recovered_hours_per_month": None,
    "monthly_value": None,
    "annual_value": None,
}
NO_TOTALS = {
    "recovered_hours_per_month": None,
    "monthly_value": None,
    "annual_value": None,
    "net_annual_value": None,
    "roi_percent": None,
    "payback_months": None,
}
REPORTING_RESULTS = {
    "baseline_hours_per_month": "18.00",
    "current_hours_per_month": "3.00",
    "recovered_hours_per_month": "15.00",
    "monthly_value": "487.50",
    "annual_value": "5850.00",
}
TROUBLESHOOTING_RESULTS = {
    "baseline_hours_per_month": "6.00",
    "current_hours_per_month": "2.67",
    "recovered_hours_per_month": "3.33",
    "monthly_value": "108.33",
    "annual_value": "1300.00",
}
TOTALS = {
    "recovered_hours_per_month": "18.33",
    "monthly_value": "595.83",
    "annual_value": "7150.00",
    "net_annual_value": "2350.00",
    "roi_percent": "49.0",
    "payback_months": "8.1",
}
# What the route answers when BLOCK is what is stored and the organization has no Efficiency default.
WHOLE = {
    "currency": "USD",
    "assumptions": {"labor_rate": OWN_RATE, "subscription_annual_cost": "4800.00", "reporting": REPORTING, "troubleshooting": TROUBLESHOOTING},
    "results": {"reporting": REPORTING_RESULTS, "troubleshooting": TROUBLESHOOTING_RESULTS, **TOTALS},
    "missing": [],
}
NOTHING = {
    "currency": "USD",
    "assumptions": {
        "labor_rate": NO_RATE,
        "subscription_annual_cost": None,
        "reporting": dict.fromkeys(REPORTING),
        "troubleshooting": dict.fromkeys(TROUBLESHOOTING),
    },
    "results": {"reporting": NO_CATEGORY, "troubleshooting": NO_CATEGORY, **NO_TOTALS},
    "missing": list(USED_INPUTS),
}


class Store:
    """The organizations' settings documents, in memory, behind the service's
    functions -- and a record of every call made to them. What a document
    SAYS is read by the service's own stored_sortview_value."""

    def __init__(self, monkeypatch):
        self.organizations: dict[str, dict] = {"acme": copy.deepcopy(ORG_DOCUMENT), "beta": {}}
        self.calls: list[tuple] = []
        self.gone = False
        monkeypatch.setattr(sortview_value_service, "read_sortview_value", self.read_sortview_value)
        monkeypatch.setattr(sortview_value_service, "replace_sortview_value", self.replace_sortview_value)

    def read_sortview_value(self, org_slug, *, user_id):
        self.calls.append(("read", org_slug, user_id))
        if self.gone or org_slug not in self.organizations:
            return None
        document = self.organizations[org_slug]
        return stored_sortview_value(document.get("sortview_value"), document.get("efficiency"))

    def replace_sortview_value(self, org_slug, settings, *, user_id):
        self.calls.append(("replace", org_slug, user_id))
        block = serialize_sortview_value_settings(settings)
        if block:
            self.organizations[org_slug]["sortview_value"] = block
        else:
            self.organizations[org_slug].pop("sortview_value", None)
        return self.read_sortview_value(org_slug, user_id=user_id)


@pytest.fixture
def api():
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


@pytest.fixture(autouse=True)
def world(monkeypatch):
    """Acme (active) and Beta (active). OWNER, ADMIN, MANAGER and VIEWER are members of Acme with those roles;
    STRANGER is an owner of Beta only."""
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", ORIGIN)
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


def _get(api, path=ACME, user=OWNER):
    return api.get(path, headers={"Cookie": f"{COOKIE}={user}"} if user is not None else {})


def _stored(store, block, org="acme"):
    store.organizations[org]["sortview_value"] = copy.deepcopy(block)


def _block(**changes) -> dict:
    """BLOCK with fields changed by dotted name. A value of ... removes the field."""
    block = {**BLOCK, "reporting": dict(REPORTING), "troubleshooting": dict(TROUBLESHOOTING)}
    for name, value in changes.items():
        target, _, field = name.rpartition(".")
        owner = block[target] if target else block
        if value is ...:
            owner.pop(field, None)
        else:
            owner[field] = value
    return block


def _report(api, store, block, efficiency=None) -> dict:
    _stored(store, block)
    if efficiency is not None:
        store.organizations["acme"]["efficiency"] = efficiency
    response = _get(api)
    assert response.status_code == 200, response.text
    return response.json()


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


# =====================================================================================================================
# Who may read it
# =====================================================================================================================

@pytest.mark.parametrize("user", [OWNER, ADMIN])
def test_an_owner_and_an_admin_can_read_the_report(api, store, user):
    _stored(store, BLOCK)

    response = _get(api, user=user)

    assert response.status_code == 200
    assert response.json() == WHOLE
    assert response.headers["cache-control"] == "no-store"
    # The acting user, from the session, is who the one read is made for.
    assert store.calls == [("read", "acme", user)]


@pytest.mark.parametrize("user", [MANAGER, VIEWER])
def test_a_member_who_is_not_an_owner_or_admin_cannot_read_it(api, store, user):
    _stored(store, BLOCK)

    response = _get(api, user=user)

    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert response.headers["cache-control"] == "no-store"
    for figure in ("32.50", "4800", "595.83", "7150", "49.0"):
        assert figure not in response.text
    # Nothing was read for them.
    assert store.calls == []


def test_no_session_is_401_before_anything_is_looked_up(api, store, world):
    _stored(store, BLOCK)

    for response in (api.get(ACME), api.get(ACME, headers={"Cookie": f"{COOKIE}=not-a-session"})):
        assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert store.calls == []
    assert world["lookups"] == []


def test_another_organizations_owner_gets_the_same_404_as_for_an_organization_that_does_not_exist(api, store):
    _stored(store, BLOCK)

    for target in (ACME, REPORT.format(org="no-such-org")):
        response = _get(api, target, STRANGER)
        assert (response.status_code, response.json()) == (404, ORGANIZATION_NOT_FOUND)
        assert "32.50" not in response.text
    assert store.calls == []
    # And their own organization is theirs.
    assert _get(api, BETA, STRANGER).json() == NOTHING


def test_a_suspended_organizations_report_can_still_be_read(api, store, world):
    _stored(store, BLOCK)
    world["modes"]["acme"] = "read_only"

    assert _get(api).json() == WHOLE


def test_a_cancelled_or_unknown_access_mode_is_not_found_for_its_own_owner(api, store, world):
    _stored(store, BLOCK)
    world["modes"]["acme"] = "blocked"

    assert (_get(api).status_code, _get(api).json()) == (404, ORGANIZATION_NOT_FOUND)
    assert store.calls == []


def test_an_organization_that_stops_resolving_between_the_check_and_the_read_is_the_same_404(api, store):
    store.gone = True

    assert (_get(api).status_code, _get(api).json()) == (404, ORGANIZATION_NOT_FOUND)


@pytest.mark.parametrize(
    "user, mode",
    [(None, "full"), (STRANGER, "full"), (VIEWER, "full"), (MANAGER, "full"), (ADMIN, "full"), (OWNER, "read_only"), (VIEWER, "read_only"), (OWNER, "blocked")],
)
def test_who_is_answered_what_is_decided_exactly_as_for_reading_the_value_settings(api, store, world, user, mode):
    """The same person asking for the report and for the settings gets the same status and the same error: the report
    uses the settings' own dependency, and adds no rule of its own."""
    _stored(store, BLOCK)
    world["modes"]["acme"] = mode

    report, settings = _get(api, ACME, user), _get(api, SETTINGS.format(org="acme"), user)

    assert report.status_code == settings.status_code
    if report.status_code != 200:
        assert report.json() == settings.json()


def test_the_report_is_one_get_and_nothing_else_and_has_no_other_address(api, store):
    routes = sorted((method, route.path) for route in main.customer_router.routes for method in route.methods if route.path.endswith("/reports/sortview-value"))

    assert routes == [("GET", "/api/organizations/{org_slug}/reports/sortview-value")]
    for method in ("post", "put", "patch", "delete"):
        assert getattr(api, method)(ACME, headers={"Cookie": f"{COOKIE}={OWNER}", "Origin": ORIGIN}).status_code == 405
    for alias in (
        "/api/organizations/acme/reports/value",
        "/api/organizations/acme/reports/sortview_value",
        "/api/organizations/acme/branches/main/reports/sortview-value",
        "/api/organizations/acme/sorters/main/reports/sortview-value",
    ):
        assert _get(api, alias).status_code == 404


# =====================================================================================================================
# The answer
# =====================================================================================================================

def test_complete_settings_give_every_figure_beside_what_it_was_worked_out_from(api, store):
    body = _report(api, store, BLOCK)

    assert body == WHOLE
    assert list(body) == ["currency", "assumptions", "results", "missing"]
    assert list(body["assumptions"]) == ["labor_rate", "subscription_annual_cost", "reporting", "troubleshooting"]
    assert list(body["results"]) == ["reporting", "troubleshooting", *TOTALS]
    assert list(body["results"]["reporting"]) == list(REPORTING_RESULTS) == list(body["results"]["troubleshooting"])


def test_an_organization_with_nothing_stored_is_a_report_of_nulls_that_names_everything_missing(api, store):
    response = _get(api)

    assert response.status_code == 200
    assert response.json() == NOTHING
    assert response.json()["missing"] == [
        "labor_rate",
        "subscription_annual_cost",
        "reporting.tasks_per_month",
        "reporting.baseline_minutes_per_task",
        "reporting.current_minutes_per_task",
        "troubleshooting.incidents_per_month",
        "troubleshooting.staff_per_incident",
        "troubleshooting.baseline_minutes",
        "troubleshooting.current_minutes",
    ]


@pytest.mark.parametrize("block", [BLOCK, {}, {"labor_rate": "32.50"}, {"reporting": REPORTING}, {"troubleshooting": {"basis": "measured"}}, _block(labor_rate=...)])
def test_every_key_is_always_there_whatever_is_stored(api, store, block):
    body = _report(api, store, block)

    def shape(value):
        return {key: shape(inner) for key, inner in value.items()} if isinstance(value, dict) else None

    assert shape({**body, "missing": None}) == shape({**NOTHING, "missing": None})


def test_every_figure_is_text_or_null_and_never_a_json_number(api, store):
    for block in (BLOCK, {}, _block(**{"reporting.current_minutes_per_task": "120"}), _block(subscription_annual_cost="0")):
        body = _report(api, store, block)
        for leaf in _walk(body):
            assert leaf is None or isinstance(leaf, str), leaf


def test_every_figure_has_its_fixed_number_of_places(api, store):
    # Whole numbers throughout: 60 tasks of 60 minutes saved is 60 hours; at 10.00 an hour, 600.00 a month.
    block = {
        "labor_rate": "10",
        "subscription_annual_cost": "3600",
        "reporting": {"tasks_per_month": "60", "baseline_minutes_per_task": "60", "current_minutes_per_task": "0"},
        "troubleshooting": {"incidents_per_month": "0", "staff_per_incident": "1", "baseline_minutes": "0", "current_minutes": "0"},
    }
    results = _report(api, store, block)["results"]

    assert results["reporting"] == {
        "baseline_hours_per_month": "60.00",
        "current_hours_per_month": "0.00",
        "recovered_hours_per_month": "60.00",
        "monthly_value": "600.00",
        "annual_value": "7200.00",
    }
    assert {name: results[name] for name in TOTALS} == {
        "recovered_hours_per_month": "60.00",
        "monthly_value": "600.00",
        "annual_value": "7200.00",
        "net_annual_value": "3600.00",
        "roi_percent": "100.0",
        "payback_months": "6.0",
    }
    two_places = re.compile(r"-?\d+\.\d{2}")
    one_place = re.compile(r"-?\d+\.\d")
    for name, value in {**results["reporting"], **results["troubleshooting"], **{name: results[name] for name in TOTALS}}.items():
        assert (one_place if name in ("roi_percent", "payback_months") else two_places).fullmatch(value), (name, value)


def test_a_large_figure_is_written_out_in_full_with_no_separator_or_exponent(api, store):
    block = {
        "labor_rate": "10000.00",
        "subscription_annual_cost": "10000000.00",
        "reporting": {"tasks_per_month": "100000", "baseline_minutes_per_task": "10000", "current_minutes_per_task": "0"},
        "troubleshooting": {"incidents_per_month": "100000", "staff_per_incident": "1000", "baseline_minutes": "10000", "current_minutes": "0.1"},
    }
    results = _report(api, store, block)["results"]

    assert results["annual_value"] == "2001980000000000.00"
    assert results["troubleshooting"]["baseline_hours_per_month"] == "16666666666.67"
    for leaf in _walk(results):
        if isinstance(leaf, str) and leaf[:1].isdigit():
            assert "e" not in leaf.lower() and "," not in leaf


def test_the_currency_is_usd_and_there_is_no_other(api, store):
    assert _report(api, store, BLOCK)["currency"] == "USD"
    assert _get(api, BETA, STRANGER).json()["currency"] == "USD"
    assert sortview_value_report_routes.CURRENCY == "USD"


def test_the_answer_is_figures_and_names_with_no_prose_about_them(api, store):
    text = _get(api).text + _report(api, store, BLOCK).__repr__()

    for prose in ("estimate based", "guarantee", "disclos", "saved", "savings", "planning and evaluation", "message"):
        assert prose not in text, prose


# =====================================================================================================================
# The assumptions
# =====================================================================================================================

@pytest.mark.parametrize(
    "block, efficiency",
    [
        (BLOCK, None),
        ({}, None),
        (_block(labor_rate=...), {"labor_rate": "17.56", "manual_items_per_hour": "45.0"}),
        (BLOCK, {"labor_rate": "17.56"}),
        ({"subscription_annual_cost": "0", "reporting": {"tasks_per_month": "0", "basis": "measured"}}, None),
        ({"troubleshooting": {"as_of": "2026-09-15"}}, {"labor_rate": "bad"}),
    ],
)
def test_the_assumptions_are_exactly_what_the_settings_route_answers(api, store, block, efficiency):
    report = _report(api, store, block, efficiency)

    settings = _get(api, SETTINGS.format(org="acme")).json()

    assert report["assumptions"] == settings["sortview_value"]


def test_the_assumptions_are_made_by_the_settings_routes_own_function():
    source = inspect.getsource(sortview_value_report_routes)

    assert "assumptions=sortview_value_view(org_slug, stored)" in source
    assert sortview_value_report_routes.sortview_value_view is sortview_value_routes.sortview_value_view
    assert sortview_value_report_schemas.SortViewValueReportResponse.model_fields["assumptions"].annotation is sortview_value_schemas.SortViewValue
    # Nothing here reads the stored block for itself.
    for own_reading in ("serialize_sortview_value_settings", "parse_sortview_value_settings", "serialize_organization_efficiency_settings"):
        assert own_reading not in source, own_reading


def test_what_the_figures_are_and_when_they_are_as_of_is_kept_with_them_and_changes_nothing(api, store):
    with_metadata = _report(api, store, BLOCK)
    without = _report(api, store, _block(**{"reporting.basis": ..., "reporting.as_of": ..., "troubleshooting.basis": ..., "troubleshooting.as_of": ...}))
    other = _report(api, store, _block(**{"reporting.basis": "measured", "troubleshooting.as_of": "2019-01-01"}))

    assert (with_metadata["assumptions"]["reporting"]["basis"], with_metadata["assumptions"]["reporting"]["as_of"]) == ("estimate", "2026-09-01")
    assert (with_metadata["assumptions"]["troubleshooting"]["basis"], with_metadata["assumptions"]["troubleshooting"]["as_of"]) == ("measured", "2026-09-15")
    assert (without["assumptions"]["reporting"]["basis"], without["assumptions"]["troubleshooting"]["as_of"]) == (None, None)
    assert (other["assumptions"]["reporting"]["basis"], other["assumptions"]["troubleshooting"]["as_of"]) == ("measured", "2019-01-01")
    assert with_metadata["results"] == without["results"] == other["results"]
    assert with_metadata["missing"] == without["missing"] == other["missing"] == []


# =====================================================================================================================
# The labor rate
# =====================================================================================================================

def test_the_blocks_own_rate_is_the_one_used_and_the_answer_says_so(api, store):
    body = _report(api, store, BLOCK, {"labor_rate": "17.56"})

    assert body["assumptions"]["labor_rate"] == {"sortview_value": "32.50", "organization_efficiency": "17.56", "effective": "32.50", "source": "sortview_value"}
    assert body["results"]["monthly_value"] == "595.83"
    assert body["missing"] == []


def test_without_its_own_the_organizations_efficiency_default_is_the_one_used(api, store):
    body = _report(api, store, _block(labor_rate=...), {"labor_rate": "20.00", "manual_items_per_hour": "45.0"})

    assert body["assumptions"]["labor_rate"] == {"sortview_value": None, "organization_efficiency": "20.00", "effective": "20.00", "source": "organization_efficiency"}
    # 18.333... hours at 20.00.
    assert body["results"]["reporting"]["monthly_value"] == "300.00"
    assert body["results"]["monthly_value"] == "366.67"
    assert body["results"]["annual_value"] == "4400.00"
    assert body["results"]["net_annual_value"] == "-400.00"
    assert body["missing"] == []


def test_with_no_rate_anywhere_the_hours_are_known_and_nothing_has_a_value(api, store):
    body = _report(api, store, _block(labor_rate=...))

    assert body["assumptions"]["labor_rate"] == NO_RATE
    assert body["results"]["reporting"] == {**REPORTING_RESULTS, "monthly_value": None, "annual_value": None}
    assert body["results"]["troubleshooting"] == {**TROUBLESHOOTING_RESULTS, "monthly_value": None, "annual_value": None}
    assert {name: body["results"][name] for name in TOTALS} == {**NO_TOTALS, "recovered_hours_per_month": "18.33"}
    assert body["missing"] == ["labor_rate"]


# =====================================================================================================================
# Settings that are not all there
# =====================================================================================================================

@pytest.mark.parametrize(
    "name, reporting",
    [
        ("reporting.tasks_per_month", NO_CATEGORY),
        ("reporting.baseline_minutes_per_task", {**NO_CATEGORY, "current_hours_per_month": "3.00"}),
        ("reporting.current_minutes_per_task", {**NO_CATEGORY, "baseline_hours_per_month": "18.00"}),
    ],
)
def test_incomplete_reporting_is_a_report_with_no_reporting_value_and_no_organization_figures(api, store, name, reporting):
    response_body = _report(api, store, _block(**{name: ...}))

    assert response_body["results"]["reporting"] == reporting
    # The other kind of work is whole, and is reported by itself -- and is never passed off as the whole.
    assert response_body["results"]["troubleshooting"] == TROUBLESHOOTING_RESULTS
    assert {key: response_body["results"][key] for key in TOTALS} == NO_TOTALS
    assert response_body["missing"] == [name]


@pytest.mark.parametrize(
    "name, troubleshooting",
    [
        ("troubleshooting.incidents_per_month", NO_CATEGORY),
        ("troubleshooting.staff_per_incident", NO_CATEGORY),
        ("troubleshooting.baseline_minutes", {**NO_CATEGORY, "current_hours_per_month": "2.67"}),
        ("troubleshooting.current_minutes", {**NO_CATEGORY, "baseline_hours_per_month": "6.00"}),
    ],
)
def test_incomplete_troubleshooting_is_a_report_with_no_troubleshooting_value_and_no_organization_figures(api, store, name, troubleshooting):
    response_body = _report(api, store, _block(**{name: ...}))

    assert response_body["results"]["troubleshooting"] == troubleshooting
    assert response_body["results"]["reporting"] == REPORTING_RESULTS
    assert {key: response_body["results"][key] for key in TOTALS} == NO_TOTALS
    assert response_body["missing"] == [name]


@pytest.mark.parametrize("category", ["reporting", "troubleshooting"])
def test_one_kind_of_work_alone_is_a_200_and_is_never_a_total(api, store, category):
    block = {key: value for key, value in BLOCK.items() if key != category}
    _stored(store, block)

    response = _get(api)

    assert response.status_code == 200
    body = response.json()
    assert body["results"][category] == NO_CATEGORY
    assert {key: body["results"][key] for key in TOTALS} == NO_TOTALS
    assert body["missing"] == [name for name in USED_INPUTS if name.startswith(f"{category}.")]


def test_incomplete_settings_are_never_a_422_or_any_error(api, store):
    for block in ({}, {"labor_rate": "32.50"}, {"reporting": {"basis": "estimate"}}, _block(subscription_annual_cost=...), _block(**{"troubleshooting.staff_per_incident": ...})):
        _stored(store, block)
        response = _get(api)
        assert response.status_code == 200, block
        assert "code" not in response.json()


def test_missing_keeps_its_fixed_order_and_never_names_a_basis_or_a_date(api, store):
    block = _block(**{"troubleshooting.current_minutes": ..., "labor_rate": ..., "reporting.tasks_per_month": ..., "reporting.basis": ..., "troubleshooting.as_of": ...})

    assert _report(api, store, block)["missing"] == ["labor_rate", "reporting.tasks_per_month", "troubleshooting.current_minutes"]
    assert list(sortview_value_report_schemas.MissingInput.__args__) == list(USED_INPUTS)


def test_an_entered_zero_is_worked_through_as_zero_and_is_never_missing(api, store):
    body = _report(api, store, _block(**{"reporting.tasks_per_month": "0", "troubleshooting.current_minutes": "0"}))

    assert body["results"]["reporting"] == {
        "baseline_hours_per_month": "0.00",
        "current_hours_per_month": "0.00",
        "recovered_hours_per_month": "0.00",
        "monthly_value": "0.00",
        "annual_value": "0.00",
    }
    assert body["results"]["troubleshooting"] == {**TROUBLESHOOTING_RESULTS, "current_hours_per_month": "0.00", "recovered_hours_per_month": "6.00", "monthly_value": "195.00", "annual_value": "2340.00"}
    assert body["results"]["monthly_value"] == "195.00"
    assert body["missing"] == []
    # The same two left out are unknown: null, and named.
    unknown = _report(api, store, _block(**{"reporting.tasks_per_month": ..., "troubleshooting.current_minutes": ...}))
    assert unknown["results"]["reporting"]["recovered_hours_per_month"] is None
    assert unknown["missing"] == ["reporting.tasks_per_month", "troubleshooting.current_minutes"]


# =====================================================================================================================
# The cost, and results that are zero or less
# =====================================================================================================================

def test_with_no_subscription_cost_the_value_is_known_and_nothing_is_set_against_it(api, store):
    body = _report(api, store, _block(subscription_annual_cost=...))

    assert body["assumptions"]["subscription_annual_cost"] is None
    assert (body["results"]["monthly_value"], body["results"]["annual_value"]) == ("595.83", "7150.00")
    assert (body["results"]["net_annual_value"], body["results"]["roi_percent"], body["results"]["payback_months"]) == (None, None, None)
    assert body["missing"] == ["subscription_annual_cost"]


def test_a_subscription_that_costs_nothing_has_a_net_value_and_no_return_on_it(api, store):
    body = _report(api, store, _block(subscription_annual_cost="0"))

    assert body["assumptions"]["subscription_annual_cost"] == "0.00"
    assert body["results"]["net_annual_value"] == "7150.00"
    # A return on a cost of zero does not exist: null, not "0.0" and not infinite.
    assert body["results"]["roi_percent"] is None
    assert body["results"]["payback_months"] == "0.0"
    assert body["missing"] == []


def test_work_that_takes_longer_now_is_reported_as_negative_and_nothing_is_clipped(api, store):
    # Reporting now takes 120 minutes a task where it took 90: 6 hours more a month.
    body = _report(api, store, _block(**{"reporting.current_minutes_per_task": "120"}))

    assert body["results"]["reporting"] == {
        "baseline_hours_per_month": "18.00",
        "current_hours_per_month": "24.00",
        "recovered_hours_per_month": "-6.00",
        "monthly_value": "-195.00",
        "annual_value": "-2340.00",
    }
    assert {name: body["results"][name] for name in TOTALS} == {
        "recovered_hours_per_month": "-2.67",
        "monthly_value": "-86.67",
        "annual_value": "-1040.00",
        "net_annual_value": "-5840.00",
        "roi_percent": "-121.7",
        # A value that never covers the cost has no payback: null, not zero and not negative.
        "payback_months": None,
    }
    assert body["missing"] == []


def test_a_positive_value_smaller_than_the_cost_is_a_negative_return_and_a_payback_over_a_year(api, store):
    results = _report(api, store, _block(subscription_annual_cost="10000"))["results"]

    assert (results["net_annual_value"], results["roi_percent"], results["payback_months"]) == ("-2850.00", "-28.5", "16.8")


def test_a_monthly_value_of_exactly_zero_has_no_payback_and_is_written_as_zero_not_minus_zero(api, store):
    # Reporting recovers 15 hours; troubleshooting takes 15 more.
    results = _report(api, store, _block(**{"troubleshooting.baseline_minutes": "0", "troubleshooting.current_minutes": "112.5"}))["results"]

    assert results["troubleshooting"]["recovered_hours_per_month"] == "-15.00"
    assert (results["recovered_hours_per_month"], results["monthly_value"], results["annual_value"]) == ("0.00", "0.00", "0.00")
    assert (results["net_annual_value"], results["roi_percent"], results["payback_months"]) == ("-4800.00", "-100.0", None)
    assert "-0.0" not in str(results)


def test_zero_cost_and_a_value_that_is_zero_or_less_has_no_return_and_no_payback(api, store):
    for current in ("90", "120"):
        results = _report(api, store, _block(subscription_annual_cost="0", **{"reporting.current_minutes_per_task": current, "troubleshooting.current_minutes": "45"}))["results"]
        assert (results["roi_percent"], results["payback_months"]) == (None, None)
        assert results["net_annual_value"] == results["annual_value"]


# =====================================================================================================================
# The arithmetic is the settings model's, and only its
# =====================================================================================================================

@pytest.mark.parametrize(
    "block, efficiency",
    [
        (BLOCK, None),
        ({}, None),
        (_block(labor_rate=...), {"labor_rate": "17.56"}),
        (_block(labor_rate=...), None),
        (_block(subscription_annual_cost="0"), None),
        (_block(subscription_annual_cost=...), None),
        (_block(**{"reporting.current_minutes_per_task": "120"}), None),
        (_block(**{"reporting.tasks_per_month": ...}), None),
        (_block(**{"troubleshooting.staff_per_incident": ...}), {"labor_rate": "44.44"}),
        ({"labor_rate": "27.75", "subscription_annual_cost": "1500", "reporting": {"tasks_per_month": "2.5", "baseline_minutes_per_task": "37.5", "current_minutes_per_task": "7.5"},
          "troubleshooting": {"incidents_per_month": "0.5", "staff_per_incident": "1.5", "baseline_minutes": "22.5", "current_minutes": "2.5"}}, None),
    ],
)
def test_every_figure_in_the_answer_is_the_one_the_settings_model_works_out(api, store, block, efficiency):
    body = _report(api, store, block, efficiency)

    stored = stored_sortview_value(block or None, efficiency)
    assert stored.settings == parse_sortview_value_settings({"sortview_value": block})
    expected = calculate_sortview_value(stored.settings, resolve_labor_rate(stored.settings, stored.organization_efficiency))

    def text(value):
        return None if value is None else str(value)

    for category in ("reporting", "troubleshooting"):
        assert body["results"][category] == {name: text(getattr(getattr(expected, category), name)) for name in REPORTING_RESULTS}
    assert {name: body["results"][name] for name in TOTALS} == {name: text(getattr(expected, name)) for name in TOTALS}
    assert body["missing"] == list(expected.missing)


def test_the_route_composes_the_settings_read_the_rate_and_the_arithmetic_and_works_nothing_out_itself():
    source = inspect.getsource(sortview_value_report_routes)
    code = source.split('"""', 2)[2]

    assert "sortview_value_service.read_sortview_value(org_slug, user_id=user[\"id\"])" in code
    assert "calculate_sortview_value(stored.settings, resolve_labor_rate(stored.settings, stored.organization_efficiency))" in code
    # No arithmetic of its own: nothing is multiplied, divided, added up, rounded or compared here.
    for arithmetic in (" * ", " / ", " + ", "quantize", "round(", "Decimal(", "MINUTES_PER_HOUR", "MONTHS_PER_YEAR", "max(", "min(", " < ", " > ", "<= 0"):
        assert arithmetic not in code, arithmetic
    for forbidden in ("sqlalchemy", "get_engine", "execute(", "feature_enabled", "entitlements", "advanced_reports", "is_platform_admin", "streamlit"):
        assert forbidden not in source, forbidden


def test_nothing_is_read_but_the_stored_settings_once_and_nothing_is_written(api, store, monkeypatch):
    """No sorter, no event, no range: the one thing the database is asked is the settings service's one read."""
    _stored(store, BLOCK)
    before = copy.deepcopy(store.organizations)
    opened: list[str] = []
    from customer_api import tenant_scope
    from services import (
        efficiency_settings_service,
        operational_report_service,
        organization_report_service,
    )

    monkeypatch.setattr(tenant_scope, "open_customer_tenant_connection", lambda *args, **kwargs: opened.append("tenant connection"))
    monkeypatch.setattr(tenant_scope, "resolve_site_tenant", lambda *args, **kwargs: opened.append("site tenant"))
    for module, name in (
        (operational_report_service, "report_window"),
        (operational_report_service, "get_checkin_counts_by_day"),
        (organization_report_service, "get_organization_overview"),
        (efficiency_settings_service, "read_organization_efficiency"),
        (efficiency_settings_service, "read_sorter_efficiency"),
    ):
        monkeypatch.setattr(module, name, lambda *args, _name=name, **kwargs: opened.append(_name))

    assert _get(api).json() == WHOLE

    assert store.calls == [("read", "acme", OWNER)]
    assert opened == []
    assert store.organizations == before


def test_the_route_imports_nothing_operational_and_has_no_range():
    source = inspect.getsource(sortview_value_report_routes)
    code = source.split('"""', 2)[2]

    for operational in ("tenant_scope", "operational", "report_routes import", "Range", "ResolvedTenant", "branch_slug", "checkin", "from_date", "to_date", "require_report_range", "organization_report"):
        assert operational not in code, operational
    assert list(inspect.signature(sortview_value_report_routes.create_sortview_value_report_router).parameters) == []
    route = next(route for route in main.customer_router.routes if route.path.endswith("/reports/sortview-value"))
    assert [parameter.name for parameter in route.dependant.query_params] == []
    assert [parameter.name for parameter in route.dependant.path_params] == ["org_slug"]


def test_a_date_range_or_any_other_query_is_simply_not_used(api, store):
    _stored(store, BLOCK)

    for query in ("?from=2026-09-01&to=2026-09-30", "?from=not-a-date", "?compare=previous", "?anything=else&from=2030-01-01&to=2000-01-01"):
        response = _get(api, ACME + query)
        assert (response.status_code, response.json()) == (200, WHOLE)
    assert "range" not in _get(api).json()


# =====================================================================================================================
# What is stored but malformed
# =====================================================================================================================

@pytest.mark.parametrize(
    "stored",
    [
        {"labor_rate": "CANARY-not-a-rate", "reporting": {"tasks_per_month": 12}},
        "CANARY not an object",
        ["labor_rate"],
        {"reporting": "CANARY not an object"},
        {"troubleshooting": {"basis": "CANARY-basis"}},
    ],
)
def test_a_malformed_stored_value_block_is_the_contained_error_and_nothing_is_worked_out_from_it(api, store, caplog, stored):
    store.organizations["acme"]["sortview_value"] = stored

    with caplog.at_level(logging.ERROR):
        response = _get(api)

    assert (response.status_code, response.json()) == (500, STORED_INVALID)
    assert response.headers["cache-control"] == "no-store"
    assert "CANARY" not in response.text and "Traceback" not in response.text
    assert "results" not in response.text and "assumptions" not in response.text
    assert "Stored SortView Value settings are malformed" in caplog.text
    assert "CANARY" not in caplog.text
    # The same answer the settings route gives for the same block: it is never a 422.
    assert _get(api, SETTINGS.format(org="acme")).json() == STORED_INVALID


@pytest.mark.parametrize("efficiency", [{"labor_rate": "CANARY-bad"}, {"labor_rate": 17.56}, "CANARY not an object", '{"labor_rate": "17.56"}', ["labor_rate"], 7])
def test_a_malformed_stored_efficiency_block_is_still_a_report_with_no_rate_to_fall_back_on(api, store, caplog, efficiency):
    _stored(store, _block(labor_rate=...))
    store.organizations["acme"]["efficiency"] = efficiency

    with caplog.at_level(logging.WARNING, logger="sortview.customer_api"):
        response = _get(api)

    assert response.status_code == 200
    body = response.json()
    assert body["assumptions"]["labor_rate"] == NO_RATE
    # Hours are still worked out; nothing has a value, and the rate is what is missing.
    assert body["results"]["recovered_hours_per_month"] == "18.33"
    assert (body["results"]["monthly_value"], body["results"]["annual_value"], body["results"]["roi_percent"]) == (None, None, None)
    assert body["missing"] == ["labor_rate"]
    assert "CANARY" not in response.text and "17.56" not in response.text
    assert "Stored efficiency settings are malformed; no fallback labor rate | org=acme" in caplog.text
    assert "CANARY" not in caplog.text


def test_with_a_malformed_efficiency_block_the_blocks_own_rate_still_gives_every_figure(api, store):
    assert _report(api, store, BLOCK, {"labor_rate": "bad"}) == WHOLE


def test_a_database_failure_is_the_generic_server_error_and_says_nothing(api, store, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("CANARY connection to 10.0.0.9 failed: password=hunter2")

    monkeypatch.setattr(sortview_value_service, "read_sortview_value", broken)

    response = _get(api)

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    assert "CANARY" not in response.text


# =====================================================================================================================
# The contract
# =====================================================================================================================

def test_no_answer_carries_an_internal_identifier_or_anything_else_of_the_document(api, store):
    _stored(store, BLOCK)
    store.organizations["acme"]["efficiency"] = {"labor_rate": "17.56", "manual_items_per_hour": "46.5"}

    for response in (_get(api), _get(api, user=ADMIN), _get(api, user=VIEWER), _get(api, BETA, STRANGER)):
        text = response.text
        for user_id in (OWNER, ADMIN, VIEWER, STRANGER):
            assert str(user_id) not in text
        for kept_out in ("organization_id", "user_id", "customer_id", "branch_id", '"id"', CANARY_HASH, "library_name", "security", "transit", "Westside", "manual_items_per_hour", "46.5"):
            assert kept_out not in text, kept_out


def test_the_response_models_have_exactly_these_fields_and_take_no_others():
    schemas = sortview_value_report_schemas

    assert list(schemas.SortViewValueReportResponse.model_fields) == ["currency", "assumptions", "results", "missing"]
    assert list(schemas.SortViewValueResults.model_fields) == ["reporting", "troubleshooting", *TOTALS]
    assert list(schemas.CategoryResults.model_fields) == list(REPORTING_RESULTS)
    for model in (schemas.SortViewValueReportResponse, schemas.SortViewValueResults, schemas.CategoryResults):
        assert model.model_config["extra"] == "forbid"
    # No range, no id, no prose.
    names = set(_walk({name: None for model in (schemas.SortViewValueReportResponse, schemas.SortViewValueResults, schemas.CategoryResults) for name in model.model_fields}))
    assert not names & {"range", "from", "to", "from_date", "to_date", "id", "organization_id", "disclosure", "message", "note"}


def test_the_settings_routes_still_answer_no_calculated_figure(api, store):
    """The report is where figures are. Reading or replacing the settings still returns what was entered and which
    rate applies, and nothing worked out."""
    _stored(store, BLOCK)
    headers = {"Cookie": f"{COOKIE}={OWNER}", "Origin": ORIGIN}

    for response in (_get(api, SETTINGS.format(org="acme")), api.put(SETTINGS.format(org="acme"), json=BLOCK, headers=headers)):
        assert response.status_code == 200
        assert list(response.json()) == ["sortview_value"]
        text = response.text
        for figure in ("18.33", "595.83", "7150", "2350", "49.0", "8.1", "487.50", "108.33"):
            assert figure not in text, figure
        for name in ("results", "assumptions", "recovered", "monthly_value", "annual_value", "net_annual_value", "roi", "payback", "missing", "currency"):
            assert name not in text, name
    assert "calculate_sortview_value" not in inspect.getsource(sortview_value_routes)
    assert "calculate_sortview_value" not in inspect.getsource(sortview_value_service)
    # The settings routes know nothing of the report: it is the report that uses them.
    imports = [line for line in inspect.getsource(sortview_value_routes).splitlines() if line.startswith(("import ", "from "))]
    assert not any("sortview_value_report" in line for line in imports)
