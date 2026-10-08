"""Reports R6C: a sorter site's Efficiency report -- the route.

    GET /api/organizations/{org_slug}/branches/{branch_slug}/reports/efficiency?from=&to=

These tests drive the real production route through TestClient(main.app).
Nothing between the request and the rows is replaced: the session and role
dependencies, the real access, entitlement and tenant-resolution services,
the scoped and verified connection, the report window and per-day counts of
services.operational_report_service, the real settings read
(services.efficiency_settings_service) and the real arithmetic
(services.efficiency_report) all run, and so does every SQL statement --
against the in-memory database of tests/test_customer_api_organization_reports.py,
whose fixtures and seeded week this file uses as they are.

THE INVARIANT THIS FILE IS BUILT AROUND: the check-ins the Efficiency report
counts are the ones the sorter's other reports count. So the counts here are
compared with those endpoints' own answers for the same sorter and range.

The arithmetic itself is tested in tests/test_efficiency_report.py; real
JSONB, real row level security and a real non-owning role in
tests/test_efficiency_report_postgres.py.
"""

from __future__ import annotations

import inspect
import logging
import re
from datetime import UTC, datetime

import pytest
import test_customer_api_organization_reports as organization_reports
from fastapi.testclient import TestClient
from test_customer_api_organization_reports import (
    ACME,
    ACME_SETTINGS,
    COOKIE,
    EAST,
    EVERY_OPERATIONAL_ID,
    JUNE_8_TO_12,
    MAIN,
    RANGE,
    SITE,
    _keys,
    _seed_acme,
    _site,
)

import main
from customer_api import efficiency_report_routes, efficiency_report_schemas
from services import efficiency_settings_service, entitlement_service

# That file's fixtures, used here as they are: its database, its clock, its session and its client.
api, clock, db, session = organization_reports.api, organization_reports.clock, organization_reports.db, organization_reports.session

BOB = {"id": 2, "email": "bob@example.invalid", "full_name": "Bob"}

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
ORGANIZATION_NOT_FOUND = {"code": "organization_not_found", "message": "Organization not found."}
TENANT_NOT_FOUND = {"code": "tenant_not_found", "message": "Organization or branch not found."}
SORTER_NOT_FOUND = {"code": "sorter_not_found", "message": "Sorter not found."}
FORBIDDEN = {"code": "forbidden", "message": "You do not have permission to manage these settings."}
STORED_INVALID = {"code": "efficiency_settings_invalid", "message": "The stored efficiency settings could not be read."}

ORG_RATES = {"labor_rate": "17.56", "manual_items_per_hour": "45.0"}
SORTER_COSTS = {"one_time_cost": "118003.92", "recurring_annual_cost": "8400.00", "in_service_date": "2020-11-20"}
NO_RESULTS = {
    "staff_time_equivalent_hours": None,
    "labor_value_equivalent": None,
    "recurring_cost": None,
    "net_operational_value": None,
    "recurring_cost_per_item": None,
}


@pytest.fixture(autouse=True)
def _one_database(db, monkeypatch):
    """The role lookup and the settings read use the same database as everything else. Alice, a viewer of Acme in
    the shared fixture, is its owner here unless a test says otherwise."""
    for module in (efficiency_settings_service, entitlement_service):
        monkeypatch.setattr(module, "get_engine", lambda: db.engine)
    _role(db, "owner")


def _role(db, role: str, *, organization: int = 1, user: int = 1) -> None:
    db.run("UPDATE memberships SET role = :r WHERE organization_id = :o AND user_id = :u", r=role, o=organization, u=user)


def _configure(db, *, organization: dict | None = None, main_site: dict | None = None, east: dict | None = None) -> None:
    """Stores Efficiency blocks beside whatever else each settings document holds."""
    if organization is not None:
        db.settings(organization=1, document={**ACME_SETTINGS, "efficiency": organization})
    if main_site is not None:
        db.settings(branch=MAIN, document={"branch_name": "Main", "efficiency": main_site})
    if east is not None:
        db.settings(branch=EAST, document={"transit": {"home_branch_label": "East"}, "efficiency": east})


def _get(api, branch="main", params=JUNE_8_TO_12, *, org="acme", headers=COOKIE):
    return api.get(SITE.format(org=org, branch=branch, report="efficiency"), headers=headers, params=params)


