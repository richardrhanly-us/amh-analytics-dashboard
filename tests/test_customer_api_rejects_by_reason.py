"""Block 8c: the single-date rejects-by-reason endpoint.

    GET /api/organizations/{org_slug}/branches/{branch_slug}/rejects/by-reason?date=YYYY-MM-DD

These tests drive the real production route through TestClient(main.app): the
session dependency, the tenant-scope dependency, the scoped and verified
connection, the metrics service, the reason classifier and the response
schemas all run for real. What stands in for PostgreSQL is:

- an in-memory SQLite database for the SaaS tables, so the REAL tenant
  resolver decides which organization/branch pairs resolve;
- a recording fake engine for the operational connection, which answers the
  set_config / current_setting calls the way PostgreSQL does and returns a
  chosen cutover and chosen GROUPS -- (stored reason value, row count) pairs,
  as the two grouped statements return them -- to the REAL metrics service.
  It answers the plain reject count from the same groups, so the two reject
  endpoints can be compared over one data state, and the check-in statements
  with different numbers, so a route reading the wrong tables would show.

The classification rules are tested in tests/test_reject_reason.py and the
time, cutover and aggregation rules in tests/test_operational_metrics_service.py;
row level security, real TIMESTAMP / TIMESTAMPTZ comparison and the database
session time zone are tested against a real server in
tests/test_rls_phase1_postgres.py. This file is about the HTTP contract.
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import main
from customer_api import operational_routes, operational_schemas, tenant_scope
from customer_api.operational_schemas import (
    RejectCountResponse,
    RejectReasonCount,
    RejectsByReasonResponse,
)
from services import (
    operational_metrics_service,
    reject_reason,
    session_service,
    tenant_resolution_service,
)
from services.operational_metrics_service import RejectReasonCounts
from services.reject_reason import REJECT_REASONS, RejectReason

PATH = "/api/organizations/{org}/branches/{branch}/rejects/by-reason"
ACME_MAIN = PATH.format(org="acme", branch="main")
REJECT_COUNT_ACME_MAIN = "/api/organizations/acme/branches/main/rejects/count"
CHECKIN_COUNT_ACME_MAIN = "/api/organizations/acme/branches/main/checkins/count"
CHECKINS_BY_HOUR_ACME_MAIN = "/api/organizations/acme/branches/main/checkins/by-hour"
COOKIE = {"Cookie": "__Host-sortview_api_session=synthetic-opaque-session-token"}
USER = {"id": 1, "email": "alice@example.invalid", "full_name": "Alice"}

CUSTOMER, BRANCH = 8101, 11
JUNE_10 = {"date": "2026-06-10"}
NOON_CUTOVER = datetime(2026, 6, 10, 17, 0, tzinfo=UTC)   # 12:00 local on 10 June in America/Chicago

NOT_AUTHENTICATED = {"code": "not_authenticated", "message": "Authentication is required."}
TENANT_NOT_FOUND = {"code": "tenant_not_found", "message": "Organization or branch not found."}
INTERNAL_ERROR = {"code": "internal_error", "message": "Internal server error."}

APPROVED_REASONS = ["item_not_found", "ils_acs_failure", "rfid_collision", "configuration_error", "routing_error",
                    "communication_error", "other", "unknown"]
CANARY = "CANARY-31234000123456 Smith, Pat"


def _local(year, month, day, hour=0, minute=0) -> datetime:
    """A NAIVE local wall-clock datetime, as rejects.event_time holds it."""
    return datetime(year, month, day, hour, minute)  # noqa: DTZ001


def _body(*counts: int, day: str = "2026-06-10", timezone: str = "America/Chicago") -> dict:
    """The whole approved response for eight counts given in reason order."""
    assert len(counts) == 8
    return {
        "date": day,
        "timezone": timezone,
        "reasons": [{"reason": reason, "reject_count": count} for reason, count in zip(APPROVED_REASONS, counts, strict=True)],
    }


def _counts(response) -> dict[str, int]:
    """Only the reasons with a count, by code."""
    return {entry["reason"]: entry["reject_count"] for entry in response.json()["reasons"] if entry["reject_count"]}


# --- SaaS tables for the real resolver -------------------------------------------------------------------------------

_SAAS = (
    "CREATE TABLE app_users (id INTEGER PRIMARY KEY, email TEXT, is_active BOOLEAN)",
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT, status TEXT, operational_customer_id INTEGER)",
    (
        "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, slug TEXT, status TEXT, "
        "operational_branch_id INTEGER)"
    ),
    "CREATE TABLE memberships (id INTEGER PRIMARY KEY, organization_id INTEGER, user_id INTEGER, role TEXT)",
    "INSERT INTO app_users (id, email, is_active) VALUES (1, 'alice@example.invalid', 1)",
    (
        f"INSERT INTO organizations (id, slug, status, operational_customer_id) VALUES "
        f"(1, 'acme', 'active', {CUSTOMER}), (2, 'beta', 'active', 8202), (3, 'unmapped-org', 'active', NULL), "
        f"(4, 'closed', 'cancelled', 8404)"
    ),
    (
        f"INSERT INTO branches (id, organization_id, slug, status, operational_branch_id) VALUES "
        f"({BRANCH}, 1, 'main', 'active', {BRANCH}), (12, 1, 'shut', 'inactive', 12), "
        f"(13, 1, 'unmapped', 'active', NULL), (21, 2, 'north', 'active', 21), (31, 3, 'main', 'active', 31), "
        f"(41, 4, 'main', 'active', 41)"
    ),
    # Alice belongs to acme, unmapped-org and closed -- not to beta.
    "INSERT INTO memberships (organization_id, user_id, role) VALUES (1, 1, 'viewer'), (3, 1, 'admin'), (4, 1, 'admin')",
)


@pytest.fixture
def saas_db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in _SAAS:
            conn.execute(text(statement))
    monkeypatch.setattr(tenant_resolution_service, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


# --- a recording fake of the operational PostgreSQL connection -------------------------------------------------------

class _Result:
    def __init__(self, value):
        self._value = value

    def mappings(self):
        return self

    def first(self):
        return self._value

    def one(self):
        return self._value

    def scalar_one(self):
        return self._value

    def __iter__(self):
        return iter(self._value)   # a grouped statement's rows


class FakeOperationalConnection:
    def __init__(self, owner):
        self.owner = owner
        self.settings: dict[str, str] = {}

    def __enter__(self):
        self.owner.log.append("open")
        return self

    def __exit__(self, *_exc):
        self.owner.log.append("close")
        return False

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        owner = self.owner

        if "set_config('app.operational_customer_id'" in sql:
            self.settings["customer_id"] = parameters["v"]
            return _Result(None)
        if "set_config('app.operational_branch_id'" in sql:
            self.settings["branch_id"] = parameters["v"]
            return _Result(None)
        if "current_setting(" in sql:
            owner.log.append("verify context")
            if owner.fail_on == "context":
                raise RuntimeError("synthetic database failure")
            return _Result(owner.read_back(dict(self.settings)))

        grouped, hourly = "GROUP BY" in sql, "FILTER" in sql
        for table, groups in (("rejects", owner.v1_groups), ("reject_events", owner.v2_groups)):
            if f"FROM {table} " in sql:
                # Grouped: one row per stored value. Plain: the same rows, counted.
                return self._answer(table, sql, parameters,
                                    lambda g=groups: list(g) if grouped else sum(count for _, count in g))
        for table, answer in (
            ("v2_cutovers", lambda: None if owner.cutover is None else (owner.cutover,)),
            ("checkins", lambda: (owner.v1_checkins,) + (0,) * 23 if hourly else owner.v1_checkins),
            ("checkin_events", lambda: (owner.v2_checkins,) + (0,) * 23 if hourly else owner.v2_checkins),
        ):
            if f"FROM {table} " in sql:
                return self._answer(table, sql, parameters, answer)

        raise AssertionError(f"unexpected statement: {sql}")

    def _answer(self, table, sql, parameters, answer):
        owner = self.owner
        owner.log.append(table)
        owner.queries.append((table, dict(parameters or {}), dict(self.settings)))
        owner.statements.append(sql)
        if owner.fail_on == table:
            raise RuntimeError(f"synthetic database failure reading {table} for customer {CUSTOMER} {CANARY}")
        return _Result(answer())


class FakeOperationalDatabase:
    """The flat database engine, as the tenant scope sees it. `v1_groups` is
    what the legacy table holds for the part of the day v1 owns, as (stored
    error_message, row count) pairs; `v2_groups` the same for the v2 table,
    as (stored error_class, row count) pairs."""

    def __init__(self):
        self.cutover: datetime | None = None
        self.v1_groups: list[tuple[str | None, int]] = []
        self.v2_groups: list[tuple[str | None, int]] = []
        self.v1_checkins = 90001       # never a reject count: appears only if the wrong tables are read
        self.v2_checkins = 80002
        self.fail_on: str | None = None
        self.read_back = lambda settings: settings
        self.log: list[str] = []
        self.queries: list[tuple] = []
        self.statements: list[str] = []

    def connect(self):
        return FakeOperationalConnection(self)

    def parameters_for(self, table: str) -> dict:
        (found,) = [parameters for name, parameters, _ in self.queries if name == table]
        return found


@pytest.fixture
def operational(monkeypatch):
    database = FakeOperationalDatabase()
    monkeypatch.setattr(tenant_scope, "get_engine", lambda: database)
    return database


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: dict(USER))
    monkeypatch.delenv("SORTVIEW_LIVE_TIMEZONE", raising=False)
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


# =====================================================================================================================
# Success and the shape of the answer
# =====================================================================================================================

def test_a_member_gets_the_rejects_of_the_requested_local_date_by_reason(api, saas_db, operational):
    operational.v1_groups = [("Item not found in database", 4), ("ACS timeout", 1), ("Library not found", 2)]

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 200
    assert response.json() == {
        "date": "2026-06-10",
        "timezone": "America/Chicago",
        "reasons": [
            {"reason": "item_not_found", "reject_count": 4},
            {"reason": "ils_acs_failure", "reject_count": 1},
            {"reason": "rfid_collision", "reject_count": 0},
            {"reason": "configuration_error", "reject_count": 0},
            {"reason": "routing_error", "reject_count": 2},
            {"reason": "communication_error", "reject_count": 0},
            {"reason": "other", "reject_count": 0},
            {"reason": "unknown", "reject_count": 0},
        ],
    }
    assert response.headers["cache-control"] == "no-store"


def test_the_response_has_exactly_the_three_approved_top_level_keys(api, saas_db, operational):
    operational.v1_groups = [("Item not found", 3)]

    body = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()

    assert list(body) == ["date", "timezone", "reasons"]
    assert type(body["reasons"]) is list


def test_every_entry_has_exactly_a_reason_and_a_count(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_groups, operational.v2_groups = [("Item not found", 3)], [("rfid_collision", 2)]

    reasons = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()["reasons"]

    for entry in reasons:
        assert list(entry) == ["reason", "reject_count"]
        assert type(entry["reason"]) is str and type(entry["reject_count"]) is int


def test_there_are_always_exactly_eight_reasons_in_the_fixed_order(api, saas_db, operational):
    assert list(REJECT_REASONS) == APPROVED_REASONS
    operational.cutover = NOON_CUTOVER
    # Stored in an order unlike the reasons': the order of the answer never comes from the data.
    operational.v1_groups = [("Library not found", 5), ("Item not found", 1)]
    operational.v2_groups = [("unknown", 8), ("other", 7), ("communication_error", 6), ("rfid_collision", 3)]

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert [entry["reason"] for entry in response.json()["reasons"]] == APPROVED_REASONS
    assert response.json() == _body(1, 0, 3, 0, 5, 6, 7, 8)


def test_a_reason_with_no_rejects_is_kept_as_zero(api, saas_db, operational):
    operational.v1_groups = [("Multiple RFID tags detected", 9)]

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json() == _body(0, 0, 9, 0, 0, 0, 0, 0)
    assert [entry["reject_count"] for entry in response.json()["reasons"]].count(0) == 7


def test_a_day_with_no_rejects_is_eight_zeros(api, saas_db, operational):
    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 200
    assert response.json() == _body(0, 0, 0, 0, 0, 0, 0, 0)
    assert operational.log == ["open", "verify context", "v2_cutovers", "rejects", "close"]   # it really looked


@pytest.mark.parametrize("day", ["2099-12-31", "1999-01-01", "2028-02-29"], ids=["future", "ancient", "leap day"])
def test_a_date_with_no_data_still_returns_all_eight_reasons(api, saas_db, operational, day):
    response = api.get(ACME_MAIN, headers=COOKIE, params={"date": day})

    assert response.status_code == 200
    assert response.json() == _body(0, 0, 0, 0, 0, 0, 0, 0, day=day)


def test_both_eras_are_added_together_reason_by_reason(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_groups = [("Item not found", 40), ("ACS timeout", 5)]
    operational.v2_groups = [("item_not_found", 2), ("communication_error", 3)]

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert _counts(response) == {"item_not_found": 42, "ils_acs_failure": 5, "communication_error": 3}
    assert operational.log == ["open", "verify context", "v2_cutovers", "rejects", "reject_events", "close"]


def test_the_reasons_are_machine_codes_never_display_labels(api, saas_db, operational):
    operational.v1_groups = [(message, 1) for message in (
        "Item not found", "ACS timeout", "Multiple tags", "Collection code missing", "Library not found", "Anything else",
    )]

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json() == _body(1, 1, 1, 1, 1, 0, 1, 0)
    for label in ("Item Not Found", "ILS / ACS Failure", "RFID Collision", "Call Number / Config Error",
                  "Routing Error", "Communication Error", "Other", "Unknown", "label", "name", "description"):
        assert label not in response.text, label
    for entry in response.json()["reasons"]:
        assert entry["reason"] == entry["reason"].lower() and " " not in entry["reason"]


def test_a_legacy_message_with_no_text_is_unknown(api, saas_db, operational):
    # NULL, empty, blank and the literal "nan": `unknown`, as for the Contract v2 collector.
    operational.v1_groups = [(None, 1), ("", 2), ("   ", 3), ("nan", 4)]

    assert _counts(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)) == {"unknown": 10}


def test_the_reasons_add_up_to_the_reject_count_of_the_same_day(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_groups = [("Item not found", 4), ("something else", 2), (None, 1), (CANARY, 3)]
    operational.v2_groups = [("rfid_collision", 5), ("jam", 6), ("unknown", 1)]

    by_reason = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).json()
    count = api.get(REJECT_COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10).json()

    assert sum(entry["reject_count"] for entry in by_reason["reasons"]) == count["reject_count"] == 22
    assert (by_reason["date"], by_reason["timezone"]) == (count["date"], count["timezone"])


# =====================================================================================================================
# What is never in the answer
# =====================================================================================================================

LEAKS = ("v1_counts", "v2_counts", "v1", "v2", "unexpected", "unexpected_class_rows", "customer_id", "branch_id",
         "operational", "cutover", "cutover_at", "era", "source", "item_key", "event_key", "key_id", "barcode",
         "total", "error_message", "error_class", "message", "class", "rate", "hours", "label")


def test_there_is_no_total(api, saas_db, operational):
    operational.v1_groups = [("Item not found", 3), ("ACS timeout", 4)]

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert "total" not in response.text and "reject_count\":7" not in response.text.replace(" ", "")
    assert list(response.json()) == ["date", "timezone", "reasons"]


def test_no_raw_legacy_message_is_ever_in_the_response_or_a_log(api, saas_db, operational, caplog):
    operational.v1_groups = [(f"Item not found {CANARY}", 2), (CANARY, 1), ("ACS said: Smith, Pat 31234000123456", 1)]

    with caplog.at_level(logging.DEBUG):
        response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json() == _body(2, 1, 0, 0, 0, 0, 1, 0)
    for leaked in ("CANARY", "Smith", "Pat", "31234000123456", "said", "not found"):
        assert leaked not in response.text, leaked
        assert leaked not in caplog.text, leaked


def test_an_unexpected_stored_v2_class_is_counted_as_other_and_never_emitted(api, saas_db, operational, caplog):
    operational.cutover = datetime(2026, 6, 1, 5, 0, tzinfo=UTC)
    operational.v2_groups = [("jam", 3), ("sensor_fault", 2), ("other", 1), ("item_not_found", 4)]

    with caplog.at_level(logging.DEBUG):
        response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 200                       # one stray class does not take the endpoint down
    assert response.json() == _body(4, 0, 0, 0, 0, 0, 6, 0)
    assert [entry["reason"] for entry in response.json()["reasons"]] == APPROVED_REASONS   # the contract stays closed
    for leaked in ("jam", "sensor_fault", "unexpected", "5"):
        assert leaked not in response.text, leaked
    # The service's warning carries the row count and nothing else.
    warnings = [record for record in caplog.records if record.name == "sortview.operational_metrics"]
    assert [record.args for record in warnings] == [(5,)]
    assert "jam" not in caplog.text and "sensor_fault" not in caplog.text


def test_the_era_split_the_cutover_and_the_ids_are_never_exposed(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_groups, operational.v2_groups = [("Item not found", 4001)], [("item_not_found", 2003)]

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert _counts(response) == {"item_not_found": 6004}
    for leaked in ("4001", "2003", "17:00", str(CUSTOMER), *LEAKS):
        assert leaked not in response.text, leaked


def test_only_the_services_counts_are_returned_never_its_other_fields(api, monkeypatch, saas_db, operational):
    # A deliberately inconsistent result: if the route added the eras up itself, or serialized the whole value,
    # one of the distinctive numbers below would appear. Only `counts` may.
    def distinctive(conn, tenant, **kwargs):
        return RejectReasonCounts(
            counts=(11, 12, 13, 14, 15, 16, 17, 18),
            v1_counts=(4001, 4002, 4003, 4004, 4005, 4006, 4007, 4008),
            v2_counts=(2001, 2002, 2003, 2004, 2005, 2006, 2007, 2008),
            unexpected_class_rows=7777,
        )

    monkeypatch.setattr(operational_routes, "get_reject_counts_by_reason", distinctive)

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json() == _body(11, 12, 13, 14, 15, 16, 17, 18)
    for leaked in ("400", "200", "600", "7777", *LEAKS):
        assert leaked not in response.text, leaked


def test_the_route_reads_nothing_of_the_result_but_its_counts(api, monkeypatch, saas_db, operational):
    # A result that HAS nothing else: reading the per-era counts or the unexpected-row count would fail.
    monkeypatch.setattr(
        operational_routes, "get_reject_counts_by_reason",
        lambda conn, tenant, **kwargs: SimpleNamespace(counts=(1, 2, 3, 4, 5, 6, 7, 8)),
    )

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 200
    assert response.json() == _body(1, 2, 3, 4, 5, 6, 7, 8)


def test_nothing_but_reason_codes_counts_the_date_and_the_zone_is_in_the_body(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_groups, operational.v2_groups = [(CANARY, 2), (None, 1)], [("jam", 1), ("rfid_collision", 1)]

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    words = set(response.text.replace('"', " ").replace(":", " ").replace(",", " ").replace("{", " ")
                .replace("}", " ").replace("[", " ").replace("]", " ").split())
    assert words == {"date", "2026-06-10", "timezone", "America/Chicago", "reasons", "reason", "reject_count",
                     *APPROVED_REASONS, "0", "1", "3"}
    # ...and the statements that ran selected no column that identifies an item or an event.
    for sql in operational.statements:
        for column in ("barcode", "item_key", "event_key", "key_id", "source_file"):
            assert column not in sql, column


# =====================================================================================================================
# The public schemas
# =====================================================================================================================

def test_the_response_schemas_declare_only_the_approved_fields():
    assert list(RejectsByReasonResponse.model_fields) == ["date", "timezone", "reasons"]
    assert RejectsByReasonResponse.model_fields["date"].annotation is date
    assert RejectsByReasonResponse.model_fields["timezone"].annotation is str
    assert RejectsByReasonResponse.model_fields["reasons"].annotation == list[RejectReasonCount]
    assert list(RejectReasonCount.model_fields) == ["reason", "reject_count"]
    assert RejectReasonCount.model_fields["reason"].annotation == RejectReason
    assert RejectReasonCount.model_fields["reject_count"].annotation is int
    assert operational_schemas.RejectReason is reject_reason.RejectReason   # one definition of the eight codes
    assert RejectsByReasonResponse is not RejectCountResponse
    assert list(RejectCountResponse.model_fields) == ["date", "timezone", "reject_count"]   # untouched


def test_the_schemas_are_built_on_the_customer_response_model():
    for model in (RejectReasonCount, RejectsByReasonResponse):
        assert issubclass(model, operational_schemas._ResponseModel)
        assert model.model_config["extra"] == "forbid"


@pytest.mark.parametrize("reason", APPROVED_REASONS)
def test_the_schema_accepts_each_of_the_eight_reason_codes(reason):
    assert RejectReasonCount(reason=reason, reject_count=0).model_dump() == {"reason": reason, "reject_count": 0}


@pytest.mark.parametrize(
    "reason",
    ["jam", "sensor_fault", "Item Not Found", "ITEM_NOT_FOUND", " other", "other ", "", "nan", "total", None, 0, CANARY],
    ids=repr,
)
def test_the_schema_refuses_a_reason_that_is_not_one_of_the_eight_codes(reason):
    # The last line of defence: whatever reached this point, nothing but an approved code can be serialized.
    with pytest.raises(ValidationError):
        RejectReasonCount(reason=reason, reject_count=1)


@pytest.mark.parametrize(
    "extra",
    ["total", "reject_count", "v1_counts", "v2_counts", "unexpected_class_rows", "customer_id", "branch_id",
     "cutover_at", "era", "error_class", "error_message", "reject_rate", "checkin_count", "hours", "labels"],
)
def test_the_response_schema_refuses_any_top_level_field_that_is_not_approved(extra):
    with pytest.raises(ValidationError):
        RejectsByReasonResponse(date=date(2026, 6, 10), timezone="America/Chicago", reasons=[], **{extra: 1})


@pytest.mark.parametrize(
    "extra",
    ["label", "name", "description", "v1_count", "v2_count", "error_message", "error_class", "message", "raw",
     "barcode", "item_key", "event_key", "key_id", "share", "rate", "hour"],
)
def test_a_reason_entry_refuses_any_field_that_is_not_approved(extra):
    with pytest.raises(ValidationError):
        RejectReasonCount(reason="other", reject_count=1, **{extra: 1})
    with pytest.raises(ValidationError):
        RejectsByReasonResponse(
            date=date(2026, 6, 10), timezone="America/Chicago",
            reasons=[{"reason": "other", "reject_count": 1, extra: 1}],
        )


def test_a_service_result_that_is_not_exactly_eight_counts_is_500_never_a_shorter_list(
    api, monkeypatch, saas_db, operational
):
    for counts in ((1, 2, 3, 4, 5, 6, 7), (1, 2, 3, 4, 5, 6, 7, 8, 9), ()):
        monkeypatch.setattr(
            operational_routes, "get_reject_counts_by_reason",
            lambda conn, tenant, counts=counts, **kwargs: SimpleNamespace(counts=counts),
        )

        response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

        assert response.status_code == 500 and response.json() == INTERNAL_ERROR, counts


# =====================================================================================================================
# Who may read
# =====================================================================================================================

def test_a_suspended_organization_reads_exactly_like_a_full_one(api, saas_db, operational):
    operational.v1_groups = [("Item not found", 7)]
    full = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    with saas_db.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE slug = 'acme'"))

    read_only = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert read_only.status_code == full.status_code == 200
    assert read_only.json() == full.json()


@pytest.mark.parametrize("status", ["active", "trial", "suspended", "cancelled"])
def test_an_organizations_status_decides_this_route_exactly_as_it_decides_the_reject_count(
    api, saas_db, operational, status
):
    with saas_db.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = :s WHERE slug = 'acme'"), {"s": status})

    by_reason = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    count = api.get(REJECT_COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert by_reason.status_code == count.status_code
    if by_reason.status_code != 200:
        assert by_reason.content == count.content


def test_the_read_only_tenant_reaches_the_service_marked_as_such(api, monkeypatch, saas_db, operational):
    seen = []
    real = operational_metrics_service.get_reject_counts_by_reason

    def recording(conn, tenant, **kwargs):
        seen.append(tenant.access_mode)
        return real(conn, tenant, **kwargs)

    monkeypatch.setattr(operational_routes, "get_reject_counts_by_reason", recording)
    with saas_db.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE slug = 'acme'"))

    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 200
    assert seen == ["read_only"]


def test_any_role_may_read_as_on_the_reject_count_route(api, saas_db, operational):
    for role in ("viewer", "manager", "admin", "owner"):
        with saas_db.begin() as conn:
            conn.execute(text("UPDATE memberships SET role = :r WHERE organization_id = 1"), {"r": role})

        assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 200, role
        assert api.get(REJECT_COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 200, role


# =====================================================================================================================
# The date, the zone and the tenant reach the service exactly
# =====================================================================================================================

def test_the_route_passes_the_parsed_local_date_and_the_configured_zone_to_the_service(
    api, monkeypatch, saas_db, operational
):
    seen = {}

    def recording(conn, tenant, **kwargs):
        seen.update(kwargs, conn=conn, tenant=tenant)
        return RejectReasonCounts((5, 0, 0, 0, 0, 0, 0, 0), (5, 0, 0, 0, 0, 0, 0, 0), (0,) * 8, 0)

    monkeypatch.setattr(operational_routes, "get_reject_counts_by_reason", recording)

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert _counts(response) == {"item_not_found": 5}
    assert seen["local_date"] == date(2026, 6, 10) and type(seen["local_date"]) is date
    assert seen["zone"] == ZoneInfo("America/Chicago") and isinstance(seen["zone"], ZoneInfo)
    assert isinstance(seen["conn"], FakeOperationalConnection)       # the scoped connection, not a new one
    assert (seen["tenant"].operational_customer_id, seen["tenant"].operational_branch_id) == (CUSTOMER, BRANCH)
    assert set(seen) == {"local_date", "zone", "conn", "tenant"}


def test_the_resolved_tenant_is_the_one_the_scoped_connection_is_opened_for(api, monkeypatch, saas_db, operational):
    opened, served = [], []
    real_open = tenant_scope.open_customer_tenant_connection
    real_service = operational_metrics_service.get_reject_counts_by_reason

    def recording_open(tenant):
        opened.append(tenant)
        return real_open(tenant)

    def recording_service(conn, tenant, **kwargs):
        served.append(tenant)
        return real_service(conn, tenant, **kwargs)

    monkeypatch.setattr(operational_routes, "open_customer_tenant_connection", recording_open)
    monkeypatch.setattr(operational_routes, "get_reject_counts_by_reason", recording_service)

    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 200
    assert len(opened) == len(served) == 1 and opened[0] is served[0]
    assert (opened[0].org_slug, opened[0].branch_slug) == ("acme", "main")
    assert (opened[0].operational_customer_id, opened[0].operational_branch_id) == (CUSTOMER, BRANCH)


def test_the_queries_read_the_reject_tables_for_the_local_day_of_the_configured_zone(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER

    api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert [table for table, _, _ in operational.queries] == ["v2_cutovers", "rejects", "reject_events"]
    v1, v2 = operational.parameters_for("rejects"), operational.parameters_for("reject_events")
    assert (v1["start_local"], v1["end_local"]) == (_local(2026, 6, 10), _local(2026, 6, 10, 12))
    assert (v2["start_utc"], v2["end_utc"]) == (NOON_CUTOVER, datetime(2026, 6, 11, 5, 0, tzinfo=UTC))
    assert all("GROUP BY" in sql for sql in operational.statements[1:])   # the grouped statements, not the counts


def test_the_reasons_and_the_count_ask_for_the_same_rows(api, saas_db, operational):
    for cutover in (None, datetime(2026, 6, 1, 5, 0, tzinfo=UTC), NOON_CUTOVER, datetime(2026, 6, 20, 5, 0, tzinfo=UTC)):
        operational.cutover = cutover
        operational.queries.clear()
        api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
        by_reason = list(operational.queries)
        operational.queries.clear()
        api.get(REJECT_COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10)

        assert by_reason == operational.queries   # same tables, same ids, same bounds, same context


def test_every_query_runs_for_the_resolved_tenant_on_a_connection_carrying_its_context(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER

    api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert len(operational.queries) == 3
    for _table, parameters, context in operational.queries:
        assert (parameters["customer_id"], parameters["branch_id"]) == (CUSTOMER, BRANCH)
        assert context == {"customer_id": str(CUSTOMER), "branch_id": str(BRANCH)}


def test_the_configured_zone_is_echoed_and_used(api, monkeypatch, saas_db, operational):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "Asia/Kolkata")
    operational.cutover = NOON_CUTOVER   # 22:30 on 10 June in Kolkata

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.json()["timezone"] == "Asia/Kolkata"
    assert operational.parameters_for("rejects")["end_local"] == _local(2026, 6, 10, 22, 30)
    # Local midnight in Kolkata is 18:30Z the day before; the day ends at 18:30Z, 90 minutes after the cutover.
    assert operational.parameters_for("reject_events")["end_utc"] == datetime(2026, 6, 10, 18, 30, tzinfo=UTC)


def test_a_local_day_is_never_assumed_to_be_24_hours(api, saas_db, operational):
    operational.cutover = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)   # long before: v2 owns each whole day

    spans = {}
    for label, day in (("spring forward", "2026-03-08"), ("ordinary", "2026-06-10"), ("fall back", "2026-11-01")):
        operational.queries.clear()
        api.get(ACME_MAIN, headers=COOKIE, params={"date": day})
        v2 = operational.parameters_for("reject_events")
        spans[label] = v2["end_utc"] - v2["start_utc"]

    assert spans == {
        "spring forward": timedelta(hours=23), "ordinary": timedelta(hours=24), "fall back": timedelta(hours=25),
    }


# =====================================================================================================================
# The date parameter
# =====================================================================================================================

def test_the_date_is_required_and_never_defaults_to_today(api, saas_db, operational):
    response = api.get(ACME_MAIN, headers=COOKIE)

    assert response.status_code == 422
    assert operational.log == []


@pytest.mark.parametrize(
    "value",
    [
        "", "today", "not-a-date-CANARY", "2026-6-1", "20260610", "2026/06/10", "10-06-2026", "2026-W24-3",
        "2026-06-10T00:00:00", "2026-06-10T12:30:00Z", "2026-06-10 00:00:00", "1781136000", "2026-06-10 ",
        " 2026-06-10", "2026-06", "+2026-06-10",
    ],
)
def test_anything_that_is_not_exactly_a_calendar_date_is_422(api, saas_db, operational, value):
    response = api.get(ACME_MAIN, headers=COOKIE, params={"date": value})

    assert response.status_code == 422
    assert operational.log == []
    if value.strip():
        assert value.strip() not in response.text   # the hardened 422 never echoes a submitted value


@pytest.mark.parametrize("value", ["2026-02-30", "2026-13-01", "2026-00-10", "2026-06-31", "2025-02-29", "0000-01-01"])
def test_an_impossible_date_is_422(api, saas_db, operational, value):
    response = api.get(ACME_MAIN, headers=COOKIE, params={"date": value})

    assert response.status_code == 422
    assert operational.log == []


def test_a_repeated_date_parameter_behaves_as_on_the_reject_count_route(api, saas_db, operational):
    by_reason = api.get(ACME_MAIN + "?date=2026-06-10&date=2026-06-11", headers=COOKIE)
    count = api.get(REJECT_COUNT_ACME_MAIN + "?date=2026-06-10&date=2026-06-11", headers=COOKIE)

    # One date reaches the service -- never a range built from two values -- and it is the same one on both routes.
    assert by_reason.status_code == count.status_code == 200
    assert by_reason.json()["date"] == count.json()["date"]
    assert by_reason.json()["date"] in ("2026-06-10", "2026-06-11")
    assert operational.log.count("rejects") == 2   # one statement for each of the two requests


@pytest.mark.parametrize("value", ["2026-06-10T00:00:00", "2026-02-30", "", "today"])
def test_a_422_here_is_the_same_as_on_the_reject_count_route(api, saas_db, operational, value):
    by_reason = api.get(ACME_MAIN, headers=COOKIE, params={"date": value})
    count = api.get(REJECT_COUNT_ACME_MAIN, headers=COOKIE, params={"date": value})

    assert (by_reason.status_code, by_reason.content) == (count.status_code, count.content)
    assert by_reason.status_code == 422


def test_a_missing_date_is_the_same_422_as_on_the_reject_count_route(api, saas_db, operational):
    by_reason = api.get(ACME_MAIN, headers=COOKIE)
    count = api.get(REJECT_COUNT_ACME_MAIN, headers=COOKIE)

    assert (by_reason.status_code, by_reason.content) == (count.status_code, count.content)


# =====================================================================================================================
# Authentication and tenant reachability
# =====================================================================================================================

def test_no_session_is_401_and_nothing_is_resolved_or_read(saas_db, operational):
    main.limiter.reset()

    response = TestClient(main.app).get(ACME_MAIN, params=JUNE_10)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert operational.log == []


def test_a_session_that_does_not_validate_is_401(monkeypatch, saas_db, operational):
    monkeypatch.setattr(session_service, "validate_session", lambda raw_token: None)

    response = TestClient(main.app).get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert operational.log == []


UNREACHABLE = {
    "unknown organization": ("no-such-org", "main"),
    "organization the user is not a member of": ("beta", "north"),
    "cancelled organization": ("closed", "main"),
    "organization with no operational customer id": ("unmapped-org", "main"),
    "unknown branch": ("acme", "no-such-branch"),
    "inactive branch": ("acme", "shut"),
    "branch belonging to another organization": ("acme", "north"),
    "branch with no operational branch id": ("acme", "unmapped"),
}


@pytest.mark.parametrize(("org", "branch"), UNREACHABLE.values(), ids=UNREACHABLE.keys())
def test_every_unreachable_tenant_is_the_same_404_and_opens_no_connection(api, saas_db, operational, org, branch):
    response = api.get(PATH.format(org=org, branch=branch), headers=COOKIE, params=JUNE_10)

    assert response.status_code == 404
    assert response.json() == TENANT_NOT_FOUND
    assert operational.log == []


def test_the_unreachable_responses_are_byte_for_byte_identical(api, saas_db, operational):
    seen = {
        (r.status_code, r.content, json.dumps(sorted(r.headers.items())))
        for r in (
            api.get(PATH.format(org=org, branch=branch), headers=COOKIE, params=JUNE_10)
            for org, branch in UNREACHABLE.values()
        )
    }

    assert len(seen) == 1


def test_an_unreachable_tenant_answers_exactly_as_it_does_on_the_reject_count_route(api, saas_db, operational):
    for org, branch in (("beta", "north"), ("acme", "no-such-branch")):
        by_reason = api.get(PATH.format(org=org, branch=branch), headers=COOKIE, params=JUNE_10)
        count = api.get(f"/api/organizations/{org}/branches/{branch}/rejects/count", headers=COOKIE, params=JUNE_10)

        assert (by_reason.status_code, by_reason.content) == (count.status_code, count.content)
        assert by_reason.status_code == 404


def test_eight_zeros_and_an_unreachable_tenant_are_different_answers(api, saas_db, operational):
    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 200
    assert api.get(PATH.format(org="acme", branch="shut"), headers=COOKIE, params=JUNE_10).status_code == 404


# =====================================================================================================================
# The request cannot choose the tenant, or narrow the answer
# =====================================================================================================================

def test_operational_ids_and_other_hints_in_the_request_change_nothing(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_groups, operational.v2_groups = [("Item not found", 3)], [("rfid_collision", 4)]
    plain = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    plain_queries = list(operational.queries)
    operational.queries.clear()

    # The customer API's policy for a query parameter it does not declare: ignored, never an error, never obeyed.
    tampered = api.get(
        ACME_MAIN,
        headers={**COOKIE, "X-Customer-Id": "8202", "X-Branch-Id": "21"},
        params={**JUNE_10, "customer_id": 8202, "branch_id": 21, "operational_customer_id": 8202,
                "operational_branch_id": 21, "org_slug": "beta", "branch_slug": "north", "user_id": 2,
                "timezone": "Asia/Tokyo", "zone": "UTC", "cutover": "2020-01-01T00:00:00Z", "start_date": "2020-01-01",
                "end_date": "2030-01-01", "from": "2020-01-01", "to": "2030-01-01", "era": "v2", "v1": "false",
                "reason": "item_not_found", "reasons": "rfid_collision", "error_class": "rfid_collision",
                "include_zero": "false", "labels": "true", "sort": "count", "order": "desc", "limit": 1, "raw": "true",
                "hour": 9, "distinct": "true", "total": "true"},
    )

    assert tampered.status_code == 200
    assert tampered.json() == plain.json() == _body(3, 0, 4, 0, 0, 0, 0, 0)
    assert operational.queries == plain_queries   # same statements, same ids, same bounds, same context


def test_the_route_takes_the_two_path_slugs_and_one_required_date():
    route = next(r for r in main.app.routes if r.path.endswith("/rejects/by-reason"))
    flat = get_flat_dependant(route.dependant)

    assert route.path == "/api/organizations/{org_slug}/branches/{branch_slug}/rejects/by-reason"
    assert route.methods == {"GET"}
    assert sorted(p.name for p in flat.path_params) == ["branch_slug", "org_slug"]
    # One query parameter, required: no range, no reason filter, no zone, no ordering, no paging.
    assert [(p.alias, p.field_info.is_required()) for p in flat.query_params] == [("date", True)]
    assert flat.body_params == []
    assert flat.header_params == []
    assert flat.cookie_params == []


def test_the_route_is_read_only(api):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(api, method)(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 405


def test_the_route_is_not_rate_limited_differently_from_its_siblings():
    routes = {r.path.split("/branches/{branch_slug}/")[1]: r for r in main.app.routes
              if "/branches/{branch_slug}/" in getattr(r, "path", "")}

    assert not hasattr(routes["rejects/by-reason"].endpoint, "__wrapped__")       # no limiter decorator
    assert routes["rejects/by-reason"].dependencies == routes["rejects/count"].dependencies


# =====================================================================================================================
# Failures are 500, never a partial or empty answer
# =====================================================================================================================

def _assert_generic_500(response) -> None:
    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert "reasons" not in response.text and "reject_count" not in response.text
    for leaked in ("synthetic", str(CUSTOMER), "CANARY", "Smith", "rejects", "reject_events", "v2_cutovers",
                   "current_setting", "SELECT", "GROUP BY", "ZoneInfo", "Mars", "Traceback", "RuntimeError",
                   "item_not_found", "jam"):
        assert leaked not in response.text, leaked
    assert response.headers["cache-control"] == "no-store"


def test_a_resolver_database_failure_is_500(api, monkeypatch, operational):
    def broken_resolve(user_id, org_slug, branch_slug):
        raise RuntimeError(f"synthetic database failure for customer {CUSTOMER}")

    monkeypatch.setattr(tenant_scope, "resolve_operational_tenant", broken_resolve)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))
    assert operational.log == []


@pytest.mark.parametrize("value", ["Mars/Olympus_Mons", "", "   ", "America/Chicagoo", "../etc/passwd"])
def test_an_invalid_configured_zone_is_500_and_opens_no_connection(api, monkeypatch, saas_db, operational, value):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", value)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))
    assert operational.log == []   # never a silent fall-back to some other zone, and never a query in the wrong one


def test_a_failure_reading_the_product_time_zone_is_500(api, monkeypatch, saas_db, operational):
    def broken_zone():
        raise RuntimeError("synthetic settings failure")

    monkeypatch.setattr(operational_routes.settings, "product_timezone", broken_zone)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))
    assert operational.log == []


def test_an_engine_failure_is_500(api, monkeypatch, saas_db):
    def broken_get_engine():
        raise RuntimeError("synthetic engine failure")

    monkeypatch.setattr(tenant_scope, "get_engine", broken_get_engine)

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))


def test_a_failure_reading_the_tenant_context_back_is_500(api, saas_db, operational):
    operational.fail_on = "context"

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))
    assert operational.log == ["open", "verify context", "close"]


@pytest.mark.parametrize(
    "read_back",
    [
        lambda settings: {**settings, "customer_id": None},
        lambda settings: {**settings, "branch_id": "21"},
    ],
    ids=["context missing", "another tenant's context"],
)
def test_a_tenant_context_that_does_not_verify_is_500_and_nothing_is_read(api, saas_db, operational, read_back):
    operational.read_back = read_back

    _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))
    assert operational.log == ["open", "verify context", "close"]
    assert operational.queries == []


@pytest.mark.parametrize("failing", ["v2_cutovers", "rejects", "reject_events"])
def test_a_failure_in_any_metric_query_is_500_never_partial_reasons(api, saas_db, operational, caplog, failing):
    operational.cutover = NOON_CUTOVER
    operational.v1_groups, operational.v2_groups = [(CANARY, 4001)], [("jam", 2003)]
    operational.fail_on = failing

    with caplog.at_level(logging.DEBUG):
        response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    _assert_generic_500(response)
    assert "4001" not in response.text and "2003" not in response.text   # neither era's reasons come back alone
    assert operational.log[-2:] == [failing, "close"]
    # Logged as a safe summary only: not the exception's own text, and not a row's content.
    for leaked in ("synthetic", "CANARY", "Smith", "jam"):
        assert leaked not in caplog.text, leaked


def test_a_service_failure_is_500(api, monkeypatch, saas_db, operational, caplog):
    def broken_service(conn, tenant, **kwargs):
        raise RuntimeError(f"synthetic service failure for customer {CUSTOMER}: {CANARY}")

    monkeypatch.setattr(operational_routes, "get_reject_counts_by_reason", broken_service)

    with caplog.at_level(logging.DEBUG):
        _assert_generic_500(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10))

    assert operational.log == ["open", "verify context", "close"]   # the scoped connection is still closed
    assert "CANARY" not in caplog.text and "synthetic" not in caplog.text


def test_a_500_here_is_the_same_as_on_the_reject_count_route(api, saas_db, operational):
    operational.fail_on = "v2_cutovers"

    by_reason = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    count = api.get(REJECT_COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert (by_reason.status_code, by_reason.content) == (count.status_code, count.content) == (500, by_reason.content)


def test_a_failure_of_the_check_in_tables_does_not_touch_the_reasons(api, saas_db, operational):
    operational.v1_groups = [("Item not found", 6)]
    operational.fail_on = "checkins"

    assert _counts(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)) == {"item_not_found": 6}


# =====================================================================================================================
# Lifecycle, caching and the response itself
# =====================================================================================================================

def test_the_connection_is_closed_before_the_response_is_built(api, monkeypatch, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    real_model, real_response = operational_routes.RejectsByReasonResponse, operational_routes.JSONResponse

    def recording_model(*args, **kwargs):
        operational.log.append("build body")
        return real_model(*args, **kwargs)

    def recording_response(*args, **kwargs):
        operational.log.append("build response")
        return real_response(*args, **kwargs)

    monkeypatch.setattr(operational_routes, "RejectsByReasonResponse", recording_model)
    monkeypatch.setattr(operational_routes, "JSONResponse", recording_response)

    assert api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10).status_code == 200
    assert operational.log == [
        "open", "verify context", "v2_cutovers", "rejects", "reject_events", "close", "build body", "build response",
    ]


def test_every_request_opens_its_own_connection_and_nothing_is_cached(api, saas_db, operational):
    operational.v1_groups = [("Item not found", 5)]
    assert _counts(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)) == {"item_not_found": 5}

    operational.v1_groups = [("Library not found", 9)]
    assert _counts(api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)) == {"routing_error": 9}
    assert operational.log.count("open") == operational.log.count("close") == 2


def test_the_response_is_never_cacheable(api, saas_db, operational):
    ok = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    count = api.get(REJECT_COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert ok.headers["cache-control"] == count.headers["cache-control"] == "no-store"
    assert "etag" not in ok.headers and "last-modified" not in ok.headers and "expires" not in ok.headers


def test_the_response_is_json_with_the_same_headers_as_its_sibling_routes(api, saas_db, operational):
    by_reason = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)
    siblings = [api.get(path, headers=COOKIE, params=JUNE_10)
                for path in (REJECT_COUNT_ACME_MAIN, CHECKIN_COUNT_ACME_MAIN, CHECKINS_BY_HOUR_ACME_MAIN)]

    assert by_reason.headers["content-type"] == "application/json"
    assert json.loads(by_reason.content) == by_reason.json()
    for sibling in siblings:
        assert sibling.headers["content-type"] == by_reason.headers["content-type"]
        assert sorted(set(sibling.headers) - {"content-length"}) == sorted(set(by_reason.headers) - {"content-length"})
    assert "set-cookie" not in by_reason.headers


# =====================================================================================================================
# How the route is built
# =====================================================================================================================

def _endpoint(suffix: str):
    return next(r for r in main.app.routes if getattr(r, "path", "").endswith(suffix)).endpoint


def test_the_route_reaches_the_database_only_through_the_tenant_scope_and_the_metrics_service():
    source = inspect.getsource(operational_routes)

    assert operational_routes.get_reject_counts_by_reason is operational_metrics_service.get_reject_counts_by_reason
    assert operational_routes.open_customer_tenant_connection is tenant_scope.open_customer_tenant_connection
    assert operational_routes.REJECT_REASONS is reject_reason.REJECT_REASONS
    for forbidden in ("get_engine", "tenant_connection(", "resolve_operational_tenant", "import database",
                      "from database", "tenant_db", "set_config", "SELECT", "GROUP BY", "COUNT(", "text(",
                      "data_loader", "mixed_era", "pandas", "streamlit", "import main", "from main", "America/Chicago",
                      "v1_count", "v2_count", "unexpected", "reject_logic", "simplify_error", "ingest_v2"):
        assert forbidden not in source.replace("open_customer_tenant_connection(", ""), forbidden


def test_the_operational_modules_import_no_pandas_streamlit_dashboard_or_collector_code():
    for module in (operational_routes, operational_schemas):
        imports = [line for line in inspect.getsource(module).splitlines() if line.startswith(("import ", "from "))]
        for forbidden in ("pandas", "streamlit", "reject_logic", "collector", "ingest_v2", "agent", "data_loader",
                          "dashboard", "mixed_era", "sqlalchemy"):
            assert not any(forbidden in line for line in imports), (module.__name__, forbidden)

    assert "from services.reject_reason import REJECT_REASONS" in inspect.getsource(operational_routes)
    assert "from services.reject_reason import RejectReason" in inspect.getsource(operational_schemas)


def test_the_route_classifies_nothing_itself():
    source = inspect.getsource(_endpoint("/rejects/by-reason"))

    # No rule, no wording, no stored value and no code is named: the reasons and their order come from
    # REJECT_REASONS, and what counts under each is the service's answer.
    for forbidden in ("classify", "reason_for_error_class", "error_", "message", ".lower(", ".strip(", " in text",
                      "not found", "acs", "rfid", "label", "if ", "else", "sorted(", ".sort(", "sum(",
                      *APPROVED_REASONS):
        assert forbidden not in source, forbidden
    assert "zip(REJECT_REASONS, by_reason.counts, strict=True)" in source
    assert source.count("by_reason.") == 1   # `.counts`, once, and nothing else of the result


def test_the_route_is_built_like_the_reject_count_route():
    routes = {r.path.split("/branches/{branch_slug}/")[1]: r for r in main.app.routes
              if "/branches/{branch_slug}/" in getattr(r, "path", "")}
    by_reason, count = routes["rejects/by-reason"], routes["rejects/count"]

    # The same two declared parameters -- the tenant-scope dependency and the strict required date...
    signatures = [{name: p.annotation for name, p in inspect.signature(r.endpoint).parameters.items()}
                  for r in (by_reason, count)]
    assert signatures[0] == signatures[1] == {"tenant": "ResolvedTenant", "local_date": "LocalDate"}
    assert [d.call for d in by_reason.dependant.dependencies] == [d.call for d in count.dependant.dependencies]
    # ...the same route class producing the same error bodies, and the same steps in the same order.
    assert type(by_reason) is type(count)
    source = inspect.getsource(by_reason.endpoint)
    steps = ["zone = settings.product_timezone()", "with open_customer_tenant_connection(tenant) as conn:",
             "get_reject_counts_by_reason(conn, tenant, local_date=local_date, zone=zone)", "RejectsByReasonResponse(",
             " date=local_date,", "timezone=zone.key", "RejectReasonCount(reason=reason, reject_count=reject_count)",
             "headers=NO_STORE_HEADERS"]
    positions = [source.index(step) for step in steps]
    assert positions == sorted(positions)


def test_the_customer_api_still_loads_without_streamlit_or_pandas():
    # The seam itself is proven in a clean process by tests/test_customer_api_import_seam.py; this pins that the
    # modules this block touched, and the two they now reach, name neither.
    for module in (operational_routes, operational_schemas, operational_metrics_service, reject_reason):
        imports = [line for line in inspect.getsource(module).splitlines() if line.startswith(("import ", "from "))]
        assert not any("streamlit" in line or "pandas" in line or "reject_logic" in line for line in imports)


# =====================================================================================================================
# The sibling routes are as they were
# =====================================================================================================================

def test_the_reject_count_route_is_unchanged(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_groups, operational.v2_groups = [("Item not found", 3), (None, 1)], [("jam", 4)]

    response = api.get(REJECT_COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert response.status_code == 200
    assert response.json() == {"date": "2026-06-10", "timezone": "America/Chicago", "reject_count": 8}
    assert response.headers["cache-control"] == "no-store"
    assert operational.log == ["open", "verify context", "v2_cutovers", "rejects", "reject_events", "close"]
    # Still plain counts: it reads no reason column and groups nothing.
    for sql in operational.statements:
        assert "error_" not in sql and "GROUP BY" not in sql
    assert "reason" not in response.text

    source = inspect.getsource(_endpoint("/rejects/count"))
    assert "get_reject_count(conn, tenant, local_date=local_date, zone=zone)" in source
    assert "reject_count=count.total" in source and "reason" not in source


def test_the_check_in_routes_are_unchanged_and_never_read_a_reject_table(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_groups, operational.v2_groups = [("Item not found", 3)], [("rfid_collision", 4)]
    operational.v1_checkins, operational.v2_checkins = 100, 20

    count = api.get(CHECKIN_COUNT_ACME_MAIN, headers=COOKIE, params=JUNE_10)
    by_hour = api.get(CHECKINS_BY_HOUR_ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert count.json() == {"date": "2026-06-10", "timezone": "America/Chicago", "checkin_count": 120}
    assert list(by_hour.json()) == ["date", "timezone", "hours"]
    assert by_hour.json()["hours"][0] == {"hour": 0, "checkin_count": 120} and len(by_hour.json()["hours"]) == 24
    assert operational.log == ["open", "verify context", "v2_cutovers", "checkins", "checkin_events", "close"] * 2
    for suffix in ("/checkins/count", "/checkins/by-hour", "/ingest-status"):
        assert "reason" not in inspect.getsource(_endpoint(suffix)).lower(), suffix


def test_the_reasons_route_never_reads_a_check_in_table(api, saas_db, operational):
    operational.cutover = NOON_CUTOVER
    operational.v1_groups, operational.v2_groups = [("Item not found", 3)], [("rfid_collision", 4)]

    response = api.get(ACME_MAIN, headers=COOKIE, params=JUNE_10)

    assert _counts(response) == {"item_not_found": 3, "rfid_collision": 4}
    assert "checkins" not in operational.log and "checkin_events" not in operational.log
    assert "90001" not in response.text and "80002" not in response.text


def test_the_branch_routes_are_exactly_the_five_operational_endpoints():
    paths = sorted(r.path.split("/branches/{branch_slug}/")[1] for r in main.app.routes
                   if "/branches/{branch_slug}/" in getattr(r, "path", ""))

    assert paths == ["checkins/by-hour", "checkins/count", "ingest-status", "rejects/by-reason", "rejects/count"]
