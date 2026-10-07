"""Reports R4: the three organization-level range reports.

    GET /api/organizations/{org_slug}/reports/overview?from=&to=
    GET /api/organizations/{org_slug}/reports/routing-network?from=&to=
    GET /api/organizations/{org_slug}/reports/reliability?from=&to=

These tests drive the real production routes through TestClient(main.app).
Nothing between the request and the rows is replaced: the session and
organization-access dependencies, the real access service, the real sorter
inventory, the real tenant resolver, the scoped and verified connection, the
sorter-site report service, the organization report service and the response
schemas all run, and so does every SQL statement -- against one in-memory
SQLite database holding the SaaS tables, the settings, the collector
installations and the four operational tables. As in
tests/test_customer_api_reports.py, the operational engine is that database
behind a thin wrapper that keeps PostgreSQL's two tenant-context settings and
records every statement run under them.

THE INVARIANT THIS FILE IS BUILT AROUND: an organization report is exactly
its sorter sites' own reports -- the R2 endpoints' answers -- added up. So
most expectations here are computed from those endpoints' answers, and the
rest are counted by hand from the seeded rows.

The clock the routes read is the shared controlled clock
(tests/controlled_clock.py), which only a test moves.

Real row level security and real TIMESTAMP / TIMESTAMPTZ comparison are
tested against a real server in tests/test_rls_phase1_postgres.py.
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, date, datetime, timedelta

import pytest
from controlled_clock import ControlledClock
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool
from test_customer_api_checkins_by_destination import CANARY
from test_customer_api_reports import (
    _REJECT_TABLES,
    ReportDatabase,
    _local,
    _seed_a_mixed_week,
    _utc,
)

import main
from customer_api import (
    organization_report_routes,
    organization_report_schemas,
    report_routes,
    tenant_scope,
)
from services import (
    access_service,
    organization_report_service,
    session_service,
    sorter_inventory_service,
    tenant_resolution_service,
)
from services.organization_report_service import (
    OrganizationReportError,
    SiteNotResolvedError,
    read_sorter_sites,
    site_has_no_operational_scope,
)
from services.reject_reason import REJECT_REASONS
from services.tenant_resolution_service import ResolvedOperationalTenant

ORG = "/api/organizations/{org}/reports/{report}"
SITE = "/api/organizations/{org}/branches/{branch}/reports/{report}"
REPORTS = ("overview", "routing-network", "reliability")
ALICE = {"id": 1, "email": "alice@example.invalid", "full_name": "Alice"}
BOB = {"id": 2, "email": "bob@example.invalid", "full_name": "Bob"}
COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
ORGANIZATION_NOT_FOUND = {"code": "organization_not_found", "message": "Organization not found."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

# Operational ids. They are never the same number as a count any test expects.
ACME, MAIN, EAST, NORTH, WESTSIDE = 8101, 11, 14, 15, 16
BETA, BETA_NORTH = 8202, 21
SOLO, SOLO_MAIN = 8303, 31
PAUSED, PAUSED_MAIN = 8505, 51
EVERY_OPERATIONAL_ID = ("8101", "8202", "8303", "8404", "8505", "8606")

# Where the clock starts unless a test moves it: 1 PM on Saturday 20 June 2026 in America/Chicago.
CLOCK = ControlledClock(datetime(2026, 6, 20, 18, 0, tzinfo=UTC))
JUNE_8_TO_12 = {"from": "2026-06-08", "to": "2026-06-12"}
RANGE = {"from": "2026-06-08", "to": "2026-06-12", "days": 5, "timezone": "America/Chicago", "includes_today": False}
DATES = ["2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12"]

# Acme's settings: home is "Main"; three destinations. "East" is ALSO the name of a place that hosts a sorter of
# its own -- which the main sorter's routing neither knows nor says.
ACME_SETTINGS = {
    "security": {"admin_lock_hash": CANARY},
    "transit": {
        "home_branch_label": "Main",
        "destinations": [
            {"key": "branch_1", "label": "Westside"},
            {"key": "branch_2", "label": "Library Express"},
            {"key": "branch_3", "label": "East"},
            {"key": "branch_4", "label": "Old Annex", "enabled": False},
        ],
    },
}
# The east site has its own: the same destination as the main site's, written differently, and one of its own.
EAST_SETTINGS = {
    "transit": {
        "home_branch_label": "East",
        "destinations": [{"key": "a", "label": "WESTSIDE"}, {"key": "b", "label": "Central Annex"}],
    },
}

_SCHEMA = (
    "CREATE TABLE app_users (id INTEGER PRIMARY KEY, email TEXT, is_active BOOLEAN)",
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT, name TEXT, status TEXT, operational_customer_id INTEGER)",
    (
        "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, slug TEXT, name TEXT, "
        "is_primary BOOLEAN, status TEXT, operational_branch_id INTEGER)"
    ),
    "CREATE TABLE memberships (id INTEGER PRIMARY KEY, organization_id INTEGER, user_id INTEGER, role TEXT, removed_at TEXT)",
    "CREATE TABLE organization_settings (id INTEGER PRIMARY KEY, organization_id INTEGER UNIQUE, settings_json TEXT)",
    "CREATE TABLE branch_settings (id INTEGER PRIMARY KEY, branch_id INTEGER UNIQUE, settings_json TEXT)",
    (
        "CREATE TABLE collector_installations (id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER, "
        "name TEXT, hostname TEXT, collector_version TEXT, status TEXT)"
    ),
    (
        "CREATE TABLE checkins (id INTEGER PRIMARY KEY, customer_id INTEGER, branch_id INTEGER, event_time TEXT, "
        "barcode TEXT, title TEXT, destination TEXT)"
    ),
    (
        "CREATE TABLE checkin_events (id INTEGER PRIMARY KEY, customer_id INTEGER, branch_id INTEGER, key_id TEXT, "
        "event_key TEXT, event_time TEXT, item_key TEXT, destination TEXT, bin TEXT)"
    ),
    (
        "CREATE TABLE v2_cutovers (id INTEGER PRIMARY KEY, customer_id INTEGER, branch_id INTEGER, cutover_at TEXT, "
        "set_by TEXT, set_at TEXT, note TEXT)"
    ),
    *_REJECT_TABLES,
    "INSERT INTO app_users (id, email, is_active) VALUES (1, 'alice@example.invalid', 1), (2, 'bob@example.invalid', 1)",
    (
        f"INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES "
        f"(1, 'acme', 'Acme Public Library', 'active', {ACME}), (2, 'beta', 'Beta Libraries', 'active', {BETA}), "
        f"(3, 'solo', 'Solo Library', 'active', {SOLO}), (4, 'closed', 'Closed', 'cancelled', 8404), "
        f"(5, 'paused', 'Paused', 'suspended', {PAUSED}), (6, 'empty', 'No Sorters Yet', 'active', 8606), "
        f"(7, 'unmapped-org', 'Not Yet Mapped', 'active', NULL)"
    ),
    (
        f"INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) VALUES "
        f"({MAIN}, 1, 'main', 'Main Library', 1, 'active', {MAIN}), "
        f"(12, 1, 'shut', 'Shut', 0, 'inactive', 12), "
        f"(13, 1, 'unmapped', 'Unmapped Branch', 0, 'active', NULL), "
        f"({EAST}, 1, 'east', 'East Library', 0, 'active', {EAST}), "
        f"({NORTH}, 1, 'north', 'North Library', 0, 'active', {NORTH}), "
        # A branch, a place the main sorter routes to, and operational rows of its own -- but no sorter.
        f"({WESTSIDE}, 1, 'westside', 'Westside', 0, 'active', {WESTSIDE}), "
        f"({BETA_NORTH}, 2, 'north', 'Beta North', 1, 'active', {BETA_NORTH}), "
        f"({SOLO_MAIN}, 3, 'main', 'Solo Main', 1, 'active', {SOLO_MAIN}), "
        f"(41, 4, 'main', 'Closed Main', 1, 'active', 41), "
        f"({PAUSED_MAIN}, 5, 'main', 'Paused Main', 1, 'active', {PAUSED_MAIN}), "
        f"(61, 6, 'main', 'Empty Main', 1, 'active', 61), "
        f"(71, 7, 'main', 'Unmapped Org Main', 1, 'active', 71)"
    ),
    # Alice belongs to every organization but beta. Bob belongs to beta only.
    (
        "INSERT INTO memberships (organization_id, user_id, role) VALUES "
        "(1, 1, 'viewer'), (3, 1, 'admin'), (4, 1, 'admin'), (5, 1, 'viewer'), (6, 1, 'viewer'), (7, 1, 'viewer'), (2, 2, 'admin')"
    ),
    # The sorters. Acme has two; east is registered before nothing else at its branch.
    (
        "INSERT INTO collector_installations (id, organization_id, branch_id, name, hostname, collector_version, status) VALUES "
        f"(1, 1, {MAIN}, 'Main Library AMH', 'CANARY-HOST-01', '9.9.9-canary', 'active'), "
        f"(2, 1, {EAST}, 'East AMH', 'CANARY-HOST-02', '9.9.9-canary', 'active'), "
        f"(3, 2, {BETA_NORTH}, 'Beta AMH', 'CANARY-HOST-03', '9.9.9-canary', 'active'), "
        f"(4, 3, {SOLO_MAIN}, 'Solo AMH', 'CANARY-HOST-04', '9.9.9-canary', 'active'), "
        f"(5, 4, 41, 'Closed AMH', 'CANARY-HOST-05', '9.9.9-canary', 'active'), "
        f"(6, 5, {PAUSED_MAIN}, 'Paused AMH', 'CANARY-HOST-06', '9.9.9-canary', 'active'), "
        f"(7, 7, 71, 'Unmapped Org AMH', 'CANARY-HOST-07', '9.9.9-canary', 'provisioning')"
    ),
)


class Database(ReportDatabase):
    """The R2 tests' database, with collector installations."""

    def install(self, organization: int, branch: int, name: str, status: str = "active") -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO collector_installations (organization_id, branch_id, name, hostname, "
                     "collector_version, status) VALUES (:o, :b, :n, 'CANARY-HOST-99', '9.9.9-canary', :s)"),
                {"o": organization, "b": branch, "n": name, "s": status},
            )

    def run(self, sql: str, **parameters) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(sql), parameters)

    def scoped_reads(self) -> list[tuple[str, str, str]]:
        """(table, customer id, branch id) of every statement run on a scoped connection, by the CONTEXT it ran under."""
        return [(table, settings.get("customer_id", ""), settings.get("branch_id", "")) for table, _params, settings in self.queries]


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in _SCHEMA:
            conn.execute(text(statement))
    database = Database(engine)
    database.settings(organization=1, document=ACME_SETTINGS)
    database.settings(branch=EAST, document=EAST_SETTINGS)
    # One database for everything the routes read: who the user is a member of, the organization's sorters,
    # each site's scope, and -- behind the wrapper that keeps the tenant context -- the operational rows.
    for module in (access_service, sorter_inventory_service, organization_report_service, tenant_resolution_service):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setattr(tenant_scope, "get_engine", lambda: database)
    yield database
    engine.dispose()


