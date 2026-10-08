"""R8K: a sorter site's holds report on a REAL PostgreSQL -- the migrated schema, real legacy ACS rows and real
Contract v2 hold rows across a cutover, real JSONB settings, real plans, real row level security and a real NON-OWNING
runtime role.

The requests go through the real production route (TestClient(main.app)). Only the session lookup is replaced:
membership, the plan's features, tenant resolution, the verified tenant connection, the report window, the three
statements of services.hold_report_service and both dashboard classifiers all run, as the runtime role.

What only a real server can prove:

  * the legacy rows are read as TIMESTAMP before the cutover and the current rows as TIMESTAMPTZ from it, and a row
    on the wrong side of the cutover is counted by neither era;
  * the legacy rules are the organization's `internal_routing` with the site's own merged over it, read from real
    JSONB, and reach only the legacy holds;
  * a role provisioned from the privilege baseline, bound by row level security, can read every row the report
    needs and nothing of another tenant's;
  * the plan's `internal_workflow` feature is what lets an organization have the report.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_efficiency_report_postgres.py: runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION, local server. The module creates its
own throwaway database (migrated with the project's real Alembic chain) and its own throwaway runtime role, and drops
both afterward. Production is never touched. Every name and account here is synthetic.
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

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL holds report tests)")

HOLDS = "/api/organizations/{org}/branches/{branch}/reports/holds"
COOKIE = "__Host-sortview_api_session"
JUNE_8_TO_12 = {"from": "2026-06-08", "to": "2026-06-12"}

ACME, ACME_MAIN = 7310, 7311
BETA, BETA_MAIN = 7420, 7422
GAMMA, GAMMA_MAIN = 7530, 7533
OWNER, VIEWER, BETA_OWNER, GAMMA_OWNER = 9301, 9303, 9401, 9501
KEY_ACME = "6f8e2d3b-9c4a-4b7f-8d2e-3a5c7b9d1f24"
RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only

# 1 PM on Saturday 20 June 2026 in America/Chicago: the product's "today" for these tests.
CLOCK = ControlledClock(datetime(2026, 6, 20, 18, 0, tzinfo=UTC))

ACCOUNT = "EXAMPLE (ST)STAFF ACCOUNT A"
DEPARTMENT = "EXAMPLE TS-CATALOGING"
ORGANIZATION_RULES = {"branch_services_names": [ACCOUNT], "collection_services_names": [DEPARTMENT],
                      "branch_services_da_patterns": [], "collection_services_da_patterns": []}


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
    name = f"sortview_holds_report_test_{secrets.token_hex(4)}"
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
    """A non-owning role provisioned from the privilege baseline's OWN provisioning SQL."""
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


def _key(*parts) -> str:
    return hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()


def _v1_hold(conn, customer, branch, local_wall_clock, barcode, patron="", hold=True):
    """A legacy item record. `local_wall_clock` has no offset: it is stored as given."""
    prefix = "101YNY" if hold else "101YNN"
    conn.execute(
        text("INSERT INTO acs_events (customer_id, branch_id, event_time, message_code, barcode, destination, patron_id, raw_message) "
             "VALUES (:c, :b, CAST(:t AS timestamp), '10', :barcode, 'Main', :p, :raw)"),
        {"c": customer, "b": branch, "t": local_wall_clock, "barcode": barcode, "p": patron,
         "raw": f"{prefix}20260608    090000|AO1|AB{barcode}|AJSynthetic Title|"},
    )


def _v1_patron(conn, customer, branch, local_wall_clock, patron, name, kind="ADULT"):
    """A legacy patron record (message 64)."""
    conn.execute(
        text("INSERT INTO acs_events (customer_id, branch_id, event_time, message_code, barcode, destination, patron_id, raw_message) "
             "VALUES (:c, :b, CAST(:t AS timestamp), '64', '', '', :p, :raw)"),
        {"c": customer, "b": branch, "t": local_wall_clock, "p": patron, "raw": f"64              001|AA{patron}|AE{name}|PT{kind}|"},
    )


def _v2_hold(conn, customer, branch, instant, n, **flags):
    """A Contract v2 hold. `instant` carries an explicit offset."""
    conn.execute(
        text("INSERT INTO acs_item_events (customer_id, branch_id, key_id, event_key, event_time, state, item_key, destination, "
             "is_ill, is_branch_services, is_collection_services, ruleset_id) "
             "VALUES (:c, :b, :k, :ek, CAST(:t AS timestamptz), 'hold', :item, 'main', :ill, :branch_services, :collection, NULL)"),
        {"c": customer, "b": branch, "k": KEY_ACME, "ek": _key(customer, branch, instant, n), "t": instant, "item": _key("item", customer, n),
         "ill": flags.get("ill", False), "branch_services": flags.get("branch_services", False), "collection": flags.get("collection", False)},
    )


