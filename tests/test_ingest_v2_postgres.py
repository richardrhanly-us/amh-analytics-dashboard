"""Privacy Contract v2 on a REAL PostgreSQL: the migration, the constraints, the dedup index, concurrency and the endpoints.

SQLite cannot check a `~` regex CHECK, TIMESTAMPTZ, a real unique index, `ON CONFLICT ... RETURNING` under concurrent
transactions, or the v1 `AFTER INSERT` triggers. These tests run the project's real Alembic migrations on a real server and
prove: the migration is purely additive over existing v1 data; the v2 tables lack every prohibited column; the database
itself rejects a raw label, a bad key or a naive-shaped value the API would also reject; identical resends are idempotent and
conflicting duplicates are detected even under concurrency; the endpoints work end to end; and no v2 request writes a v1 table
or fires a v1 trigger.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_admin_lock_migration_postgres.py. They run only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server the tests may create and drop databases
on, e.g.

    SORTVIEW_TEST_POSTGRES_URL=postgresql://postgres:@127.0.0.1:5432/postgres

Each fixture creates a brand-new throwaway database, migrates it in a subprocess, and drops it afterwards. The host must be
local unless SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also set, so a production URL left in an environment variable cannot be
used. The API's own DATABASE_URL is never read: the endpoint tests point `main.engine` at the throwaway database.

Every value is SYNTHETIC. Timestamps come from the real clock.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

import main
from src.services import ingest_v2_service as service
from src.services.ingest_v2_models import (
    AcsHoldEvent,
    AcsNonHoldEvent,
    CheckinEvent,
    RejectEvent,
    StatusV2Request,
)

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
PREVIOUS_HEAD = "b4e91d7a3c58"      # the head before Contract v2 existed
STEP3_HEAD = "d3f1a8c95b27"         # Step 3 as merged: acs_hold_events
RLS_HEAD = "0acba192bf69"           # RLS phase 1: RLS enabled on the seven operational-domain tables
HEAD = "f2a91c7d4e83"               # government-readiness audit: add v2_cutovers (purely additive, unrelated to v2 ingest)

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL migration tests)"
)

CUSTOMER, BRANCH = 10, 1
OTHER_CUSTOMER, OTHER_BRANCH = 20, 2
TOKEN = "CANARY-V2-PG-BEARER-TOKEN-3101"
OTHER_TOKEN = "CANARY-V2-PG-OTHER-TOKEN-3102"
_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
_BUILTIN_SHA256_EXPR = "encode(sha256(convert_to(:token, 'UTF8')), 'hex')"  # pgcrypto is not on every throwaway server
V2_TABLES = ("checkin_events", "reject_events", "acs_item_events", "ingest_key_ids")
EVENT_TABLES = ("checkin_events", "reject_events", "acs_item_events")
V1_TABLES = ("checkins", "rejects", "acs_events", "checkins_clean", "rejects_clean", "pipeline_status", "agent_tokens",
             "organizations", "branches", "customers", "collector_installations", "collector_enrollment_codes")
PROHIBITED = {"barcode", "barcode_key", "title", "patron_id", "raw_message", "message", "error_message", "source_event_id",
              "source_file", "call_number", "shelf_code", "collection_code", "flag_1", "flag_2", "flag_3", "is_problem"}

client = TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


# --- helpers ---------------------------------------------------------------------------------------------------------

def hmac_like(n: int) -> str:
    return hashlib.sha256(f"synthetic-{n}".encode()).hexdigest()


def when(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def key_id(n: int) -> str:
    return f"{n:08x}-4d5a-4b6c-8d7e-9f0a1b2c3d4e"


KEY, SECOND_KEY, OTHER_KEY, RETIRED_KEY = key_id(0x3F2B8C1E), key_id(0x5A6B7C8D), key_id(0x7C8D9E0F), key_id(0x6B7C8D9E)


def checkin(n=1, **overrides):
    event = {"event_key": hmac_like(n), "event_time": when(minutes=5), "item_key": hmac_like(n + 1000),
             "destination": "westside", "bin": "3"}
    event.update(overrides)
    return event


def reject(n=1, **overrides):
    event = {"event_key": hmac_like(n), "event_time": when(minutes=5), "error_class": "item_not_found", "item_key": hmac_like(n + 1000)}
    event.update(overrides)
    return event


def acs_hold(n=1, **overrides):
    event = {"state": "hold", "event_key": hmac_like(n), "event_time": when(minutes=5), "item_key": hmac_like(n + 1000),
             "destination": "library_express", "is_ill": False, "is_branch_services": False, "is_collection_services": True,
             "ruleset_id": "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d"}
    event.update(overrides)
    return event


def acs_non_hold(n=1, state="non_hold_101", **overrides):
    event = {"state": state, "event_key": hmac_like(n), "event_time": when(minutes=5), "item_key": hmac_like(n + 1000)}
    event.update(overrides)
    return event


def upload(key=KEY, **overrides):
    body = {"contract_version": 2, "key_id": key, "checkins": [checkin()]}
    body.update(overrides)
    return body


# --- throwaway databases ----------------------------------------------------------------------------------------------

def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


def _alembic(url, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True, check=False)  # nosec B603


class Throwaway:
    """A brand-new database on the test server, dropped when the context exits."""

    def __init__(self, revision: str | None):
        self.revision = revision
        admin = make_url(ADMIN_URL)
        _guard(admin)
        self.admin = admin
        self.name = f"sortview_v2_test_{secrets.token_hex(4)}"
        self.admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")

    def __enter__(self):
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{self.name}"'))  # nosec B608 - generated name, no user input
        self.url = self.admin.set(database=self.name)
        if self.revision:
            migrated = _alembic(self.url, "upgrade", self.revision)
            assert migrated.returncode == 0, migrated.stderr[-2000:]
        return self

    def __exit__(self, *_exc):
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))  # nosec B608
        self.admin_engine.dispose()


@pytest.fixture(scope="module")
def pg_url():
    with Throwaway("head") as db:
        yield db.url


def _seed_tenants(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE checkin_events, reject_events, acs_item_events, ingest_key_ids, checkins, rejects, "
                          "acs_events, checkins_clean, rejects_clean, agent_tokens, branches, organizations, customers "
                          "RESTART IDENTITY CASCADE"))
        for org_id, customer, branch, slug in ((1, CUSTOMER, BRANCH, "lib"), (2, OTHER_CUSTOMER, OTHER_BRANCH, "other")):
            conn.execute(text("INSERT INTO customers (id, name) VALUES (:c, :n)"), {"c": customer, "n": slug})
            conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) "
                              "VALUES (:o, :s, :s, 'active', :c)"), {"o": org_id, "s": slug, "c": customer})
            conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) "
                              "VALUES (:b, :o, 'main', 'Main', 'active', :b)"), {"b": branch, "o": org_id})
        for token, customer, branch in ((TOKEN, CUSTOMER, BRANCH), (OTHER_TOKEN, OTHER_CUSTOMER, OTHER_BRANCH)):
            conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active) "
                              "VALUES (:h, :c, :b, 't', TRUE)"),
                         {"h": hashlib.sha256(token.encode()).hexdigest(), "c": customer, "b": branch})
        for key, customer, branch, state in ((KEY, CUSTOMER, BRANCH, "active"), (SECOND_KEY, CUSTOMER, BRANCH, "active"),
                                             (RETIRED_KEY, CUSTOMER, BRANCH, "retired"), (OTHER_KEY, OTHER_CUSTOMER, OTHER_BRANCH, "active")):
            conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, status, retired_at) VALUES "
                              "(:k, :c, :b, :s, CASE WHEN :s = 'retired' THEN now() END)"),
                         {"k": key, "c": customer, "b": branch, "s": state})


@pytest.fixture
def engine(pg_url):
    engine = create_engine(pg_url, hide_parameters=True)
    _seed_tenants(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def api(engine, monkeypatch):
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", True)
    assert _HASH_EXPR in main._AGENT_TOKEN_LOOKUP_SQL
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR))
    return client


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def rows(engine, sql, **params):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params)]


def post_upload(api, body, token=TOKEN):
    return api.post("/v2/upload", json=body, headers={"Authorization": f"Bearer {token}"})


# =====================================================================================================================
# 1. The schema the migration really builds
# =====================================================================================================================

def _columns(engine, table):
    return {r[0]: (r[1], r[2]) for r in rows(engine, "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                                     "WHERE table_schema = 'public' AND table_name = :t", t=table)}


def test_the_four_tables_exist_and_the_event_tables_have_exactly_the_approved_columns(engine):
    assert set(_columns(engine, "checkin_events")) == {"id", "customer_id", "branch_id", "key_id", "event_key", "event_time",
                                                       "item_key", "destination", "bin", "received_at"}
    assert set(_columns(engine, "reject_events")) == {"id", "customer_id", "branch_id", "key_id", "event_key", "event_time",
                                                      "error_class", "item_key", "received_at"}
    assert set(_columns(engine, "acs_item_events")) == {"id", "customer_id", "branch_id", "key_id", "event_key", "event_time",
                                                        "state", "item_key", "destination", "is_ill", "is_branch_services",
                                                        "is_collection_services", "ruleset_id", "received_at"}
    assert scalar(engine, "SELECT count(*) FROM ingest_key_ids") == 4


@pytest.mark.parametrize("table", V2_TABLES)
def test_no_v2_table_has_a_prohibited_legacy_column(engine, table):
    assert set(_columns(engine, table)).isdisjoint(PROHIBITED)


@pytest.mark.parametrize("table", V2_TABLES)
def test_every_v2_timestamp_is_timestamptz_and_no_column_is_a_naive_timestamp(engine, table):
    types = {name: kind for name, (kind, _n) in _columns(engine, table).items()}

    assert "timestamp without time zone" not in types.values()
    for name in ("event_time", "received_at", "created_at", "retired_at", "last_heartbeat_at", "last_success_at"):
        if name in types:
            assert types[name] == "timestamp with time zone", name


def test_the_event_tables_are_unique_on_tenant_key_id_and_event_key(engine):
    for table in EVENT_TABLES:
        definition = scalar(engine, "SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexname = :i",
                            t=table, i=f"{table}_event_identity_uidx")
        assert "UNIQUE" in definition and "(customer_id, branch_id, key_id, event_key)" in definition


def test_no_trigger_exists_on_a_v2_table(engine):
    assert rows(engine, "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                        "WHERE NOT t.tgisinternal AND c.relname = ANY(:names)", names=list(V2_TABLES)) == []


def test_the_two_v1_sync_triggers_are_still_the_only_triggers_on_the_v1_event_tables(engine):
    assert sorted(r[0] for r in rows(engine, "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")) == [
        "trg_sync_checkins_to_clean", "trg_sync_rejects_to_clean"]


SECRET_WORDS = ("secret", "hmac", "salt", "seed", "password", "passphrase", "private", "material", "credential", "token", "signing")


@pytest.mark.parametrize("table", V2_TABLES)
def test_no_column_of_a_real_v2_table_can_hold_an_hmac_secret_or_key_material(engine, table):
    for column in _columns(engine, table):
        assert not any(word in column for word in SECRET_WORDS), (table, column)


def test_the_database_accepts_exactly_the_approved_status_and_error_class_values(engine):
    for value in ("healthy", "degraded", "error"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE ingest_key_ids SET health_status = :v WHERE key_id = :k"), {"v": value, "k": KEY})
    for value in ("retryable_infra", "auth_failure", "permanent_rejection", "source_unavailable", "configuration_error", "other"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE ingest_key_ids SET last_error_class = :v WHERE key_id = :k"), {"v": value, "k": KEY})

    assert rows(engine, "SELECT health_status, last_error_class FROM ingest_key_ids WHERE key_id = :k", k=KEY) == [("error", "other")]


# =====================================================================================================================
# 2. The database itself enforces the formats (a writer that skips the API still cannot store a raw label)
# =====================================================================================================================

def _insert_checkin(conn, **overrides):
    values = {"c": CUSTOMER, "b": BRANCH, "k": KEY, "e": hmac_like(1), "t": datetime.now(UTC), "i": hmac_like(2),
              "d": "westside", "bin": "3"}
    values.update(overrides)
    conn.execute(text("INSERT INTO checkin_events (customer_id, branch_id, key_id, event_key, event_time, item_key, destination, bin) "
                      "VALUES (:c, :b, :k, :e, :t, :i, :d, :bin)"), values)


def test_a_valid_row_is_accepted_by_the_database(engine):
    with engine.begin() as conn:
        _insert_checkin(conn)

    assert scalar(engine, "SELECT count(*) FROM checkin_events") == 1


@pytest.mark.parametrize("override", [
    {"k": "not-a-uuid"}, {"k": KEY.upper()}, {"k": key_id(1).replace("-4b6c-", "-1b6c-")},
    {"e": hmac_like(1).upper()}, {"e": hmac_like(1)[:-1]}, {"e": "CANARY-PATRON-CARD-2300000000003"}, {"e": ""},
    {"i": "B-1"}, {"i": hmac_like(1).upper()},
    {"d": "Westside"}, {"d": "Library Express"}, {"d": "DA(AH) TS(AH)-CATALOGING"}, {"d": ""}, {"d": "x" * 33}, {"d": "1main"},
    {"bin": ""}, {"bin": "Bin 1"}, {"bin": "x" * 17},
])
def test_the_database_rejects_a_raw_label_a_bad_key_or_a_wrong_format(engine, override):
    with pytest.raises(IntegrityError) as caught, engine.begin() as conn:
        _insert_checkin(conn, **override)

    assert "violates check constraint" in str(caught.value.orig)
    assert scalar(engine, "SELECT count(*) FROM checkin_events") == 0


@pytest.mark.parametrize("column", ["destination", "bin", "event_time", "event_key", "key_id"])
def test_the_database_requires_destination_bin_time_and_identity(engine, column):
    with pytest.raises(IntegrityError) as caught, engine.begin() as conn:
        _null_column(conn, column)

    assert "not-null constraint" in str(caught.value.orig) or "null value" in str(caught.value.orig)


def _null_column(conn, column):
    values = {"customer_id": CUSTOMER, "branch_id": BRANCH, "key_id": KEY, "event_key": hmac_like(1),
              "event_time": datetime.now(UTC), "destination": "westside", "bin": "3"}
    values[column] = None
    names = ", ".join(values)
    binds = ", ".join(f":{n}" for n in values)
    conn.execute(text(f"INSERT INTO checkin_events ({names}) VALUES ({binds})"), values)  # nosec B608 - test SQL, fixed names


@pytest.mark.parametrize("error_class", ["Item Not Found", "", "free text", "x" * 33, "1abc"])
def test_the_database_rejects_a_reject_error_class_that_is_not_a_slug(engine, error_class):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("INSERT INTO reject_events (customer_id, branch_id, key_id, event_key, event_time, error_class) "
                          "VALUES (:c, :b, :k, :e, now(), :ec)"), {"c": CUSTOMER, "b": BRANCH, "k": KEY, "e": hmac_like(1), "ec": error_class})


@pytest.mark.parametrize("override", [{"ruleset": "ruleset-2026"}, {"ruleset": hmac_like(3)}, {"item": None}, {"dest": "Main"}])
def test_the_database_rejects_a_bad_acs_hold(engine, override):
    values = {"c": CUSTOMER, "b": BRANCH, "k": KEY, "e": hmac_like(1), "item": hmac_like(2), "dest": "main", "ruleset": None}
    values.update(override)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("INSERT INTO acs_item_events (customer_id, branch_id, key_id, event_key, event_time, state, item_key, "
                          "destination, is_ill, is_branch_services, is_collection_services, ruleset_id) "
                          "VALUES (:c, :b, :k, :e, now(), 'hold', :item, :dest, false, false, false, :ruleset)"), values)


def test_the_registry_lifecycle_and_heartbeat_columns_are_constrained(engine):
    bad = [
        "UPDATE ingest_key_ids SET status = 'retired', retired_at = NULL WHERE key_id = :k",       # retired without a time
        "UPDATE ingest_key_ids SET retired_at = now() WHERE key_id = :k",                           # active with a retirement time
        "UPDATE ingest_key_ids SET status = 'deleted' WHERE key_id = :k",
        "UPDATE ingest_key_ids SET algorithm = 'sha256' WHERE key_id = :k",
        "UPDATE ingest_key_ids SET health_status = 'everything is fine' WHERE key_id = :k",
        "UPDATE ingest_key_ids SET health_status = 'auth_failure' WHERE key_id = :k",  # a kind of failure, not an overall state
        "UPDATE ingest_key_ids SET health_status = 'errors' WHERE key_id = :k",
        "UPDATE ingest_key_ids SET last_error_class = 'error' WHERE key_id = :k",
        "UPDATE ingest_key_ids SET last_error_class = 'Traceback CANARY' WHERE key_id = :k",
        "UPDATE ingest_key_ids SET pending_outbox_count = -1 WHERE key_id = :k",
        "UPDATE ingest_key_ids SET quarantined_count = 10000001 WHERE key_id = :k",
        "UPDATE ingest_key_ids SET key_id = 'CANARY-HMAC-SECRET' WHERE key_id = :k",
    ]
    for statement in bad:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(statement), {"k": KEY})


def test_a_key_id_is_globally_unique_in_the_registry(engine):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id) VALUES (:k, :c, :b)"),
                     {"k": KEY, "c": OTHER_CUSTOMER, "b": OTHER_BRANCH})


def test_the_database_enforces_the_dedup_identity(engine):
    with engine.begin() as conn:
        _insert_checkin(conn)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_checkin(conn, d="main")  # same tenant + key + event_key: a duplicate identity, whatever the content
    with engine.begin() as conn:
        _insert_checkin(conn, k=SECOND_KEY)                                    # another key_id: another identity
        _insert_checkin(conn, c=OTHER_CUSTOMER, b=OTHER_BRANCH, k=OTHER_KEY)   # another tenant: another identity

    assert scalar(engine, "SELECT count(*) FROM checkin_events") == 3


def test_the_event_time_round_trips_as_an_instant_whatever_the_offset(engine):
    instant = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)
    local = instant.astimezone(timezone_of(-5)).isoformat()

    response_engine = engine  # the endpoint is tested below; here only the column type's behaviour
    with response_engine.begin() as conn:
        conn.execute(text("INSERT INTO checkin_events (customer_id, branch_id, key_id, event_key, event_time, destination, bin) "
                          "VALUES (:c, :b, :k, :e, :t, 'a', '1')"), {"c": CUSTOMER, "b": BRANCH, "k": KEY, "e": hmac_like(1), "t": local})

    assert scalar(engine, "SELECT event_time FROM checkin_events") == instant


def timezone_of(hours: int):
    from datetime import timezone
    return timezone(timedelta(hours=hours))


# =====================================================================================================================
# 3. Storage on PostgreSQL: idempotent resend, conflicts, volume, concurrency
# =====================================================================================================================

def _store(engine, checkins=(), rejects=(), acs_items=(), key=KEY, customer=CUSTOMER, branch=BRANCH):
    with engine.begin() as conn:
        return service.store_events(
            conn, customer_id=customer, branch_id=branch, key_id=key,
            checkins=[CheckinEvent.model_validate(e) for e in checkins], rejects=[RejectEvent.model_validate(e) for e in rejects],
            acs_items=[AcsHoldEvent.model_validate(e) for e in acs_items])


def test_storage_is_idempotent_and_detects_a_content_change(engine):
    first = _store(engine, [checkin(1), checkin(2)], [reject(3)], [acs_hold(4)])
    again = _store(engine, [checkin(1), checkin(2)], [reject(3)], [acs_hold(4)])

    assert first["checkins_inserted"] == 2 and again["checkins_inserted"] == 0 and again["checkins_duplicates"] == 2
    assert (scalar(engine, "SELECT count(*) FROM checkin_events"), scalar(engine, "SELECT count(*) FROM reject_events"),
            scalar(engine, "SELECT count(*) FROM acs_item_events")) == (2, 1, 1)
    for kind, build, change in (("checkins", checkin, {"bin": "9"}), ("rejects", reject, {"error_class": "other"}),
                                ("acs_items", acs_hold, {"is_ill": True})):
        n = {"checkins": 1, "rejects": 3, "acs_items": 4}[kind]
        with pytest.raises(service.EventConflict) as caught:
            _store(engine, **{kind: [{**build(n), **change}]})
        assert caught.value.conflicts == {kind: [0]}


def test_a_conflict_rolls_back_everything_the_request_wrote(engine):
    _store(engine, [checkin(1)])

    with pytest.raises(service.EventConflict):
        _store(engine, [checkin(2), checkin(1, bin="9"), checkin(3)], [reject(4)])

    assert scalar(engine, "SELECT count(*) FROM checkin_events") == 1 and scalar(engine, "SELECT count(*) FROM reject_events") == 0


def test_a_full_thousand_events_are_stored_in_chunks(engine):
    checkins = [checkin(i) for i in range(500)]
    rejects = [reject(5000 + i) for i in range(250)]
    acs_items = [acs_hold(9000 + i) for i in range(250)]

    result = _store(engine, checkins, rejects, acs_items)

    assert (
        result["checkins_inserted"],
        result["rejects_inserted"],
        result["acs_items_inserted"],
    ) == (500, 250, 250)

    assert _store(engine, checkins)["checkins_duplicates"] == 500

def test_the_stored_rows_carry_the_token_tenant_the_key_and_a_receive_time(engine):
    _store(engine, [checkin(1)], [reject(2)], [acs_hold(3)])

    for table in EVENT_TABLES:
        (row,) = rows(engine, f"SELECT customer_id, branch_id, key_id, received_at IS NOT NULL FROM {table}")  # nosec B608
        assert row == (CUSTOMER, BRANCH, KEY, True), table


def test_concurrent_identical_events_store_one_row_and_all_succeed(engine):
    outcomes: list[object] = []
    gate = threading.Barrier(8)
    identical = checkin(1)

    def worker():
        gate.wait()
        try:
            outcomes.append(_store(engine, [identical])["checkins_inserted"])
        except Exception as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sorted(o for o in outcomes if isinstance(o, int)) == [0] * 7 + [1]
    assert not [o for o in outcomes if not isinstance(o, int)]
    assert scalar(engine, "SELECT count(*) FROM checkin_events") == 1


def test_concurrent_events_with_the_same_identity_and_different_content_never_lose_the_difference(engine):
    outcomes: list[object] = []
    gate = threading.Barrier(8)

    def worker(i: int):
        gate.wait()
        try:
            _store(engine, [checkin(1, bin=str(i % 2))])
            outcomes.append(("ok", str(i % 2)))
        except service.EventConflict:
            outcomes.append(("conflict", str(i % 2)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    (stored_bin,) = [r[0] for r in rows(engine, "SELECT bin FROM checkin_events")]
    assert scalar(engine, "SELECT count(*) FROM checkin_events") == 1
    for verdict, sent_bin in outcomes:  # a sender is told "ok" only if the stored row is exactly what it sent
        assert (verdict == "ok") == (sent_bin == stored_bin), (verdict, sent_bin, stored_bin)
    assert len(outcomes) == 8


# =====================================================================================================================
# 4. The endpoints end to end on PostgreSQL
# =====================================================================================================================

def test_an_upload_and_a_resend_through_the_endpoint(api, engine):
    body = upload(checkins=[checkin(1), checkin(2)], rejects=[reject(3)], acs_items=[acs_hold(4)])

    first = post_upload(api, body)
    again = post_upload(api, body)

    assert first.status_code == again.status_code == 200
    assert first.json()["checkins_inserted"] == 2 and again.json()["checkins_duplicates"] == 2
    assert scalar(engine, "SELECT count(*) FROM checkin_events") == 2
    assert rows(engine, "SELECT DISTINCT customer_id, branch_id, key_id FROM acs_item_events") == [(CUSTOMER, BRANCH, KEY)]


def test_the_endpoint_answers_a_conflict_with_409_and_stores_nothing_more(api, engine):
    post_upload(api, upload(checkins=[checkin(1)]))

    response = post_upload(api, upload(checkins=[checkin(1, destination="main"), checkin(2)]))

    assert response.status_code == 409 and response.json()["conflicts"] == {"checkins": [0]}
    assert rows(engine, "SELECT destination FROM checkin_events") == [("westside",)]


def test_unknown_retired_and_wrong_tenant_keys_are_one_403_on_postgres(api, engine):
    responses = [post_upload(api, upload(key=k)) for k in (key_id(0xDEAD), RETIRED_KEY, OTHER_KEY)]

    assert {r.status_code for r in responses} == {403} and len({r.text for r in responses}) == 1
    assert scalar(engine, "SELECT count(*) FROM checkin_events") == 0


def test_a_naive_timestamp_never_reaches_the_database(api, engine):
    assert post_upload(api, upload(checkins=[checkin(event_time="2026-09-21T10:00:00")])).status_code == 422
    assert scalar(engine, "SELECT count(*) FROM checkin_events") == 0


def test_the_heartbeat_is_stored_on_the_active_key_and_touches_nothing_else(api, engine):
    before = {t: scalar(engine, f"SELECT count(*) FROM {t}") for t in ("pipeline_status", "collector_installations", "checkins")}  # nosec B608

    response = api.post("/v2/status", headers={"Authorization": f"Bearer {TOKEN}"}, json={
        "contract_version": 2, "key_id": KEY, "status": "degraded", "last_error_class": "retryable_infra",
        "pending_outbox_count": 12, "quarantined_count": 1, "last_success_at": when(minutes=20)})

    assert response.status_code == 200
    assert rows(engine, "SELECT health_status, last_error_class, pending_outbox_count, quarantined_count, "
                        "last_success_at IS NOT NULL, last_heartbeat_at IS NOT NULL FROM ingest_key_ids WHERE key_id = :k", k=KEY) == [
        ("degraded", "retryable_infra", 12, 1, True, True)]
    assert {t: scalar(engine, f"SELECT count(*) FROM {t}") for t in before} == before  # nosec B608
    assert rows(engine, "SELECT health_status FROM ingest_key_ids WHERE key_id = :k", k=SECOND_KEY) == [(None,)]


def test_a_heartbeat_for_a_retired_or_foreign_key_is_refused(api, engine):
    for key in (RETIRED_KEY, OTHER_KEY):
        response = api.post("/v2/status", headers={"Authorization": f"Bearer {TOKEN}"},
                            json={"contract_version": 2, "key_id": key, "status": "healthy"})
        assert response.status_code == 403
    assert scalar(engine, "SELECT count(*) FROM ingest_key_ids WHERE health_status IS NOT NULL") == 0


# =====================================================================================================================
# 5. No v2 request touches a v1 table or fires a v1 trigger; v1 is unaffected
# =====================================================================================================================

def test_v2_requests_write_nothing_to_a_v1_table_and_fire_no_v1_trigger(api, engine):
    body = upload(checkins=[checkin(1), checkin(2)], rejects=[reject(3)], acs_items=[acs_hold(4)])
    post_upload(api, body)
    post_upload(api, body)
    post_upload(api, upload(checkins=[checkin(1, bin="9")]))
    api.post("/v2/status", headers={"Authorization": f"Bearer {TOKEN}"}, json={"contract_version": 2, "key_id": KEY, "status": "healthy"})

    for table in ("checkins", "rejects", "acs_events", "checkins_clean", "rejects_clean", "pipeline_status"):
        assert scalar(engine, f"SELECT count(*) FROM {table}") == 0, table  # nosec B608
    assert scalar(engine, "SELECT count(*) FROM checkin_events") == 2


def test_a_v1_upload_still_works_on_postgres_writes_no_v2_table_and_still_fires_its_triggers(api, engine):
    v1 = {"checkins": [{"customer_id": CUSTOMER, "branch_id": BRANCH, "event_time": "2026-01-01 10:00:00", "barcode": "B-1",
                        "destination": "Main", "bin": "1"}],
          "rejects": [{"customer_id": CUSTOMER, "branch_id": BRANCH, "event_time": "2026-01-01 10:01:00", "barcode": "B-2",
                       "message": "Item not found"}]}

    response = api.post("/upload", json=v1, headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200 and response.json()["checkins_inserted"] == 1 and response.json()["rejects_inserted"] == 1
    assert scalar(engine, "SELECT count(*) FROM checkins_clean") == 1 and scalar(engine, "SELECT count(*) FROM rejects_clean") == 1
    assert sum(scalar(engine, f"SELECT count(*) FROM {t}") for t in EVENT_TABLES) == 0  # nosec B608


# =====================================================================================================================
# 6. The migration over existing v1 data, and its downgrade
# =====================================================================================================================

def _v1_snapshot(engine) -> dict:
    """Columns, indexes, constraints and triggers of every v1 table -- everything the migration must leave alone."""
    snapshot: dict = {}
    for table in V1_TABLES:
        snapshot[table] = {
            "columns": rows(engine, "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                                    "WHERE table_schema = 'public' AND table_name = :t ORDER BY ordinal_position", t=table),
            "indexes": rows(engine, "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t "
                                    "ORDER BY indexname", t=table),
            "constraints": rows(engine, "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = "
                                        "to_regclass(:t) ORDER BY conname", t=f"public.{table}"),
            "triggers": rows(engine, "SELECT tgname FROM pg_trigger WHERE tgrelid = to_regclass(:t) AND NOT tgisinternal "
                                     "ORDER BY tgname", t=f"public.{table}"),
        }
    snapshot["routed_view"] = rows(engine, "SELECT pg_get_viewdef('checkins_routed'::regclass)")
    return snapshot


def test_the_migration_is_additive_over_existing_v1_data_and_leaves_every_v1_object_identical():
    with Throwaway(PREVIOUS_HEAD) as db:
        engine = create_engine(db.url)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO customers (id, name) VALUES (1, 'c')"))
            conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES (1, 'l', 'l', 'active', 1)"))
            conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, operational_branch_id) VALUES (1, 1, 'm', 'm', 1)"))
            conn.execute(text("INSERT INTO checkins (customer_id, branch_id, event_time, title, barcode, destination, bin, message) "
                              "VALUES (1, 1, now(), 'CANARY-TITLE', 'CANARY-BARCODE', 'Main', '1', 'CANARY-MESSAGE')"))
            conn.execute(text("INSERT INTO rejects (customer_id, branch_id, event_time, barcode, error_message) "
                              "VALUES (1, 1, now(), 'CANARY-BARCODE', 'Item not found')"))
            conn.execute(text("INSERT INTO acs_events (customer_id, branch_id, event_time, message_code, barcode, barcode_key, "
                              "patron_id, raw_message) VALUES (1, 1, now(), '10', 'B', 'B', 'CANARY-PATRON', '101YNY|ABB')"))
            conn.execute(text("INSERT INTO pipeline_status (customer_id, branch_id, status) VALUES (1, 1, 'ok')"))
        before = _v1_snapshot(engine)
        data_before = {t: rows(engine, f"SELECT * FROM {t} ORDER BY 1") for t in ("checkins", "rejects", "acs_events", "checkins_clean",  # nosec B608
                                                                                    "rejects_clean", "pipeline_status")}

        upgraded = _alembic(db.url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr[-2000:]

        assert _v1_snapshot(engine) == before  # not one column, index, constraint or trigger of a v1 object changed
        assert {t: rows(engine, f"SELECT * FROM {t} ORDER BY 1") for t in data_before} == data_before  # nor one row  # nosec B608
        assert all(scalar(engine, f"SELECT count(*) FROM {t}") == 0 for t in V2_TABLES)  # nosec B608
        engine.dispose()


def test_upgrade_downgrade_upgrade_is_clean_and_the_downgrade_leaves_v1_alone():
    with Throwaway("head") as db:
        engine = create_engine(db.url)
        v1_before = _v1_snapshot(engine)

        down = _alembic(db.url, "downgrade", PREVIOUS_HEAD)  # both v2 revisions: the amendment, then Step 3
        assert down.returncode == 0, down.stderr[-2000:]
        assert rows(engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(:n)", n=list(V2_TABLES)) == []
        assert _v1_snapshot(engine) == v1_before
        assert scalar(engine, "SELECT version_num FROM alembic_version") == PREVIOUS_HEAD

        up = _alembic(db.url, "upgrade", "head")
        assert up.returncode == 0, up.stderr[-2000:]
        assert sorted(r[0] for r in rows(engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(:n)",
                                         n=list(V2_TABLES))) == sorted(V2_TABLES)
        assert _v1_snapshot(engine) == v1_before
        engine.dispose()


def test_the_migration_history_is_one_linear_chain_ending_at_the_new_head(pg_url):
    engine = create_engine(pg_url)

    assert scalar(engine, "SELECT version_num FROM alembic_version") == HEAD
    engine.dispose()


# =====================================================================================================================
# 7. The v2 heartbeat model and the stored snapshot agree
# =====================================================================================================================

def test_record_heartbeat_writes_a_full_snapshot_and_only_for_an_active_matching_key(engine):
    def send(key, customer=CUSTOMER, branch=BRANCH, **fields):
        with engine.begin() as conn:
            return service.record_heartbeat(conn, customer_id=customer, branch_id=branch,
                                            data=StatusV2Request.model_validate({"contract_version": 2, "key_id": key, "status": "healthy", **fields}))

    assert send(KEY, status="error", pending_outbox_count=5, last_error_class="auth_failure") is True
    assert rows(engine, "SELECT health_status, last_error_class FROM ingest_key_ids WHERE key_id = :k", k=KEY) == [("error", "auth_failure")]
    assert send(KEY) is True  # a later, smaller heartbeat clears what it omits
    assert rows(engine, "SELECT health_status, last_error_class, pending_outbox_count FROM ingest_key_ids WHERE key_id = :k", k=KEY) == [
        ("healthy", None, None)]
    assert send(RETIRED_KEY) is False and send(OTHER_KEY) is False and send(key_id(0xDEAD)) is False


# =====================================================================================================================
# 8. ACS item events: the shape the database itself enforces
# =====================================================================================================================

def test_state_is_required_and_only_a_hold_has_the_hold_only_columns(engine):
    columns = _columns(engine, "acs_item_events")

    assert columns["state"][1] == "NO"  # NOT NULL
    for hold_only in ("destination", "is_ill", "is_branch_services", "is_collection_services", "ruleset_id"):
        assert columns[hold_only][1] == "YES", hold_only  # nullable: a non-hold stores NULL
    for required in ("event_key", "event_time", "item_key", "key_id", "customer_id", "branch_id"):
        assert columns[required][1] == "NO", required


def test_the_acs_item_table_and_its_dependent_objects_carry_the_new_name_and_none_keep_the_old_one(engine):
    names = [r[0] for r in rows(engine, "SELECT indexname FROM pg_indexes WHERE tablename = 'acs_item_events'")]
    names += [r[0] for r in rows(engine, "SELECT conname FROM pg_constraint WHERE conrelid = 'acs_item_events'::regclass")]
    names += [r[0] for r in rows(engine, "SELECT relname FROM pg_class WHERE relkind = 'S' AND relname LIKE 'acs_%'")]

    assert not [n for n in names if "acs_hold" in n], names
    assert scalar(engine, "SELECT to_regclass('acs_hold_events')") is None
    assert {"acs_item_events_event_identity_uidx", "acs_item_events_scope_time_idx", "acs_item_events_scope_item_time_idx",
            "acs_item_events_state_chk", "acs_item_events_state_shape_chk"} <= set(names)


def test_the_stored_columns_are_exactly_the_model_fields_plus_the_infrastructure_columns(engine):
    model_fields = set(AcsHoldEvent.model_fields) | set(AcsNonHoldEvent.model_fields)

    assert set(_columns(engine, "acs_item_events")) == model_fields | {"id", "customer_id", "branch_id", "key_id", "received_at"}
    assert set(_columns(engine, "acs_item_events")).isdisjoint(PROHIBITED | {"message_code", "raw_message_code", "message_type"})


def _insert_item(conn, **overrides):
    values = {"c": CUSTOMER, "b": BRANCH, "k": KEY, "e": hmac_like(1), "item": hmac_like(2), "state": "hold", "dest": "main",
              "ill": False, "bs": False, "cs": False, "rs": None}
    values.update(overrides)
    conn.execute(text("INSERT INTO acs_item_events (customer_id, branch_id, key_id, event_key, event_time, state, item_key, "
                      "destination, is_ill, is_branch_services, is_collection_services, ruleset_id) "
                      "VALUES (:c, :b, :k, :e, now(), :state, :item, :dest, :ill, :bs, :cs, :rs)"), values)


NON_HOLD_SHAPE = {"dest": None, "ill": None, "bs": None, "cs": None, "rs": None}


def test_the_database_accepts_a_hold_and_both_non_hold_states_in_their_own_shape(engine):
    with engine.begin() as conn:
        _insert_item(conn, e=hmac_like(1))
        _insert_item(conn, e=hmac_like(2), state="non_hold_101", **NON_HOLD_SHAPE)
        _insert_item(conn, e=hmac_like(3), state="other_code10", **NON_HOLD_SHAPE)

    assert rows(engine, "SELECT state, destination, is_ill, ruleset_id FROM acs_item_events ORDER BY id") == [
        ("hold", "main", False, None), ("non_hold_101", None, None, None), ("other_code10", None, None, None)]


@pytest.mark.parametrize("override", [
    {"state": "64", **NON_HOLD_SHAPE}, {"state": "patron", **NON_HOLD_SHAPE}, {"state": "message_64", **NON_HOLD_SHAPE},
    {"state": "hold "}, {"state": "HOLD"}, {"state": "non_hold", **NON_HOLD_SHAPE}, {"state": "10"}, {"state": ""}, {"state": None},
])
def test_the_database_rejects_a_state_outside_the_closed_enum(engine, override):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_item(conn, **override)


@pytest.mark.parametrize("field,value", [("dest", "unknown"), ("dest", "main"), ("ill", False), ("ill", True), ("bs", False),
                                         ("cs", False), ("rs", "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d")])
@pytest.mark.parametrize("state", ["non_hold_101", "other_code10"])
def test_the_database_rejects_a_non_hold_that_carries_a_dummy_hold_field(engine, state, field, value):
    with pytest.raises(IntegrityError) as caught, engine.begin() as conn:
        _insert_item(conn, state=state, **{**NON_HOLD_SHAPE, field: value})

    assert "state_shape_chk" in str(caught.value.orig)


@pytest.mark.parametrize("missing", ["dest", "ill", "bs", "cs"])
def test_the_database_rejects_a_hold_missing_a_derived_field(engine, missing):
    with pytest.raises(IntegrityError) as caught, engine.begin() as conn:
        _insert_item(conn, **{missing: None})

    assert "state_shape_chk" in str(caught.value.orig)


def test_a_hold_may_have_no_ruleset_id_but_the_database_still_checks_its_format(engine):
    with engine.begin() as conn:
        _insert_item(conn, e=hmac_like(1), rs=None)
        _insert_item(conn, e=hmac_like(2), rs="0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_item(conn, e=hmac_like(3), rs="ruleset-2026")


def test_the_dedup_identity_still_holds_for_every_state(engine):
    with engine.begin() as conn:
        _insert_item(conn, state="non_hold_101", **NON_HOLD_SHAPE)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_item(conn)  # same tenant + key_id + event_key, whatever the state or content


# =====================================================================================================================
# 9. ACS item events on PostgreSQL: storage, ordering and the retraction sequences
# =====================================================================================================================

def _store_items(engine, items, key=KEY):
    with engine.begin() as conn:
        return service.store_events(conn, customer_id=CUSTOMER, branch_id=BRANCH, key_id=key, checkins=[], rejects=[],
                                    acs_items=[AcsHoldEvent.model_validate(i) if i["state"] == "hold" else AcsNonHoldEvent.model_validate(i)
                                               for i in items])

def test_ruleset_only_change_is_an_idempotent_duplicate_on_postgres(engine):
    original_ruleset = "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d"
    new_ruleset = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"

    original = acs_hold(1, ruleset_id=original_ruleset)
    resend = acs_hold(1, ruleset_id=new_ruleset)

    first = _store_items(engine, [original])
    second = _store_items(engine, [resend])

    assert first["acs_items_inserted"] == 1
    assert second["acs_items_inserted"] == 0
    assert second["acs_items_duplicates"] == 1

    assert rows(
        engine,
        "SELECT ruleset_id FROM acs_item_events",
    ) == [(original_ruleset,)]

def test_a_non_hold_is_stored_with_nulls_and_a_state_change_under_one_identity_is_a_conflict(engine):
    _store_items(engine, [acs_hold(1), acs_non_hold(2, "non_hold_101"), acs_non_hold(3, "other_code10")])

    assert rows(engine, "SELECT state, destination, is_ill, ruleset_id FROM acs_item_events ORDER BY id")[1:] == [
        ("non_hold_101", None, None, None), ("other_code10", None, None, None)]
    assert _store_items(engine, [acs_non_hold(2, "non_hold_101")])["acs_items_duplicates"] == 1  # identical resend
    with pytest.raises(service.EventConflict) as caught:
        _store_items(engine, [acs_non_hold(2, "other_code10")])
    assert caught.value.conflicts == {"acs_items": [0]}


def test_ids_follow_send_order_on_postgres_so_they_are_a_deterministic_tiebreak(engine):
    same, item = when(hours=1), hmac_like(555)
    _store_items(engine, [acs_hold(1, event_time=same, item_key=item), acs_non_hold(2, "non_hold_101", event_time=same, item_key=item),
                          acs_hold(3, event_time=same, item_key=item)])
    _store_items(engine, [acs_non_hold(4, "other_code10", event_time=same, item_key=item)])

    ordered = rows(engine, "SELECT event_key FROM acs_item_events WHERE item_key = :i ORDER BY event_time, id", i=item)
    assert [r[0] for r in ordered] == [hmac_like(n) for n in (1, 2, 3, 4)]


def _latest(engine, states):
    latest = {}
    for state, item in rows(engine, "SELECT state, item_key FROM acs_item_events ORDER BY event_time, id"):
        if state in states:
            latest[item] = state
    return sorted(latest.values())


def test_the_hold_then_other_code10_sequence_differs_between_overview_and_live_today_on_postgres(api, engine):
    item = hmac_like(900)
    body = upload(checkins=[], acs_items=[acs_hold(1, item_key=item, event_time=when(hours=3)),
                                          acs_non_hold(2, "other_code10", item_key=item, event_time=when(hours=2))])

    assert post_upload(api, body).status_code == 200

    assert _latest(engine, {"hold", "non_hold_101"}) == ["hold"]                    # Overview ignores other_code10
    assert _latest(engine, {"hold", "non_hold_101", "other_code10"}) == ["other_code10"]  # Live Today: the latest code-10 record wins


@pytest.mark.parametrize("later_state,overview,live", [("non_hold_101", "non_hold_101", "non_hold_101"),
                                                       ("other_code10", "hold", "other_code10"), ("hold", "hold", "hold")])
def test_a_later_record_retracts_a_hold_exactly_as_each_dashboard_path_does(api, engine, later_state, overview, live):
    item = hmac_like(901)
    later = acs_hold(2, item_key=item, event_time=when(hours=1)) if later_state == "hold" \
        else acs_non_hold(2, later_state, item_key=item, event_time=when(hours=1))

    assert post_upload(api, upload(checkins=[], acs_items=[acs_hold(1, item_key=item, event_time=when(hours=2)), later])).status_code == 200

    assert _latest(engine, {"hold", "non_hold_101"}) == [overview]
    assert _latest(engine, {"hold", "non_hold_101", "other_code10"}) == [live]


def test_an_acs_item_upload_writes_nothing_to_a_v1_table_and_fires_no_trigger(api, engine):
    assert post_upload(api, upload(checkins=[], acs_items=[acs_hold(1), acs_non_hold(2, "other_code10")])).status_code == 200

    for table in ("acs_events", "checkins", "rejects", "checkins_clean", "rejects_clean", "pipeline_status"):
        assert scalar(engine, f"SELECT count(*) FROM {table}") == 0, table  # nosec B608


# =====================================================================================================================
# 10. The amendment migration over Step 3 data, and its downgrade
# =====================================================================================================================

def _seed_step3(engine) -> None:
    """A database at Step 3's revision, with a tenant, a key and three hold rows in the OLD table shape."""
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'c')"))
        conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES (1, 'l', 'l', 'active', 10)"))
        conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, operational_branch_id) VALUES (1, 1, 'm', 'm', 1)"))
        conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id) VALUES (:k, 10, 1)"), {"k": KEY})
        for n, dest in ((1, "main"), (2, "westside"), (3, "library_express")):
            conn.execute(text("INSERT INTO acs_hold_events (customer_id, branch_id, key_id, event_key, event_time, item_key, destination, "
                              "is_ill, is_branch_services, is_collection_services, ruleset_id) "
                              "VALUES (10, 1, :k, :e, now(), :i, :d, :ill, false, false, NULL)"),
                         {"k": KEY, "e": hmac_like(n), "i": hmac_like(n + 100), "d": dest, "ill": n == 2})


