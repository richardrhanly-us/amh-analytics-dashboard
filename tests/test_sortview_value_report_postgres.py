"""Reports R10F: the SortView Value report on a REAL PostgreSQL -- the migrated schema, real JSONB.

The requests go through the real production route (TestClient(main.app)) and nothing between a request and the rows is
replaced except the session lookup: the real access and entitlement services and the real
services.sortview_value_service run against the database, and the real arithmetic (services.sortview_value) runs on
what they read.

What only a real server can prove:
  * a block stored by the settings route is what the report is worked out from, to the cent;
  * the report reaches a row only through the acting user's own organization, by the settings service's own SQL;
  * a suspended organization's report can be read and a cancelled one's cannot;
  * a stored SortView Value block that cannot be read is the contained error, and a stored Efficiency block that
    cannot be read only leaves no rate to fall back on;
  * reading the report writes nothing.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_sortview_value_settings_postgres.py: runs only
when SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION, local server. The module creates
its own throwaway database (migrated with the project's real Alembic chain) and drops it afterward. Production is
never touched.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import main
from services import (
    access_service,
    entitlement_service,
    session_service,
    sortview_value_service,
)

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL SortView Value report tests)"
)

ORIGIN = "https://app.example.invalid"
COOKIE = "__Host-sortview_api_session"
REPORT = "/api/organizations/{org}/reports/sortview-value"
SETTINGS = "/api/organizations/{org}/settings/sortview-value"

# Database ids. Distinctive, so none can be in a response by accident; never sent in a request.
ACME, BETA, PAUSED, CLOSED = 7121, 7222, 7323, 7424
OWNER, ADMIN, VIEWER, BETA_OWNER, FORMER_ADMIN = 9121, 9122, 9123, 9221, 9124
ACME_MAIN = 5121

CANARY_HASH = "CANARY-admin-lock-hash-83f1"
ACME_EFFICIENCY = {"labor_rate": "20.00", "manual_items_per_hour": "45.0"}
ACME_DOCUMENT = {
    "library_name": "Acme Library",
    "security": {"admin_enabled": True, "admin_password_hash": CANARY_HASH},
    "efficiency": ACME_EFFICIENCY,
}
# A sorter site's own Efficiency override. It is never a SortView Value rate.
MAIN_DOCUMENT = {"branch_name": "Main", "efficiency": {"labor_rate": "99.00"}}

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
# What BLOCK comes to: 18.333... hours a month at 32.50, against 4,800.00 a year.
TOTALS = {
    "recovered_hours_per_month": "18.33",
    "monthly_value": "595.83",
    "annual_value": "7150.00",
    "net_annual_value": "2350.00",
    "roi_percent": "49.0",
    "payback_months": "8.1",
}
ORGANIZATION_NOT_FOUND = {"code": "organization_not_found", "message": "Organization not found."}
STORED_INVALID = {"code": "sortview_value_settings_invalid", "message": "The stored SortView Value settings could not be read."}


def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


@pytest.fixture(scope="module")
def engine():
    admin = make_url(ADMIN_URL)
    _guard(admin)
    name = f"sortview_value_report_test_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    url = admin.set(database=name)
    database = create_engine(url, hide_parameters=True)
    try:
        migrated = subprocess.run(  # nosec B603
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        yield database
    finally:
        database.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


def _seed(conn) -> None:
    conn.execute(text(
        "TRUNCATE organization_settings, branch_settings, collector_installations, memberships, branches, app_users, "
        "organizations RESTART IDENTITY CASCADE"
    ))
    for org_id, slug, status in ((ACME, "acme", "active"), (BETA, "beta", "trial"), (PAUSED, "paused", "suspended"), (CLOSED, "closed", "cancelled")):
        conn.execute(
            text("INSERT INTO organizations (id, slug, name, status) VALUES (:id, :slug, :name, :status)"),
            {"id": org_id, "slug": slug, "name": f"{slug.title()} Library", "status": status},
        )
    for user_id in (OWNER, ADMIN, VIEWER, BETA_OWNER, FORMER_ADMIN):
        conn.execute(text("INSERT INTO app_users (id, email) VALUES (:id, :email)"), {"id": user_id, "email": f"user{user_id}@example.invalid"})
    for org_id, user_id, role in (
        (ACME, OWNER, "owner"), (ACME, ADMIN, "admin"), (ACME, VIEWER, "viewer"), (ACME, FORMER_ADMIN, "admin"), (BETA, BETA_OWNER, "owner"),
        # OWNER also owns the suspended and the cancelled organization.
        (PAUSED, OWNER, "owner"), (CLOSED, OWNER, "owner"),
    ):
        conn.execute(text("INSERT INTO memberships (organization_id, user_id, role) VALUES (:org, :user, :role)"), {"org": org_id, "user": user_id, "role": role})
    # FORMER_ADMIN was an admin of Acme and was removed.
    conn.execute(text("UPDATE memberships SET removed_at = NOW() WHERE organization_id = :org AND user_id = :user"), {"org": ACME, "user": FORMER_ADMIN})
    conn.execute(
        text("INSERT INTO branches (id, organization_id, slug, name, status) VALUES (:id, :org, 'main', 'Main', 'active')"),
        {"id": ACME_MAIN, "org": ACME},
    )
    conn.execute(
        text("INSERT INTO collector_installations (organization_id, branch_id, name, status) VALUES (:org, :branch, 'AMH', 'active')"),
        {"org": ACME, "branch": ACME_MAIN},
    )
    conn.execute(text("INSERT INTO organization_settings (organization_id, settings_json) VALUES (:org, CAST(:doc AS JSONB))"), {"org": ACME, "doc": json.dumps(ACME_DOCUMENT)})
    conn.execute(text("INSERT INTO branch_settings (branch_id, settings_json) VALUES (:branch, CAST(:doc AS JSONB))"), {"branch": ACME_MAIN, "doc": json.dumps(MAIN_DOCUMENT)})
    # The suspended and the cancelled organization each had a block stored while they could be written to.
    for org_id in (PAUSED, CLOSED):
        conn.execute(
            text("INSERT INTO organization_settings (organization_id, settings_json) VALUES (:org, CAST(:doc AS JSONB))"),
            {"org": org_id, "doc": json.dumps({"sortview_value": BLOCK})},
        )


@pytest.fixture
def db(engine, monkeypatch):
    with engine.begin() as conn:
        _seed(conn)
    # Every service a request here can reach reads the same throwaway database.
    for module in (access_service, entitlement_service, sortview_value_service):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setattr(
        session_service,
        "validate_session",
        lambda raw_token: {"id": int(raw_token), "email": f"user{raw_token}@example.invalid", "full_name": "Test User"} if raw_token.isdigit() else None,
    )
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", ORIGIN)
    return engine


@pytest.fixture
def api(db):
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


def _report(api, org, user=OWNER):
    return api.get(REPORT.format(org=org), headers={"Cookie": f"{COOKIE}={user}"})


def _put_settings(api, org, body, user=OWNER):
    return api.put(SETTINGS.format(org=org), json=body, headers={"Cookie": f"{COOKIE}={user}", "Origin": ORIGIN})


def _rows(engine) -> dict[str, list]:
    """Every settings row there is, of both kinds: what reading a report must leave exactly as it was."""
    with engine.connect() as conn:
        return {
            "organization_settings": [tuple(row) for row in conn.execute(text("SELECT organization_id, settings_json, updated_at FROM organization_settings ORDER BY id"))],
            "branch_settings": [tuple(row) for row in conn.execute(text("SELECT branch_id, settings_json, updated_at FROM branch_settings ORDER BY id"))],
        }


def _set_key(engine, org_id: int, key: str, value: object) -> None:
    """Puts `value` under one key of a stored organization document, as another writer would."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE organization_settings SET settings_json = jsonb_set(settings_json, CAST(:path AS TEXT[]), CAST(:value AS JSONB), true) WHERE organization_id = :org"),
            {"org": org_id, "path": "{" + key + "}", "value": json.dumps(value)},
        )