def _seed(conn) -> None:
    """Acme's main sorter, 8-12 June 2026, cut over at noon (Chicago) on the 10th:

        legacy (before the cutover)    b1 for the service account (not public), b2 public, b3 ILL patron (ILL),
                                       b4 for the department (not public), b5 not a hold
        legacy AFTER the cutover       b6 public -- counted by neither era
        current BEFORE the cutover     1 public -- counted by neither era
        current (from the cutover)     2 public (exactly at the cutover), 3 public, 4 ILL, 5 collection services
                                                                 public 1 + 2 = 3, ILL 1 + 1 = 2

    Beta: the same rules at organization level, but its sorter's branch settings replace the branch-services list
    with an empty one, so its one hold for the service account is public there. Gamma's plan has no holds report.
    """
    conn.execute(text(
        "TRUNCATE checkin_events, reject_events, acs_item_events, ingest_key_ids, v2_cutovers, checkins, rejects, acs_events, "
        "checkins_clean, rejects_clean, organization_settings, branch_settings, collector_installations, memberships, "
        "subscriptions, feature_entitlements, plans, app_users, branches, organizations, customers RESTART IDENTITY CASCADE"
    ))
    conn.execute(text("INSERT INTO plans (id, code, name) VALUES (1, 'with-holds', 'With holds'), (2, 'without', 'Without holds')"))
    conn.execute(text("INSERT INTO feature_entitlements (plan_id, feature_key, enabled) VALUES (1, 'internal_workflow', TRUE), "
                      "(2, 'internal_workflow', FALSE)"))
    for org_id, slug, customer, branch, plan in ((1, "acme", ACME, ACME_MAIN, 1), (2, "beta", BETA, BETA_MAIN, 1), (3, "gamma", GAMMA, GAMMA_MAIN, 2)):
        conn.execute(text("INSERT INTO customers (id, name) VALUES (:c, :n)"), {"c": customer, "n": slug})
        conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES (:o, :s, :s, 'active', :c)"),
                     {"o": org_id, "s": slug, "c": customer})
        conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) VALUES (:b, :o, 'main', 'Main', 'active', :b)"),
                     {"b": branch, "o": org_id})
        conn.execute(text("INSERT INTO collector_installations (organization_id, branch_id, name, status) VALUES (:o, :b, 'AMH', 'active')"),
                     {"o": org_id, "b": branch})
        conn.execute(text("INSERT INTO subscriptions (organization_id, plan_id, status) VALUES (:o, :p, 'active')"), {"o": org_id, "p": plan})
        conn.execute(text("INSERT INTO organization_settings (organization_id, settings_json) VALUES (:o, CAST(:doc AS JSONB))"),
                     {"o": org_id, "doc": json.dumps({"transit": {"home_branch_label": "Main", "destinations": []}, "internal_routing": ORGANIZATION_RULES})})
    conn.execute(text("INSERT INTO branch_settings (branch_id, settings_json) VALUES (:b, CAST(:doc AS JSONB))"),
                 {"b": BETA_MAIN, "doc": json.dumps({"internal_routing": {"branch_services_names": []}})})
    for user in (OWNER, VIEWER, BETA_OWNER, GAMMA_OWNER):
        conn.execute(text("INSERT INTO app_users (id, email, is_active) VALUES (:u, :e, TRUE)"), {"u": user, "e": f"user{user}@example.invalid"})
    for org_id, user, role in ((1, OWNER, "owner"), (1, VIEWER, "viewer"), (2, BETA_OWNER, "owner"), (3, GAMMA_OWNER, "owner")):
        conn.execute(text("INSERT INTO memberships (organization_id, user_id, role) VALUES (:o, :u, :r)"), {"o": org_id, "u": user, "r": role})

    conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id) VALUES (:k, :c, :b)"), {"k": KEY_ACME, "c": ACME, "b": ACME_MAIN})
    conn.execute(text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by) "
                      "VALUES (:c, :b, CAST('2026-06-10T12:00:00-05:00' AS timestamptz), 'holds-test')"), {"c": ACME, "b": ACME_MAIN})

    _v1_patron(conn, ACME, ACME_MAIN, "2026-06-08 08:00:00", "p1", ACCOUNT)
    _v1_patron(conn, ACME, ACME_MAIN, "2026-06-08 08:00:00", "p2", "SAMPLE PATRON", kind="ILL")
    _v1_patron(conn, ACME, ACME_MAIN, "2026-06-08 08:00:00", "p3", DEPARTMENT)
    _v1_hold(conn, ACME, ACME_MAIN, "2026-06-08 09:00:00", "b1", "p1")
    _v1_hold(conn, ACME, ACME_MAIN, "2026-06-08 09:00:00", "b2")
    _v1_hold(conn, ACME, ACME_MAIN, "2026-06-09 09:00:00", "b3", "p2")
    _v1_hold(conn, ACME, ACME_MAIN, "2026-06-09 09:00:00", "b4", "p3")
    _v1_hold(conn, ACME, ACME_MAIN, "2026-06-10 09:00:00", "b5", hold=False)
    _v1_hold(conn, ACME, ACME_MAIN, "2026-06-10 13:00:00", "b6")                 # legacy after the cutover
    _v2_hold(conn, ACME, ACME_MAIN, "2026-06-10T16:59:00+00:00", 1)             # current before the cutover
    _v2_hold(conn, ACME, ACME_MAIN, "2026-06-10T17:00:00+00:00", 2)             # exactly at the cutover
    _v2_hold(conn, ACME, ACME_MAIN, "2026-06-11T15:00:00+00:00", 3)
    _v2_hold(conn, ACME, ACME_MAIN, "2026-06-11T15:00:00+00:00", 4, ill=True)
    _v2_hold(conn, ACME, ACME_MAIN, "2026-06-11T15:00:00+00:00", 5, collection=True)

    _v1_patron(conn, BETA, BETA_MAIN, "2026-06-08 08:00:00", "p1", ACCOUNT)
    _v1_hold(conn, BETA, BETA_MAIN, "2026-06-08 09:00:00", "b1", "p1")
    _v1_hold(conn, GAMMA, GAMMA_MAIN, "2026-06-08 09:00:00", "b1")


