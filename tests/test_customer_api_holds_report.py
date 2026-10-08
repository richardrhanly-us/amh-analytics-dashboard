"""R8K: a sorter site's holds report -- the route.

    GET /api/organizations/{org_slug}/branches/{branch_slug}/reports/holds?from=YYYY-MM-DD&to=YYYY-MM-DD

These tests drive the real production route through TestClient(main.app), over the in-memory database of
tests/test_customer_api_organization_reports.py, whose fixtures they use as they are: the session lookup, the real
membership and tenant resolution, the scoped and verified connection and the report window all run. Two things are
stood in for:

  * the counts (services.hold_report_service.get_holds_report), whose statements are PostgreSQL's and are run for
    real in tests/test_hold_report_postgres.py -- here it records what it was asked and answers fixed numbers;
  * the organization's plan (services.entitlement_service.build_entitlement_context), so that whether the
    `internal_workflow` feature is on is said by each test.

Every name here is synthetic.
"""

from __future__ import annotations

import inspect
import logging
import re
from datetime import date, timedelta

import pytest
import test_customer_api_organization_reports as organization_reports
from fastapi.dependencies.utils import get_flat_dependant
from test_customer_api_organization_reports import COOKIE, JUNE_8_TO_12, RANGE

import main
from customer_api import holds_report_routes, holds_report_schemas
from services import entitlement_service
from services.hold_report_service import HoldsReport

# That file's fixtures, used here as they are: its database, its clock, its session and its client.
api, clock, db, session = organization_reports.api, organization_reports.clock, organization_reports.db, organization_reports.session

HOLDS = "/api/organizations/{org}/branches/{branch}/reports/holds"
NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
TENANT_NOT_FOUND = {"code": "tenant_not_found", "message": "Organization or branch not found."}
NOT_AVAILABLE = {"code": "feature_not_available", "message": "This report is not available for this organization."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}


class Plan:
    """Which organizations' plans include the holds report, and every lookup that was made."""

    def __init__(self, monkeypatch):
        self.enabled = {"acme", "paused", "solo", "closed", "beta"}
        self.history: int | None = 90
        self.lookups: list[tuple[int, str]] = []
        monkeypatch.setattr(entitlement_service, "build_entitlement_context", self.context)

    def context(self, user_id, org_slug):
        self.lookups.append((user_id, org_slug))
        on = org_slug in self.enabled
        return {"role": "viewer", "subscription": None, "entitlements": {"internal_workflow": {"enabled": on, "limit_value": None}, "history_days": {"enabled": True, "limit_value": self.history},
                                                                    # Transit routing on throughout: only the holds feature varies here (R9C).
                                                                    "transits": {"enabled": True, "limit_value": None}}}


class Counts:
    """Stands in for the service: answers fixed numbers, and records each tenant and window it was asked about."""

    def __init__(self, monkeypatch):
        self.asked: list[tuple] = []
        self.answer = HoldsReport(public_hold_count=12, ill_hold_count=3)
        monkeypatch.setattr(holds_report_routes, "get_holds_report", self.report)

    def report(self, conn, tenant, window):
        self.asked.append((tenant.org_slug, tenant.branch_slug, tenant.operational_customer_id, window.local_range.from_date, window.local_range.to_date))
        return self.answer


@pytest.fixture
def plan(monkeypatch):
    return Plan(monkeypatch)


@pytest.fixture
def counts(monkeypatch):
    return Counts(monkeypatch)


def _get(api, org="acme", branch="main", params=JUNE_8_TO_12, headers=COOKIE):
    return api.get(HOLDS.format(org=org, branch=branch), headers=headers, params=params)


def _refused(response, status, body):
    assert (response.status_code, response.json()) == (status, body), response.text
    assert response.headers["cache-control"] == "no-store"


# =====================================================================================================================
# The answer
# =====================================================================================================================

def test_the_answer_is_the_range_and_two_counts_and_nothing_else(api, db, plan, counts):
    response = _get(api)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"range": RANGE, "public_hold_count": 12, "ill_hold_count": 3}
    # Asked about the site in the address, for the range in the request -- nothing else.
    assert counts.asked == [("acme", "main", organization_reports.ACME, date(2026, 6, 8), date(2026, 6, 12))]