@pytest.fixture
def clock():
    CLOCK.reset()
    with CLOCK.controlling(report_routes):
        yield CLOCK


class _Session:
    user: dict | None = ALICE


@pytest.fixture
def session(monkeypatch):
    current = _Session()
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: dict(current.user) if current.user else None)
    return current


@pytest.fixture
def api(monkeypatch, clock, session):
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


def _get(api, report: str, params=JUNE_8_TO_12, *, org="acme", headers=COOKIE):
    return api.get(ORG.format(org=org, report=report), headers=headers, params=params)


def _report(api, report: str, params=JUNE_8_TO_12, *, org="acme") -> dict:
    response = _get(api, report, params, org=org)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def _site(api, branch: str, report: str, params=JUNE_8_TO_12, *, org="acme") -> dict:
    """What the R2 sorter-site report answers for one site."""
    response = api.get(SITE.format(org=org, branch=branch, report=report), headers=COOKIE, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _failing(report: str, params=JUNE_8_TO_12, *, org="acme"):
    """The response to a request the server cannot answer, as a client sees it."""
    return TestClient(main.app, raise_server_exceptions=False).get(
        ORG.format(org=org, report=report), headers=COOKIE, params=params)


def _reasons(counts: dict[str, int]) -> list[dict]:
    return [{"reason": reason, "reject_count": counts.get(reason, 0)} for reason in REJECT_REASONS]


def _dates(first: str, last: str) -> list[str]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def _keys(value) -> set[str]:
    """Every object key anywhere in a JSON value."""
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def _numbers(value) -> list:
    if isinstance(value, dict):
        return [number for item in value.values() for number in _numbers(item)]
    if isinstance(value, list):
        return [number for item in value for number in _numbers(item)]
    return [value] if isinstance(value, (int, float)) and not isinstance(value, bool) else []


def _seed_other_tenants_and_other_branches(db) -> None:
    """Rows that belong to no sorter of Acme's, on every day of the range, in all four tables. None of them may
    ever be counted for Acme: another organization's, and Acme's own branches that host no sorter."""
    for customer, branch in ((BETA, BETA_NORTH), (ACME, WESTSIDE), (ACME, 12), (BETA, MAIN), (ACME, BETA_NORTH)):
        for day in range(8, 13):
            db.v1("Westside", 7, at=_local(6, day, 10), customer=customer, branch=branch)
            db.v2("westside", 7, at=_utc(6, day, 18), customer=customer, branch=branch)
            db.reject_v1("Item not found in database", 7, at=_local(6, day, 10), customer=customer, branch=branch)
            db.reject_v2("item_not_found", 7, at=_utc(6, day, 18), customer=customer, branch=branch)


def _seed_acme(db) -> None:
    """Acme's two sorters over 8-12 June 2026.

    MAIN (crosses its cutover at noon on the 10th): the R2 tests' mixed week, plus three legacy check-ins routed
    to "East" on the 9th.

        check-ins by day   6, 8, 8, 8, 0   = 30      home 17 · Westside 5 · Library Express 3 · East 3 · other 2
        rejects by day     2, 0, 3, 3, 0   = 8

    EAST (never cut over: legacy only):

        8 June    East x5 (home), Westside x1                      rejects: 1
        12 June   Central Annex x2, Nowhere x1 (not configured)    rejects: 0

        check-ins by day   6, 0, 0, 0, 3   = 9       home 5 · WESTSIDE 1 · Central Annex 2 · other 1
        rejects by day     1, 0, 0, 0, 0   = 1
    """
    _seed_a_mixed_week(db)
    db.v1("East", 3, at=_local(6, 9, 11))

    db.v1("East", 5, at=_local(6, 8, 9), branch=EAST)
    db.v1("Westside", 1, at=_local(6, 8, 15), branch=EAST)
    db.v1("Central Annex", 2, at=_local(6, 12, 9), branch=EAST)
    db.v1("Nowhere", 1, at=_local(6, 12, 10), branch=EAST)
    db.reject_v1("Item not found in database", 1, at=_local(6, 8, 9), branch=EAST)

    _seed_other_tenants_and_other_branches(db)


MAIN_SORTER = {"slug": "main", "name": "Main Library AMH", "host_branch": {"slug": "main", "name": "Main Library"}}
EAST_SORTER = {"slug": "east", "name": "East AMH", "host_branch": {"slug": "east", "name": "East Library"}}


# =====================================================================================================================
# The three reports, for an organization with two sorters
# =====================================================================================================================

def test_the_overview_report(api, db):
    _seed_acme(db)

    assert _report(api, "overview") == {
        "range": RANGE,
        "totals": {"checkin_count": 39, "home_count": 22, "transit_count": 14, "other_count": 3, "reject_count": 9},
        "sorters": [
            {**MAIN_SORTER, "status": "active", "collector_count": 1, "available": True,
             "checkin_count": 30, "active_days": 4, "transit_count": 11, "reject_count": 8},
            {**EAST_SORTER, "status": "active", "collector_count": 1, "available": True,
             "checkin_count": 9, "active_days": 2, "transit_count": 3, "reject_count": 1},
        ],
        "days": [
            {"date": "2026-06-08", "checkin_count": 12, "reject_count": 3},
            {"date": "2026-06-09", "checkin_count": 8, "reject_count": 0},
            {"date": "2026-06-10", "checkin_count": 8, "reject_count": 3},
            {"date": "2026-06-11", "checkin_count": 8, "reject_count": 3},
            {"date": "2026-06-12", "checkin_count": 3, "reject_count": 0},
        ],
    }


def test_the_routing_network_report(api, db):
    _seed_acme(db)

    assert _report(api, "routing-network") == {
        "range": RANGE,
        "totals": {"checkin_count": 39, "transit_count": 14},
        "sources": [
            {
                "sorter": MAIN_SORTER,
                "checkin_count": 30,
                "home": {"label": "Main", "checkin_count": 17},
                "transit_count": 11,
                "other_count": 2,
                "transit": [
                    {"key": "westside", "label": "Westside", "checkin_count": 5},
                    {"key": "library_express", "label": "Library Express", "checkin_count": 3},
                    {"key": "east", "label": "East", "checkin_count": 3},
                ],
            },
            {
                "sorter": EAST_SORTER,
                "checkin_count": 9,
                "home": {"label": "East", "checkin_count": 5},
                "transit_count": 3,
                "other_count": 1,
                "transit": [
                    {"key": "westside", "label": "WESTSIDE", "checkin_count": 1},
                    {"key": "central_annex", "label": "Central Annex", "checkin_count": 2},
                ],
            },
        ],
        "destinations": [
            {"key": "westside", "label": "Westside", "checkin_count": 6, "source_count": 2},
            {"key": "library_express", "label": "Library Express", "checkin_count": 3, "source_count": 1},
            {"key": "east", "label": "East", "checkin_count": 3, "source_count": 1},
            {"key": "central_annex", "label": "Central Annex", "checkin_count": 2, "source_count": 1},
        ],
    }


def test_the_reliability_report(api, db):
    _seed_acme(db)

    main_reasons = {"item_not_found": 3, "ils_acs_failure": 1, "rfid_collision": 1, "routing_error": 2, "other": 1}

    assert _report(api, "reliability") == {
        "range": RANGE,
        "totals": {
            "checkin_count": 39,
            "reject_count": 9,
            "reasons": _reasons({**main_reasons, "item_not_found": 4}),
        },
        "sorters": [
            {"sorter": MAIN_SORTER, "available": True, "checkin_count": 30, "reject_count": 8, "reasons": _reasons(main_reasons)},
            {"sorter": EAST_SORTER, "available": True, "checkin_count": 9, "reject_count": 1,
             "reasons": _reasons({"item_not_found": 1})},
        ],
        "days": [
            {"date": "2026-06-08", "checkin_count": 12, "reject_count": 3},
            {"date": "2026-06-09", "checkin_count": 8, "reject_count": 0},
            {"date": "2026-06-10", "checkin_count": 8, "reject_count": 3},
            {"date": "2026-06-11", "checkin_count": 8, "reject_count": 3},
            {"date": "2026-06-12", "checkin_count": 3, "reject_count": 0},
        ],
    }


# =====================================================================================================================
# An organization report is its sorter sites' own reports, added up
# =====================================================================================================================

def _assert_organization_is_the_sum_of_its_sites(api, org: str, branches: list[str], params=JUNE_8_TO_12) -> None:
    overview, network, reliability = (_report(api, report, params, org=org) for report in REPORTS)
    sites = {
        branch: {report: _site(api, branch, report, params, org=org) for report in ("overview", "routing", "reliability")}
        for branch in branches
    }

    # --- overview ---
    assert [sorter["slug"] for sorter in overview["sorters"]] == branches
    for sorter in overview["sorters"]:
        site = sites[sorter["slug"]]["overview"]
        assert sorter["available"] is True
        assert {key: sorter[key] for key in ("checkin_count", "active_days", "transit_count", "reject_count")} == {
            key: site[key] for key in ("checkin_count", "active_days", "transit_count", "reject_count")}
    for total in ("checkin_count", "home_count", "transit_count", "other_count", "reject_count"):
        assert overview["totals"][total] == sum(site["overview"][total] for site in sites.values()), total
    for index, day in enumerate(overview["days"]):
        assert day == {
            "date": next(iter(sites.values()))["overview"]["days"][index]["date"],
            "checkin_count": sum(site["overview"]["days"][index]["checkin_count"] for site in sites.values()),
            "reject_count": sum(site["overview"]["days"][index]["reject_count"] for site in sites.values()),
        }
    totals = overview["totals"]
    assert totals["home_count"] + totals["transit_count"] + totals["other_count"] == totals["checkin_count"]
    assert sum(day["checkin_count"] for day in overview["days"]) == totals["checkin_count"]
    assert sum(day["reject_count"] for day in overview["days"]) == totals["reject_count"]

    # --- routing network: each source IS that site's routing report, without its days ---
    assert [source["sorter"]["slug"] for source in network["sources"]] == branches
    for source in network["sources"]:
        site = sites[source["sorter"]["slug"]]["routing"]
        assert {key: source[key] for key in ("checkin_count", "home", "transit", "transit_count", "other_count")} == {
            key: site[key] for key in ("checkin_count", "home", "transit", "transit_count", "other_count")}
    by_key: dict[str, int] = {}
    for source in network["sources"]:
        for destination in source["transit"]:
            by_key[destination["key"]] = by_key.get(destination["key"], 0) + destination["checkin_count"]
    assert {destination["key"]: destination["checkin_count"] for destination in network["destinations"]} == by_key
    assert network["totals"] == {"checkin_count": totals["checkin_count"], "transit_count": totals["transit_count"]}
    assert sum(destination["checkin_count"] for destination in network["destinations"]) == totals["transit_count"]

    # --- reliability ---
    assert [sorter["sorter"]["slug"] for sorter in reliability["sorters"]] == branches
    for sorter in reliability["sorters"]:
        site = sites[sorter["sorter"]["slug"]]["reliability"]
        assert {key: sorter[key] for key in ("checkin_count", "reject_count", "reasons")} == {
            key: site[key] for key in ("checkin_count", "reject_count", "reasons")}
    assert [reason["reason"] for reason in reliability["totals"]["reasons"]] == list(REJECT_REASONS)
    for index, reason in enumerate(reliability["totals"]["reasons"]):
        assert reason["reject_count"] == sum(site["reliability"]["reasons"][index]["reject_count"] for site in sites.values())
    assert sum(reason["reject_count"] for reason in reliability["totals"]["reasons"]) == reliability["totals"]["reject_count"]
    assert reliability["totals"]["checkin_count"] == totals["checkin_count"]
    assert reliability["totals"]["reject_count"] == totals["reject_count"]
    assert reliability["days"] == overview["days"]


def test_every_figure_of_every_report_is_the_sorter_site_reports_added_up(api, db):
    _seed_acme(db)

    _assert_organization_is_the_sum_of_its_sites(api, "acme", ["main", "east"])


def test_an_organization_with_one_sorter_reports_exactly_that_sorter(api, db):
    db.settings(organization=3, document=ACME_SETTINGS)
    db.v1("Main", 6, at=_local(6, 8, 9), customer=SOLO, branch=SOLO_MAIN)
    db.v1("Westside", 2, at=_local(6, 9, 9), customer=SOLO, branch=SOLO_MAIN)
    db.reject_v1("ACS timeout", 3, at=_local(6, 9, 9), customer=SOLO, branch=SOLO_MAIN)
    _seed_other_tenants_and_other_branches(db)

    _assert_organization_is_the_sum_of_its_sites(api, "solo", ["main"])

    overview = _report(api, "overview", org="solo")
    site = _site(api, "main", "overview", org="solo")
    assert overview["totals"] == {key: site[key] for key in ("checkin_count", "home_count", "transit_count", "other_count", "reject_count")}
    assert overview["totals"]["checkin_count"] == 8 and overview["totals"]["reject_count"] == 3
    assert overview["days"] == site["days"]
    assert len(_report(api, "routing-network", org="solo")["sources"]) == 1
    reliability = _report(api, "reliability", org="solo")
    assert reliability["totals"]["reasons"] == reliability["sorters"][0]["reasons"] == _site(api, "main", "reliability", org="solo")["reasons"]


def test_sorters_stay_separate_rows_in_the_inventorys_order(api, db):
    _seed_acme(db)
    db.install(1, NORTH, "North AMH", status="provisioning")

    overview = _report(api, "overview")

    # Primary branch first, then by branch name: exactly the inventory the organization detail's `sorters` come from.
    listed = sorter_inventory_service.list_sorter_sites("acme")
    assert [sorter["slug"] for sorter in overview["sorters"]] == [site.slug for site in listed] == ["main", "east", "north"]
    for sorter, site in zip(overview["sorters"], listed, strict=True):
        assert {key: sorter[key] for key in ("slug", "name", "host_branch", "status", "collector_count")} == {
            "slug": site.slug, "name": site.name, "status": site.status, "collector_count": site.collector_count,
            "host_branch": {"slug": site.host_branch_slug, "name": site.host_branch_name},
        }
    assert overview["sorters"][2] == {
        "slug": "north", "name": "North AMH", "host_branch": {"slug": "north", "name": "North Library"},
        "status": "provisioning", "collector_count": 1, "available": True,
        "checkin_count": 0, "active_days": 0, "transit_count": 0, "reject_count": 0,
    }
    assert [source["sorter"]["slug"] for source in _report(api, "routing-network")["sources"]] == ["main", "east", "north"]
    assert [sorter["sorter"]["slug"] for sorter in _report(api, "reliability")["sorters"]] == ["main", "east", "north"]


def test_processing_is_events_not_items_the_same_item_at_two_sorters_is_two(api, db):
    # One physical item: checked in at main, routed to East, then checked in again at east.
    db.v1("East", 1, at=_local(6, 9, 9))
    db.v1("East", 1, at=_local(6, 10, 9), branch=EAST)

    overview = _report(api, "overview")

    assert overview["totals"]["checkin_count"] == 2
    assert [sorter["checkin_count"] for sorter in overview["sorters"]] == [1, 1]


# =====================================================================================================================
# Counts only: rates are for whoever shows them, from the summed counts
# =====================================================================================================================

def test_the_totals_carry_the_summed_numerators_and_denominators_a_correct_rate_needs(api, db):
    _seed_acme(db)

    overview = _report(api, "overview")
    main_site, east_site = overview["sorters"]

    # Main rejects 8 of 30 (26.7%); east 1 of 9 (11.1%). The organization's rate is 9 of 39 (23.1%) -- not the
    # 18.9% the two sorters' rates average to.
    correct = overview["totals"]["reject_count"] / overview["totals"]["checkin_count"]
    averaged = (main_site["reject_count"] / main_site["checkin_count"] + east_site["reject_count"] / east_site["checkin_count"]) / 2
    assert (overview["totals"]["reject_count"], overview["totals"]["checkin_count"]) == (9, 39)
    assert correct == pytest.approx(9 / 39) and averaged == pytest.approx((8 / 30 + 1 / 9) / 2)
    assert abs(correct - averaged) > 0.04

    # The same for transit: 14 of 39 (35.9%), not the 35.0% that 11 of 30 and 3 of 9 average to.
    assert (overview["totals"]["transit_count"], overview["totals"]["checkin_count"]) == (14, 39)
    assert 14 / 39 != pytest.approx((11 / 30 + 3 / 9) / 2)

    reliability = _report(api, "reliability")
    assert (reliability["totals"]["reject_count"], reliability["totals"]["checkin_count"]) == (9, 39)


@pytest.mark.parametrize("report", REPORTS)
def test_every_figure_is_a_non_negative_integer_and_nothing_derived_is_returned(api, db, report):
    _seed_acme(db)

    body = _report(api, report)

    assert all(isinstance(number, int) and number >= 0 for number in _numbers(body))
    for key in _keys(body):
        for derived in ("rate", "percent", "share", "average", "avg", "mean", "ratio", "rank", "busiest", "top",
                        "outlier", "issue", "matrix", "has_sorter", "is_sorter", "active_days_total"):
            assert derived not in key, key
    # No organization-wide active-day count: a day is active for a sorter, and that is where it is reported.
    assert "active_days" not in body.get("totals", {})


# =====================================================================================================================
# The routing network: destinations are keys, never sorters
# =====================================================================================================================

def test_a_destination_that_shares_a_sorters_name_stays_a_destination_and_is_never_that_sorter(api, db):
    _seed_acme(db)

    network = _report(api, "routing-network")
    east_destination = next(destination for destination in network["destinations"] if destination["key"] == "east")
    east_source = next(source for source in network["sources"] if source["sorter"]["slug"] == "east")

    # Main routed 3 to "East". The east sorter processed 9 of its own. Neither figure is in the other.
    assert east_destination == {"key": "east", "label": "East", "checkin_count": 3, "source_count": 1}
    assert east_source["checkin_count"] == 9
    assert set(east_destination) == {"key", "label", "checkin_count", "source_count"}
    assert set(east_source["sorter"]) == {"slug", "name", "host_branch"}
    # Nothing links the two: no destination carries a sorter, and no source's own key is among its destinations.
    for destination in network["destinations"]:
        assert not {"sorter", "slug", "host_branch", "branch", "site", "available"} & set(destination)
    assert "east" not in [destination["key"] for destination in east_source["transit"]]


def test_equal_keys_are_added_up_and_the_first_sources_label_names_the_entry(api, db):
    _seed_acme(db)

    westside = _report(api, "routing-network")["destinations"][0]

    # "Westside" (main) and "WESTSIDE" (east) are one key. Main is listed first, so its label is shown.
    assert westside == {"key": "westside", "label": "Westside", "checkin_count": 6, "source_count": 2}

    # The other way round when east is the organization's primary branch, and so is listed first.
    db.run("UPDATE branches SET is_primary = (slug = 'east') WHERE organization_id = 1")
    reordered = _report(api, "routing-network")

    assert [source["sorter"]["slug"] for source in reordered["sources"]] == ["east", "main"]
    assert reordered["destinations"][0] == {"key": "westside", "label": "WESTSIDE", "checkin_count": 6, "source_count": 2}
    assert [destination["key"] for destination in reordered["destinations"]] == ["westside", "central_annex", "library_express", "east"]


def test_source_count_is_the_sources_that_have_the_key_configured_whether_or_not_they_routed_anything(api, db):
    # No check-ins at all: every configured destination is still there, with a zero and its source count.
    assert _report(api, "routing-network")["destinations"] == [
        {"key": "westside", "label": "Westside", "checkin_count": 0, "source_count": 2},
        {"key": "library_express", "label": "Library Express", "checkin_count": 0, "source_count": 1},
        {"key": "east", "label": "East", "checkin_count": 0, "source_count": 1},
        {"key": "central_annex", "label": "Central Annex", "checkin_count": 0, "source_count": 1},
    ]

    # East routes to Library Express without having it configured: that is east's "other", and Library Express
    # still has one source.
    db.v1("Library Express", 4, at=_local(6, 9, 9), branch=EAST)
    network = _report(api, "routing-network")

    assert network["destinations"][1] == {"key": "library_express", "label": "Library Express", "checkin_count": 0, "source_count": 1}
    assert network["sources"][1]["other_count"] == 4
    assert network["totals"] == {"checkin_count": 4, "transit_count": 0}


def test_other_is_never_given_a_key_or_a_place_among_the_destinations(api, db):
    db.v1("Depot", 2, at=_local(6, 9, 9))
    db.v1(None, 1, at=_local(6, 9, 9))
    db.v1("", 1, at=_local(6, 9, 9))
    db.v2("unknown", 3, at=_utc(6, 9, 15), branch=EAST)
    db.cutover(_utc(6, 1, 0), branch=EAST)

    network = _report(api, "routing-network")

    assert [source["other_count"] for source in network["sources"]] == [4, 3]
    assert network["totals"] == {"checkin_count": 7, "transit_count": 0}
    keys = [destination["key"] for destination in network["destinations"]]
    assert keys == ["westside", "library_express", "east", "central_annex"]
    for unnamed in ("other", "unknown", "depot", ""):
        assert unnamed not in keys
    assert "Depot" not in str(network)


def test_each_source_keeps_its_own_destinations_in_its_own_configured_order(api, db):
    db.settings(branch=EAST, document={"transit": {"home_branch_label": "East", "destinations": [
        {"label": "Zebra Annex"}, {"label": "Library Express"}, {"label": "Alpha Annex"}, {"label": "Hidden", "enabled": False},
    ]}})
    db.install(1, NORTH, "North AMH")
    db.settings(branch=NORTH, document={"transit": {"home_branch_label": "North", "destinations": []}})

    network = _report(api, "routing-network")

    assert [[destination["label"] for destination in source["transit"]] for source in network["sources"]] == [
        ["Westside", "Library Express", "East"],
        ["Zebra Annex", "Library Express", "Alpha Annex"],
        [],
    ]
    # First met first: main's three, then east's two new ones.
    assert [(d["key"], d["source_count"]) for d in network["destinations"]] == [
        ("westside", 1), ("library_express", 2), ("east", 1), ("zebra_annex", 1), ("alpha_annex", 1),
    ]


def test_the_response_is_enough_to_draw_source_by_destination_without_a_matrix_of_its_own(api, db):
    _seed_acme(db)

    network = _report(api, "routing-network")
    cell = {
        (source["sorter"]["slug"], destination["key"]): destination["checkin_count"]
        for source in network["sources"] for destination in source["transit"]
    }

    assert cell == {
        ("main", "westside"): 5, ("main", "library_express"): 3, ("main", "east"): 3,
        ("east", "westside"): 1, ("east", "central_annex"): 2,
    }
    assert list(network) == ["range", "totals", "sources", "destinations"]


# =====================================================================================================================
# Sorter sites: collapsed installations, and the one legitimate "not available"
# =====================================================================================================================

def test_two_collectors_at_one_branch_are_one_sorter_counted_once(api, db):
    _seed_acme(db)
    before = _report(api, "overview")
    db.install(1, MAIN, "Main Library AMH 2")
    db.install(1, MAIN, "Main Library old unit", status="retired")

    after = _report(api, "overview")

    assert [sorter["slug"] for sorter in after["sorters"]] == ["main", "east"]
    assert after["sorters"][0]["collector_count"] == 2 and after["sorters"][0]["name"] == "Main Library AMH"
    assert after["totals"] == before["totals"] and after["days"] == before["days"]
    assert after["sorters"][0]["checkin_count"] == 30
    assert _report(api, "routing-network")["totals"] == {"checkin_count": 39, "transit_count": 14}
    assert _report(api, "reliability")["totals"]["reject_count"] == 9
    # The site was opened and read once per report, not once per collector.
    db.log.clear()
    _report(api, "overview")
    assert db.log.count("open") == 2


def test_a_sorter_registered_at_a_branch_with_no_operational_id_is_not_available_and_adds_nothing(api, db):
    _seed_acme(db)
    db.install(1, 13, "Unmapped Branch AMH", status="provisioning")

    overview, network, reliability = (_report(api, report) for report in REPORTS)

    assert overview["sorters"][2] == {
        "slug": "unmapped", "name": "Unmapped Branch AMH", "host_branch": {"slug": "unmapped", "name": "Unmapped Branch"},
        "status": "provisioning", "collector_count": 1, "available": False,
        "checkin_count": 0, "active_days": 0, "transit_count": 0, "reject_count": 0,
    }
    assert [sorter["available"] for sorter in overview["sorters"]] == [True, True, False]
    assert overview["totals"] == {"checkin_count": 39, "home_count": 22, "transit_count": 14, "other_count": 3, "reject_count": 9}
    assert [day["checkin_count"] for day in overview["days"]] == [12, 8, 8, 8, 3]

    # It has no configuration and no check-ins to read: it is not a routing source.
    assert [source["sorter"]["slug"] for source in network["sources"]] == ["main", "east"]
    assert network["totals"] == {"checkin_count": 39, "transit_count": 14}

    assert reliability["sorters"][2] == {
        "sorter": {"slug": "unmapped", "name": "Unmapped Branch AMH", "host_branch": {"slug": "unmapped", "name": "Unmapped Branch"}},
        "available": False, "checkin_count": 0, "reject_count": 0, "reasons": _reasons({}),
    }
    assert reliability["totals"]["reject_count"] == 9
    # Nothing was opened for it: two sites were read, not three.
    db.log.clear()
    _report(api, "overview")
    assert db.log.count("open") == 2


def test_an_organization_with_no_operational_id_has_only_unavailable_sorters_and_a_report_of_zeros(api, db):
    overview, network, reliability = (_report(api, report, org="unmapped-org") for report in REPORTS)

    assert [(sorter["slug"], sorter["available"]) for sorter in overview["sorters"]] == [("main", False)]
    assert overview["totals"] == {"checkin_count": 0, "home_count": 0, "transit_count": 0, "other_count": 0, "reject_count": 0}
    assert [day["date"] for day in overview["days"]] == DATES
    assert network["sources"] == [] and network["destinations"] == []
    assert reliability["sorters"][0]["available"] is False and reliability["totals"]["reasons"] == _reasons({})
    assert db.log == []


def test_only_a_missing_operational_id_makes_a_site_unavailable(db):
    assert site_has_no_operational_scope("acme", "unmapped") is True        # the branch has no id
    assert site_has_no_operational_scope("unmapped-org", "main") is True    # the organization has no id
    assert site_has_no_operational_scope("acme", "main") is False           # mapped: not resolving is something else
    assert site_has_no_operational_scope("acme", "shut") is False           # inactive: not listed at all
    assert site_has_no_operational_scope("acme", "nope") is False
    assert site_has_no_operational_scope("nope", "main") is False

    # Two rows where the schema allows one is not "unmapped" either.
    db.run("INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) "
           "VALUES (99, 1, 'unmapped', 'Duplicate', 0, 'active', NULL)")
    assert site_has_no_operational_scope("acme", "unmapped") is False


def test_the_mapping_check_reads_two_booleans_and_no_identifier():
    sql = " ".join(str(organization_report_service._SITE_MAPPING_SQL).split())
    selected = sql.split(" FROM ")[0]

    assert selected == ("SELECT (o.operational_customer_id IS NOT NULL) AS organization_is_mapped, "
                        "(b.operational_branch_id IS NOT NULL) AS branch_is_mapped")
    assert "WHERE o.slug = :org_slug AND b.slug = :branch_slug AND b.status = 'active' LIMIT 2" in sql
    for forbidden in ("checkins", "checkin_events", "rejects", "reject_events", "INSERT", "UPDATE", "DELETE"):
        assert forbidden not in sql


# =====================================================================================================================
# All or nothing
# =====================================================================================================================

@pytest.mark.parametrize("report", REPORTS)
def test_a_mapped_site_that_does_not_resolve_fails_the_whole_report_and_is_never_called_unavailable(api, db, monkeypatch, report):
    _seed_acme(db)
    real = tenant_scope.resolve_operational_tenant
    monkeypatch.setattr(tenant_scope, "resolve_operational_tenant",
                        lambda user_id, org, branch: None if branch == "east" else real(user_id, org, branch))

    response = _failing(report)

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    for leaked in ("available", "checkin_count", "30", "main", "east"):
        assert leaked not in response.text


@pytest.mark.parametrize("report", REPORTS)
def test_a_user_who_stops_being_a_member_while_the_sites_are_read_gets_the_ordinary_404(api, db, monkeypatch, report):
    _seed_acme(db)
    real = tenant_scope.resolve_operational_tenant

    def removed_before_the_second_site(user_id, org, branch):
        if branch == "east":
            db.run("DELETE FROM memberships WHERE organization_id = 1 AND user_id = 1")
        return real(user_id, org, branch)

    monkeypatch.setattr(tenant_scope, "resolve_operational_tenant", removed_before_the_second_site)

    response = _failing(report)

    assert (response.status_code, response.json()) == (404, ORGANIZATION_NOT_FOUND)
    assert "checkin_count" not in response.text


@pytest.mark.parametrize(("report", "reader"), [
    ("overview", "get_overview_report"), ("routing-network", "get_routing_report"), ("reliability", "get_reliability_report"),
])
def test_one_site_that_cannot_be_read_fails_the_whole_report_with_no_partial_totals(api, db, monkeypatch, report, reader):
    _seed_acme(db)
    real = getattr(organization_report_service, reader)

    def failing_for_east(conn, tenant, *args):
        if tenant.branch_slug == "east":
            raise RuntimeError(f"synthetic failure reading a site for customer {ACME} {CANARY}")
        return real(conn, tenant, *args)

    monkeypatch.setattr(organization_report_service, reader, failing_for_east)

    response = _failing(report)

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    # Main was read in full before east failed. None of it is in the answer.
    assert str(MAIN) in {branch for _table, _customer, branch in db.scoped_reads()}
    for leaked in ("synthetic", "CANARY", str(ACME), "checkin", "reject", "30", "totals", "sorters", "Smith"):
        assert leaked not in response.text
    assert db.log[-1] == "close"


@pytest.mark.parametrize(("report", "table"), [
    ("overview", "organizations"), ("overview", "v2_cutovers"), ("overview", "checkins"), ("overview", "checkin_events"),
    ("overview", "rejects"), ("overview", "reject_events"),
    ("routing-network", "organizations"), ("routing-network", "v2_cutovers"), ("routing-network", "checkins"),
    ("routing-network", "checkin_events"),
    ("reliability", "v2_cutovers"), ("reliability", "checkins"), ("reliability", "checkin_events"),
    ("reliability", "rejects"), ("reliability", "reject_events"),
])
def test_a_failed_statement_is_a_500_that_says_nothing(api, db, report, table):
    _seed_acme(db)
    db.fail_on = table

    response = _failing(report)

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    for leaked in ("synthetic", "CANARY", str(ACME), "checkin", "reject", "Smith"):
        assert leaked not in response.text
    assert db.log[-1] == "close"


@pytest.mark.parametrize("report", REPORTS)
def test_a_tenant_context_that_does_not_match_is_a_500_and_nothing_is_read(api, db, report):
    _seed_acme(db)
    db.read_back = lambda settings: {**settings, "branch_id": str(BETA_NORTH)}

    response = _failing(report)

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    assert set(db.log) == {"open", "close"}


@pytest.mark.parametrize("report", REPORTS)
def test_two_listed_sites_with_one_operational_scope_are_refused_rather_than_counted_twice(api, db, report):
    _seed_acme(db)
    db.run("UPDATE branches SET operational_branch_id = :main WHERE slug = 'east' AND organization_id = 1", main=MAIN)

    response = _failing(report)

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)
    assert "60" not in response.text and "checkin_count" not in response.text