def _report(api, branch="main", params=JUNE_8_TO_12, *, org="acme") -> dict:
    response = _get(api, branch, params, org=org)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


# =====================================================================================================================
# The report
# =====================================================================================================================

def test_a_fully_configured_sorters_report(api, db):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS)

    # Main: 30 check-ins over 5 days. 30 / 45 = 0.67 h; x 17.56 = 11.71; 8400 / 365 * 5 = 115.07; 115.0685 / 30 = 3.8356.
    assert _report(api) == {
        "range": RANGE,
        "currency": "USD",
        "checkin_count": 30,
        "assumptions": {
            "manual_items_per_hour": {"value": "45.0", "source": "organization"},
            "labor_rate": {"value": "17.56", "source": "organization"},
            "recurring_annual_cost": "8400.00",
            "one_time_cost": "118003.92",
            "in_service_date": "2020-11-20",
        },
        "results": {
            "in_service_days": 5,
            "in_service_checkin_count": 30,
            "staff_time_equivalent_hours": "0.67",
            "labor_value_equivalent": "11.71",
            "recurring_cost": "115.07",
            "net_operational_value": "-103.36",
            "recurring_cost_per_item": "3.8356",
        },
        "missing": [],
    }


def test_the_check_ins_are_the_ones_the_sorters_other_reports_count(api, db):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS, east=SORTER_COSTS)

    for branch, expected in (("main", 30), ("east", 9)):
        efficiency = _report(api, branch)
        overview, volume = _site(api, branch, "overview"), _site(api, branch, "volume")

        assert efficiency["checkin_count"] == overview["checkin_count"] == volume["checkin_count"] == expected
        assert efficiency["results"]["in_service_checkin_count"] == sum(day["checkin_count"] for day in volume["days"])
        assert efficiency["range"] == overview["range"]


def test_the_range_crosses_the_sorters_cutover_and_both_eras_are_counted_once(api, db):
    """Main cuts over at noon on 10 June: legacy rows before, current rows from then on, and rows on the wrong side
    of the cutover that count for nothing. The seeded week is the R2 tests' own."""
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS)

    by_day = [day["checkin_count"] for day in _site(api, "main", "volume")["days"]]
    assert by_day == [6, 8, 8, 8, 0]
    for params, expected in (
        ({"from": "2026-06-10", "to": "2026-06-10"}, 8),    # the cutover day alone: 3 legacy + 5 current
        ({"from": "2026-06-09", "to": "2026-06-10"}, 16),
        ({"from": "2026-06-10", "to": "2026-06-11"}, 16),
        ({"from": "2026-06-08", "to": "2026-06-09"}, 14),   # legacy only
        ({"from": "2026-06-11", "to": "2026-06-12"}, 8),    # current only
    ):
        assert _report(api, params=params)["checkin_count"] == _site(api, "main", "overview", params)["checkin_count"] == expected


def test_only_the_sorters_own_scope_is_read_and_only_check_ins(api, db):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS)
    db.queries.clear()

    _report(api)

    reads = db.scoped_reads()
    assert reads
    assert {(customer, branch) for _table, customer, branch in reads} == {(str(ACME), str(MAIN))}
    assert {table for table, _customer, _branch in reads} <= {"v2_cutovers", "checkins", "checkin_events"}