def test_the_amendment_converts_existing_step_3_holds_in_place_and_touches_nothing_else():
    with Throwaway(STEP3_HEAD) as db:
        engine = create_engine(db.url)
        _seed_step3(engine)
        before = rows(engine, "SELECT id, customer_id, branch_id, key_id, event_key, event_time, item_key, destination, is_ill, "
                              "is_branch_services, is_collection_services, ruleset_id, received_at FROM acs_hold_events ORDER BY id")
        v1_before = _v1_snapshot(engine)
        other_v2_before = {t: rows(engine, f"SELECT * FROM {t} ORDER BY id") for t in ("checkin_events", "reject_events", "ingest_key_ids")}  # nosec B608

        up = _alembic(db.url, "upgrade", "head")
        assert up.returncode == 0, up.stderr[-2000:]

        after = rows(engine, "SELECT id, customer_id, branch_id, key_id, event_key, event_time, item_key, destination, is_ill, "
                             "is_branch_services, is_collection_services, ruleset_id, received_at FROM acs_item_events ORDER BY id")
        assert after == before  # every row, id and timestamp preserved
        assert rows(engine, "SELECT DISTINCT state FROM acs_item_events") == [("hold",)]  # existing rows are all holds
        assert _v1_snapshot(engine) == v1_before  # not one v1 object changed
        assert {t: rows(engine, f"SELECT * FROM {t} ORDER BY id") for t in other_v2_before} == other_v2_before  # nosec B608
        with pytest.raises(IntegrityError), engine.begin() as conn:  # the identity index survived the rename
            _insert_item(conn, e=hmac_like(1), item=hmac_like(101), dest="main")
        engine.dispose()