def _tenant(branch_slug: str, customer: int, branch: int) -> ResolvedOperationalTenant:
    return ResolvedOperationalTenant(org_slug="acme", branch_slug=branch_slug, access_mode="full",
                                     operational_customer_id=customer, operational_branch_id=branch)


def test_the_service_refuses_a_site_answer_that_is_not_the_shape_of_the_range(db, monkeypatch):
    # A site whose report covers a different number of days cannot be lined up with the others by date.
    from zoneinfo import ZoneInfo

    from services.operational_report_service import (
        RejectReasonRangeCounts,
        ReliabilityReport,
        local_range,
    )

    five_days = local_range(date(2026, 6, 8), date(2026, 6, 12), ZoneInfo("America/Chicago"))
    four = ReliabilityReport(
        checkin_days=(1, 1, 1, 1),
        rejects=RejectReasonRangeCounts(reason_counts=(0,) * len(REJECT_REASONS), day_counts=(0, 0, 0, 0), unexpected_class_rows=0),
    )
    monkeypatch.setattr(organization_report_service, "get_reliability_report", lambda conn, tenant, window: four)

    with pytest.raises(OrganizationReportError):
        organization_report_service.get_organization_reliability(
            "acme", five_days,
            resolve_site=lambda slug: _tenant(slug, ACME, MAIN if slug == "main" else EAST),
            open_site=tenant_scope.open_customer_tenant_connection,
        )