def test_a_report_writes_nothing(api, db):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS)
    with db.engine.connect() as conn:
        before = [conn.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id").all() for table in ("organization_settings", "branch_settings")]

    _report(api)
    _get(api, params={"from": "2026-06-12", "to": "2026-06-08"})

    with db.engine.connect() as conn:
        after = [conn.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id").all() for table in ("organization_settings", "branch_settings")]
    assert before == after
    source = inspect.getsource(efficiency_report_routes)
    assert "replace_" not in source and ".put(" not in source and "require_allowed_origin" not in source


# =====================================================================================================================
# In-service days
# =====================================================================================================================

def test_a_range_that_starts_before_the_in_service_date_is_worked_out_from_that_date(api, db):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site={**SORTER_COSTS, "in_service_date": "2026-06-10"})

    report = _report(api)
    volume = _site(api, "main", "volume")

    # 10, 11 and 12 June: 8 + 8 + 0 check-ins. 16 / 45 = 0.36 h; x 17.56 = 6.24; 8400 / 365 * 3 = 69.04.
    assert report["checkin_count"] == volume["checkin_count"] == 30
    assert report["results"] == {
        "in_service_days": 3,
        "in_service_checkin_count": 16,
        "staff_time_equivalent_hours": "0.36",
        "labor_value_equivalent": "6.24",
        "recurring_cost": "69.04",
        "net_operational_value": "-62.80",
        "recurring_cost_per_item": "4.3151",
    }
    # Exactly the other reports' own counts for those days -- not another definition of a check-in.
    assert report["results"]["in_service_checkin_count"] == sum(day["checkin_count"] for day in volume["days"] if day["date"] >= "2026-06-10")
    assert report["missing"] == []
    # And the same figures as asking for those three days alone.
    assert _report(api, params={"from": "2026-06-10", "to": "2026-06-12"})["results"] == report["results"]


@pytest.mark.parametrize(("in_service_date", "days", "counted"), [("2026-06-08", 5, 30), ("2026-06-07", 5, 30), ("2026-06-09", 4, 24), ("2026-06-12", 1, 0)])
def test_the_in_service_days_are_the_days_on_or_after_the_date(api, db, in_service_date, days, counted):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site={**SORTER_COSTS, "in_service_date": in_service_date})

    results = _report(api)["results"]

    assert (results["in_service_days"], results["in_service_checkin_count"]) == (days, counted)


def test_a_sorter_that_went_into_service_after_the_range_has_nothing_counted_and_nothing_charged(api, db):
    _seed_acme(db)
    # After 8-12 June and not after the product's today (20 June): a date that can be stored.
    _configure(db, organization=ORG_RATES, main_site={**SORTER_COSTS, "in_service_date": "2026-06-15"})

    report = _report(api)

    assert report["checkin_count"] == 30
    assert report["results"] == {
        "in_service_days": 0,
        "in_service_checkin_count": 0,
        "staff_time_equivalent_hours": "0.00",
        "labor_value_equivalent": "0.00",
        "recurring_cost": "0.00",
        "net_operational_value": "0.00",
        "recurring_cost_per_item": None,
    }


def test_with_no_in_service_date_the_whole_range_is_used_and_the_date_is_listed_as_missing(api, db):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site={"recurring_annual_cost": "8400.00"})

    report = _report(api)

    assert report["assumptions"]["in_service_date"] is None
    assert (report["results"]["in_service_days"], report["results"]["in_service_checkin_count"]) == (5, 30)
    assert report["results"]["recurring_cost"] == "115.07"
    assert report["missing"] == ["in_service_date"]


# =====================================================================================================================
# Whose settings, and which are missing
# =====================================================================================================================

def test_a_sorters_own_rates_override_the_organizations_and_say_so(api, db):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site={**SORTER_COSTS, "labor_rate": "20"}, east={"manual_items_per_hour": "60.0"})

    main_site, east = _report(api), _report(api, "east")

    assert main_site["assumptions"]["labor_rate"] == {"value": "20.00", "source": "sorter"}
    assert main_site["assumptions"]["manual_items_per_hour"] == {"value": "45.0", "source": "organization"}
    assert main_site["results"]["labor_value_equivalent"] == "13.33"   # 30 / 45 * 20
    assert east["assumptions"]["manual_items_per_hour"] == {"value": "60.0", "source": "sorter"}
    assert east["assumptions"]["labor_rate"] == {"value": "17.56", "source": "organization"}
    assert east["results"]["staff_time_equivalent_hours"] == "0.15"    # 9 / 60
    assert east["results"]["labor_value_equivalent"] == "2.63"         # 9 / 60 * 17.56 = 2.634


def test_a_cost_in_the_organizations_block_is_never_the_sorters(api, db):
    _seed_acme(db)
    _configure(db, organization={**ORG_RATES, "recurring_annual_cost": "9000.00", "one_time_cost": "5.00", "in_service_date": "2026-06-10"})

    report = _report(api)

    assert report["assumptions"] | {"manual_items_per_hour": None, "labor_rate": None} == {
        "manual_items_per_hour": None, "labor_rate": None, "recurring_annual_cost": None, "one_time_cost": None, "in_service_date": None,
    }
    assert report["results"]["recurring_cost"] is None
    assert report["results"]["in_service_days"] == 5
    assert report["missing"] == ["recurring_annual_cost", "in_service_date"]
    assert "9000" not in str(report)