def test_zero_is_an_answer_like_any_other(api, db, plan, counts):
    counts.answer = HoldsReport(public_hold_count=0, ill_hold_count=0)

    assert _get(api).json() == {"range": RANGE, "public_hold_count": 0, "ill_hold_count": 0}


def test_today_is_a_range_of_one_day_that_says_it_includes_today(api, db, plan, counts):
    # The shared clock's product date is Saturday 20 June 2026.
    response = _get(api, params={"from": "2026-06-20", "to": "2026-06-20"})

    assert response.status_code == 200, response.text
    assert response.json()["range"] == {"from": "2026-06-20", "to": "2026-06-20", "days": 1, "timezone": "America/Chicago",
                                        "includes_today": True}


def test_the_response_model_has_exactly_the_three_fields_and_refuses_any_other():
    assert set(holds_report_schemas.HoldsReportResponse.model_fields) == {"range", "public_hold_count", "ill_hold_count"}
    with pytest.raises(ValueError):
        holds_report_schemas.HoldsReportResponse(range=organization_reports_range(), public_hold_count=1, ill_hold_count=0, programming_total=1)


def organization_reports_range():
    from customer_api.report_schemas import ReportRange

    return ReportRange(from_date="2026-06-08", to_date="2026-06-12", days=5, timezone="America/Chicago", includes_today=False)


def test_no_answer_holds_an_internal_category_a_destination_or_anything_private(api, db, plan, counts):
    text = _get(api).text

    for forbidden in ("programming", "branch_services", "collection", "internal", "ill_main", "ill_by_branch", "destination", "source_era",
                      "v1", "v2", "patron", "raw_message", "rules", "total_hold", "8101", "customer_id", "branch_id"):
        assert forbidden not in text, forbidden


# =====================================================================================================================
# Who may read it, and in what order that is decided
# =====================================================================================================================

@pytest.mark.parametrize("role", ["owner", "admin", "manager", "viewer"])
def test_every_member_role_may_read_it_when_the_plan_includes_it(api, db, plan, counts, role):
    db.run("UPDATE memberships SET role = :r WHERE organization_id = 1 AND user_id = 1", r=role)

    assert _get(api).status_code == 200


def test_without_a_session_it_is_401_and_nothing_is_looked_up(api, db, plan, counts, session):
    session.user = None

    _refused(_get(api), 401, NOT_AUTHENTICATED)
    assert plan.lookups == [] and counts.asked == []


@pytest.mark.parametrize(("org", "branch"), [("beta", "north"), ("closed", "main"), ("nowhere", "main"), ("acme", "nowhere"), ("acme", "shut")],
                         ids=["not-a-member", "cancelled", "no-such-organization", "no-such-branch", "inactive-branch"])
def test_a_site_the_user_cannot_see_is_the_other_reports_404_and_the_plan_is_not_looked_at(api, db, plan, counts, org, branch):
    _refused(_get(api, org=org, branch=branch), 404, TENANT_NOT_FOUND)
    assert plan.lookups == [] and counts.asked == []


def test_a_suspended_organizations_holds_can_be_read(api, db, plan, counts):
    response = _get(api, org="paused")

    assert response.status_code == 200, response.text
    assert [asked[0] for asked in counts.asked] == ["paused"]


def test_a_plan_without_the_feature_is_403_and_nothing_is_counted(api, db, plan, counts):
    plan.enabled.discard("acme")

    _refused(_get(api), 403, NOT_AVAILABLE)
    assert plan.lookups == [(1, "acme")] and counts.asked == []


@pytest.mark.parametrize("entitlements", [{}, {"internal_workflow": {"enabled": False}}, {"internal_workflow": None}, {"other": {"enabled": True}}])
def test_a_feature_that_is_missing_off_or_empty_is_not_on(api, db, counts, monkeypatch, entitlements):
    monkeypatch.setattr(entitlement_service, "build_entitlement_context", lambda user_id, org_slug: {"entitlements": entitlements})

    _refused(_get(api), 403, NOT_AVAILABLE)