@pytest.fixture
def api(owner, runtime, monkeypatch):
    with owner.begin() as conn:
        _seed(conn)
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


def _get(api, org="acme", user=OWNER, params=JUNE_8_TO_12):
    return api.get(HOLDS.format(org=org, branch="main"), headers={"Cookie": f"{COOKIE}={user}"}, params=params)


def _counts(api, org="acme", user=OWNER, params=JUNE_8_TO_12) -> tuple[int, int]:
    response = _get(api, org, user, params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"range", "public_hold_count", "ill_hold_count"}
    return body["public_hold_count"], body["ill_hold_count"]


def test_the_runtime_role_is_bound_by_row_level_security(api, runtime):
    with runtime.connect() as conn:
        attributes = conn.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()
        visible = conn.execute(text("SELECT (SELECT count(*) FROM acs_events) + (SELECT count(*) FROM acs_item_events)")).scalar()

    assert tuple(attributes) == (False, False)
    assert visible == 0


def test_the_report_across_a_cutover_counts_each_era_on_its_own_side_by_its_own_rules(api):
    assert _counts(api) == (3, 2)


def test_a_range_wholly_before_or_after_the_cutover_is_read_from_one_era(api):
    assert _counts(api, params={"from": "2026-06-08", "to": "2026-06-09"}) == (1, 1)   # legacy only: b2 public, b3 ILL
    assert _counts(api, params={"from": "2026-06-11", "to": "2026-06-12"}) == (1, 1)   # current only: 3 public, 4 ILL
    assert _counts(api, params={"from": "2026-06-12", "to": "2026-06-12"}) == (0, 0)


def test_every_member_role_reads_the_same_numbers(api):
    assert _counts(api, user=VIEWER) == _counts(api, user=OWNER)


def test_a_sites_own_rules_are_merged_over_the_organizations(api):
    # Beta's branch settings empty its branch-services list, so the hold for the service account is public there.
    assert _counts(api, org="beta", user=BETA_OWNER) == (1, 0)


def test_another_tenant_sees_none_of_this_one_and_cannot_read_it(api):
    assert _get(api, org="acme", user=BETA_OWNER).status_code == 404
    assert _get(api, org="beta", user=OWNER).status_code == 404
    assert _counts(api, org="beta", user=BETA_OWNER) == (1, 0)    # its own one hold, and none of Acme's


def test_a_plan_without_the_feature_has_no_holds_report(api):
    response = _get(api, org="gamma", user=GAMMA_OWNER)

    assert (response.status_code, response.json()) == (403, {"code": "feature_not_available", "message": "This report is not available for this organization."})
    # Its other reports are unaffected.
    assert api.get("/api/organizations/gamma/branches/main/reports/overview", headers={"Cookie": f"{COOKIE}={GAMMA_OWNER}"},
                   params=JUNE_8_TO_12).status_code == 200


def test_no_patron_account_or_identifier_reaches_the_answer(api):
    text_ = _get(api).text

    for forbidden in (ACCOUNT, DEPARTMENT, "SAMPLE PATRON", "p1", "b2", "101YNY", "Synthetic Title", str(ACME), str(ACME_MAIN),
                      "internal_routing", "branch_services", "collection", "programming", "destination"):
        assert forbidden not in text_, forbidden