def test_the_downgrade_refuses_while_a_non_hold_row_exists_and_leaves_everything_intact():
    with Throwaway("head") as db:
        engine = create_engine(db.url)
        _seed_tenants(engine)
        with engine.begin() as conn:
            _insert_item(conn, e=hmac_like(1))
            _insert_item(conn, e=hmac_like(2), state="non_hold_101", **NON_HOLD_SHAPE)
        before = rows(engine, "SELECT * FROM acs_item_events ORDER BY id")

        # -4, not -1: three migrations now sit on top of the ACS amendment
        # (v2_cutovers, and below it the RLS phase 1 migration, and below
        # that the trigger-security fix), so reaching the amendment's own
        # downgrade (the one that must refuse here) needs four steps.
        # Confirmed empirically: alembic runs a multi-step downgrade as one
        # overall transaction -- when the last step raises, the earlier
        # steps' (trivial) changes are rolled back too, not just the
        # failing one. alembic_version is therefore left completely
        # unchanged at HEAD.
        down = _alembic(db.url, "downgrade", "-4")

        assert down.returncode != 0 and "cannot downgrade" in down.stderr
        assert rows(engine, "SELECT * FROM acs_item_events ORDER BY id") == before  # nothing destroyed
        assert scalar(engine, "SELECT version_num FROM alembic_version") == HEAD
        assert scalar(engine, "SELECT to_regclass('acs_hold_events')") is None
        engine.dispose()


