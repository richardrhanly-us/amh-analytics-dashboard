"""Server-enforced Contract v2 enrollment (onboarding stage A1): the contract an enrollment code REQUIRES, what an
installer may ask for, and what a contract-2 enrollment creates and returns.

Runs the real service SQL and the real endpoints against SQLite, as tests/test_collector_enrollment.py does for the
legacy enrollment (which this file leaves alone: every test there still passes, unchanged, and is the proof that a
legacy code with a legacy request behaves exactly as it always did). What needs a real server -- the migration, the
runtime role's privileges, row level security on the key insert, and true concurrency -- is in
tests/test_collector_enrollment_v2_postgres.py.

Operational customer ids (10, 11, ...) never equal SaaS organization ids (1, 2, ...), so confusing the two fails a
test. Every account name below is made up and marked CANARY, so "it was never logged or sent" can be asserted.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool
from test_ingest_v2_api import _V1_DDL, _V2_DDL, checkin, v1_payload

import main
from src.services import collector_enrollment_service as enrollment
from src.services import collector_rules
from src.services.collector_enrollment_service import EnrollmentError, EnrollmentRefused

PUBLIC = {"detail": "Invalid or expired enrollment code"}
_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
UUID4 = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"

LEGACY_KEYS = {"customer_id", "branch_id", "installation_id", "agent_token"}
V2_KEYS = LEGACY_KEYS | {"contract_version", "contract_mode", "key_id", "timezone", "classification_rules"}

ORG_A_CREATED = "2025-03-04T05:06:07+00:00"
ORG_B_CREATED = "2025-08-09T10:11:12+00:00"
REVIEWED = collector_rules.rules_review_marker(reviewed_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC), reviewed_by_user_id=5)

CANARY_A = "CANARY-A (ST)STAFFER"
CANARY_B = "CANARY-B (ST)OTHERLIB"
SETTINGS_A = {
    "library_name": "Lib A",
    "collector_timezone": "America/New_York",
    "collector_rules": REVIEWED,
    "transit": {"home_branch_label": "Central", "destinations": [{"key": "north_annex", "label": "North Annex", "enabled": True}]},
    "internal_routing": {
        "branch_services_names": [CANARY_A, "outreach desk"],
        "collection_services_names": ["(ZZ) TS(ZZ)-CATALOGING"],
        "branch_services_da_patterns": [f"DA{CANARY_A}"],
        "collection_services_da_patterns": [],
    },
}
SETTINGS_B = {
    "collector_timezone": "America/Denver",
    "collector_rules": REVIEWED,
    "internal_routing": {"branch_services_names": [CANARY_B]},
}
# Reviewed, and nothing in any list: a site with no service accounts.
SETTINGS_EMPTY = {"collector_timezone": "Pacific/Honolulu", "collector_rules": REVIEWED,
                  "internal_routing": {name: [] for name in collector_rules.NAME_LISTS}}

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


# No test here depends on WHEN it runs: codes are generated and redeemed against the same real clock within one test,
# and an expired code is written as one (see _insert_code). Expiry itself is tested, on a controlled clock, in
# tests/test_collector_enrollment.py.


# --- database ------------------------------------------------------------------------------------------------------------

_DDL = [
    "CREATE TABLE customers (id INTEGER PRIMARY KEY)",
    """CREATE TABLE organizations (
        id INTEGER PRIMARY KEY, slug TEXT, status TEXT, operational_customer_id INTEGER, created_at TEXT)""",
    "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, status TEXT, operational_branch_id INTEGER)",
    "CREATE TABLE organization_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER UNIQUE, settings_json TEXT)",
    "CREATE TABLE branch_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id INTEGER UNIQUE, settings_json TEXT)",
    """CREATE TABLE collector_installations (
        id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER, name TEXT, hostname TEXT,
        collector_version TEXT, status TEXT, installed_at TEXT, last_seen_at TEXT, created_at TEXT, updated_at TEXT,
        required_contract INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE agent_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE, customer_id INTEGER NOT NULL,
        branch_id INTEGER NOT NULL, description TEXT, is_active BOOLEAN NOT NULL DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_used_at TEXT, installation_id INTEGER)""",
    """CREATE TABLE collector_enrollment_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, installation_id INTEGER NOT NULL, code_hash TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL, used_at TEXT, revoked_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_by_user_id INTEGER, required_contract INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE v2_cutovers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL, branch_id INTEGER NOT NULL, cutover_at TEXT,
        set_by TEXT NOT NULL, set_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, note TEXT)""",
    *_V1_DDL, *_V2_DDL,
]


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _register_sha256(dbapi_connection, _record):
        dbapi_connection.create_function("sha256hex", 1, lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest())

    with engine.begin() as conn:
        for statement in _DDL:
            conn.execute(text(statement))
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", True)
    assert _HASH_EXPR in main._AGENT_TOKEN_LOOKUP_SQL
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, "sha256hex(:token)"))
    return engine


def _run(engine, sql, **params):
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _tenant(engine, org_id, customer_id, branch_id, *, created_at, settings=None, branch_settings=None, org_status="active"):
    _run(engine, "INSERT OR IGNORE INTO customers VALUES (:c)", c=customer_id)
    _run(engine, "INSERT INTO organizations VALUES (:i, :slug, :s, :c, :created)",
         i=org_id, slug=f"org-{org_id}", s=org_status, c=customer_id, created=created_at)
    _run(engine, "INSERT INTO branches VALUES (:i, :o, 'active', :i)", i=branch_id, o=org_id)
    if settings is not None:
        _set_settings(engine, org_id, settings)
    if branch_settings is not None:
        _run(engine, "INSERT INTO branch_settings (branch_id, settings_json) VALUES (:b, :j)",
             b=branch_id, j=json.dumps(branch_settings))


def _set_settings(engine, org_id, settings):
    stored = settings if isinstance(settings, str) else json.dumps(settings)
    _run(engine, "DELETE FROM organization_settings WHERE organization_id = :o", o=org_id)
    _run(engine, "INSERT INTO organization_settings (organization_id, settings_json) VALUES (:o, :j)", o=org_id, j=stored)


def _installation(engine, installation_id, org_id, branch_id, *, contract=1, status="provisioning"):
    _run(engine,
         "INSERT INTO collector_installations (id, organization_id, branch_id, name, hostname, collector_version, status, "
         "required_contract) VALUES (:id, :o, :b, :n, 'AMH-PC', '1.0.13', :s, :rc)",
         id=installation_id, o=org_id, b=branch_id, n=f"Sorter {installation_id}", s=status, rc=contract)


@pytest.fixture
def world(db):
    """org 1 / customer 10 / branch 1   ready for Contract v2: installation 101 requires contract 2,
                                         102 (a second machine at the same branch) requires 2, 103 is legacy
       org 2 / customer 11 / branch 2   another library, also ready: installation 201 requires 2
       org 3 / customer 12 / branch 3   a contract-2 installation (301) whose site has NO settings at all
       org 4 / customer 13 / branch 4   a legacy library: installation 401 requires contract 1
    """
    _tenant(db, 1, 10, 1, created_at=ORG_A_CREATED, settings=SETTINGS_A)
    _tenant(db, 2, 11, 2, created_at=ORG_B_CREATED, settings=SETTINGS_B)
    _tenant(db, 3, 12, 3, created_at="2026-01-01T00:00:00+00:00")
    _tenant(db, 4, 13, 4, created_at="2024-01-01T00:00:00+00:00", settings={"library_name": "Legacy Lib"})
    _installation(db, 101, 1, 1, contract=2)
    _installation(db, 102, 1, 1, contract=2)
    _installation(db, 103, 1, 1, contract=1)
    _installation(db, 201, 2, 2, contract=2)
    _installation(db, 301, 3, 3, contract=2)
    _installation(db, 401, 4, 4, contract=1)
    return db


# --- helpers ---------------------------------------------------------------------------------------------------------------

def _generate(engine, installation_id, **kwargs):
    with engine.begin() as conn:
        return enrollment.create_enrollment_code(conn, installation_id, **kwargs)


def _code(engine, installation_id) -> str:
    return _generate(engine, installation_id)["enrollment_code"]


def _redeem(engine, code, **kwargs):
    with engine.begin() as conn:
        return enrollment.redeem_enrollment_code(conn, code, **kwargs)


def _table(engine, name):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(f"SELECT * FROM {name} ORDER BY 1")).mappings().all()]  # nosec B608


def _written(engine) -> dict:
    """Everything an enrollment can write: for "nothing happened" comparisons."""
    return {
        "codes": _table(engine, "collector_enrollment_codes"),
        "tokens": _table(engine, "agent_tokens"),
        "keys": _table(engine, "ingest_key_ids"),
        "cutovers": _table(engine, "v2_cutovers"),
        "installations": _table(engine, "collector_installations"),
    }


def _insert_code(engine, installation_id, *, required_contract, used=False, revoked=False, expired=False):
    """A code row written directly, for states generation would not produce."""
    raw = enrollment.new_enrollment_code()
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(enrollment._timestamped(
            "INSERT INTO collector_enrollment_codes (installation_id, code_hash, expires_at, used_at, revoked_at, "
            "required_contract) VALUES (:installation_id, :code_hash, :expires_at, :used_at, :revoked_at, :required_contract)",
            installation_id=installation_id, code_hash=enrollment.hash_enrollment_code(raw),
            expires_at=now + (timedelta(minutes=-1) if expired else enrollment.DEFAULT_ENROLLMENT_CODE_TTL),
            used_at=now - timedelta(hours=1) if used else None,
            revoked_at=now - timedelta(hours=1) if revoked else None,
            required_contract=required_contract,
        ))
    return raw


def _key(engine, customer_id, branch_id, *, status="active", key_id=None):
    key_id = key_id or f"00000000-0000-4000-8000-{customer_id:06d}{branch_id:06d}"
    _run(engine,
         "INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, status, retired_at) VALUES "
         "(:k, :c, :b, :s, CASE WHEN :s = 'retired' THEN CURRENT_TIMESTAMP END)", k=key_id, c=customer_id, b=branch_id, s=status)
    return key_id


def _cutover(engine, customer_id, branch_id, cutover_at):
    _run(engine, "INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, set_at, note) "
                 "VALUES (:c, :b, :at, 'operator', '2026-01-02 03:04:05+00:00', 'an operator decision')",
         c=customer_id, b=branch_id, at=cutover_at)


def _post(code, **body):
    return client.post("/collector/enroll", json={"enrollment_code": code, **body})


def _refused(engine, code, **kwargs) -> EnrollmentRefused:
    with pytest.raises(EnrollmentRefused) as excinfo:
        _redeem(engine, code, **kwargs)
    return excinfo.value


# =====================================================================================================================
# the contract an installation requires, and the code's snapshot of it
# =====================================================================================================================

def test_a_code_snapshots_its_installations_required_contract(world):
    v2, legacy = _generate(world, 101), _generate(world, 103)

    assert v2["required_contract"] == 2 and legacy["required_contract"] == 1
    assert [(c["installation_id"], c["required_contract"]) for c in _table(world, "collector_enrollment_codes")] == [(101, 2), (103, 1)]


def test_the_snapshot_does_not_follow_a_later_change_to_the_installation(world):
    v2_code, legacy_code = _code(world, 101), _code(world, 103)
    # The installations change their minds after the codes were issued.
    _run(world, "UPDATE collector_installations SET required_contract = 1 WHERE id = 101")
    _run(world, "UPDATE collector_installations SET required_contract = 2 WHERE id = 103")

    # Each code still permits exactly what it was issued for.
    assert _refused(world, v2_code).code == "collector_upgrade_required"
    assert _refused(world, legacy_code, contract_version=2).code == "contract_not_permitted"
    assert set(_redeem(world, legacy_code)) == LEGACY_KEYS
    assert set(_redeem(world, v2_code, contract_version=2)) == V2_KEYS
    # A NEW code follows the installation as it now is.
    assert _generate(world, 103)["required_contract"] == 2


def test_an_installation_with_a_contract_this_server_does_not_implement_gets_no_code(world):
    _run(world, "UPDATE collector_installations SET required_contract = 3 WHERE id = 101")

    with pytest.raises(EnrollmentError) as excinfo:
        _generate(world, 101)

    assert excinfo.value.reason == "installation_contract_unsupported"
    assert _table(world, "collector_enrollment_codes") == []


def test_the_contract_numbers_are_the_wire_contracts_and_the_refusals_are_four_fixed_sentences():
    assert (enrollment.CONTRACT_LEGACY, enrollment.CONTRACT_V2, enrollment.SUPPORTED_CONTRACTS) == (1, 2, (1, 2))
    assert enrollment.CONTRACT_MODE_V2 == "v2"
    assert enrollment.REFUSAL_DETAILS == {
        "collector_upgrade_required": "This enrollment code requires a newer SortView Collector.",
        "contract_not_permitted": "This enrollment code does not permit the requested contract.",
        "unsupported_contract_version": "The requested contract version is not supported.",
        "enrollment_not_ready": "This installation is not ready to enroll. Contact SortView support.",
    }


# =====================================================================================================================
# the compatibility matrix
# =====================================================================================================================

@pytest.mark.parametrize("request_version", [None, 1])
def test_a_legacy_code_with_a_legacy_request_is_the_legacy_enrollment_and_nothing_more(world, request_version):
    code = _code(world, 401)
    kwargs = {} if request_version is None else {"contract_version": request_version}

    result = _redeem(world, code, **kwargs)

    assert set(result) == LEGACY_KEYS  # exactly the four fields, as always
    assert (result["customer_id"], result["branch_id"], result["installation_id"]) == (13, 4, 401)
    assert len(_table(world, "agent_tokens")) == 1
    # No Contract v2 state of any kind for a legacy enrollment.
    assert _table(world, "ingest_key_ids") == [] and _table(world, "v2_cutovers") == []


MATRIX_REFUSALS = [
    # installation (its contract), requested version, public code, internal reason
    (401, 2, "contract_not_permitted", "code_permits_contract_1_only"),
    (101, None, "collector_upgrade_required", "code_requires_contract_2"),
    (101, 1, "collector_upgrade_required", "code_requires_contract_2"),
    (101, 0, "unsupported_contract_version", "requested_contract_unsupported"),
    (101, 3, "unsupported_contract_version", "requested_contract_unsupported"),
    (101, -1, "unsupported_contract_version", "requested_contract_unsupported"),
    (101, 99, "unsupported_contract_version", "requested_contract_unsupported"),
    (401, 0, "unsupported_contract_version", "requested_contract_unsupported"),
    (401, 3, "unsupported_contract_version", "requested_contract_unsupported"),
]


@pytest.mark.parametrize("installation_id, requested, public_code, reason", MATRIX_REFUSALS)
def test_a_pairing_the_code_does_not_permit_is_refused_and_consumes_nothing(world, installation_id, requested, public_code, reason):
    code = _code(world, installation_id)
    before = _written(world)

    refusal = _refused(world, code, **({} if requested is None else {"contract_version": requested}))

    assert (refusal.code, refusal.reason) == (public_code, reason)
    assert refusal.detail == enrollment.REFUSAL_DETAILS[public_code]
    # The code is still unused, and nothing was created: no token, no key, no cutover.
    assert _written(world) == before
    assert _table(world, "collector_enrollment_codes")[0]["used_at"] is None


@pytest.mark.parametrize("requested", [None, 1, 2])
def test_a_code_requiring_a_contract_this_server_does_not_implement_needs_a_newer_collector(world, requested):
    code = _insert_code(world, 101, required_contract=3)
    before = _written(world)

    refusal = _refused(world, code, **({} if requested is None else {"contract_version": requested}))

    assert (refusal.code, refusal.reason) == ("collector_upgrade_required", "code_requires_unsupported_contract")
    assert _written(world) == before


@pytest.mark.parametrize("requested", [True, False, "2", 2.0, [2]])
def test_a_requested_version_that_is_not_an_integer_is_never_treated_as_one(world, requested):
    # The API's request model refuses these as malformed (a 422) before the service is reached; the service itself
    # still never mistakes one for a contract.
    code = _code(world, 101)

    assert _refused(world, code, contract_version=requested).code == "unsupported_contract_version"
    assert _table(world, "collector_enrollment_codes")[0]["used_at"] is None


def test_a_refused_code_can_still_be_redeemed_by_an_installer_that_can(world):
    code = _code(world, 101)
    for requested in (None, 1, 3):
        _refused(world, code, **({} if requested is None else {"contract_version": requested}))

    result = _redeem(world, code, contract_version=2)

    assert set(result) == V2_KEYS and len(_table(world, "agent_tokens")) == 1


# --- a refusal of the CODE comes first, and is still generic ------------------------------------------------------

@pytest.mark.parametrize("state, reason", [
    ({"used": True}, "already_used"), ({"revoked": True}, "revoked"), ({"expired": True}, "expired"),
])
@pytest.mark.parametrize("requested", [None, 2, 3])
def test_a_used_revoked_or_expired_code_is_the_generic_refusal_whatever_was_asked_for(world, state, reason, requested):
    code = _insert_code(world, 101, required_contract=2, **state)

    with pytest.raises(EnrollmentError) as excinfo:
        _redeem(world, code, **({} if requested is None else {"contract_version": requested}))

    assert excinfo.value.reason == reason  # never an EnrollmentRefused: nothing about the code is confirmed


@pytest.mark.parametrize("requested", [None, 2, 3])
def test_an_unknown_code_is_the_generic_refusal_whatever_was_asked_for(world, requested):
    with pytest.raises(EnrollmentError) as excinfo:
        _redeem(world, enrollment.new_enrollment_code(), **({} if requested is None else {"contract_version": requested}))

    assert excinfo.value.reason == "unknown_code"


@pytest.mark.parametrize("change, reason", [
    ("UPDATE organizations SET status = 'suspended' WHERE id = 1", "organization_status_suspended"),
    ("UPDATE organizations SET status = 'cancelled' WHERE id = 1", "organization_status_cancelled"),
    ("UPDATE branches SET status = 'inactive' WHERE id = 1", "branch_status_inactive"),
    ("UPDATE collector_installations SET status = 'retired' WHERE id = 101", "installation_status_retired"),
    ("UPDATE collector_installations SET status = 'inactive' WHERE id = 101", "installation_status_inactive"),
])
def test_a_tenant_that_cannot_be_enrolled_is_the_generic_refusal_for_a_contract_2_code_too(world, change, reason):
    code = _code(world, 101)
    _run(world, change)
    before = _written(world)

    with pytest.raises(EnrollmentError) as excinfo:
        _redeem(world, code, contract_version=2)

    assert excinfo.value.reason == reason and _written(world) == before


# =====================================================================================================================
# a Contract v2 enrollment
# =====================================================================================================================

def test_a_contract_2_enrollment_returns_exactly_the_nine_fields(world):
    result = _redeem(world, _code(world, 101), contract_version=2, hostname="AMH-PC", collector_version="1.1.0")

    assert set(result) == V2_KEYS and len(result) == 9
    assert (result["customer_id"], result["branch_id"], result["installation_id"]) == (10, 1, 101)
    assert result["contract_version"] == 2 and result["contract_mode"] == "v2"
    assert result["timezone"] == "America/New_York"
    assert isinstance(result["agent_token"], str) and len(result["agent_token"]) >= 40
    import re

    assert re.fullmatch(UUID4, result["key_id"])
    assert result["classification_rules"] == {
        "schema_version": 1,
        "destinations": [{"slug": "north_annex", "contains": "NORTH ANNEX"}],
        "branch_services_names": [CANARY_A, "OUTREACH DESK"],
        "collection_services_names": ["(ZZ) TS(ZZ)-CATALOGING"],
        "branch_services_da_patterns": [f"DA{CANARY_A}"],
        "collection_services_da_patterns": [],
    }
    json.dumps(result)  # it is a plain JSON document


def test_it_issues_the_token_the_key_and_the_cutover_together_and_marks_the_code_used(world):
    result = _redeem(world, _code(world, 101), contract_version=2)

    [code_row] = _table(world, "collector_enrollment_codes")
    [token] = _table(world, "agent_tokens")
    [key] = _table(world, "ingest_key_ids")
    [cutover] = _table(world, "v2_cutovers")

    assert code_row["used_at"] is not None and code_row["required_contract"] == 2
    # The token: hashed, scoped to the OPERATIONAL pair, bound to the installation -- exactly as a legacy enrollment's.
    assert token["token_hash"] == hashlib.sha256(result["agent_token"].encode("utf-8")).hexdigest()
    assert (token["customer_id"], token["branch_id"], token["installation_id"], token["is_active"]) == (10, 1, 101, 1)
    # The key: the id that was returned, active, for the same pair -- and nothing secret beside it.
    assert (key["key_id"], key["customer_id"], key["branch_id"], key["status"]) == (result["key_id"], 10, 1, "active")
    assert key["algorithm"] == "hmac-sha256-v1" and key["retired_at"] is None and key["last_heartbeat_at"] is None
    # The cutover: the branch's first, at the moment its ORGANIZATION was created, recorded with ids only.
    assert (cutover["customer_id"], cutover["branch_id"]) == (10, 1)
    assert datetime.fromisoformat(cutover["cutover_at"]) == datetime.fromisoformat(ORG_A_CREATED)
    assert cutover["set_by"] == "collector-enrollment"
    assert cutover["note"] == f"initial v2 cutover | enrollment_code_id={code_row['id']} installation_id=101"
    # The installation row is the heartbeat's to change, never enrollment's.
    assert _table(world, "collector_installations")[0]["status"] == "provisioning"


def test_the_initial_cutover_is_the_organizations_creation_instant_not_the_moment_of_enrollment(world):
    # Long after the organization was created -- and with the branch row holding no usable time of its own (this
    # schema's branches table has none at all: the service never reads one).
    _redeem(world, _code(world, 201), contract_version=2)

    [cutover] = _table(world, "v2_cutovers")
    assert datetime.fromisoformat(cutover["cutover_at"]) == datetime(2025, 8, 9, 10, 11, 12, tzinfo=UTC)
    assert datetime.fromisoformat(cutover["cutover_at"]).tzinfo is not None
    assert datetime.fromisoformat(cutover["cutover_at"]) < datetime.now(UTC) - timedelta(days=300)
    assert "b.created_at" not in enrollment._V2_SITE_SQL and "o.created_at" in enrollment._V2_SITE_SQL


def test_there_is_no_secret_in_the_response_and_none_on_the_server(world):
    result = _redeem(world, _code(world, 101), contract_version=2)

    # The only credential is the agent token. The HMAC secret behind key_id is generated on the Collector's machine.
    assert [name for name in result if any(word in name for word in ("secret", "hmac", "master", "private", "password"))] == []
    assert {name for name in result if "key" in name} == {"key_id"}
    stored_columns = {column for row in _table(world, "ingest_key_ids") for column in row}
    assert not {c for c in stored_columns if any(word in c for word in ("secret", "hmac", "material", "master"))}
    # Nothing raw is stored either: not the code, not the token.
    everything = json.dumps(_written(world), default=str)
    assert result["agent_token"] not in everything


def test_the_rules_are_the_enrolling_sites_own_and_never_another_librarys(world):
    a = _redeem(world, _code(world, 101), contract_version=2)
    b = _redeem(world, _code(world, 201), contract_version=2)

    assert CANARY_A in json.dumps(a) and CANARY_B not in json.dumps(a)
    assert CANARY_B in json.dumps(b) and CANARY_A not in json.dumps(b)
    assert (a["timezone"], b["timezone"]) == ("America/New_York", "America/Denver")
    assert b["classification_rules"]["destinations"] == []  # nothing configured: nothing invented
    for forbidden in ("WESTSIDE", "LIBRARY EXPRESS", "westside", "library_express"):
        assert forbidden not in json.dumps([a, b])
    # Each has its own key and its own cutover, scoped to its own pair.
    assert {(k["customer_id"], k["branch_id"]) for k in _table(world, "ingest_key_ids")} == {(10, 1), (11, 2)}
    assert {(c["customer_id"], c["branch_id"]) for c in _table(world, "v2_cutovers")} == {(10, 1), (11, 2)}
    assert a["key_id"] != b["key_id"]


def test_the_rules_are_exactly_what_the_builder_makes_of_the_sites_effective_settings(world):
    _run(world, "INSERT INTO branch_settings (branch_id, settings_json) VALUES (1, :j)",
         j=json.dumps({"internal_routing": {"branch_services_names": ["BRANCH ONLY DESK"]}, "branch_name": "Central"}))

    result = _redeem(world, _code(world, 101), contract_version=2)

    effective = collector_rules.effective_settings(
        SETTINGS_A, {"internal_routing": {"branch_services_names": ["BRANCH ONLY DESK"]}, "branch_name": "Central"})
    assert result["classification_rules"] == collector_rules.build_classification_rules(effective)
    # The branch's own list replaced the organization's; the organization's other lists stayed.
    assert result["classification_rules"]["branch_services_names"] == ["BRANCH ONLY DESK"]
    assert result["classification_rules"]["collection_services_names"] == ["(ZZ) TS(ZZ)-CATALOGING"]


def test_reviewed_and_empty_rules_are_a_valid_site(world):
    _set_settings(world, 3, SETTINGS_EMPTY)

    result = _redeem(world, _code(world, 301), contract_version=2)

    assert result["timezone"] == "Pacific/Honolulu"
    assert result["classification_rules"] == {"schema_version": 1, "destinations": [],
                                              **{name: [] for name in collector_rules.NAME_LISTS}}


def test_a_reinstall_style_active_installation_can_be_enrolled_on_contract_2_when_the_branch_has_no_identity(world):
    _run(world, "UPDATE collector_installations SET status = 'active' WHERE id = 201")

    assert set(_redeem(world, _code(world, 201), contract_version=2)) == V2_KEYS


def test_a_code_cannot_be_redeemed_twice(world):
    code = _code(world, 101)
    _redeem(world, code, contract_version=2)
    before = _written(world)

    with pytest.raises(EnrollmentError) as excinfo:
        _redeem(world, code, contract_version=2)

    assert excinfo.value.reason == "already_used" and _written(world) == before


# =====================================================================================================================
# a site that is not ready
# =====================================================================================================================

def _without(settings, key):
    return {k: v for k, v in settings.items() if k != key}


NOT_READY_SETTINGS = {
    "no settings row at all": (None, "timezone_not_configured"),
    "an empty settings document": ({}, "timezone_not_configured"),
    "no time zone": (_without(SETTINGS_A, "collector_timezone"), "timezone_not_configured"),
    "a null time zone": ({**SETTINGS_A, "collector_timezone": None}, "timezone_not_configured"),
    "a blank time zone": ({**SETTINGS_A, "collector_timezone": "   "}, "timezone_not_configured"),
    "a time zone that is not IANA": ({**SETTINGS_A, "collector_timezone": "Central Time"}, "timezone_invalid"),
    "a time zone that is not text": ({**SETTINGS_A, "collector_timezone": -5}, "timezone_invalid"),
    "rules never reviewed": (_without(SETTINGS_A, "collector_rules"), "rules_not_reviewed"),
    "a review record with no time": ({**SETTINGS_A, "collector_rules": {"reviewed_by_user_id": 5}}, "rules_not_reviewed"),
    "a review record with a naive time": (
        {**SETTINGS_A, "collector_rules": {"reviewed_at": "2026-09-01T12:00:00", "reviewed_by_user_id": 5}}, "rules_not_reviewed"),
    "a review record with no reviewer": (
        {**SETTINGS_A, "collector_rules": {"reviewed_at": "2026-09-01T12:00:00+00:00"}}, "rules_not_reviewed"),
    "a review flag that is only true": ({**SETTINGS_A, "collector_rules": True}, "rules_not_reviewed"),
    "a name list that is text": (
        {**SETTINGS_A, "internal_routing": {"branch_services_names": CANARY_A}}, "rules_invalid"),
    "a name that is not text": (
        {**SETTINGS_A, "internal_routing": {"branch_services_names": [CANARY_A, 7]}}, "rules_invalid"),
    "destinations that are not a list": ({**SETTINGS_A, "transit": {"destinations": "North Annex"}}, "rules_invalid"),
    "an internal_routing block that is a list": ({**SETTINGS_A, "internal_routing": [CANARY_A]}, "rules_invalid"),
    "settings that are not JSON": ("{not json " + CANARY_A, "settings_unreadable"),
}


@pytest.mark.parametrize("settings, reason", NOT_READY_SETTINGS.values(), ids=NOT_READY_SETTINGS.keys())
def test_a_site_whose_settings_are_not_ready_is_refused_and_nothing_is_written(world, settings, reason):
    if settings is None:
        _run(world, "DELETE FROM organization_settings WHERE organization_id = 1")
    else:
        _set_settings(world, 1, settings)
    code = _code(world, 101)
    before = _written(world)

    refusal = _refused(world, code, contract_version=2)

    assert (refusal.code, refusal.reason) == ("enrollment_not_ready", reason)
    assert _written(world) == before
    # Nothing of the settings rides along: not in the refusal, and not behind it.
    assert CANARY_A not in repr(refusal) and CANARY_A not in str(refusal)
    assert refusal.__cause__ is None and (refusal.__context__ is None or CANARY_A not in repr(refusal.__context__))


def test_full_lists_do_not_make_an_unreviewed_site_ready(world):
    _set_settings(world, 1, _without(SETTINGS_A, "collector_rules"))  # names, destinations, a zone -- and no review

    assert _refused(world, _code(world, 101), contract_version=2).reason == "rules_not_reviewed"


def test_there_is_no_default_time_zone_not_even_the_products(world, monkeypatch):
    monkeypatch.setenv("SORTVIEW_LIVE_TIMEZONE", "America/Chicago")
    _set_settings(world, 1, _without(SETTINGS_A, "collector_timezone"))

    assert _refused(world, _code(world, 101), contract_version=2).reason == "timezone_not_configured"


def test_the_review_record_and_the_zone_are_the_organizations_own_and_a_branch_cannot_supply_them(world):
    _set_settings(world, 3, {})
    _run(world, "INSERT INTO branch_settings (branch_id, settings_json) VALUES (3, :j)",
         j=json.dumps({"collector_timezone": "America/Chicago", "collector_rules": REVIEWED}))

    assert _refused(world, _code(world, 301), contract_version=2).reason == "timezone_not_configured"


@pytest.mark.parametrize("created_at", ["2025-03-04 05:06:07", "not a time", None])
def test_an_organization_creation_time_that_is_not_an_instant_is_never_guessed_at(world, created_at):
    _run(world, "UPDATE organizations SET created_at = :c WHERE id = 1", c=created_at)
    code = _code(world, 101)
    before = _written(world)

    assert _refused(world, code, contract_version=2).reason == "organization_created_at_unusable"
    assert _written(world) == before


# --- an existing v2 identity ------------------------------------------------------------------------------------------

def test_a_branch_that_already_has_an_active_key_is_refused_and_its_key_is_left_alone(world):
    existing = _key(world, 10, 1)
    code = _code(world, 101)
    before = _written(world)

    refusal = _refused(world, code, contract_version=2)

    assert (refusal.code, refusal.reason) == ("enrollment_not_ready", "active_ingest_key_exists")
    assert _written(world) == before and [k["key_id"] for k in _table(world, "ingest_key_ids")] == [existing]


def test_a_retired_key_does_not_block(world):
    retired = _key(world, 10, 1, status="retired")

    result = _redeem(world, _code(world, 101), contract_version=2)

    keys = {k["key_id"]: k["status"] for k in _table(world, "ingest_key_ids")}
    assert keys == {retired: "retired", result["key_id"]: "active"}  # the retired key is not reused and not revived


def test_another_tenants_key_and_cutover_do_not_block(world):
    _key(world, 11, 2)
    _cutover(world, 11, 2, "2025-09-01T00:00:00+00:00")
    _key(world, 10, 9, key_id="00000000-0000-4000-8000-00000000abcd")  # the same customer, another branch

    assert set(_redeem(world, _code(world, 101), contract_version=2)) == V2_KEYS


def test_a_second_installation_at_a_branch_that_was_just_enrolled_is_refused(world):
    # The registry cannot say which installation a key belongs to, so the rule is the branch's: one identity.
    first = _redeem(world, _code(world, 101), contract_version=2)
    code = _code(world, 102)
    before = _written(world)

    assert _refused(world, code, contract_version=2).reason == "active_ingest_key_exists"
    assert _written(world) == before
    assert [k["key_id"] for k in _table(world, "ingest_key_ids")] == [first["key_id"]]
    assert len(_table(world, "v2_cutovers")) == 1


@pytest.mark.parametrize("cutover_at", ["2026-06-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00", None],
                         ids=["a cutover in effect", "a cutover in the future", "a recorded rollback"])
def test_any_existing_cutover_row_blocks_and_is_left_exactly_as_it_was(world, cutover_at):
    _cutover(world, 10, 1, cutover_at)
    rows_before = _table(world, "v2_cutovers")
    code = _code(world, 101)
    before = _written(world)

    refusal = _refused(world, code, contract_version=2)

    assert (refusal.code, refusal.reason) == ("enrollment_not_ready", "cutover_exists")
    assert _written(world) == before
    # Row for row, value for value: nothing was added, moved or rewritten.
    assert _table(world, "v2_cutovers") == rows_before and len(rows_before) == 1


def test_a_legacy_enrollment_at_a_cut_over_branch_is_untouched_by_all_of_this(world):
    # An already-migrated library (a key, a cutover, a legacy-coded installation) re-enrolls the legacy way.
    key = _key(world, 13, 4)
    _cutover(world, 13, 4, "2026-06-01T00:00:00+00:00")
    cutovers = _table(world, "v2_cutovers")

    result = _redeem(world, _code(world, 401))

    assert set(result) == LEGACY_KEYS
    assert _table(world, "v2_cutovers") == cutovers and [k["key_id"] for k in _table(world, "ingest_key_ids")] == [key]


# =====================================================================================================================
# all or nothing
# =====================================================================================================================

class _Boom(RuntimeError):
    pass


def _explode(*_args, **_kwargs):
    raise _Boom("forced failure")


@pytest.mark.parametrize("step", ["new_agent_token", "issue_ingest_key", "record_initial_v2_cutover"])
def test_a_failure_at_any_write_leaves_the_code_unused_and_nothing_created(world, monkeypatch, step):
    code = _code(world, 101)
    before = _written(world)
    monkeypatch.setattr(enrollment, step, _explode)

    with pytest.raises(_Boom):
        _redeem(world, code, contract_version=2)

    assert _written(world) == before  # no token, no key, no cutover, and the code is still unused
    monkeypatch.undo()
    assert set(_redeem(world, code, contract_version=2)) == V2_KEYS  # and it can still be redeemed


def test_everything_is_read_and_checked_before_the_first_write(world):
    statements: list[str] = []

    @event.listens_for(world, "before_cursor_execute")
    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(statement.split()))

    _redeem(world, _code(world, 101), contract_version=2)

    redemption = statements[next(i for i, s in enumerate(statements) if "FROM collector_enrollment_codes" in s):]
    writes = [i for i, s in enumerate(redemption) if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    reads_of = {name: next(i for i, s in enumerate(redemption) if name in s and s.startswith("SELECT"))
                for name in ("organization_settings", "ingest_key_ids", "v2_cutovers")}
    assert all(index < writes[0] for index in reads_of.values())
    # In order: claim the code, the token, the key, the cutover.
    assert [redemption[i].split()[0:3] for i in writes][:4] == [
        ["UPDATE", "collector_enrollment_codes", "SET"], ["INSERT", "INTO", "agent_tokens"],
        ["INSERT", "INTO", "ingest_key_ids"], ["INSERT", "INTO", "v2_cutovers"]]
    assert not any(s.startswith("DELETE") for s in redemption)
    assert not any(s.startswith("UPDATE") and "v2_cutovers" in s for s in redemption)


def test_a_cutover_that_appears_between_the_check_and_the_insert_writes_nothing(world, monkeypatch):
    # The branch lock prevents this on PostgreSQL; the cutover helper refuses it on any database.
    code = _code(world, 101)
    real_issue = enrollment.issue_ingest_key

    def issue_then_someone_else_cuts_over(conn, customer_id, branch_id):
        key_id = real_issue(conn, customer_id, branch_id)
        conn.execute(text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by) "
                          "VALUES (10, 1, '2026-06-01T00:00:00+00:00', 'operator')"))
        return key_id

    monkeypatch.setattr(enrollment, "issue_ingest_key", issue_then_someone_else_cuts_over)

    with pytest.raises(ValueError, match="already has a v2 cutover record"):
        _redeem(world, code, contract_version=2)

    assert _table(world, "v2_cutovers") == [] and _table(world, "ingest_key_ids") == [] and _table(world, "agent_tokens") == []
    assert _table(world, "collector_enrollment_codes")[0]["used_at"] is None


# =====================================================================================================================
# what is never logged
# =====================================================================================================================

def test_no_token_rule_or_setting_is_logged_by_a_contract_2_enrollment(world, caplog):
    with caplog.at_level(logging.DEBUG):
        code = _code(world, 101)
        result = _redeem(world, code, contract_version=2)

    logged = "\n".join(record.getMessage() for record in caplog.records) + caplog.text
    assert result["agent_token"] not in logged and code not in logged
    for secret in (CANARY_A, "OUTREACH DESK", "CATALOGING", "NORTH ANNEX", "north_annex", "America/New_York"):
        assert secret not in logged, secret
    # What IS logged: ids and counts.
    assert "contract=2" in logged and "rule_destinations=1" in logged and "rule_entries=4" in logged
    assert f"key_id={result['key_id']}" in logged  # a non-secret id


def test_the_site_record_does_not_show_its_rules():
    site = enrollment._V2Site(timezone="UTC", cutover_at=datetime(2025, 1, 1, tzinfo=UTC), rules={"x": [CANARY_A]})

    assert CANARY_A not in repr(site) and "rules" not in repr(site)


# =====================================================================================================================
# POST /collector/enroll
# =====================================================================================================================

def test_the_endpoint_gives_a_legacy_request_with_a_legacy_code_exactly_the_four_fields(world):
    response = _post(_code(world, 401), hostname="AMH-PC", collector_version="1.0.13")

    assert response.status_code == 200
    assert set(response.json()) == LEGACY_KEYS
    assert response.headers["cache-control"] == "no-store"
    assert _table(world, "ingest_key_ids") == [] and _table(world, "v2_cutovers") == []


def test_the_endpoint_gives_a_contract_2_request_exactly_the_nine_fields(world):
    response = _post(_code(world, 101), hostname="AMH-PC", collector_version="1.1.0", contract_version=2)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == V2_KEYS
    assert body["contract_version"] == 2 and body["contract_mode"] == "v2" and body["timezone"] == "America/New_York"
    assert body["classification_rules"]["branch_services_names"] == [CANARY_A, "OUTREACH DESK"]
    assert response.headers["cache-control"] == "no-store"  # it carries a credential and account names
    assert not [name for name in body if "secret" in name or "hmac" in name]


def test_an_old_installer_against_a_contract_2_code_is_told_to_upgrade_and_the_code_survives(world):
    code = _code(world, 101)
    before = _written(world)

    # The request an installer released before Contract v2 enrollment sends: no contract_version at all.
    response = _post(code, hostname="AMH-PC", collector_version="1.0.13")

    assert response.status_code == 409
    assert response.json() == {"code": "collector_upgrade_required",
                               "detail": "This enrollment code requires a newer SortView Collector."}
    assert response.headers["cache-control"] == "no-store"
    assert _written(world) == before  # it got no token it could have used for v1
    # The same code, from an installer that can: enrolled.
    assert _post(code, contract_version=2).status_code == 200


@pytest.mark.parametrize("installation_id, body, expected", [
    (401, {"contract_version": 2}, {"code": "contract_not_permitted",
                                    "detail": "This enrollment code does not permit the requested contract."}),
    (101, {"contract_version": 1}, {"code": "collector_upgrade_required",
                                    "detail": "This enrollment code requires a newer SortView Collector."}),
    (101, {"contract_version": 3}, {"code": "unsupported_contract_version",
                                    "detail": "The requested contract version is not supported."}),
    (401, {"contract_version": 0}, {"code": "unsupported_contract_version",
                                    "detail": "The requested contract version is not supported."}),
    (301, {"contract_version": 2}, {"code": "enrollment_not_ready",
                                    "detail": "This installation is not ready to enroll. Contact SortView support."}),
])
def test_each_contract_refusal_is_a_409_with_its_fixed_code_and_sentence(world, installation_id, body, expected):
    code = _code(world, installation_id)
    before = _written(world)

    response = _post(code, **body)

    assert response.status_code == 409 and response.json() == expected
    assert _written(world) == before


@pytest.mark.parametrize("value", ["2", "v2", True, False, 2.5, 2.0, [2], {"version": 2}])
def test_a_malformed_contract_version_is_a_422_and_nothing_is_issued(world, value):
    code = _code(world, 101)
    before = _written(world)

    response = _post(code, contract_version=value)

    assert response.status_code == 422
    assert code not in response.text
    assert _written(world) == before


def test_a_null_contract_version_is_the_same_as_none_sent(world):
    assert set(_post(_code(world, 401), contract_version=None).json()) == LEGACY_KEYS
    assert _post(_code(world, 101), contract_version=None).json()["code"] == "collector_upgrade_required"


def test_a_code_that_is_not_valid_is_still_the_generic_400_for_a_contract_2_request(world):
    used = _insert_code(world, 101, required_contract=2, used=True)
    expired = _insert_code(world, 101, required_contract=2, expired=True)
    revoked = _insert_code(world, 101, required_contract=2, revoked=True)
    unknown = enrollment.new_enrollment_code()
    suspended = _code(world, 201)
    _run(world, "UPDATE organizations SET status = 'suspended' WHERE id = 2")

    bodies = set()
    for code in (used, expired, revoked, unknown, suspended, "not-a-code"):
        main.limiter.reset()  # eighteen requests from one client: the endpoint's own rate limit is not what is tested
        for body in ({}, {"contract_version": 2}, {"contract_version": 3}):
            response = _post(code, **body)
            assert response.status_code == 400 and response.json() == PUBLIC
            bodies.add(response.content)
    assert len(bodies) == 1  # byte-identical: nothing about the code, its contract or its tenant is confirmed


def test_not_ready_says_the_same_thing_whatever_the_cause_and_never_what_it_was(world):
    causes = {
        "no time zone": lambda: _set_settings(world, 1, _without(SETTINGS_A, "collector_timezone")),
        "a bad time zone": lambda: _set_settings(world, 1, {**SETTINGS_A, "collector_timezone": "Nowhere/CANARY-ZONE"}),
        "rules not reviewed": lambda: _set_settings(world, 1, _without(SETTINGS_A, "collector_rules")),
        "rules invalid": lambda: _set_settings(world, 1, {**SETTINGS_A, "internal_routing": {"branch_services_names": CANARY_A}}),
        "an active key": lambda: (_set_settings(world, 1, SETTINGS_A), _key(world, 10, 1)),
        "a cutover": lambda: (_run(world, "DELETE FROM ingest_key_ids"), _cutover(world, 10, 1, None)),
    }
    bodies = set()
    for arrange in causes.values():
        arrange()
        response = _post(_code(world, 101), contract_version=2)
        assert response.status_code == 409
        bodies.add(response.content)
        for leaked in (CANARY_A, "CANARY-ZONE", "timezone", "time zone", "rules", "review", "key", "cutover", "America"):
            assert leaked not in response.text, leaked

    assert len(bodies) == 1
    assert json.loads(bodies.pop())["code"] == "enrollment_not_ready"


def test_the_endpoint_logs_a_refusal_as_fixed_codes_only(world, caplog):
    _set_settings(world, 1, {**SETTINGS_A, "internal_routing": {"branch_services_names": [CANARY_A, 7]}})
    code = _code(world, 101)

    with caplog.at_level(logging.DEBUG):
        _post(code, contract_version=2)

    logged = caplog.text
    assert "Collector enrollment refused | code=enrollment_not_ready reason=rules_invalid" in logged
    assert CANARY_A not in logged and code not in logged and "America/New_York" not in logged


def test_no_secret_or_rule_reaches_the_logs_through_the_endpoint(world, caplog):
    code = _code(world, 101)

    with caplog.at_level(logging.DEBUG):
        body = _post(code, contract_version=2).json()

    for secret in (body["agent_token"], code, CANARY_A, "OUTREACH DESK"):
        assert secret not in caplog.text


def test_an_unexpected_failure_is_a_generic_500_with_no_detail_and_nothing_issued(world, monkeypatch):
    code = _code(world, 101)
    before = _written(world)
    monkeypatch.setattr(enrollment, "issue_ingest_key", _explode)

    response = _post(code, contract_version=2)

    assert response.status_code == 500 and response.json() == {"detail": "Internal server error"}
    assert CANARY_A not in response.text and _written(world) == before


# --- what the enrollment means for the two upload routes --------------------------------------------------------------

def test_a_newly_enrolled_contract_2_branch_cannot_use_the_v1_upload_and_can_use_v2(world):
    body = _post(_code(world, 101), contract_version=2).json()
    auth = {"Authorization": f"Bearer {body['agent_token']}"}

    # v1 /upload: closed from the first moment -- the raw rows never land.
    legacy = client.post("/upload", json=v1_payload(customer=10, branch=1), headers=auth)
    assert legacy.status_code == 403 and legacy.json() == {"detail": main.V1_UPLOAD_CLOSED_DETAIL}
    assert _table(world, "checkins") == [] and _table(world, "rejects") == [] and _table(world, "acs_events") == []

    # v2 /upload with the key the enrollment issued: accepted, and stored under the token's own tenant.
    accepted = client.post("/v2/upload", json={"contract_version": 2, "key_id": body["key_id"], "checkins": [checkin()]},
                           headers=auth)
    assert accepted.status_code == 200 and accepted.json()["contract_version"] == 2
    [stored] = _table(world, "checkin_events")
    assert (stored["customer_id"], stored["branch_id"], stored["key_id"]) == (10, 1, body["key_id"])

    # The heartbeat route for that key works too.
    status = client.post("/v2/status", json={"contract_version": 2, "key_id": body["key_id"], "status": "healthy"}, headers=auth)
    assert status.status_code == 200


def test_the_issued_key_belongs_to_its_own_tenant_only(world):
    a = _post(_code(world, 101), contract_version=2).json()
    b = _post(_code(world, 201), contract_version=2).json()

    crossed = client.post("/v2/upload", json={"contract_version": 2, "key_id": b["key_id"], "checkins": [checkin()]},
                          headers={"Authorization": f"Bearer {a['agent_token']}"})

    assert crossed.status_code == 403 and _table(world, "checkin_events") == []


def test_a_legacy_branch_keeps_its_v1_upload_whoever_else_enrolls_on_contract_2(world):
    legacy = _post(_code(world, 401)).json()
    _post(_code(world, 101), contract_version=2)

    response = client.post("/upload", json=v1_payload(customer=13, branch=4),
                           headers={"Authorization": f"Bearer {legacy['agent_token']}"})

    assert response.status_code == 200
    assert len(_table(world, "checkins")) == 1
    assert {(c["customer_id"], c["branch_id"]) for c in _table(world, "v2_cutovers")} == {(10, 1)}


def test_the_v2_kill_switch_still_closes_the_v2_routes_for_an_enrolled_branch(world, monkeypatch):
    body = _post(_code(world, 101), contract_version=2).json()
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", False)

    response = client.post("/v2/upload", json={"contract_version": 2, "key_id": body["key_id"], "checkins": [checkin()]},
                           headers={"Authorization": f"Bearer {body['agent_token']}"})

    assert response.status_code == 404  # Stage A1 leaves the flag exactly as it was: global, and off means off


# =====================================================================================================================
# the deliberate default
# =====================================================================================================================

def test_contract_2_is_chosen_explicitly_and_the_legacy_contract_is_still_every_default():
    """A1 landed before an installer that could enroll on Contract v2, and pinned here that nothing asked for it. A3a
    ships that installer (Collector 1.0.14) and an explicit contract selector in Super Admin -- so this is where the
    deliberate change shows, and where what did NOT change is held: the service's default, and the selector's default,
    are still the legacy contract. Making 2 the default is a later stage's change, and will show here too."""
    from pathlib import Path

    from src.services import tenant_service

    root = Path(main.__file__).resolve().parent
    assert tenant_service.create_collector_installation.__defaults__[-1] == tenant_service.COLLECTOR_CONTRACT_LEGACY == 1
    for page in ("super_admin/pages/Provision_Library.py", "super_admin/pages/Manage_Libraries.py"):
        source = (root / page).read_text(encoding="utf-8")
        assert "create_collector_installation(" in source
        # The page passes the contract explicitly -- the selector's value, never a literal 2 ...
        assert source.count("required_contract=") == 1 and "required_contract=2" not in source, page
        assert "required_contract=COLLECTOR_CONTRACT_V2" not in source, page
        # ... and the selector's default is the legacy contract.
        assert "DEFAULT_COLLECTOR_CONTRACT = COLLECTOR_CONTRACT_LEGACY" in source, page
    # The installer now asks for Contract v2 first, and its one retry for a legacy code sends NO contract_version --
    # the legacy request this server has always accepted.
    setup = (root / "collector" / "deploy" / "setup-collector.ps1").read_text(encoding="utf-8")
    assert 'if ($ContractVersion -eq 2) { $request["contract_version"] = 2 }' in setup
    assert setup.count('$request["contract_version"]') == 1