def test_the_service_reads_nothing_it_was_not_given_a_scope_for(db):
    opened = []

    class _Opened:
        def __init__(self, tenant):
            opened.append(tenant.branch_slug)

        def __enter__(self):
            return object()

        def __exit__(self, *_exc):
            return False

    # Main resolves; east does not, and it is mapped: the read stops there.
    with pytest.raises(SiteNotResolvedError) as raised:
        read_sorter_sites(
            "acme", lambda conn, tenant: tenant.branch_slug,
            resolve_site=lambda slug: _tenant(slug, ACME, MAIN) if slug == "main" else None,
            open_site=_Opened,
        )

    assert opened == ["main"]
    for leaked in ("acme", "east", str(ACME)):
        assert leaked not in str(raised.value)


# =====================================================================================================================
# Tenant isolation: one site at a time, each under its own scope
# =====================================================================================================================

@pytest.mark.parametrize("report", REPORTS)
def test_every_statement_runs_under_the_scope_of_the_one_site_it_reads(api, db, report):
    _seed_acme(db)

    _report(api, report)

    assert db.queries, "the report read nothing"
    for table, parameters, settings in db.queries:
        # The ids a statement is bound to are exactly the context it runs under: never wider, never another's.
        assert (str(parameters["customer_id"]), str(parameters["branch_id"])) == (settings["customer_id"], settings["branch_id"]), table
        assert set(parameters) - {"customer_id", "branch_id", "span_start", "span_end"} == {
            name for name in parameters if name.startswith("boundary_")}, table
    # Only Acme's two sorter sites were ever in scope -- not its other branches, and not another organization.
    assert {(customer, branch) for _table, customer, branch in db.scoped_reads()} == {(str(ACME), str(MAIN)), (str(ACME), str(EAST))}
    # One connection per site, each closed before the next is opened.
    assert [entry for entry in db.log if entry in ("open", "close")] == ["open", "close", "open", "close"]