def test_the_downgrade_restores_step_3_exactly_when_only_holds_exist_and_the_upgrade_reapplies():
    with Throwaway("head") as db:
        engine = create_engine(db.url)
        _seed_tenants(engine)
        with engine.begin() as conn:
            _insert_item(conn, e=hmac_like(1))
            _insert_item(conn, e=hmac_like(2), dest="westside", ill=True)
        holds = rows(engine, "SELECT id, event_key, destination, is_ill FROM acs_item_events ORDER BY id")

        # -4: undo v2_cutovers, the RLS phase 1 migration, and the
        # trigger-security fix (all three trivial here) first, then the ACS
        # amendment itself -- see the sibling refusal test above for why -1
        # alone no longer reaches the ACS amendment.
        down = _alembic(db.url, "downgrade", "-4")
        assert down.returncode == 0, down.stderr[-2000:]
        assert scalar(engine, "SELECT version_num FROM alembic_version") == STEP3_HEAD
        assert rows(engine, "SELECT id, event_key, destination, is_ill FROM acs_hold_events ORDER BY id") == holds
        assert "state" not in _columns(engine, "acs_hold_events")
        assert _columns(engine, "acs_hold_events")["destination"][1] == "NO"  # NOT NULL again
        assert scalar(engine, "SELECT to_regclass('acs_item_events')") is None
        step3_indexes = {r[0] for r in rows(engine, "SELECT indexname FROM pg_indexes WHERE tablename = 'acs_hold_events'")}
        assert {"acs_hold_events_event_identity_uidx", "acs_hold_events_scope_time_idx", "acs_hold_events_scope_item_idx"} <= step3_indexes

        up = _alembic(db.url, "upgrade", "head")
        assert up.returncode == 0, up.stderr[-2000:]
        assert rows(engine, "SELECT id, event_key, destination, is_ill FROM acs_item_events ORDER BY id") == holds
        engine.dispose()
