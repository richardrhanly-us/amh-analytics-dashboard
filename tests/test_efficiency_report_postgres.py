"""Reports R6C: a sorter's Efficiency report on a REAL PostgreSQL -- the migrated schema, real JSONB settings, real
TIMESTAMP / TIMESTAMPTZ operational rows, real row level security and a real NON-OWNING runtime role.

The requests go through the real production route (TestClient(main.app)). Only the session lookup is replaced:
membership, role, tenant resolution, the verified tenant connection, the report window, the per-day counts, the
settings read and the arithmetic all run, as the runtime role, against the database.

What only a real server can prove:
  * the report is put together from real settings rows and real operational rows, across a v1 -> v2 cutover;
  * its check-ins are the sorter's other reports' own, day for day;
  * a role provisioned from the privilege baseline, bound by row level security, can read everything the report
    needs and nothing of another tenant's;
  * the same branch slug in two organizations is two sorters with two sets of figures.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_rls_phase1_postgres.py: runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION, local server. The module creates its
own throwaway database (migrated with the project's real Alembic chain) and its own throwaway runtime role, and drops
both afterward. Production is never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from controlled_clock import ControlledClock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import database
import main
from customer_api import report_routes
from scripts import runtime_role_privileges as privileges
from services import session_service

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL efficiency report tests)"
)

REPORT = "/api/organizations/{org}/branches/{branch}/reports/{report}"
COOKIE = "__Host-sortview_api_session"
JUNE_8_TO_12 = {"from": "2026-06-08", "to": "2026-06-12"}

# Operational (customer, branch) ids, and user ids. Distinctive: none may reach an answer.
ACME, ACME_MAIN, ACME_ANNEX = 7110, 7111, 7113
BETA, BETA_MAIN = 7220, 7222
OWNER, VIEWER, BETA_OWNER = 9101, 9103, 9201
KEY_ACME = "5e7d1c2a-8b3f-4a6e-9c1d-2f4b6a8c0e13"  # a v4 UUID, the form the schema requires of a key id

RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only

# 1 PM on Saturday 20 June 2026 in America/Chicago: the product's "today" for these tests.
CLOCK = ControlledClock(datetime(2026, 6, 20, 18, 0, tzinfo=UTC))

ACME_ORG_DOCUMENT = {
    "security": {"admin_password_hash": "CANARY-hash"},
    "transit": {"home_branch_label": "Main", "destinations": [{"key": "b1", "label": "Westside"}]},
    "efficiency": {"labor_rate": "17.56", "manual_items_per_hour": "45.0"},
}
# The sorter overrides the labor rate, and went into service on the day it cut over.
ACME_MAIN_DOCUMENT = {
    "branch_name": "Main",
    "efficiency": {"labor_rate": "20.00", "one_time_cost": "118003.92", "recurring_annual_cost": "8400.00", "in_service_date": "2026-06-10"},
}
BETA_ORG_DOCUMENT = {"efficiency": {"labor_rate": "99.00", "manual_items_per_hour": "2.0"}}


def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


@pytest.fixture(scope="module")
def pg_url():
    admin = make_url(ADMIN_URL)
    _guard(admin)
    name = f"sortview_efficiency_report_test_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    url = admin.set(database=name)
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
        yield url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


@pytest.fixture(scope="module")
def owner(pg_url):
    engine = create_engine(pg_url, hide_parameters=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def runtime(pg_url, owner):
    """A non-owning role provisioned from the privilege baseline's OWN provisioning SQL: exactly what a fresh
    environment's application role is given."""
    name = f"sortview_rt_test_{secrets.token_hex(4)}"
    with owner.begin() as conn:
        conn.execute(text(
            f"CREATE ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT "  # nosec B608
            f"PASSWORD '{RUNTIME_ROLE_PASSWORD}'"
        ))
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{pg_url.database}" TO {name}'))  # nosec B608
        for line in privileges.provisioning_sql(name).splitlines():
            if line and not line.startswith("--") and line not in ("BEGIN;", "COMMIT;"):
                conn.execute(text(line))
    engine = create_engine(pg_url.set(username=name, password=RUNTIME_ROLE_PASSWORD), hide_parameters=True)
    yield engine
    engine.dispose()
    with owner.begin() as conn:
        conn.execute(text(f"DROP OWNED BY {name}"))  # nosec B608
        conn.execute(text(f"DROP ROLE IF EXISTS {name}"))  # nosec B608