def test_with_no_settings_at_all_the_report_is_the_check_ins_and_what_is_missing(api, db):
    _seed_acme(db)

    assert _report(api) == {
        "range": RANGE,
        "currency": "USD",
        "checkin_count": 30,
        "assumptions": {"manual_items_per_hour": None, "labor_rate": None, "recurring_annual_cost": None, "one_time_cost": None, "in_service_date": None},
        "results": {"in_service_days": 5, "in_service_checkin_count": 30, **NO_RESULTS},
        "missing": ["manual_items_per_hour", "labor_rate", "recurring_annual_cost", "in_service_date"],
    }


@pytest.mark.parametrize(
    ("organization", "main_site", "expected", "missing"),
    [
        ({"labor_rate": "17.56"}, SORTER_COSTS, (None, None, "115.07", None, "3.8356"), ["manual_items_per_hour"]),
        ({"manual_items_per_hour": "45.0"}, SORTER_COSTS, ("0.67", None, "115.07", None, "3.8356"), ["labor_rate"]),
        (ORG_RATES, {"in_service_date": "2020-11-20"}, ("0.67", "11.71", None, None, None), ["recurring_annual_cost"]),
        (ORG_RATES, {"one_time_cost": "118003.92"}, ("0.67", "11.71", None, None, None), ["recurring_annual_cost", "in_service_date"]),
        # A cost that was SET to zero is a cost of zero.
        (ORG_RATES, {**SORTER_COSTS, "recurring_annual_cost": "0.00"}, ("0.67", "11.71", "0.00", "11.71", "0.0000"), []),
        # No one-time cost: nothing depends on it, and it is not "missing".
        (ORG_RATES, {"recurring_annual_cost": "8400.00", "in_service_date": "2020-11-20"}, ("0.67", "11.71", "115.07", "-103.36", "3.8356"), []),
        (ORG_RATES, {**SORTER_COSTS, "one_time_cost": "0.00"}, ("0.67", "11.71", "115.07", "-103.36", "3.8356"), []),
    ],
)
def test_each_figure_is_worked_out_from_what_is_known_and_the_rest_are_null(api, db, organization, main_site, expected, missing):
    _seed_acme(db)
    _configure(db, organization=organization, main_site=main_site)

    report = _report(api)

    results = report["results"]
    assert (
        results["staff_time_equivalent_hours"], results["labor_value_equivalent"], results["recurring_cost"],
        results["net_operational_value"], results["recurring_cost_per_item"],
    ) == expected
    assert report["missing"] == missing


def test_a_range_with_no_check_ins_has_zero_time_and_value_a_cost_and_no_cost_per_item(api, db):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, east=SORTER_COSTS)

    # East had nothing on 9-11 June. 8400 / 365 * 3 = 69.04.
    report = _report(api, "east", {"from": "2026-06-09", "to": "2026-06-11"})

    assert report["checkin_count"] == _site(api, "east", "overview", {"from": "2026-06-09", "to": "2026-06-11"})["checkin_count"] == 0
    assert report["results"] == {
        "in_service_days": 3,
        "in_service_checkin_count": 0,
        "staff_time_equivalent_hours": "0.00",
        "labor_value_equivalent": "0.00",
        "recurring_cost": "69.04",
        "net_operational_value": "-69.04",
        "recurring_cost_per_item": None,
    }
    assert report["missing"] == []


# =====================================================================================================================
# Who may read it
# =====================================================================================================================

@pytest.mark.parametrize("role", ["owner", "admin"])
def test_an_owner_and_an_admin_can_read_it(api, db, role):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS)
    _role(db, role)

    assert _report(api)["results"]["labor_value_equivalent"] == "11.71"


@pytest.mark.parametrize("role", ["manager", "viewer"])
def test_any_other_member_is_refused_and_learns_nothing(api, db, role):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS)
    _role(db, role)
    db.queries.clear()

    for branch, params in (
        ("main", JUNE_8_TO_12),
        ("no-such-branch", JUNE_8_TO_12),                      # not even whether the sorter exists
        ("main", {"from": "2026-06-12", "to": "2026-06-08"}),  # nor whether the range is valid
        ("main", {}),
    ):
        response = _get(api, branch, params)
        assert (response.status_code, response.json()) == (403, FORBIDDEN)
        assert response.headers["cache-control"] == "no-store"
        assert "17.56" not in response.text and "8400" not in response.text
    # No operational row was read for them.
    assert db.queries == []
    # The sorter's other reports are still theirs to read.
    assert _site(api, "main", "overview")["checkin_count"] == 30