def test_the_feature_is_checked_before_the_range(api, db, plan, counts):
    plan.enabled.discard("acme")

    _refused(_get(api, params={"from": "2026-06-12", "to": "2026-06-08"}), 403, NOT_AVAILABLE)
    plan.enabled.add("acme")
    assert _get(api, params={"from": "2026-06-12", "to": "2026-06-08"}).status_code == 422


def test_the_feature_is_checked_before_the_history_window_and_both_lookups_are_one(api, db, plan, counts):
    # R9C. The plan here allows 90 days back from 20 June: 23 March is the first date a range may start on.
    before = {"from": "2026-03-22", "to": "2026-03-31"}
    plan.enabled.discard("acme")
    _refused(_get(api, params=before), 403, NOT_AVAILABLE)

    plan.enabled.add("acme")
    refused = _get(api, params=before)
    assert (refused.status_code, refused.json()["code"]) == (422, "range_before_history")
    # The holds feature and the history window come from one read of the plan per request.
    lookups = len(plan.lookups)
    assert _get(api, params={"from": "2026-03-23", "to": "2026-03-31"}).status_code == 200
    assert plan.lookups[lookups:] == [(1, "acme")]


@pytest.mark.parametrize("params", [
    {"from": "2026-06-12", "to": "2026-06-08"},           # ends before it starts
    {"from": "2026-01-01", "to": "2026-06-12"},           # longer than the reports allow
    {"from": "2026-06-08"},                               # no end
    {"from": "2026-06-08T00:00:00", "to": "2026-06-12"},  # not a calendar date
    {"from": "2099-01-01", "to": "2099-01-02"},           # in the future
])
def test_a_range_that_cannot_be_reported_on_is_the_other_reports_422(api, db, plan, counts, params):
    response = _get(api, params=params)

    assert response.status_code == 422, response.text
    assert counts.asked == []


def test_the_feature_does_not_reach_the_other_reports(api, db, plan, counts):
    plan.enabled.clear()

    # (Bin Volume needs a column this file's database does not have; its own file tests it.)
    for report in ("overview", "volume", "routing", "reliability"):
        assert api.get(f"/api/organizations/acme/branches/main/reports/{report}", headers=COOKIE, params=JUNE_8_TO_12).status_code == 200, report
    _refused(_get(api), 403, NOT_AVAILABLE)


# =====================================================================================================================
# Failures
# =====================================================================================================================

def test_a_failure_while_counting_is_a_generic_500_that_carries_nothing_of_it(api, db, plan, counts, monkeypatch, caplog):
    def fail(*_args, **_kwargs):
        raise RuntimeError("CANARY |AECANARY PATRON NAME| raw 101YNY for patron CANARY-ID")

    monkeypatch.setattr(holds_report_routes, "get_holds_report", fail)

    with caplog.at_level(logging.DEBUG):
        response = _get(api)

    _refused(response, 500, INTERNAL_ERROR)
    assert "CANARY" not in response.text and "CANARY" not in caplog.text and "101YNY" not in caplog.text