def _v1(conn, customer: int, branch: int, local_wall_clock: str, count: int) -> None:
    """Legacy rows. `local_wall_clock` has no offset: it is stored as given."""
    for number in range(count):
        conn.execute(
            text("INSERT INTO checkins (customer_id, branch_id, event_time, title, barcode, destination, bin, source_file) "
                 "VALUES (:c, :b, CAST(:t AS timestamp), 'CANARY-title', :barcode, 'Main', 'bin1', 'efficiency_test.csv')"),
            {"c": customer, "b": branch, "t": local_wall_clock, "barcode": f"CANARY-{local_wall_clock}-{number}"},
        )


def _v2(conn, customer: int, branch: int, instant: str, count: int) -> None:
    """Contract v2 rows. `instant` carries an explicit offset."""
    for number in range(count):
        event_key = hashlib.sha256(f"{customer}:{branch}:{instant}:{number}".encode()).hexdigest()
        conn.execute(
            text("INSERT INTO checkin_events (customer_id, branch_id, key_id, event_key, event_time, destination, bin) "
                 "VALUES (:c, :b, :k, :ek, CAST(:t AS timestamptz), 'main', 'unknown')"),
            {"c": customer, "b": branch, "k": KEY_ACME, "ek": event_key, "t": instant},
        )


def _seed(conn) -> None:
    """Acme's main sorter, 8-12 June 2026, cut over at noon (Chicago) on the 10th:

        8 June   legacy   4
        9 June   legacy   3
        10 June  legacy   2 (09:00)  +  current 3 (at the cutover exactly)      = 5
                 -- 50 legacy rows after the cutover and 7 current rows before it count for nothing
        11 June  current  5
        12 June  nothing
                                                                    by day  4, 3, 5, 5, 0  = 17

    Beta's sorter is at a branch that is also called "main": 9 legacy check-ins on 8 June.
    """
    conn.execute(text(
        "TRUNCATE checkin_events, reject_events, acs_item_events, ingest_key_ids, v2_cutovers, checkins, rejects, acs_events, "
        "checkins_clean, rejects_clean, organization_settings, branch_settings, collector_installations, memberships, "
        "app_users, branches, organizations, customers RESTART IDENTITY CASCADE"
    ))
    for org_id, slug, customer in ((1, "acme", ACME), (2, "beta", BETA)):
        conn.execute(text("INSERT INTO customers (id, name) VALUES (:c, :n)"), {"c": customer, "n": slug})
        conn.execute(
            text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES (:o, :s, :s, 'active', :c)"),
            {"o": org_id, "s": slug, "c": customer},
        )
    for branch, org_id, slug in ((ACME_MAIN, 1, "main"), (ACME_ANNEX, 1, "annex"), (BETA_MAIN, 2, "main")):
        conn.execute(
            text("INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) VALUES (:b, :o, :s, :s, 'active', :b)"),
            {"b": branch, "o": org_id, "s": slug},
        )
    for user in (OWNER, VIEWER, BETA_OWNER):
        conn.execute(text("INSERT INTO app_users (id, email, is_active) VALUES (:u, :e, TRUE)"), {"u": user, "e": f"user{user}@example.invalid"})
    for org_id, user, role in ((1, OWNER, "owner"), (1, VIEWER, "viewer"), (2, BETA_OWNER, "owner")):
        conn.execute(text("INSERT INTO memberships (organization_id, user_id, role) VALUES (:o, :u, :r)"), {"o": org_id, "u": user, "r": role})
    # The annex is a branch with a data scope and no sorter.
    for org_id, branch in ((1, ACME_MAIN), (2, BETA_MAIN)):
        conn.execute(
            text("INSERT INTO collector_installations (organization_id, branch_id, name, status) VALUES (:o, :b, 'AMH', 'active')"),
            {"o": org_id, "b": branch},
        )
    for table, key, owner_id, document in (
        ("organization_settings", "organization_id", 1, ACME_ORG_DOCUMENT),
        ("branch_settings", "branch_id", ACME_MAIN, ACME_MAIN_DOCUMENT),
        ("organization_settings", "organization_id", 2, BETA_ORG_DOCUMENT),
    ):
        conn.execute(text(f"INSERT INTO {table} ({key}, settings_json) VALUES (:id, CAST(:doc AS JSONB))"), {"id": owner_id, "doc": json.dumps(document)})  # nosec B608

    conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id) VALUES (:k, :c, :b)"), {"k": KEY_ACME, "c": ACME, "b": ACME_MAIN})
    conn.execute(
        text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by) VALUES (:c, :b, CAST('2026-06-10T12:00:00-05:00' AS timestamptz), 'efficiency-test')"),
        {"c": ACME, "b": ACME_MAIN},
    )
    _v1(conn, ACME, ACME_MAIN, "2026-06-08 09:00:00", 4)
    _v1(conn, ACME, ACME_MAIN, "2026-06-09 09:00:00", 3)
    _v1(conn, ACME, ACME_MAIN, "2026-06-10 09:00:00", 2)
    _v1(conn, ACME, ACME_MAIN, "2026-06-10 13:00:00", 50)             # legacy rows after the cutover: not counted
    _v2(conn, ACME, ACME_MAIN, "2026-06-10T16:59:00+00:00", 7)        # current rows before it: not counted
    _v2(conn, ACME, ACME_MAIN, "2026-06-10T17:00:00+00:00", 3)        # exactly at the cutover: counted
    _v2(conn, ACME, ACME_MAIN, "2026-06-11T15:00:00+00:00", 5)
    _v1(conn, BETA, BETA_MAIN, "2026-06-08 09:00:00", 9)
    _v1(conn, ACME, ACME_ANNEX, "2026-06-08 09:00:00", 6)