def test_no_session_is_401(api, db, session):
    _seed_acme(db)
    session.user = None

    for headers in ({}, COOKIE):
        response = _get(api, headers=headers)
        assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert db.queries == []


def test_another_organizations_admin_gets_the_404_of_an_organization_that_does_not_exist(api, db, session):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS)
    session.user = BOB  # an admin of Beta only

    for org, branch in (("acme", "main"), ("acme", "north"), ("no-such-org", "main")):
        response = _get(api, branch, org=org)
        assert (response.status_code, response.json()) == (404, ORGANIZATION_NOT_FOUND)
    assert db.queries == []
    # Beta also has a branch called "north", with a sorter: Bob reaches his own, and only his own.
    assert _get(api, "north", org="beta").status_code == 200


def test_the_same_branch_slug_in_two_organizations_is_two_sorters(api, db, session):
    _seed_acme(db)
    db.v1("Main", 4, at=datetime(2026, 6, 9, 9, 0), customer=8202, branch=21)  # noqa: DTZ001 - a legacy local time
    db.settings(organization=2, document={"efficiency": {"labor_rate": "99.00", "manual_items_per_hour": "2.0"}})
    session.user = BOB

    beta_north = _report(api, "north", org="beta")

    # Beta's rows (the fixture's seven a day from another tenant, and these four) and Beta's rates: nothing of Acme's.
    assert beta_north["assumptions"]["labor_rate"] == {"value": "99.00", "source": "organization"}
    assert beta_north["checkin_count"] == _site_as(api, "beta", "north")
    assert beta_north["results"]["staff_time_equivalent_hours"] == f"{beta_north['checkin_count'] / 2:.2f}"


def _site_as(api, org: str, branch: str) -> int:
    response = api.get(SITE.format(org=org, branch=branch, report="overview"), headers=COOKIE, params=JUNE_8_TO_12)
    assert response.status_code == 200, response.text
    return response.json()["checkin_count"]


def test_a_branch_with_no_data_scope_is_the_other_reports_own_404(api, db):
    _seed_acme(db)

    for branch in ("unmapped", "shut", "no-such-branch"):
        efficiency = _get(api, branch)
        overview = api.get(SITE.format(org="acme", branch=branch, report="overview"), headers=COOKIE, params=JUNE_8_TO_12)
        assert (efficiency.status_code, efficiency.json()) == (overview.status_code, overview.json()) == (404, TENANT_NOT_FOUND)


def test_a_branch_that_hosts_no_sorter_has_no_efficiency_report(api, db):
    _seed_acme(db)
    db.queries.clear()

    # Westside and North are branches of Acme with a data scope and no installation: places, not sorters.
    for branch in ("westside", "north"):
        response = _get(api, branch)
        assert (response.status_code, response.json()) == (404, SORTER_NOT_FOUND)
    assert db.queries == []


def test_a_suspended_organizations_report_can_still_be_read_by_its_owner(api, db):
    _role(db, "owner", organization=5)
    db.v1("Main", 9, at=datetime(2026, 6, 9, 9, 0), customer=8505, branch=51)  # noqa: DTZ001 - a legacy local time
    db.settings(organization=5, document={"efficiency": ORG_RATES})

    report = _report(api, "main", org="paused")

    assert report["checkin_count"] == 9
    assert report["results"]["staff_time_equivalent_hours"] == "0.20"


def test_a_cancelled_organization_is_not_found(api, db):
    assert _get(api, "main", org="closed").json() == ORGANIZATION_NOT_FOUND


def test_the_route_is_guarded_by_the_settings_own_owner_or_admin_dependency_then_the_scope_then_the_range():
    route = next(r for r in main.customer_router.routes if r.path.endswith("/reports/efficiency"))

    assert route.methods == {"GET"}
    assert [dependency.call.__name__ for dependency in route.dependant.dependencies] == [
        "require_organization_admin",
        "require_resolved_tenant",
        "require_report_range",
    ]
    source = inspect.getsource(efficiency_report_routes)
    for forbidden in ("advanced_reports", "feature_enabled", "is_platform_admin", "sqlalchemy", "get_engine", "get_effective_settings", "streamlit"):
        assert forbidden not in source.split('"""', 2)[2], forbidden


