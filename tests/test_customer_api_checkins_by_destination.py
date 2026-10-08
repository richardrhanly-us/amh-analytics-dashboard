"""F5.5: the single-date check-ins-by-destination endpoint.

    GET /api/organizations/{org_slug}/branches/{branch_slug}/checkins/by-destination?date=YYYY-MM-DD

These tests drive the real production route through TestClient(main.app): the
session dependency, the tenant-scope dependency, the scoped and verified
connection, the settings read, the routing configuration, the metrics service
and the response schemas all run for real, and so does every SQL statement
-- against an in-memory SQLite database holding the SaaS tables, the two
settings tables, both check-in tables and v2_cutovers.

The one thing SQLite cannot do is carry PostgreSQL's row level security
context, so the operational engine is that same database behind a thin
wrapper that answers the set_config / current_setting calls the way
PostgreSQL does and passes everything else through. Row level security
itself, real TIMESTAMP / TIMESTAMPTZ comparison and real JSONB settings are
tested against a real server in tests/test_rls_phase1_postgres.py; here the
statements' own tenant filters are what scope every read, which is exactly
what makes a missing filter show.

The pure classification rules are in tests/test_routing_destination.py.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from entitlement_support import grant
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import main
from customer_api import operational_routes, operational_schemas, tenant_scope
from customer_api.operational_schemas import (
    CheckinsByDestinationResponse,
    RoutingDestination,
    RoutingHome,
)
from services import (
    operational_metrics_service,
    routing_config_service,
    session_service,
    tenant_resolution_service,
)
from services.operational_metrics_service import (
    CheckinDestinationCounts,
    get_checkin_counts_by_destination,
)
from services.routing_destination import RoutingConfig, TransitDestination

PATH = "/api/organizations/{org}/branches/{branch}/checkins/by-destination"
ACME_MAIN = PATH.format(org="acme", branch="main")
ACME_EAST = PATH.format(org="acme", branch="east")
CHECKIN_COUNT_ACME_MAIN = "/api/organizations/acme/branches/main/checkins/count"
CHECKINS_BY_HOUR_ACME_MAIN = "/api/organizations/acme/branches/main/checkins/by-hour"
COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 1, "email": "alice@example.invalid", "full_name": "Alice"}

CUSTOMER, BRANCH, EAST = 8101, 11, 14
OTHER_CUSTOMER, OTHER_BRANCH = 8202, 21
JUNE_10 = {"date": "2026-06-10"}
NOON_CUTOVER = datetime(2026, 6, 10, 17, 0, tzinfo=UTC)   # 12:00 local on 10 June in America/Chicago

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
TENANT_NOT_FOUND = {"code": "tenant_not_found", "message": "Organization or branch not found."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

CANARY = "CANARY-31234000123456 Smith, Pat"
_STORED = "%Y-%m-%d %H:%M:%S.%f"

# The administrator's free-form keys are deliberately useless ("branch_1"): a destination is matched by its label.
ACME_SETTINGS = {
    "library_name": "Acme Public Library",
    "security": {"admin_lock_hash": CANARY},
    "transit": {
        "home_branch_label": "Main",
        "destinations": [
            {"key": "branch_1", "label": "Westside", "enabled": True},
            {"key": "branch_2", "label": "Library Express", "enabled": True},
            {"key": "branch_3", "label": "Old Annex", "enabled": False},
        ],
    },
}
# The east site overrides the organization's transit block with its own.
EAST_SETTINGS = {"transit": {"home_branch_label": "East", "destinations": [{"key": "m", "label": "Main Library"}]}}
BETA_SETTINGS = {"transit": {"home_branch_label": "North", "destinations": [{"key": "x", "label": "Westside"}]}}


def _local(day: int, hour: int = 10, minute: int = 0) -> datetime:
    """A NAIVE local wall-clock datetime in June 2026, as checkins.event_time holds it."""
    return datetime(2026, 6, day, hour, minute)  # noqa: DTZ001


def _utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=UTC)


# --- one SQLite database: the SaaS tables, the settings and the check-ins --------------------------------------------

_SCHEMA = (
    "CREATE TABLE app_users (id INTEGER PRIMARY KEY, email TEXT, is_active BOOLEAN)",
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT, name TEXT, status TEXT, operational_customer_id INTEGER)",
    (
        "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, slug TEXT, name TEXT, status TEXT, "
        "operational_branch_id INTEGER)"
    ),
    "CREATE TABLE memberships (id INTEGER PRIMARY KEY, organization_id INTEGER, user_id INTEGER, role TEXT, removed_at TEXT)",
    "CREATE TABLE organization_settings (id INTEGER PRIMARY KEY, organization_id INTEGER UNIQUE, settings_json TEXT)",
    "CREATE TABLE branch_settings (id INTEGER PRIMARY KEY, branch_id INTEGER UNIQUE, settings_json TEXT)",
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
    "INSERT INTO app_users (id, email, is_active) VALUES (1, 'alice@example.invalid', 1)",
    (
        f"INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES "
        f"(1, 'acme', 'Acme Public Library', 'active', {CUSTOMER}), (2, 'beta', 'Beta Libraries', 'active', {OTHER_CUSTOMER}), "
        f"(3, 'unmapped-org', 'Unmapped', 'active', NULL), (4, 'closed', 'Closed', 'cancelled', 8404), "
        f"(5, 'paused', 'Paused', 'suspended', 8505)"
    ),
    (
        f"INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) VALUES "
        f"({BRANCH}, 1, 'main', 'Main Library', 'active', {BRANCH}), (12, 1, 'shut', 'Shut', 'inactive', 12), "
        f"(13, 1, 'unmapped', 'Unmapped', 'active', NULL), ({EAST}, 1, 'east', 'East Library', 'active', {EAST}), "
        f"({OTHER_BRANCH}, 2, 'north', 'North', 'active', {OTHER_BRANCH}), (31, 3, 'main', 'Main', 'active', 31), "
        f"(41, 4, 'main', 'Main', 'active', 41), (51, 5, 'main', 'Paused Main', 'active', 51)"
    ),
    # Alice belongs to acme, unmapped-org, closed and paused -- not to beta.
    (
        "INSERT INTO memberships (organization_id, user_id, role) VALUES "
        "(1, 1, 'viewer'), (3, 1, 'admin'), (4, 1, 'admin'), (5, 1, 'viewer')"
    ),
)


class _ContextResult:
    def __init__(self, value):
        self._value = value

    def mappings(self):
        return self

    def first(self):
        return self._value


class _ScopedConnection:
    """A real SQLite connection that also keeps the two transaction-local
    settings PostgreSQL would."""

    def __init__(self, owner, connection):
        self.owner = owner
        self._connection = connection
        self.settings: dict[str, str] = {}

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        owner = self.owner

        if "set_config('app.operational_customer_id'" in sql:
            self.settings["customer_id"] = parameters["v"]
            return _ContextResult(None)
        if "set_config('app.operational_branch_id'" in sql:
            self.settings["branch_id"] = parameters["v"]
            return _ContextResult(None)
        if "current_setting(" in sql:
            return _ContextResult(owner.read_back(dict(self.settings)))

        table = sql.split(" FROM ", 1)[1].split()[0]
        owner.log.append(table)
        owner.statements.append(sql)
        owner.queries.append((table, dict(parameters or {}), dict(self.settings)))
        if owner.fail_on == table:
            raise RuntimeError(f"synthetic database failure reading {table} for customer {CUSTOMER} {CANARY}")
        return self._connection.execute(statement, parameters or {})


class ScopedDatabase:
    """The flat database engine, as the tenant scope sees it."""

    def __init__(self, engine):
        self.engine = engine
        self.fail_on: str | None = None
        self.read_back = lambda settings: settings
        self.log: list[str] = []
        self.statements: list[str] = []
        self.queries: list[tuple] = []

    def connect(self):
        owner = self

        class _Opened:
            def __enter__(self):
                self._real = owner.engine.connect()
                owner.log.append("open")
                return _ScopedConnection(owner, self._real)

            def __exit__(self, *_exc):
                self._real.close()
                owner.log.append("close")
                return False

        return _Opened()

    # --- seeding ----------------------------------------------------------------------------------------------------

    def settings(self, *, organization: int | None = None, branch: int | None = None, document) -> None:
        stored = document if isinstance(document, str) else json.dumps(document)
        with self.engine.begin() as conn:
            if organization is not None:
                conn.execute(text("DELETE FROM organization_settings WHERE organization_id = :o"), {"o": organization})
                conn.execute(text("INSERT INTO organization_settings (organization_id, settings_json) VALUES (:o, :s)"),
                             {"o": organization, "s": stored})
            if branch is not None:
                conn.execute(text("DELETE FROM branch_settings WHERE branch_id = :b"), {"b": branch})
                conn.execute(text("INSERT INTO branch_settings (branch_id, settings_json) VALUES (:b, :s)"),
                             {"b": branch, "s": stored})

    def v1(self, destination, count: int = 1, *, at: datetime | None = None, customer=CUSTOMER, branch=BRANCH) -> None:
        """`count` legacy rows with this stored destination, at a naive local time."""
        at = at or _local(10)
        with self.engine.begin() as conn:
            for number in range(count):
                conn.execute(
                    text("INSERT INTO checkins (customer_id, branch_id, event_time, barcode, title, destination) "
                         "VALUES (:c, :b, :t, :bc, :ti, :d)"),
                    {"c": customer, "b": branch, "t": at.strftime(_STORED), "bc": f"{CANARY}-{number}", "ti": CANARY,
                     "d": destination},
                )

    def v2(self, destination, count: int = 1, *, at: datetime | None = None, customer=CUSTOMER, branch=BRANCH) -> None:
        """`count` Contract v2 rows with this stored destination, at an aware instant (stored as UTC)."""
        at = at or _utc(10, 18)
        with self.engine.begin() as conn:
            for _ in range(count):
                conn.execute(
                    text("INSERT INTO checkin_events (customer_id, branch_id, event_time, item_key, destination) "
                         "VALUES (:c, :b, :t, :ik, :d)"),
                    {"c": customer, "b": branch, "t": at.astimezone(UTC).strftime(_STORED), "ik": CANARY, "d": destination},
                )

    def cutover(self, cutover_at: datetime | None, *, customer=CUSTOMER, branch=BRANCH, set_at="2026-01-01 00:00:00+00:00"):
        with self.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, set_at) "
                     "VALUES (:c, :b, :at, 'test', :set_at)"),
                {"c": customer, "b": branch, "at": None if cutover_at is None else cutover_at.isoformat(sep=" "),
                 "set_at": set_at},
            )


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in _SCHEMA:
            conn.execute(text(statement))
    database = ScopedDatabase(engine)
    database.settings(organization=1, document=ACME_SETTINGS)
    database.settings(branch=EAST, document=EAST_SETTINGS)
    database.settings(organization=2, document=BETA_SETTINGS)
    monkeypatch.setattr(tenant_resolution_service, "get_engine", lambda: engine)
    monkeypatch.setattr(tenant_scope, "get_engine", lambda: database)
    yield database
    engine.dispose()


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: dict(USER))
    # Every plan feature, with no history limit: what a plan does to these reports is tested on its own (R9C).
    grant(monkeypatch)
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


def _get(api, path=ACME_MAIN, params=JUNE_10, headers=COOKIE):
    return api.get(path, headers=headers, params=params)


def _routing(api, path=ACME_MAIN, params=JUNE_10) -> dict:
    """The answer, after checking it is the approved shape and that every check-in is in exactly one place."""
    response = _get(api, path, params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == ["date", "timezone", "checkin_count", "home", "transit", "transit_count", "other_count"]
    assert list(body["home"]) == ["label", "checkin_count"]
    assert all(list(entry) == ["key", "label", "checkin_count"] for entry in body["transit"])
    counts = [body["checkin_count"], body["home"]["checkin_count"], body["transit_count"], body["other_count"],
              *(entry["checkin_count"] for entry in body["transit"])]
    assert all(type(count) is int and count >= 0 for count in counts)
    assert body["transit_count"] == sum(entry["checkin_count"] for entry in body["transit"])
    assert body["home"]["checkin_count"] + body["transit_count"] + body["other_count"] == body["checkin_count"]
    return body


def _split(body: dict) -> tuple[int, dict[str, int], int]:
    """(home, {destination key: count}, other)."""
    return (body["home"]["checkin_count"],
            {entry["key"]: entry["checkin_count"] for entry in body["transit"]},
            body["other_count"])


# =====================================================================================================================
# Success and the shape of the answer
# =====================================================================================================================

def test_a_member_gets_the_days_check_ins_by_destination(api, db):
    db.v1("Main", 5)
    db.v1("Westside", 3)
    db.v1("Library Express", 2)
    db.v1("No Agency Destination", 1)

    response = _get(api)

    assert response.status_code == 200
    assert response.json() == {
        "date": "2026-06-10",
        "timezone": "America/Chicago",
        "checkin_count": 11,
        "home": {"label": "Main", "checkin_count": 5},
        "transit": [
            {"key": "westside", "label": "Westside", "checkin_count": 3},
            {"key": "library_express", "label": "Library Express", "checkin_count": 2},
        ],
        "transit_count": 5,
        "other_count": 1,
    }
    assert response.headers["cache-control"] == "no-store"


def test_a_day_with_no_check_ins_is_all_zeros_with_every_configured_destination_still_listed(api, db):
    body = _routing(api)

    assert body["checkin_count"] == 0
    assert _split(body) == (0, {"westside": 0, "library_express": 0}, 0)
    assert body["home"]["label"] == "Main"
    assert [entry["label"] for entry in body["transit"]] == ["Westside", "Library Express"]


def test_a_day_on_which_everything_stayed_home(api, db):
    db.v1("Main", 4)
    db.v1("1", 2)
    db.v1("LOCAL", 1)

    body = _routing(api)

    assert body["checkin_count"] == 7
    assert _split(body) == (7, {"westside": 0, "library_express": 0}, 0)


def test_one_transit_destination(api, db):
    db.v1("Main", 6)
    db.v1("Westside", 4)

    assert _split(_routing(api)) == (6, {"westside": 4, "library_express": 0}, 0)


def test_a_configured_destination_with_no_check_ins_is_still_listed_with_zero(api, db):
    db.v1("Library Express", 2)

    body = _routing(api)

    assert body["transit"][0] == {"key": "westside", "label": "Westside", "checkin_count": 0}
    assert body["transit_count"] == 2


def test_destinations_come_back_in_configured_order_whatever_their_counts(api, db):
    db.settings(organization=1, document={"transit": {"home_branch_label": "Main", "destinations": [
        {"key": "k1", "label": "Zebra Road"}, {"key": "k2", "label": "Alpha Street"}, {"key": "k3", "label": "Midway"},
    ]}})
    db.v1("Midway", 9)
    db.v1("Alpha Street", 1)

    body = _routing(api)

    assert [entry["label"] for entry in body["transit"]] == ["Zebra Road", "Alpha Street", "Midway"]
    assert [entry["key"] for entry in body["transit"]] == ["zebra_road", "alpha_street", "midway"]
    assert [entry["checkin_count"] for entry in body["transit"]] == [0, 1, 9]


def test_the_date_and_zone_asked_for_are_echoed(api, db):
    db.v1("Main", 1, at=_local(11))

    body = _routing(api, params={"date": "2026-06-11"})

    assert (body["date"], body["timezone"], body["checkin_count"]) == ("2026-06-11", "America/Chicago", 1)


def test_the_day_is_a_day_in_the_products_configured_zone(api, db, monkeypatch):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Asia/Tokyo")
    db.cutover(_utc(1, 0))
    db.v2("westside", 1, at=_utc(9, 14, 59))    # 23:59 on 9 June in Tokyo
    db.v2("westside", 2, at=_utc(9, 15, 0))     # 00:00 on 10 June in Tokyo
    db.v2("main", 3, at=_utc(10, 14, 59))       # 23:59 on 10 June in Tokyo
    db.v2("main", 4, at=_utc(10, 15, 0))        # 00:00 on 11 June in Tokyo

    body = _routing(api)

    assert body["timezone"] == "Asia/Tokyo"
    assert _split(body) == (3, {"westside": 2, "library_express": 0}, 0)


def test_only_the_requested_local_day_is_counted(api, db):
    db.v1("Westside", 1, at=_local(9, 23, 59))
    db.v1("Westside", 2, at=_local(10, 0, 0))
    db.v1("Westside", 3, at=_local(10, 23, 59))
    db.v1("Westside", 4, at=_local(11, 0, 0))

    assert _routing(api)["transit_count"] == 5


# =====================================================================================================================
# Other: nothing is discarded
# =====================================================================================================================

def test_anything_that_is_neither_home_nor_a_configured_destination_is_other(api, db):
    db.v1("Main", 1)
    db.v1("No Agency Destination", 2)       # the sorter had nowhere to send it
    db.v1("Northgate Annex", 3)             # a destination this site has not configured
    db.v1("Old Annex", 4)                   # configured, but disabled
    db.v1("", 5)                            # no value
    db.v1(None, 6)                          # none at all
    db.v1("???", 7)                         # nothing usable

    body = _routing(api)

    assert body["checkin_count"] == 28
    assert _split(body) == (1, {"westside": 0, "library_express": 0}, 27)


def test_an_unrecognised_destination_is_never_named_in_the_answer(api, db):
    db.v1(f"Secret Depot {CANARY}", 2)
    db.v1("Northgate Annex", 1)

    response = _get(api)

    assert response.json()["other_count"] == 3
    for leaked in ("Secret", "Depot", "CANARY", "Northgate", "northgate", "Smith", "31234000123456"):
        assert leaked not in response.text


def test_every_check_in_is_counted_exactly_once(api, db):
    db.cutover(NOON_CUTOVER)
    db.v1("Main", 3, at=_local(10, 8))
    db.v1("Westside", 2, at=_local(10, 9))
    db.v1("Elsewhere", 1, at=_local(10, 11, 59))
    db.v2("main", 4, at=_utc(10, 17))
    db.v2("library_express", 2, at=_utc(10, 20))
    db.v2("unknown", 5, at=_utc(10, 22))

    body = _routing(api)

    assert body["checkin_count"] == 17
    assert _split(body) == (7, {"westside": 2, "library_express": 2}, 6)
    assert body["checkin_count"] == _get(api, CHECKIN_COUNT_ACME_MAIN).json()["checkin_count"]


# =====================================================================================================================
# The two eras: one destination, two stored forms
# =====================================================================================================================

@pytest.mark.parametrize("stored", ["Westside", "WESTSIDE", "westside", "  Westside  ", "WestSide", "WESTSIDE BRANCH"])
def test_a_legacy_label_is_matched_whatever_its_case_or_spacing(api, db, stored):
    db.v1(stored, 3)

    assert _split(_routing(api)) == (0, {"westside": 3, "library_express": 0}, 0)


@pytest.mark.parametrize("stored", ["Library Express", "LIBRARY EXPRESS", "library express", "Library  Express"])
def test_a_legacy_label_of_two_words_is_matched_to_its_destination(api, db, stored):
    db.v1(stored, 2)

    assert _split(_routing(api)) == (0, {"westside": 0, "library_express": 2}, 0)


@pytest.mark.parametrize("stored", ["1", "LOCAL", "Local", "MAIN", "Main", "main"])
def test_every_legacy_form_of_home_is_home(api, db, stored):
    db.v1(stored, 2)

    assert _split(_routing(api)) == (2, {"westside": 0, "library_express": 0}, 0)


def test_a_legacy_only_site_is_classified_from_its_legacy_labels(api, db):
    db.v1("Main", 5)
    db.v1("Westside", 2)
    db.v1("Library Express", 1)
    db.v2("westside", 50)      # no cutover: these are not part of the site's history

    body = _routing(api)

    assert body["checkin_count"] == 8
    assert _split(body) == (5, {"westside": 2, "library_express": 1}, 0)
    assert "checkin_events" not in db.log


def test_a_current_site_is_classified_from_its_stored_slugs(api, db):
    db.cutover(_utc(1, 0))
    db.v2("main", 6)
    db.v2("westside", 3)
    db.v2("library_express", 2)
    db.v2("no_agency_destination", 1)
    db.v2("unknown", 1)
    db.v1("Westside", 40)      # after the cutover: these are not part of the day

    body = _routing(api)

    assert body["checkin_count"] == 13
    assert _split(body) == (6, {"westside": 3, "library_express": 2}, 2)


def test_the_same_destination_is_one_destination_on_either_side_of_the_cutover(api, db):
    db.cutover(NOON_CUTOVER)
    db.v1("Library Express", 2, at=_local(10, 9))
    db.v1("LIBRARY EXPRESS", 1, at=_local(10, 10))
    db.v2("library_express", 4, at=_utc(10, 19))

    assert _split(_routing(api)) == (0, {"westside": 0, "library_express": 7}, 0)


def test_a_destination_the_collectors_built_in_rules_do_not_know_is_matched_by_its_own_text(api, db):
    db.settings(organization=1, document={"transit": {"home_branch_label": "Main", "destinations": [
        {"key": "branch_1", "label": "Northgate Annex"},
    ]}})
    db.cutover(NOON_CUTOVER)
    db.v1("Northgate Annex", 2, at=_local(10, 9))
    db.v1("NORTHGATE ANNEX", 1, at=_local(10, 10))
    db.v2("northgate_annex", 3, at=_utc(10, 19))
    db.v2("northgate", 5, at=_utc(10, 19))      # a different slug: not this destination

    body = _routing(api)

    assert body["transit"] == [{"key": "northgate_annex", "label": "Northgate Annex", "checkin_count": 6}]
    assert body["other_count"] == 5


# =====================================================================================================================
# The cutover: the same partition as /checkins/count
# =====================================================================================================================

def test_on_the_cutover_day_each_era_owns_its_own_side_of_the_boundary(api, db):
    db.cutover(NOON_CUTOVER)
    db.v1("Westside", 1, at=_local(10, 11, 59))        # before: counts
    db.v1("Westside", 10, at=_local(10, 12, 0))        # at the boundary, on the legacy clock: v2 owns it
    db.v1("Westside", 10, at=_local(10, 15))           # after: not counted
    db.v2("westside", 100, at=_utc(10, 16, 59))        # before the boundary: not counted
    db.v2("westside", 2, at=NOON_CUTOVER)              # exactly at the boundary: counts
    db.v2("main", 3, at=_utc(10, 23))                  # after: counts

    body = _routing(api)

    assert body["checkin_count"] == 6
    assert _split(body) == (3, {"westside": 3, "library_express": 0}, 0)


def test_a_day_before_the_cutover_is_read_from_the_legacy_table_alone(api, db):
    db.cutover(NOON_CUTOVER)
    db.v1("Westside", 2, at=_local(9))
    db.v1("Main", 1, at=_local(9))
    db.v2("westside", 9, at=_utc(9, 18))

    body = _routing(api, params={"date": "2026-06-09"})

    assert _split(body) == (1, {"westside": 2, "library_express": 0}, 0)
    assert db.log.count("checkins") == 1 and "checkin_events" not in db.log


def test_a_day_after_the_cutover_is_read_from_the_current_table_alone(api, db):
    db.cutover(NOON_CUTOVER)
    db.v1("Westside", 9, at=_local(11))
    db.v2("westside", 2, at=_utc(11, 18))
    db.v2("main", 1, at=_utc(11, 18))

    body = _routing(api, params={"date": "2026-06-11"})

    assert _split(body) == (1, {"westside": 2, "library_express": 0}, 0)
    assert db.log.count("checkin_events") == 1 and "checkins" not in db.log


def test_a_rollback_returns_the_site_to_its_legacy_rows(api, db):
    db.cutover(NOON_CUTOVER, set_at="2026-06-01 00:00:00+00:00")
    db.cutover(None, set_at="2026-06-02 00:00:00+00:00")
    db.v1("Westside", 2, at=_local(10, 15))
    db.v2("westside", 9, at=_utc(10, 20))

    assert _split(_routing(api)) == (0, {"westside": 2, "library_express": 0}, 0)


def test_a_legacy_time_is_compared_as_the_local_wall_clock_and_is_never_shifted_to_utc(api, db):
    """The dashboard's mixed-era path labels a legacy time UTC once a site has
    a cutover, moving it five hours. This endpoint does not: 20:30 local on
    the 10th is on the 10th."""
    db.cutover(_utc(11, 17))
    db.v1("Westside", 2, at=_local(10, 20, 30))
    db.v1("Westside", 5, at=_local(10, 0, 30))

    assert _routing(api)["transit_count"] == 7
    assert _routing(api, params={"date": "2026-06-11"})["transit_count"] == 0
    assert _routing(api, params={"date": "2026-06-09"})["transit_count"] == 0


def test_the_statements_bind_the_same_bounds_as_the_plain_count(api, db):
    db.cutover(NOON_CUTOVER)
    db.v1("Main", 1, at=_local(10, 9))
    db.v2("main", 1, at=_utc(10, 20))

    _routing(api)
    by_destination = {table: parameters for table, parameters, _ in db.queries if table in ("checkins", "checkin_events")}
    db.queries.clear()
    _get(api, CHECKIN_COUNT_ACME_MAIN)
    plain = {table: parameters for table, parameters, _ in db.queries if table in ("checkins", "checkin_events")}

    assert by_destination == plain
    assert by_destination["checkins"]["end_local"] == _local(10, 12)
    assert by_destination["checkin_events"]["start_utc"] == NOON_CUTOVER


# =====================================================================================================================
# The site's configuration
# =====================================================================================================================

def test_a_destination_is_matched_by_its_label_not_by_the_key_typed_beside_it(api, db):
    db.v1("branch_1", 4)        # the free-form key of "Westside": it identifies nothing
    db.v1("Westside", 1)

    body = _routing(api)

    assert _split(body) == (0, {"westside": 1, "library_express": 0}, 4)
    assert "branch_1" not in json.dumps(body)


def test_a_disabled_destination_is_not_listed_and_its_check_ins_are_other(api, db):
    db.v1("Old Annex", 3)

    body = _routing(api)

    assert [entry["label"] for entry in body["transit"]] == ["Westside", "Library Express"]
    assert body["other_count"] == 3


def test_a_sites_own_settings_override_the_organizations(api, db):
    db.v1("East", 2, branch=EAST)
    db.v1("LOCAL", 1, branch=EAST)
    db.v1("Main Library", 4, branch=EAST)
    db.v1("Westside", 5, branch=EAST)       # the organization's destination, not this site's

    body = _routing(api, ACME_EAST)

    assert body["home"] == {"label": "East", "checkin_count": 3}
    assert body["transit"] == [{"key": "main_library", "label": "Main Library", "checkin_count": 4}]
    assert body["other_count"] == 5


def test_a_site_with_no_settings_at_all_has_no_destinations_and_is_labelled_with_its_own_name(api, db):
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM organization_settings"))
        conn.execute(text("DELETE FROM branch_settings"))
    db.v1("Main", 2)
    db.v1("Westside", 3)

    body = _routing(api)

    assert body["home"] == {"label": "Main Library", "checkin_count": 2}
    assert body["transit"] == [] and body["transit_count"] == 0
    assert body["other_count"] == 3


@pytest.mark.parametrize("document", ["{}", "null", "[]", '"transit"', '{"transit": null}', '{"transit": {"destinations": 5}}'])
def test_settings_with_no_usable_transit_block_are_a_site_with_no_destinations(api, db, document):
    db.settings(organization=1, document=document)
    db.v1("Westside", 2)

    body = _routing(api)

    assert body["transit"] == [] and body["other_count"] == 2


def test_a_site_may_have_many_destinations(api, db):
    labels = [f"Stop {number}" for number in range(1, 31)]
    db.settings(organization=1, document={"transit": {"home_branch_label": "Main", "destinations": [
        {"key": f"k{index}", "label": label} for index, label in enumerate(labels)
    ]}})
    for number, label in enumerate(labels, start=1):
        db.v1(label, number)

    body = _routing(api)

    assert [entry["label"] for entry in body["transit"]] == labels
    assert [entry["checkin_count"] for entry in body["transit"]] == list(range(1, 31))
    assert len({entry["key"] for entry in body["transit"]}) == 30
    assert body["transit_count"] == body["checkin_count"] == sum(range(1, 31))


def test_nothing_else_in_the_settings_is_returned(api, db):
    db.v1("Main", 1)

    response = _get(api)

    for leaked in ("security", "admin_lock_hash", "CANARY", "library_name", "Acme Public Library", "enabled"):
        assert leaked not in response.text


# =====================================================================================================================
# Authentication and tenant scope
# =====================================================================================================================

def test_no_session_is_a_401_and_nothing_is_read(api, db, monkeypatch):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: None)

    for headers in ({}, COOKIE):
        response = _get(api, headers=headers)

        assert response.status_code == 401
        assert response.json() == NOT_AUTHENTICATED
    assert db.log == []


@pytest.mark.parametrize(("org", "branch"), [
    ("no-such-org", "main"),        # no such organization
    ("beta", "north"),              # an organization Alice is not a member of
    ("closed", "main"),             # a cancelled organization
    ("acme", "no-such-branch"),     # no such branch
    ("acme", "north"),              # another organization's branch
    ("acme", "shut"),               # an inactive branch
    ("acme", "unmapped"),           # a branch with no operational id
    ("unmapped-org", "main"),       # an organization with no operational id
    ("ACME", "main"),               # slugs are exact
])
def test_anything_that_does_not_resolve_is_the_same_404_and_nothing_is_read(api, db, org, branch):
    response = _get(api, PATH.format(org=org, branch=branch))

    assert response.status_code == 404
    assert response.json() == TENANT_NOT_FOUND
    assert db.log == []


def test_the_404_is_byte_for_byte_the_one_the_plain_count_gives(api, db):
    missing = "/api/organizations/beta/branches/north/checkins/"

    by_destination = api.get(missing + "by-destination", headers=COOKIE, params=JUNE_10)
    plain = api.get(missing + "count", headers=COOKIE, params=JUNE_10)

    assert (by_destination.status_code, by_destination.content) == (plain.status_code, plain.content)
    assert by_destination.headers["cache-control"] == plain.headers["cache-control"]


def test_a_suspended_organization_can_still_be_read(api, db):
    db.v1("Main", 2, customer=8505, branch=51)

    body = _routing(api, PATH.format(org="paused", branch="main"))

    assert body["checkin_count"] == 2 and body["home"] == {"label": "Paused Main", "checkin_count": 2}


def test_every_read_is_bound_to_the_resolved_tenant_and_runs_under_its_context(api, db):
    db.cutover(NOON_CUTOVER)

    _routing(api)

    assert [table for table, _, _ in db.queries] == ["organizations", "v2_cutovers", "checkins", "checkin_events"]
    for _table, parameters, context in db.queries:
        assert (parameters["customer_id"], parameters["branch_id"]) == (CUSTOMER, BRANCH)
        assert context == {"customer_id": str(CUSTOMER), "branch_id": str(BRANCH)}
    assert db.log == ["open", "organizations", "v2_cutovers", "checkins", "checkin_events", "close"]


def test_another_organizations_check_ins_and_settings_are_never_part_of_the_answer(api, db):
    db.v1("Westside", 1)
    db.v1("Westside", 70, customer=OTHER_CUSTOMER, branch=OTHER_BRANCH)
    db.v1("North", 80, customer=OTHER_CUSTOMER, branch=OTHER_BRANCH)
    # The same branch id under another customer, and the same customer under another branch id.
    db.v1("Westside", 90, customer=OTHER_CUSTOMER, branch=BRANCH)
    db.cutover(_utc(1, 0), customer=OTHER_CUSTOMER, branch=OTHER_BRANCH)
    db.v2("westside", 60, customer=OTHER_CUSTOMER, branch=OTHER_BRANCH)

    body = _routing(api)

    assert body["checkin_count"] == 1
    assert body["home"]["label"] == "Main" and "North" not in json.dumps(body)
    assert _split(body) == (0, {"westside": 1, "library_express": 0}, 0)


def test_another_site_of_the_same_organization_is_never_part_of_the_answer(api, db):
    db.v1("Westside", 2)
    db.v1("Westside", 30, branch=EAST)
    db.v1("East", 40, branch=EAST)
    db.cutover(_utc(1, 0), branch=EAST)         # the other site's cutover is not this site's
    db.v2("westside", 50, branch=EAST)

    main_site, east_site = _routing(api), _routing(api, ACME_EAST)

    assert main_site["checkin_count"] == 2 and main_site["transit_count"] == 2
    assert east_site["checkin_count"] == 50 and east_site["home"]["label"] == "East"
    assert east_site["other_count"] == 50


def test_a_tenant_context_that_does_not_match_is_a_500_and_nothing_is_read(api, db):
    db.read_back = lambda settings: {**settings, "branch_id": str(OTHER_BRANCH)}

    response = TestClient(main.app, raise_server_exceptions=False).get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert db.log == ["open", "close"]


@pytest.mark.parametrize("table", ["organizations", "v2_cutovers", "checkins", "checkin_events"])
def test_a_failed_read_is_a_500_that_says_nothing_and_is_never_a_partial_answer(api, db, table):
    db.cutover(NOON_CUTOVER)
    db.v1("Westside", 3, at=_local(10, 9))
    db.v2("westside", 2, at=_utc(10, 20))
    db.fail_on = table

    response = TestClient(main.app, raise_server_exceptions=False).get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    for leaked in ("synthetic", "CANARY", str(CUSTOMER), "checkin", "Smith"):
        assert leaked not in response.text
    assert db.log[-1] == "close"


# =====================================================================================================================
# The date
# =====================================================================================================================

@pytest.mark.parametrize("value", [
    "", "today", "2026-6-10", "2026-06-10T00:00:00", "2026-06-10T00:00:00Z", "2026-06-10 00:00:00", "20260610",
    "10/06/2026", "2026-02-30", "2026-13-01", " 2026-06-10", "2026-06-10 ",
])
def test_anything_but_a_calendar_date_is_a_422_and_nothing_is_read(api, db, value):
    response = _get(api, params={"date": value})

    assert response.status_code == 422
    assert db.log == []


def test_the_date_is_required(api, db):
    response = _get(api, params={})

    assert response.status_code == 422
    assert db.log == []


def test_a_bad_date_is_answered_exactly_as_the_plain_count_answers_it(api, db):
    by_destination = _get(api, params={"date": "yesterday"})
    plain = _get(api, CHECKIN_COUNT_ACME_MAIN, params={"date": "yesterday"})

    assert (by_destination.status_code, by_destination.json()) == (plain.status_code, plain.json())


def test_a_tenant_named_in_the_query_string_is_ignored(api, db):
    db.v1("Westside", 1)
    db.v1("Westside", 9, customer=OTHER_CUSTOMER, branch=OTHER_BRANCH)

    body = _routing(api, params={**JUNE_10, "customer_id": OTHER_CUSTOMER, "branch_id": OTHER_BRANCH,
                                 "org_slug": "beta", "branch_slug": "north", "destination": "North"})

    assert body["checkin_count"] == 1


# =====================================================================================================================
# What is, and is not, in the answer
# =====================================================================================================================

def test_no_item_patron_or_stored_value_is_in_the_answer(api, db):
    db.cutover(NOON_CUTOVER)
    db.v1("Westside", 2, at=_local(10, 9))
    db.v1(f"Hold shelf {CANARY}", 1, at=_local(10, 9))
    db.v2("westside", 1, at=_utc(10, 20))

    response = _get(api)

    for leaked in ("CANARY", "Smith", "31234000123456", "barcode", "title", "item_key", "event_key", "key_id",
                   "customer_id", "branch_id", str(CUSTOMER), "v1", "v2", "cutover", "Hold shelf"):
        assert leaked not in response.text


def test_the_statements_read_only_the_destination_and_a_count(db, api):
    db.cutover(NOON_CUTOVER)

    _routing(api)
    grouped = [sql for sql in db.statements if "GROUP BY" in sql]

    assert len(grouped) == 2
    for sql in grouped:
        assert sql.startswith("SELECT destination, COUNT(*) FROM ")
        assert sql.endswith("GROUP BY destination")
        for forbidden in ("barcode", "title", "item_key", "event_key", "patron", "JOIN", "DISTINCT", "AT TIME ZONE", "NOW()"):
            assert forbidden not in sql


def test_the_response_models_have_exactly_the_approved_fields_and_refuse_any_other():
    assert list(CheckinsByDestinationResponse.model_fields) == [
        "date", "timezone", "checkin_count", "home", "transit", "transit_count", "other_count",
    ]
    assert list(RoutingHome.model_fields) == ["label", "checkin_count"]
    assert list(RoutingDestination.model_fields) == ["key", "label", "checkin_count"]
    for model in (CheckinsByDestinationResponse, RoutingHome, RoutingDestination):
        assert model.model_config.get("extra") == "forbid"
    assert not any("percent" in name or "pct" in name or "rate" in name
                   for model in (CheckinsByDestinationResponse, RoutingHome, RoutingDestination)
                   for name in model.model_fields)


# =====================================================================================================================
# The other check-in endpoints are unchanged, and agree
# =====================================================================================================================

def test_the_plain_count_and_the_hourly_counts_are_what_they_were_and_agree_with_the_routing_total(api, db):
    db.cutover(NOON_CUTOVER)
    db.v1("Main", 3, at=_local(10, 8))
    db.v1("Westside", 2, at=_local(10, 9))
    db.v2("library_express", 4, at=_utc(10, 19))       # 14:00 local
    db.v2("somewhere_else", 1, at=_utc(10, 19))

    count = _get(api, CHECKIN_COUNT_ACME_MAIN)
    hourly = _get(api, CHECKINS_BY_HOUR_ACME_MAIN)
    routing = _routing(api)

    assert count.json() == {"date": "2026-06-10", "timezone": "America/Chicago", "checkin_count": 10}
    hours = {entry["hour"]: entry["checkin_count"] for entry in hourly.json()["hours"] if entry["checkin_count"]}
    assert hours == {8: 3, 9: 2, 14: 5}
    assert routing["checkin_count"] == 10
    assert _split(routing) == (3, {"westside": 2, "library_express": 4}, 1)


def test_the_plain_count_reads_no_settings(api, db):
    _get(api, CHECKIN_COUNT_ACME_MAIN)

    assert "organizations" not in db.log


# =====================================================================================================================
# The route and the service
# =====================================================================================================================

def test_the_route_takes_the_two_path_slugs_and_the_date_and_nothing_else():
    route = next(r for r in main.app.routes if r.path.endswith("/checkins/by-destination"))

    assert route.path == "/api/organizations/{org_slug}/branches/{branch_slug}/checkins/by-destination"
    assert route.methods == {"GET"}
    dependant = get_flat_dependant(route.dependant)
    # A slug can be read by more than one of the route's dependencies (R9C: the plan's transit feature reads the
    # organization's): still the same two path parameters, and nothing else.
    assert sorted({parameter.name for parameter in dependant.path_params}) == ["branch_slug", "org_slug"]
    assert [(parameter.alias, parameter.field_info.is_required()) for parameter in dependant.query_params] == [("date", True)]
    assert dependant.body_params == [] and dependant.header_params == []


def test_the_handler_does_no_counting_of_its_own():
    source = inspect.getsource(operational_routes)
    handler = source.split('@router.get("/checkins/by-destination")')[1].split("@router.get(")[0]

    assert "get_routing_config(conn, tenant)" in handler
    assert "get_checkin_counts_by_destination(conn, tenant, local_date=local_date, zone=zone, routing=routing)" in handler
    for forbidden in ("text(", "SELECT", "execute(", "get_engine", "destination_key", "for row", "+="):
        assert forbidden not in handler, forbidden


def test_no_module_on_the_request_path_needs_streamlit_or_pandas():
    for module in (operational_routes, operational_schemas, operational_metrics_service, routing_config_service):
        source = inspect.getsource(module)
        code = "\n".join(line for line in source.splitlines() if line.startswith(("import ", "from ")))

        assert "streamlit" not in code and "pandas" not in code, module.__name__
        assert "data_loader" not in code and "mixed_era_service" not in code and "transit_logic" not in code


def test_the_settings_read_is_one_statement_bound_to_the_resolved_tenant_only():
    sql = " ".join(str(routing_config_service._SITE_SETTINGS_SQL).split())

    assert "WHERE o.operational_customer_id = :customer_id AND b.operational_branch_id = :branch_id" in sql
    assert "JOIN branches b ON b.organization_id = o.id" in sql
    for forbidden in ("slug", "checkins", "memberships", "app_users"):
        assert forbidden not in sql


def test_the_service_gives_the_same_counts_as_the_route(db):
    db.cutover(NOON_CUTOVER)
    db.v1("Main", 3, at=_local(10, 8))
    db.v1("Westside", 2, at=_local(10, 9))
    db.v1(None, 1, at=_local(10, 9))
    db.v2("library_express", 4, at=_utc(10, 19))
    tenant = SimpleNamespace(operational_customer_id=CUSTOMER, operational_branch_id=BRANCH)
    routing = RoutingConfig(
        home_label="Main",
        home_keys=frozenset({"main"}),
        transit=(TransitDestination("westside", "Westside"), TransitDestination("library_express", "Library Express")),
    )
    from zoneinfo import ZoneInfo

    with db.engine.connect() as conn:
        counts = get_checkin_counts_by_destination(
            conn, tenant, local_date=datetime(2026, 6, 10, tzinfo=UTC).date(), zone=ZoneInfo("America/Chicago"),
            routing=routing,
        )

    assert counts == CheckinDestinationCounts(total=10, home_count=3, transit_counts=(2, 4), other_count=1,
                                              v1_total=6, v2_total=4)
    assert counts.transit_count == 6
    assert counts.home_count + counts.transit_count + counts.other_count == counts.total


def test_with_no_configured_destinations_everything_that_is_not_home_is_other(db):
    db.v1("Main", 2)
    db.v1("Westside", 3)
    tenant = SimpleNamespace(operational_customer_id=CUSTOMER, operational_branch_id=BRANCH)
    from zoneinfo import ZoneInfo

    with db.engine.connect() as conn:
        counts = get_checkin_counts_by_destination(
            conn, tenant, local_date=datetime(2026, 6, 10, tzinfo=UTC).date(), zone=ZoneInfo("America/Chicago"),
            routing=RoutingConfig(home_label="Main", home_keys=frozenset({"main"}), transit=()),
        )

    assert (counts.total, counts.home_count, counts.transit_counts, counts.other_count) == (5, 2, (), 3)


def test_a_day_boundary_one_microsecond_either_side(api, db):
    db.v1("Westside", 1, at=_local(10) - timedelta(hours=10, microseconds=1))     # 23:59:59.999999 on the 9th
    db.v1("Westside", 2, at=_local(10) - timedelta(hours=10))                      # 00:00:00 on the 10th
    db.v1("Westside", 4, at=_local(11) - timedelta(hours=10, microseconds=1))     # 23:59:59.999999 on the 10th
    db.v1("Westside", 8, at=_local(11) - timedelta(hours=10))                      # 00:00:00 on the 11th

    assert _routing(api)["transit_count"] == 6