@pytest.fixture
def api(owner, runtime, monkeypatch):
    with owner.begin() as conn:
        _seed(conn)
    # The one flat database engine every service uses: the runtime role's.
    monkeypatch.setattr(database, "_engine", runtime)
    monkeypatch.setattr(
        session_service,
        "validate_session",
        lambda raw_token: {"id": int(raw_token), "email": f"user{raw_token}@example.invalid", "full_name": "Test User"} if raw_token.isdigit() else None,
    )
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    CLOCK.reset()
    main.limiter.reset()
    with CLOCK.controlling(report_routes):
        yield TestClient(main.app, raise_server_exceptions=False)
    main.limiter.reset()


def _get(api, org: str, branch: str, report: str = "efficiency", user: int = OWNER, params=JUNE_8_TO_12):
    return api.get(REPORT.format(org=org, branch=branch, report=report), headers={"Cookie": f"{COOKIE}={user}"}, params=params)


def _ok(api, org: str, branch: str, report: str = "efficiency", user: int = OWNER, params=JUNE_8_TO_12) -> dict:
    response = _get(api, org, branch, report, user, params)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_runtime_role_is_bound_by_row_level_security(api, runtime):
    with runtime.connect() as conn:
        attributes = conn.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()
        # With no tenant context set, the role sees no operational row at all.
        visible = conn.execute(text("SELECT (SELECT count(*) FROM checkins) + (SELECT count(*) FROM checkin_events)")).scalar()

    assert tuple(attributes) == (False, False)
    assert visible == 0


def test_the_report_from_real_settings_rows_and_real_rows_across_a_cutover(api):
    report = _ok(api, "acme", "main")

    # In service from 10 June: 3 days, 5 + 5 + 0 check-ins. 10 / 45 = 0.22 h; x 20.00 = 4.44; 8400 / 365 * 3 = 69.04;
    # 69.0410... / 10 = 6.9041.
    assert report == {
        "range": {"from": "2026-06-08", "to": "2026-06-12", "days": 5, "timezone": "America/Chicago", "includes_today": False},
        "currency": "USD",
        "checkin_count": 17,
        "assumptions": {
            "manual_items_per_hour": {"value": "45.0", "source": "organization"},
            "labor_rate": {"value": "20.00", "source": "sorter"},
            "recurring_annual_cost": "8400.00",
            "one_time_cost": "118003.92",
            "in_service_date": "2026-06-10",
        },
        "results": {
            "in_service_days": 3,
            "in_service_checkin_count": 10,
            "staff_time_equivalent_hours": "0.22",
            "labor_value_equivalent": "4.44",
            "recurring_cost": "69.04",
            "net_operational_value": "-64.60",
            "recurring_cost_per_item": "6.9041",
        },
        "missing": [],
    }