# =====================================================================================================================
# The range: the other reports' own rules
# =====================================================================================================================

@pytest.mark.parametrize(
    ("params", "problem"),
    [
        ({"from": "2026-06-12", "to": "2026-06-08"}, "report_range_order"),
        ({"from": "2026-06-08", "to": "2026-06-21"}, "report_range_in_future"),
        ({"from": "2016-06-12", "to": "2026-06-20"}, "report_range_too_long"),   # 3,661 days (the guard, Reports R9D2)
        ({"from": "2026-06-31", "to": "2026-07-01"}, None),
        ({"from": "2026-06-08T00:00:00", "to": "2026-06-12"}, None),
        ({"from": "8 June", "to": "2026-06-12"}, None),
        ({"from": "2026-06-08"}, None),
        ({}, None),
    ],
)
def test_a_range_that_cannot_be_reported_on_is_the_other_reports_own_422(api, db, params, problem):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS)

    efficiency = _get(api, params=params)
    overview = api.get(SITE.format(org="acme", branch="main", report="overview"), headers=COOKIE, params=params)

    assert efficiency.status_code == overview.status_code == 422
    assert efficiency.json() == overview.json()
    if problem is not None:
        assert problem in efficiency.text
    assert "17.56" not in efficiency.text


def test_the_longest_range_and_today_are_allowed(api, db):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS)

    longest = _report(api, params={"from": "2026-03-21", "to": "2026-06-20"})   # 92 days, ending today
    # Reports R9D2: up to the 3,660-day engineering guard.
    assert _report(api, params={"from": "2016-06-13", "to": "2026-06-20"})["range"]["days"] == 3660
    today = _report(api, params={"from": "2026-06-20", "to": "2026-06-20"})

    assert longest["range"] == {"from": "2026-03-21", "to": "2026-06-20", "days": 92, "timezone": "America/Chicago", "includes_today": True}
    assert longest["results"]["in_service_days"] == 92
    assert longest["results"]["recurring_cost"] == "2117.26"   # 8400 * 92 / 365
    assert today["range"]["includes_today"] is True
    assert today["results"]["recurring_cost"] == "23.01"


def test_today_is_the_products_date(api, db, clock):
    _seed_acme(db)
    # 11:30 PM on 20 June in Chicago; already the 21st in UTC.
    clock.set(datetime(2026, 6, 21, 4, 30, tzinfo=UTC))

    assert _get(api, params={"from": "2026-06-21", "to": "2026-06-21"}).status_code == 422
    assert _get(api, params={"from": "2026-06-20", "to": "2026-06-20"}).status_code == 200


# =====================================================================================================================
# Settings that are stored but malformed
# =====================================================================================================================

@pytest.mark.parametrize(
    ("organization", "main_site"),
    [
        ({"labor_rate": "CANARY-not-a-rate"}, SORTER_COSTS),
        ({"labor_rate": 17.56}, SORTER_COSTS),
        (ORG_RATES, {"recurring_annual_cost": "CANARY-lots"}),
        (ORG_RATES, {"in_service_date": "CANARY-soon"}),
        ("CANARY not an object", SORTER_COSTS),
    ],
)
def test_malformed_stored_settings_are_a_contained_error_and_nothing_is_worked_out(api, db, caplog, organization, main_site):
    _seed_acme(db)
    _configure(db, organization=organization, main_site=main_site)
    db.queries.clear()

    with caplog.at_level(logging.ERROR):
        response = _get(api)

    assert (response.status_code, response.json()) == (500, STORED_INVALID)
    assert response.headers["cache-control"] == "no-store"
    assert "CANARY" not in response.text and "17.56" not in response.text and "Traceback" not in response.text
    assert "CANARY" not in caplog.text
    assert "Stored efficiency settings are malformed" in caplog.text
    # No operational row was read: nothing is worked out from half of a broken block.
    assert db.queries == []