# =====================================================================================================================
# A stored block, and its report
# =====================================================================================================================

def test_a_block_stored_by_the_settings_route_is_what_the_report_is_worked_out_from(api, db):
    stored = _put_settings(api, "acme", BLOCK)
    assert stored.status_code == 200
    before = _rows(db)

    response = _report(api, "acme")

    assert response.status_code == 200
    body = response.json()
    assert body["currency"] == "USD"
    # What was entered is shown exactly as the settings route answers it.
    assert body["assumptions"] == stored.json()["sortview_value"]
    assert body["assumptions"]["labor_rate"] == {"sortview_value": "32.50", "organization_efficiency": "20.00", "effective": "32.50", "source": "sortview_value"}
    assert body["results"]["reporting"] == {
        "baseline_hours_per_month": "18.00",
        "current_hours_per_month": "3.00",
        "recovered_hours_per_month": "15.00",
        "monthly_value": "487.50",
        "annual_value": "5850.00",
    }
    assert body["results"]["troubleshooting"] == {
        "baseline_hours_per_month": "6.00",
        "current_hours_per_month": "2.67",
        "recovered_hours_per_month": "3.33",
        "monthly_value": "108.33",
        "annual_value": "1300.00",
    }
    assert {name: body["results"][name] for name in TOTALS} == TOTALS
    assert body["missing"] == []
    assert CANARY_HASH not in response.text
    # Reading it wrote nothing, to either kind of settings row.
    assert _rows(db) == before