def test_another_organizations_rows_and_a_branch_with_no_sorter_are_never_counted(api, db):
    _seed_other_tenants_and_other_branches(db)      # seven check-ins and seven rejects a day, per table, for each of five scopes

    overview, network, reliability = (_report(api, report) for report in REPORTS)

    assert overview["totals"] == {"checkin_count": 0, "home_count": 0, "transit_count": 0, "other_count": 0, "reject_count": 0}
    assert network["totals"] == {"checkin_count": 0, "transit_count": 0}
    assert reliability["totals"]["reject_count"] == 0 and reliability["totals"]["checkin_count"] == 0
    assert all(number == 0 or number in (1, 2, 5) for number in _numbers(overview))    # the range's days and collector counts

    # And Westside, which is one of Acme's branches and has rows of its own, becomes a sorter only by registration.
    # It has no cutover, so its history is its legacy rows: seven a day.
    db.install(1, WESTSIDE, "Westside AMH")
    assert _report(api, "overview")["totals"]["checkin_count"] == 35


def test_each_organization_sees_only_its_own_sorters(api, db, session):
    _seed_acme(db)
    db.v1("Main", 4, at=_local(6, 9, 9), customer=BETA, branch=BETA_NORTH)

    acme = _report(api, "overview")
    session.user = BOB
    beta = _report(api, "overview", org="beta")

    assert acme["totals"]["checkin_count"] == 39
    assert [sorter["slug"] for sorter in beta["sorters"]] == ["north"]
    # Beta's own four, plus the seven legacy rows a day the isolation seed put at its north branch.
    assert beta["totals"]["checkin_count"] == 4 + 7 * 5
    assert _get(api, "overview").json() == ORGANIZATION_NOT_FOUND       # Bob is not a member of Acme