def test_malformed_efficiency_settings_do_not_touch_the_sorters_other_reports(api, db):
    _seed_acme(db)
    _configure(db, organization={"labor_rate": "bad"}, main_site={"one_time_cost": 5})

    assert _get(api).json() == STORED_INVALID
    for report in ("overview", "volume", "routing", "reliability"):
        assert _site(api, "main", report)["checkin_count"] == 30


def test_a_database_failure_is_the_generic_server_error(db):
    _seed_acme(db)
    db.fail_on = "checkins"

    response = TestClient(main.app, raise_server_exceptions=False).get(
        SITE.format(org="acme", branch="main", report="efficiency"), headers=COOKIE, params=JUNE_8_TO_12
    )

    assert (response.status_code, response.json()) == (500, {"code": "internal_error", "message": "Internal server error."})


# =====================================================================================================================
# The contract
# =====================================================================================================================

def test_the_answer_has_exactly_these_fields_and_nothing_projected(api, db):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS)

    report = _report(api)

    assert set(report) == {"range", "currency", "checkin_count", "assumptions", "results", "missing"}
    assert set(report["assumptions"]) == {"manual_items_per_hour", "labor_rate", "recurring_annual_cost", "one_time_cost", "in_service_date"}
    assert set(report["results"]) == {
        "in_service_days", "in_service_checkin_count", "staff_time_equivalent_hours", "labor_value_equivalent",
        "recurring_cost", "net_operational_value", "recurring_cost_per_item",
    }
    for key in _keys(report):
        assert not re.search(r"annual(?!_cost)|payback|roi|break_even|since_install|amh|saved|useful_life|(^|_)id$", key), key
    assert "hours_saved" not in str(report)


def test_every_count_is_an_integer_and_every_other_figure_is_text(api, db):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS)

    response = _get(api)
    report = response.json()

    assert type(report["checkin_count"]) is int
    assert type(report["results"]["in_service_days"]) is int and type(report["results"]["in_service_checkin_count"]) is int
    for name in ("staff_time_equivalent_hours", "labor_value_equivalent", "recurring_cost", "net_operational_value", "recurring_cost_per_item"):
        assert type(report["results"][name]) is str
        assert re.fullmatch(r"-?[0-9]+\.[0-9]{2}([0-9]{2})?", report["results"][name])
    assert re.fullmatch(r"[0-9]+\.[0-9]{4}", report["results"]["recurring_cost_per_item"])
    # No JSON number anywhere has a decimal point or an exponent: no float was serialized.
    assert not re.search(r"[:\[,]\s*-?[0-9]+[.eE][0-9]", response.text)


def test_no_internal_or_operational_identifier_reaches_the_answer(api, db):
    _seed_acme(db)
    _configure(db, organization=ORG_RATES, main_site=SORTER_COSTS)

    response = _get(api)

    for forbidden in (*EVERY_OPERATIONAL_ID, "CANARY", "customer_id", "branch_id", "organization_id", "user_id", "security", "transit", "Westside"):
        assert forbidden not in response.text.replace("118003.92", "")


def test_the_schemas_hold_no_number_but_counts():
    schemas = efficiency_report_schemas
    annotations = {
        name: str(field.annotation)
        for model in vars(schemas).values()
        if hasattr(model, "model_fields") and model.__module__ == schemas.__name__
        for name, field in model.model_fields.items()
    }

    assert {name for name, annotation in annotations.items() if "int" in annotation} == {"checkin_count", "in_service_days", "in_service_checkin_count"}
    assert not any("float" in annotation or "Decimal" in annotation for annotation in annotations.values())


def test_the_route_reuses_the_existing_pieces_and_counts_nothing_itself():
    source = inspect.getsource(efficiency_report_routes).split('"""', 2)[2]

    for reused in (
        "from customer_api.report_routes import Range",
        "from customer_api.operational_routes import ResolvedTenant",
        "open_customer_tenant_connection(tenant)",
        "get_checkin_counts_by_day(conn, tenant, report_window(conn, tenant, requested.local_range))",
        "efficiency_settings_service.read_sorter_efficiency(org_slug, branch_slug, user_id=user[\"id\"])",
        "resolve_efficiency_settings(stored.organization, stored.sorter)",
        "calculate_efficiency(requested.local_range.dates, checkin_days, effective)",
    ):
        assert reused in source, reused
    for forbidden in ("validate_report_range", "datetime", "SELECT", "sum(", " / ", " * ", "quantize"):
        assert forbidden not in source, forbidden