def test_an_organization_with_no_block_and_one_with_no_row_are_reports_of_nulls(api, db):
    before = _rows(db)

    with_row, without_row = _report(api, "acme"), _report(api, "beta", BETA_OWNER)

    assert (with_row.status_code, without_row.status_code) == (200, 200)
    # Acme has an Efficiency default to fall back on; Beta has nothing at all.
    assert with_row.json()["assumptions"]["labor_rate"]["source"] == "organization_efficiency"
    assert with_row.json()["missing"] == [
        "subscription_annual_cost",
        "reporting.tasks_per_month", "reporting.baseline_minutes_per_task", "reporting.current_minutes_per_task",
        "troubleshooting.incidents_per_month", "troubleshooting.staff_per_incident", "troubleshooting.baseline_minutes", "troubleshooting.current_minutes",
    ]
    assert without_row.json()["missing"][0] == "labor_rate" and len(without_row.json()["missing"]) == 9
    for body in (with_row.json(), without_row.json()):
        assert all(value is None for value in body["results"]["reporting"].values())
        assert all(body["results"][name] is None for name in TOTALS)
    # No row was created for the organization that had none.
    assert _rows(db) == before


def test_the_rate_falls_back_on_the_organizations_efficiency_default_and_never_on_a_sorters_override(api, db):
    _put_settings(api, "acme", {key: value for key, value in BLOCK.items() if key != "labor_rate"})

    body = _report(api, "acme").json()

    # Acme's default is 20.00 an hour; its one sorter site's own 99.00 is not the organization's.
    assert body["assumptions"]["labor_rate"] == {"sortview_value": None, "organization_efficiency": "20.00", "effective": "20.00", "source": "organization_efficiency"}
    assert body["results"]["monthly_value"] == "366.67"       # 18.333... hours at 20.00
    assert body["results"]["annual_value"] == "4400.00"
    assert body["results"]["net_annual_value"] == "-400.00"
    assert body["results"]["roi_percent"] == "-8.3"
    assert "99.00" not in json.dumps(body)


def test_changing_the_stored_settings_changes_the_next_report(api, db):
    _put_settings(api, "acme", BLOCK)
    assert _report(api, "acme").json()["results"]["roi_percent"] == "49.0"

    _put_settings(api, "acme", {**BLOCK, "subscription_annual_cost": "0"})
    zero_cost = _report(api, "acme").json()["results"]
    assert (zero_cost["net_annual_value"], zero_cost["roi_percent"], zero_cost["payback_months"]) == ("7150.00", None, "0.0")

    _put_settings(api, "acme", {**BLOCK, "reporting": {**REPORTING, "current_minutes_per_task": "120"}})
    slower = _report(api, "acme").json()["results"]
    assert (slower["recovered_hours_per_month"], slower["monthly_value"], slower["roi_percent"], slower["payback_months"]) == ("-2.67", "-86.67", "-121.7", None)

    _put_settings(api, "acme", {})
    assert _report(api, "acme").json()["results"]["monthly_value"] is None


# =====================================================================================================================
# Whose report
# =====================================================================================================================