def test_a_tenant_named_in_the_query_string_is_ignored(api, db):
    _seed_acme(db)

    body = _report(api, "overview", {**JUNE_8_TO_12, "customer_id": BETA, "branch_id": BETA_NORTH, "branch": "north",
                                     "org_slug": "beta", "sorter": "north"})

    assert body["totals"]["checkin_count"] == 39


@pytest.mark.parametrize("report", REPORTS)
def test_no_stored_message_item_identifier_or_machine_detail_is_in_any_report(api, db, report):
    _seed_acme(db)
    db.install(1, 13, "Unmapped Branch AMH")

    body = _report(api, report)
    text_ = str(body)

    for leaked in ("CANARY", "Smith", "31234", "9.9.9", "Item not found in database", "ACS timeout", "something_new",
                   "Depot", "Nowhere", "admin_lock", "branch_1", *EVERY_OPERATIONAL_ID):
        assert leaked not in text_, leaked
    for key in _keys(body):
        for forbidden in ("customer", "tenant", "_id", "barcode", "item", "message", "hostname", "version", "token", "installation"):
            assert forbidden not in key, key


# =====================================================================================================================
# Zero data, and organizations with nothing to read
# =====================================================================================================================

def _zeros(first: str, last: str) -> list[dict]:
    return [{"date": day, "checkin_count": 0, "reject_count": 0} for day in _dates(first, last)]


def test_one_sorter_with_no_activity_is_a_complete_answer_made_of_zeros(api, db):
    solo = {"slug": "main", "name": "Solo AMH", "host_branch": {"slug": "main", "name": "Solo Main"}}

    assert _report(api, "overview", org="solo") == {
        "range": RANGE,
        "totals": {"checkin_count": 0, "home_count": 0, "transit_count": 0, "other_count": 0, "reject_count": 0},
        "sorters": [{**solo, "status": "active", "collector_count": 1, "available": True,
                     "checkin_count": 0, "active_days": 0, "transit_count": 0, "reject_count": 0}],
        "days": _zeros("2026-06-08", "2026-06-12"),
    }
    # No routing settings at all: a source with no destinations, labelled with the site's own name.
    assert _report(api, "routing-network", org="solo") == {
        "range": RANGE,
        "totals": {"checkin_count": 0, "transit_count": 0},
        "sources": [{"sorter": solo, "checkin_count": 0, "home": {"label": "Solo Main", "checkin_count": 0},
                     "transit_count": 0, "other_count": 0, "transit": []}],
        "destinations": [],
    }
    assert _report(api, "reliability", org="solo") == {
        "range": RANGE,
        "totals": {"checkin_count": 0, "reject_count": 0, "reasons": _reasons({})},
        "sorters": [{"sorter": solo, "available": True, "checkin_count": 0, "reject_count": 0, "reasons": _reasons({})}],
        "days": _zeros("2026-06-08", "2026-06-12"),
    }


def test_several_sorters_with_no_activity_are_all_there_with_zeros(api, db):
    overview, network, reliability = (_report(api, report) for report in REPORTS)

    assert [(sorter["slug"], sorter["available"], sorter["checkin_count"]) for sorter in overview["sorters"]] == [
        ("main", True, 0), ("east", True, 0)]
    assert overview["days"] == _zeros("2026-06-08", "2026-06-12")
    assert [len(source["transit"]) for source in network["sources"]] == [3, 2]
    assert all(destination["checkin_count"] == 0 for destination in network["destinations"]) and len(network["destinations"]) == 4
    assert reliability["totals"]["reasons"] == _reasons({}) and len(reliability["sorters"]) == 2


def test_an_organization_with_no_registered_sorter_is_a_200_of_zeros_not_a_404(api, db):
    assert _report(api, "overview", org="empty") == {
        "range": RANGE,
        "totals": {"checkin_count": 0, "home_count": 0, "transit_count": 0, "other_count": 0, "reject_count": 0},
        "sorters": [],
        "days": _zeros("2026-06-08", "2026-06-12"),
    }
    assert _report(api, "routing-network", org="empty") == {
        "range": RANGE, "totals": {"checkin_count": 0, "transit_count": 0}, "sources": [], "destinations": [],
    }
    assert _report(api, "reliability", org="empty") == {
        "range": RANGE,
        "totals": {"checkin_count": 0, "reject_count": 0, "reasons": _reasons({})},
        "sorters": [],
        "days": _zeros("2026-06-08", "2026-06-12"),
    }
    assert db.log == []


def test_check_ins_with_no_rejects_have_all_eight_reasons_at_zero(api, db):
    db.v1("Main", 5, at=_local(6, 9, 9))
    db.v1("East", 2, at=_local(6, 9, 9), branch=EAST)

    reliability = _report(api, "reliability")

    assert reliability["totals"] == {"checkin_count": 7, "reject_count": 0, "reasons": _reasons({})}
    assert [sorter["reasons"] for sorter in reliability["sorters"]] == [_reasons({}), _reasons({})]
    assert all(day["reject_count"] == 0 for day in reliability["days"])


def test_rejects_with_no_check_ins_are_counted_all_the_same(api, db):
    db.reject_v1("Item not found in database", 2, at=_local(6, 9, 9))
    db.reject_v1("ACS timeout", 3, at=_local(6, 11, 9), branch=EAST)

    reliability = _report(api, "reliability")

    assert reliability["totals"] == {"checkin_count": 0, "reject_count": 5,
                                     "reasons": _reasons({"item_not_found": 2, "ils_acs_failure": 3})}
    assert [day["reject_count"] for day in reliability["days"]] == [0, 2, 0, 3, 0]


def test_every_reason_is_summed_across_sorters_in_the_canonical_order(api, db):
    for index, reason in enumerate(REJECT_REASONS):
        db.reject_v2(reason, index + 1, at=_utc(6, 9, 15))
        db.reject_v2(reason, 10, at=_utc(6, 10, 15), branch=EAST)
    db.cutover(_utc(6, 1, 0))
    db.cutover(_utc(6, 1, 0), branch=EAST)

    reliability = _report(api, "reliability")

    assert [reason["reason"] for reason in reliability["totals"]["reasons"]] == list(REJECT_REASONS)
    assert [reason["reject_count"] for reason in reliability["totals"]["reasons"]] == [11, 12, 13, 14, 15, 16, 17, 18]
    assert reliability["totals"]["reject_count"] == 36 + 80
    assert [sorter["reject_count"] for sorter in reliability["sorters"]] == [36, 80]


# =====================================================================================================================
# Mixed eras: each site's own cutover, added up without moving or repeating a row
# =====================================================================================================================