def test_the_check_ins_are_the_other_reports_own_day_for_day(api):
    efficiency = _ok(api, "acme", "main")
    overview, volume = _ok(api, "acme", "main", "overview"), _ok(api, "acme", "main", "volume")

    assert [day["checkin_count"] for day in volume["days"]] == [4, 3, 5, 5, 0]
    assert efficiency["checkin_count"] == overview["checkin_count"] == volume["checkin_count"] == 17
    assert efficiency["results"]["in_service_checkin_count"] == sum(day["checkin_count"] for day in volume["days"] if day["date"] >= "2026-06-10")
    assert efficiency["range"] == overview["range"]
    # Day by day, and across the cutover instant.
    for params, expected in (
        ({"from": "2026-06-10", "to": "2026-06-10"}, 5),
        ({"from": "2026-06-08", "to": "2026-06-09"}, 7),
        ({"from": "2026-06-11", "to": "2026-06-12"}, 5),
    ):
        assert _ok(api, "acme", "main", params=params)["checkin_count"] == _ok(api, "acme", "main", "overview", params=params)["checkin_count"] == expected


def test_before_the_in_service_date_there_is_nothing_counted_and_nothing_charged(api):
    report = _ok(api, "acme", "main", params={"from": "2026-06-08", "to": "2026-06-09"})

    assert report["checkin_count"] == 7
    assert (report["results"]["in_service_days"], report["results"]["in_service_checkin_count"]) == (0, 0)
    assert (report["results"]["recurring_cost"], report["results"]["recurring_cost_per_item"]) == ("0.00", None)


def test_the_same_branch_slug_in_two_organizations_is_two_sorters_with_two_sets_of_figures(api):
    beta = _ok(api, "beta", "main", user=BETA_OWNER)

    # Beta's nine check-ins and Beta's rates. 9 / 2 = 4.50 h; x 99.00 = 445.50. No cost configured.
    assert beta["checkin_count"] == _ok(api, "beta", "main", "overview", BETA_OWNER)["checkin_count"] == 9
    assert beta["assumptions"] == {
        "manual_items_per_hour": {"value": "2.0", "source": "organization"},
        "labor_rate": {"value": "99.00", "source": "organization"},
        "recurring_annual_cost": None,
        "one_time_cost": None,
        "in_service_date": None,
    }
    assert beta["results"] == {
        "in_service_days": 5,
        "in_service_checkin_count": 9,
        "staff_time_equivalent_hours": "4.50",
        "labor_value_equivalent": "445.50",
        "recurring_cost": None,
        "net_operational_value": None,
        "recurring_cost_per_item": None,
    }
    assert beta["missing"] == ["recurring_annual_cost", "in_service_date"]
    assert _ok(api, "acme", "main")["checkin_count"] == 17


def test_neither_owner_can_read_the_others_report(api):
    for org, user in (("beta", OWNER), ("acme", BETA_OWNER)):
        response = _get(api, org, "main", user=user)
        assert (response.status_code, response.json()["code"]) == (404, "organization_not_found")
        assert "17.56" not in response.text and "99.00" not in response.text


def test_a_viewer_is_refused_and_can_still_read_the_other_reports(api):
    refused = _get(api, "acme", "main", user=VIEWER)

    assert (refused.status_code, refused.json()["code"]) == (403, "forbidden")
    assert "8400" not in refused.text
    assert _ok(api, "acme", "main", "overview", VIEWER)["checkin_count"] == 17


def test_a_branch_that_hosts_no_sorter_has_no_efficiency_report(api):
    response = _get(api, "acme", "annex")

    assert (response.status_code, response.json()["code"]) == (404, "sorter_not_found")
    # It is a real branch with a data scope and rows of its own: its other reports answer.
    assert _ok(api, "acme", "annex", "overview")["checkin_count"] == 6


def test_malformed_stored_settings_are_a_contained_error_and_leave_the_other_reports_alone(api, owner):
    with owner.begin() as conn:
        conn.execute(text(
            "UPDATE organization_settings SET settings_json = jsonb_set(settings_json, '{efficiency,labor_rate}', '17.56') WHERE organization_id = 1"
        ))

    response = _get(api, "acme", "main")

    assert (response.status_code, response.json()) == (500, {"code": "efficiency_settings_invalid", "message": "The stored efficiency settings could not be read."})
    assert "17.56" not in response.text
    assert _ok(api, "acme", "main", "overview")["checkin_count"] == 17


def test_no_identifier_or_other_setting_reaches_the_answer(api):
    text_ = _get(api, "acme", "main").text.replace("118003.92", "")

    for forbidden in (str(ACME), str(ACME_MAIN), str(OWNER), "CANARY", "security", "transit", "Westside"):
        assert forbidden not in text_