def test_a_failure_looking_up_the_plan_is_a_generic_500_not_a_refusal(api, db, counts, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("CANARY database is down")

    monkeypatch.setattr(entitlement_service, "build_entitlement_context", fail)

    _refused(_get(api), 500, INTERNAL_ERROR)


# =====================================================================================================================
# The shape of the module
# =====================================================================================================================

def test_the_route_is_exactly_one_get_that_takes_the_two_slugs_and_the_two_dates():
    (route,) = [route for route in main.app.routes if getattr(route, "path", "").endswith("/reports/holds")]

    assert route.methods == {"GET"}
    assert route.path == "/api/organizations/{org_slug}/branches/{branch_slug}/reports/holds"
    assert sorted(param.name for param in route.dependant.path_params) == []  # the slugs are read by its dependencies
    flat = {param.alias for param in get_flat_dependant(route.dependant).query_params}
    assert flat == {"from", "to"}
    for method in ("post", "put", "patch", "delete"):
        assert getattr(organization_reports.TestClient(main.app), method)(HOLDS.format(org="acme", branch="main"), headers=COOKIE).status_code == 405


def test_the_handler_counts_nothing_itself_and_the_route_module_imports_no_dashboard_code():
    source = inspect.getsource(holds_report_routes)
    imports = "\n".join(line for line in source.splitlines() if line.startswith(("import ", "from ")))

    code = source.split('"""', 2)[2]
    for forbidden in (r"\btext\(", "SELECT", r"\.execute\(", "get_engine", r"\+=", r"\bsum\(", "holds_total", "ill_total"):
        assert not re.search(forbidden, code), forbidden
    for unwanted in ("streamlit", "pandas", "data_loader", "mixed_era_service", "metrics", "settings_service"):
        assert unwanted not in imports, unwanted
    # The one module that does count, and the only one in the customer API's reach that uses the dashboard classifiers.
    from services import hold_report_service

    service_imports = "\n".join(line for line in inspect.getsource(hold_report_service).splitlines() if line.startswith(("import ", "from ")))
    assert "import metrics\n" in service_imports + "\n" and "import metrics_v2" in service_imports and "import pandas as pd" in service_imports
    for unwanted in ("streamlit", "data_loader", "mixed_era_service", "settings_service"):
        assert unwanted not in service_imports, unwanted
    assert holds_report_routes.HOLDS_FEATURE == "internal_workflow"



# =====================================================================================================================
# Reports R9D2: the holds report keeps 92 days, whatever the plan's history allows
# =====================================================================================================================

HOLDS_TOO_LONG = {"code": "holds_range_too_long", "message": "Holds reporting is currently available for ranges up to 92 days."}


def _days(count: int, ending: str = "2026-06-20") -> dict:
    end = date.fromisoformat(ending)
    return {"from": (end - timedelta(days=count - 1)).isoformat(), "to": end.isoformat()}


def test_92_days_of_holds_are_counted_and_93_are_refused_before_anything_is_read(api, db, plan, counts):
    plan.history = None                                     # the plan would allow any length: the 92 is the report's

    assert _get(api, params=_days(92)).status_code == 200
    assert len(counts.asked) == 1
    for longer in (93, 365, 3660):
        _refused(_get(api, params=_days(longer)), 422, HOLDS_TOO_LONG)
    assert len(counts.asked) == 1                           # the service was never asked about a longer range


def test_the_other_reports_take_the_same_long_range_under_the_same_plan(api, db, plan, counts):
    plan.history = None

    _refused(_get(api, params=_days(365)), 422, HOLDS_TOO_LONG)
    for report in ("overview", "volume", "routing", "reliability"):
        response = api.get(f"/api/organizations/acme/branches/main/reports/{report}", headers=COOKIE, params=_days(365))
        assert response.status_code == 200, report


def test_the_holds_length_comes_after_the_feature_and_every_rule_every_range_has(api, db, plan, counts):
    # The feature first: a plan without the report is told so, whatever the range.
    plan.enabled.discard("acme")
    _refused(_get(api, params=_days(365)), 403, NOT_AVAILABLE)
    plan.enabled.add("acme")
    # Then the shared range: its order, the future, the plan's window ...
    assert _get(api, params={"from": "2026-06-12", "to": "2026-06-08"}).json()["detail"][0]["type"] == "report_range_order"
    plan.history = 30
    assert _get(api, params=_days(93)).json()["code"] == "range_before_history"
    # ... and only then the holds report's own length.
    plan.history = 3650
    _refused(_get(api, params=_days(93)), 422, HOLDS_TOO_LONG)
    assert counts.asked == []


def test_the_holds_length_is_the_holds_reports_own_and_no_plan_name_or_other_report_knows_it():
    assert holds_report_routes.HOLDS_MAX_RANGE_DAYS == 92
    source = inspect.getsource(holds_report_routes)
    assert "def require_holds_range(requested: Range) -> RequestedRange:" in source
    assert "requested: HoldsRange" in source
    for plan in ("starter", "enterprise", '"pro"'):
        assert plan not in source.lower()