def test_sites_on_either_side_of_and_across_their_cutovers_add_up_to_what_each_reports(api, db):
    # Main crosses its cutover inside the range (noon on the 10th). East has none: legacy only.
    _seed_acme(db)
    # North was cut over long before the range: current rows only. Its legacy rows inside the range are not history.
    db.install(1, NORTH, "North AMH")
    db.cutover(datetime(2026, 1, 1, 6, 0, tzinfo=UTC), branch=NORTH)
    db.v2("main", 4, at=_utc(6, 11, 15), branch=NORTH)
    db.v2("westside", 1, at=_utc(6, 12, 4, 59), branch=NORTH)      # 11:59 PM on the 11th, local
    db.v2("westside", 1, at=_utc(6, 12, 5, 0), branch=NORTH)       # midnight on the 12th, local
    db.v1("Main", 50, at=_local(6, 11, 9), branch=NORTH)
    db.reject_v2("rfid_collision", 2, at=_utc(6, 11, 15), branch=NORTH)
    db.reject_v1("ACS timeout", 50, at=_local(6, 11, 9), branch=NORTH)

    _assert_organization_is_the_sum_of_its_sites(api, "acme", ["main", "east", "north"])

    overview = _report(api, "overview")
    assert [sorter["checkin_count"] for sorter in overview["sorters"]] == [30, 9, 6]
    assert overview["totals"]["checkin_count"] == 45 and overview["totals"]["reject_count"] == 11
    #                                                        main + east + north
    assert [day["checkin_count"] for day in overview["days"]] == [6 + 6 + 0, 8 + 0 + 0, 8 + 0 + 0, 8 + 0 + 5, 0 + 3 + 1]
    assert [day["reject_count"] for day in overview["days"]] == [3, 0, 3, 5, 0]

    # Each site read only the tables of the eras it has in the range.
    db.queries.clear()
    _report(api, "reliability")
    tables = {branch: [table for table, _customer, site in db.scoped_reads() if site == str(branch)] for branch in (MAIN, EAST, NORTH)}
    assert tables[MAIN] == ["v2_cutovers", "checkins", "checkin_events", "rejects", "reject_events"]
    assert tables[EAST] == ["v2_cutovers", "checkins", "rejects"]
    assert tables[NORTH] == ["v2_cutovers", "checkin_events", "reject_events"]


def test_every_site_is_read_over_the_same_dates_in_the_same_zone(api, db, monkeypatch):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Asia/Tokyo")
    # 23:30 on 9 June in Tokyo is 14:30Z; legacy rows are wall-clock in the product's zone.
    db.v1("Main", 2, at=_local(6, 9, 23, 30))
    db.cutover(_utc(6, 1, 0), branch=EAST)
    db.v2("east", 3, at=_utc(6, 9, 14, 30), branch=EAST)       # 23:30 on the 9th in Tokyo
    db.v2("east", 4, at=_utc(6, 9, 15, 0), branch=EAST)        # midnight on the 10th in Tokyo

    overview = _report(api, "overview")

    assert overview["range"]["timezone"] == "Asia/Tokyo"
    assert [day["checkin_count"] for day in overview["days"]] == [0, 5, 4, 0, 0]
    _assert_organization_is_the_sum_of_its_sites(api, "acme", ["main", "east"])


# =====================================================================================================================
# The range: the sorter-site reports' own rules
# =====================================================================================================================

def _range_of(days: int, *, ending: str = "2026-06-20") -> dict:
    end = date.fromisoformat(ending)
    return {"from": (end - timedelta(days=days - 1)).isoformat(), "to": end.isoformat()}


def test_the_range_dependency_is_the_sorter_site_reports_own():
    for route in main.app.routes:
        if getattr(route, "path", "").startswith("/api/organizations/{org_slug}/reports/"):
            calls = [dependency.call for dependency in get_flat_dependant(route.dependant).dependencies]
            assert report_routes.require_report_range in calls, route.path
    assert organization_report_routes.Range is report_routes.Range
    for word in ("validate_report_range", "MAX_REPORT_RANGE_DAYS", "datetime", "product_timezone", "local_range("):
        assert word not in inspect.getsource(organization_report_routes), word


@pytest.mark.parametrize("report", REPORTS)
def test_ninety_two_days_is_accepted_and_ninety_three_is_refused(api, db, report):
    db.v1("Main", 1, at=datetime(2026, 3, 21, 9, 0))  # noqa: DTZ001 - the first day of the 92
    db.v1("East", 1, at=_local(6, 20, 9), branch=EAST)

    accepted = _get(api, report, _range_of(92))

    assert accepted.status_code == 200
    assert accepted.json()["range"]["days"] == 92 and accepted.json()["range"]["from"] == "2026-03-21"
    assert accepted.json()["totals"]["checkin_count"] == 2
    if report != "routing-network":
        days = accepted.json()["days"]
        assert [day["date"] for day in days] == _dates("2026-03-21", "2026-06-20")
        assert days[0]["checkin_count"] == 1 and days[-1]["checkin_count"] == 1

    db.log.clear()
    refused = _get(api, report, _range_of(93))

    assert refused.status_code == 422
    assert db.log == []


@pytest.mark.parametrize("report", REPORTS)
@pytest.mark.parametrize("params", [
    {"from": "2026-06-12", "to": "2026-06-08"},
    {"from": "2026-06-19", "to": "2026-06-21"},
    {"from": "2026-06-08"},
    {"to": "2026-06-12"},
    {},
    {"from": "2026-6-8", "to": "2026-06-12"},
    {"from": "2026-06-08T00:00:00", "to": "2026-06-12"},
    {"from": "06/08/2026", "to": "06/12/2026"},
    {"from": "2026-02-30", "to": "2026-03-02"},
    {"from": "today", "to": "today"},
])
def test_a_range_that_cannot_be_reported_on_is_a_422_and_nothing_operational_is_read(api, db, report, params):
    _seed_acme(db)

    response = _get(api, report, params)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert db.log == [] and db.queries == []
    for echoed in ("2026-06-21", "06/08/2026", "today", "2026-6-8"):
        assert echoed not in response.text


def test_a_refused_range_is_refused_exactly_as_the_sorter_site_reports_refuse_it(api, db):
    for params in ({"from": "2026-06-12", "to": "2026-06-08"}, {"from": "2026-06-19", "to": "2026-06-21"}, _range_of(93)):
        organization = _get(api, "overview", params)
        site = api.get(SITE.format(org="acme", branch="main", report="overview"), headers=COOKIE, params=params)

        assert organization.status_code == site.status_code == 422
        assert organization.json() == site.json()


def test_today_is_allowed_and_flagged_and_its_counts_are_in_the_totals(api, db):
    db.v1("Main", 2, at=_local(6, 20, 9))
    db.v1("East", 3, at=_local(6, 20, 9), branch=EAST)

    for report in REPORTS:
        today = _report(api, report, {"from": "2026-06-19", "to": "2026-06-20"})
        assert today["range"] == {"from": "2026-06-19", "to": "2026-06-20", "days": 2, "timezone": "America/Chicago",
                                  "includes_today": True}
        assert today["totals"]["checkin_count"] == 5
        assert _report(api, report, {"from": "2026-06-18", "to": "2026-06-19"})["range"]["includes_today"] is False
    assert _report(api, "overview", {"from": "2026-06-19", "to": "2026-06-20"})["days"][-1] == {
        "date": "2026-06-20", "checkin_count": 5, "reject_count": 0}


def test_today_is_the_products_date_not_utcs(api, db, clock):
    # 11:30 PM on 20 June in Chicago is already the 21st in UTC.
    clock.set(datetime(2026, 6, 21, 4, 30, tzinfo=UTC))

    assert _get(api, "overview", {"from": "2026-06-20", "to": "2026-06-20"}).json()["range"]["includes_today"] is True
    assert _get(api, "overview", {"from": "2026-06-20", "to": "2026-06-21"}).status_code == 422


def test_rows_outside_the_range_are_not_in_it(api, db):
    _seed_acme(db)
    db.v1("Main", 40, at=_local(6, 7, 23, 59))
    db.v1("East", 40, at=_local(6, 13, 0, 0), branch=EAST)

    assert _report(api, "overview")["totals"]["checkin_count"] == 39


# =====================================================================================================================
# Who may read an organization's reports
# =====================================================================================================================

@pytest.mark.parametrize("report", REPORTS)
def test_no_session_is_a_401_and_nothing_is_read(api, db, session, report):
    session.user = None

    for headers in (COOKIE, {}):
        response = _get(api, report, headers=headers)
        assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert db.log == []


@pytest.mark.parametrize("report", REPORTS)
@pytest.mark.parametrize("org", ["beta", "nope", "closed", "ACME", "acme ", "acme/../beta"])
def test_an_organization_the_user_cannot_see_is_the_same_404_whatever_the_reason(api, db, report, org):
    # beta: not a member. nope: no such organization. closed: cancelled, though Alice is a member of it.
    _seed_acme(db)

    response = _get(api, report, org=org)

    assert response.status_code == 404
    if org != "acme/../beta":
        assert response.json() == ORGANIZATION_NOT_FOUND
    assert db.log == []


def test_the_404_is_the_organization_details_own():
    # One function makes the answer for both, so the two cannot drift apart.
    source = inspect.getsource(organization_report_routes.require_organization_member)

    assert source.count("raise _organization_not_found()") == 2
    assert "_visible_access_mode(org_slug)" in source and "get_user_memberships" in source


def test_a_member_of_another_organization_cannot_read_this_one(api, db, session):
    _seed_acme(db)
    session.user = BOB

    for report in REPORTS:
        assert (_get(api, report).status_code, _get(api, report).json()) == (404, ORGANIZATION_NOT_FOUND)
        assert _get(api, report, org="beta").status_code == 200
    assert all(customer == str(BETA) for _table, customer, _branch in db.scoped_reads())