def test_the_report_is_reached_only_through_the_acting_users_own_organization(api, db):
    _put_settings(api, "acme", BLOCK)
    before = _rows(db)

    # Another organization's owner, and an address that names no organization: not found, and nothing of Acme's.
    for user, org in ((BETA_OWNER, "acme"), (OWNER, "beta"), (OWNER, "nowhere")):
        response = _report(api, org, user)
        assert (response.status_code, response.json()) == (404, ORGANIZATION_NOT_FOUND)
        assert "32.50" not in response.text and "595.83" not in response.text
    # A viewer is a member, and is refused; a removed admin is not a member at all.
    assert _report(api, "acme", VIEWER).status_code == 403
    assert _report(api, "acme", FORMER_ADMIN).status_code == 404
    assert "595.83" not in _report(api, "acme", VIEWER).text
    # While its own owner and admin are answered the same report.
    assert _report(api, "acme", OWNER).json() == _report(api, "acme", ADMIN).json()
    assert _report(api, "acme", ADMIN).json()["results"]["monthly_value"] == "595.83"

    assert _rows(db) == before


def test_a_suspended_organizations_report_can_be_read_and_a_cancelled_ones_cannot(api, db):
    before = _rows(db)

    suspended, cancelled = _report(api, "paused"), _report(api, "closed")

    assert suspended.status_code == 200
    assert {name: suspended.json()["results"][name] for name in TOTALS} == TOTALS
    assert suspended.json()["assumptions"]["subscription_annual_cost"] == "4800.00"
    # The cancelled organization has the same block stored, and none of it is answered.
    assert (cancelled.status_code, cancelled.json()) == (404, ORGANIZATION_NOT_FOUND)
    assert "4800" not in cancelled.text
    assert _rows(db) == before


def test_no_database_id_reaches_any_answer(api, db):
    _put_settings(api, "acme", BLOCK)

    for response in (_report(api, "acme"), _report(api, "acme", ADMIN), _report(api, "acme", VIEWER), _report(api, "beta", BETA_OWNER), _report(api, "paused")):
        for identifier in (ACME, BETA, PAUSED, OWNER, ADMIN, VIEWER, BETA_OWNER, ACME_MAIN):
            assert str(identifier) not in response.text


# =====================================================================================================================
# What is stored but malformed
# =====================================================================================================================

@pytest.mark.parametrize(
    "stored",
    [
        {"labor_rate": 32.5},
        {"reporting": "not an object"},
        # A JSON string, which the database hands back as text -- one whose text is itself a valid block included.
        "not an object",
        json.dumps(BLOCK),
    ],
)
def test_a_stored_value_block_that_cannot_be_read_is_the_contained_error_and_nothing_is_worked_out(api, db, stored):
    _set_key(db, ACME, "sortview_value", stored)
    before = _rows(db)

    response = _report(api, "acme")

    assert (response.status_code, response.json()) == (500, STORED_INVALID)
    assert "JSONDecodeError" not in response.text and "results" not in response.text and "32.5" not in response.text
    assert _rows(db) == before
    # A PUT of a block that can be read repairs it, and then there is a report.
    assert _put_settings(api, "acme", BLOCK).status_code == 200
    assert _report(api, "acme").json()["results"]["monthly_value"] == "595.83"


@pytest.mark.parametrize("efficiency", [{"labor_rate": 20.0}, {"labor_rate": "not a rate"}, "not an object", '{"labor_rate": "20.00"}', ["labor_rate"]])
def test_a_stored_efficiency_block_that_cannot_be_read_leaves_a_report_with_no_rate_to_fall_back_on(api, db, efficiency):
    _put_settings(api, "acme", {key: value for key, value in BLOCK.items() if key != "labor_rate"})
    _set_key(db, ACME, "efficiency", efficiency)

    response = _report(api, "acme")

    assert response.status_code == 200
    body = response.json()
    assert body["assumptions"]["labor_rate"] == {"sortview_value": None, "organization_efficiency": None, "effective": None, "source": None}
    # The hours need no rate; every value does.
    assert body["results"]["recovered_hours_per_month"] == "18.33"
    assert (body["results"]["monthly_value"], body["results"]["annual_value"], body["results"]["net_annual_value"]) == (None, None, None)
    assert body["missing"] == ["labor_rate"]
    assert "20.00" not in response.text

    # With a rate of its own, the same organization has every figure, the unreadable block notwithstanding.
    _put_settings(api, "acme", BLOCK)
    assert {name: _report(api, "acme").json()["results"][name] for name in TOTALS} == TOTALS