def test_a_suspended_organization_can_still_be_read(api, db):
    db.v1("Main", 3, at=_local(6, 9, 9), customer=PAUSED, branch=PAUSED_MAIN)

    overview = _report(api, "overview", org="paused")

    assert overview["totals"]["checkin_count"] == 3
    assert overview["sorters"][0]["name"] == "Paused AMH"
    assert access_service.get_org_access_mode("paused") == "read_only"
    assert _report(api, "routing-network", org="paused")["totals"]["checkin_count"] == 3
    assert _report(api, "reliability", org="paused")["totals"]["checkin_count"] == 3


def test_a_request_is_authenticated_then_the_organization_checked_then_its_range_judged(api, db, session):
    bad_range = {"from": "2026-06-12", "to": "2026-06-08"}

    assert _get(api, "overview", bad_range, org="beta").status_code == 404
    assert _get(api, "overview", bad_range).status_code == 422
    session.user = None
    assert _get(api, "overview", bad_range, org="beta").status_code == 401


def test_being_a_member_does_not_open_a_site_the_resolver_refuses(api, db):
    # The organization check lets Alice in. Each site is still resolved for HER, by the one resolver.
    _seed_acme(db)
    db.run("UPDATE app_users SET is_active = 0 WHERE id = 1")

    response = _failing("overview")

    # An inactive user's session would not normally validate at all; if one did, no site resolves for them and
    # nothing is read.
    assert response.status_code == 500
    assert db.log == []


# =====================================================================================================================
# The routes and the modules
# =====================================================================================================================

def _endpoint(report: str):
    path = ORG.format(org="{org_slug}", report=report)
    (route,) = [route for route in main.app.routes if getattr(route, "path", "") == path]
    return route


def test_the_organization_report_routes_are_exactly_these_three_and_are_read_only():
    routes = sorted(
        (route.path, sorted(route.methods)) for route in main.app.routes
        if getattr(route, "path", "").startswith("/api/organizations/{org_slug}/reports/")
    )

    assert routes == [
        ("/api/organizations/{org_slug}/reports/overview", ["GET"]),
        ("/api/organizations/{org_slug}/reports/reliability", ["GET"]),
        ("/api/organizations/{org_slug}/reports/routing-network", ["GET"]),
    ]


@pytest.mark.parametrize("report", REPORTS)
def test_each_route_takes_the_organization_slug_and_the_two_dates_and_nothing_else(report):
    dependant = get_flat_dependant(_endpoint(report).dependant)

    # The route and its organization check each read the one slug in the path.
    assert {parameter.name for parameter in dependant.path_params} == {"org_slug"}
    assert sorted(parameter.alias for parameter in dependant.query_params) == ["from", "to"]
    assert dependant.body_params == [] and dependant.header_params == []


@pytest.mark.parametrize("report", REPORTS)
def test_other_methods_are_refused(api, db, report):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(api, method)(ORG.format(org="acme", report=report), headers=COOKIE).status_code == 405
    assert db.log == []


def test_the_routes_count_nothing_open_no_scope_of_their_own_and_run_no_sql():
    source = inspect.getsource(organization_report_routes)

    for forbidden in ("sum(", "+=", "text(", "execute(", "get_engine", "tenant_connection(", "set_config",
                      "resolve_operational_tenant", "tenant_resolution_service", "import database", "pandas", "streamlit"):
        assert forbidden not in source, forbidden
    # The two things it hands the service are the sorter-site reports' own.
    assert organization_report_routes.open_customer_tenant_connection is tenant_scope.open_customer_tenant_connection
    assert organization_report_routes.resolve_site_tenant is tenant_scope.resolve_site_tenant
    assert organization_report_routes.require_organization_member.__module__ == "customer_api.organization_routes"


def test_the_service_composes_the_site_reports_and_has_one_statement_of_its_own():
    source = inspect.getsource(organization_report_service)
    code = source.split('"""', 2)[2]

    assert code.count("text(") == 1                         # the mapping check, and nothing else
    for forbidden in ("COUNT(", "checkins", "checkin_events", "reject_events", "set_config", "tenant_connection",
                      "import pandas", "import streamlit", "fastapi", "lru_cache", "cache", "requests", "httpx",
                      "customer_api", "resolve_operational_tenant", "datetime", "ZoneInfo"):
        assert forbidden not in code, forbidden
    for reused in ("get_overview_report", "get_routing_report", "get_reliability_report", "report_window",
                   "get_routing_config", "list_sorter_sites"):
        assert reused in code, reused


def test_a_site_scope_is_only_ever_obtained_through_the_resolver_it_is_given():
    signature = inspect.signature(read_sorter_sites)

    assert list(signature.parameters) == ["org_slug", "read_site", "resolve_site", "open_site"]
    for name in ("get_organization_overview", "get_organization_routing_network", "get_organization_reliability"):
        parameters = inspect.signature(getattr(organization_report_service, name)).parameters
        assert list(parameters) == ["org_slug", "local_range", "resolve_site", "open_site"]
        assert parameters["resolve_site"].kind is parameters["open_site"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_response_models_have_exactly_the_approved_fields_and_refuse_any_other():
    fields = {
        name: list(model.model_fields)
        for name, model in vars(organization_report_schemas).items()
        if isinstance(model, type) and issubclass(model, organization_report_schemas._ResponseModel)
        and model.__module__ == organization_report_schemas.__name__ and not name.startswith("_")
    }

    assert fields == {
        "SorterIdentity": ["slug", "name", "host_branch"],
        "OrganizationOverviewTotals": ["checkin_count", "home_count", "transit_count", "other_count", "reject_count"],
        "OrganizationSorterOverview": ["slug", "name", "host_branch", "status", "collector_count", "available",
                                       "checkin_count", "active_days", "transit_count", "reject_count"],
        "OrganizationOverviewResponse": ["range", "totals", "sorters", "days"],
        "RoutingNetworkTotals": ["checkin_count", "transit_count"],
        "RoutingNetworkSource": ["sorter", "checkin_count", "home", "transit_count", "other_count", "transit"],
        "RoutingNetworkDestination": ["key", "label", "checkin_count", "source_count"],
        "RoutingNetworkResponse": ["range", "totals", "sources", "destinations"],
        "OrganizationReliabilityTotals": ["checkin_count", "reject_count", "reasons"],
        "OrganizationSorterReliability": ["sorter", "available", "checkin_count", "reject_count", "reasons"],
        "OrganizationReliabilityResponse": ["range", "totals", "sorters", "days"],
    }
    for name in fields:
        assert getattr(organization_report_schemas, name).model_config["extra"] == "forbid", name


def test_the_service_results_carry_no_identifier():
    for name in ("SorterOverview", "OrganizationOverview", "RoutingSource", "NetworkDestination",
                 "OrganizationRoutingNetwork", "SorterReliability", "OrganizationReliability", "SiteRead"):
        for field in dataclasses.fields(getattr(organization_report_service, name)):
            assert "id" not in field.name.split("_") and "customer" not in field.name, (name, field.name)


# =====================================================================================================================
# What a report costs: statements per sorter site
# =====================================================================================================================

@pytest.mark.parametrize(("report", "per_site"), [
    # settings, cutover, then one statement per era that owns part of the range, per figure.
    ("overview", {MAIN: ["organizations", "v2_cutovers", "checkins", "checkin_events", "rejects", "reject_events"],
                  EAST: ["organizations", "v2_cutovers", "checkins", "rejects"]}),
    ("routing-network", {MAIN: ["organizations", "v2_cutovers", "checkins", "checkin_events"],
                         EAST: ["organizations", "v2_cutovers", "checkins"]}),
    ("reliability", {MAIN: ["v2_cutovers", "checkins", "checkin_events", "rejects", "reject_events"],
                     EAST: ["v2_cutovers", "checkins", "rejects"]}),
])
def test_the_statements_run_for_each_sorter_site(api, db, report, per_site):
    _seed_acme(db)       # main crosses its cutover (both eras); east is legacy only
    statements: list[str] = []
    listener = lambda conn, cursor, statement, *rest: statements.append(" ".join(statement.split()))
    event.listen(db.engine, "before_cursor_execute", listener)
    try:
        _report(api, report)
    finally:
        event.remove(db.engine, "before_cursor_execute", listener)

    for branch, tables in per_site.items():
        assert [table for table, _customer, site in db.scoped_reads() if site == str(branch)] == tables

    # Everything else the request runs: who the user is a member of, the organization's status, its sorters --
    # once each -- and one resolution per site. Nothing is looked up once per row or per day.
    unscoped = [statement for statement in statements if " FROM memberships" in statement or " FROM organizations WHERE" in statement
                or " FROM collector_installations" in statement]
    resolutions = [statement for statement in unscoped if "JOIN branches b ON b.organization_id = o.id WHERE m.user_id" in statement]
    inventory = [statement for statement in unscoped if " FROM collector_installations" in statement]
    assert len(resolutions) == 2 and len(inventory) == 1
    assert len(statements) == 3 + len(resolutions) + sum(len(tables) for tables in per_site.values())
    # The mapping check runs only for a site that did not resolve.
    assert not any("organization_is_mapped" in statement for statement in statements)
