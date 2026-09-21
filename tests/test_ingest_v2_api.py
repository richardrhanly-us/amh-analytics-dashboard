"""POST /v2/upload and POST /v2/status, through the real endpoints (Privacy Contract v2, docs/contract-v2-design.md).

The endpoints run against an in-memory SQLite database that has the v2 tables, the v1 tables (with SQLite copies of the v1
`AFTER INSERT` triggers) and the token/tenant tables. Every SQL statement the API sends is recorded, so "a v2 request never
touches a v1 table or fires a v1 trigger" is checked, not assumed. What SQLite cannot prove (the real constraints, the unique
index, concurrency, the migration) is proved on a real PostgreSQL in tests/test_ingest_v2_postgres.py.

Every value is SYNTHETIC. Timestamps come from the real clock.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

import main
from metrics import build_acs_item_summary
from services import live_context_service as lcs
from src.services import privacy_hardening as ph

ROOT = Path(__file__).resolve().parent.parent

CUSTOMER, BRANCH = 10, 1
OTHER_CUSTOMER, OTHER_BRANCH = 20, 2
TOKEN = "CANARY-V2-BEARER-TOKEN-3001"
OTHER_TOKEN = "CANARY-V2-OTHER-TENANT-TOKEN-3002"
KEY = "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"            # active, this tenant
SECOND_KEY = "5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d"      # active, this tenant
RETIRED_KEY = "6b7c8d9e-0f1a-4b2c-9d3e-4f5a6b7c8d9e"     # retired, this tenant
OTHER_TENANT_KEY = "7c8d9e0f-1a2b-4c3d-8e4f-5a6b7c8d9e0f"  # active, the OTHER tenant
UNKNOWN_KEY = "8d9e0f1a-2b3c-4d4e-9f5a-6b7c8d9e0f1a"     # not registered anywhere
CANARY_NAME = "P2300000000003"  # a card-number-like name: a plain identifier, so the old code would have echoed it

AUTH = {"Authorization": f"Bearer {TOKEN}"}
_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"

V1_TABLES = re.compile(r"\b(checkins|rejects|acs_events|checkins_clean|rejects_clean|pipeline_status)\b")

client = TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


# --- payload helpers -------------------------------------------------------------------------------------------------

def hmac_like(n: int) -> str:
    return hashlib.sha256(f"synthetic-{n}".encode()).hexdigest()


def when(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def checkin(n=1, **overrides):
    event = {"event_key": hmac_like(n), "event_time": when(minutes=5), "item_key": hmac_like(n + 1000),
             "destination": "westside", "bin": "3"}
    event.update(overrides)
    return event


def reject(n=1, **overrides):
    event = {"event_key": hmac_like(n), "event_time": when(minutes=5), "error_class": "item_not_found",
             "item_key": hmac_like(n + 1000)}
    event.update(overrides)
    return event


def acs_hold(n=1, **overrides):
    event = {"state": "hold", "event_key": hmac_like(n), "event_time": when(minutes=5), "item_key": hmac_like(n + 1000),
             "destination": "library_express", "is_ill": False, "is_branch_services": False,
             "is_collection_services": True, "ruleset_id": "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d"}
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


def status(key=KEY, **overrides):
    body = {"contract_version": 2, "key_id": key, "status": "healthy"}
    body.update(overrides)
    return body


# --- a real (SQLite) database behind the real endpoints ---------------------------------------------------------------

_V1_DDL = (
    (
        "CREATE TABLE checkins (customer_id INTEGER, branch_id INTEGER, event_time TEXT, title TEXT, barcode TEXT,"
        " collection_code TEXT, call_number TEXT, shelf_code TEXT, destination TEXT, bin TEXT, is_problem BOOLEAN,"
        " message TEXT, flag_1 TEXT, flag_2 TEXT, flag_3 TEXT, source_file TEXT, source_event_id TEXT)"
    ),
    (
        "CREATE TABLE rejects (customer_id INTEGER, branch_id INTEGER, event_time TEXT, barcode TEXT, error_message TEXT,"
        " source_file TEXT, source_event_id TEXT)"
    ),
    (
        "CREATE TABLE acs_events (customer_id INTEGER, branch_id INTEGER, event_time TEXT, message_code TEXT, barcode TEXT,"
        " barcode_key TEXT, title TEXT, patron_id TEXT, destination TEXT, raw_message TEXT, source_file TEXT, source_event_id TEXT)"
    ),
    "CREATE TABLE checkins_clean (customer_id INTEGER, branch_id INTEGER, barcode TEXT)",
    "CREATE TABLE rejects_clean (customer_id INTEGER, branch_id INTEGER, barcode TEXT)",
    "CREATE TABLE pipeline_status (customer_id INTEGER, branch_id INTEGER, status TEXT)",
    # the v1 AFTER INSERT triggers, as SQLite copies
    (
        "CREATE TRIGGER trg_sync_checkins_to_clean AFTER INSERT ON checkins BEGIN "
        "INSERT INTO checkins_clean VALUES (NEW.customer_id, NEW.branch_id, NEW.barcode); END"
    ),
    (
        "CREATE TRIGGER trg_sync_rejects_to_clean AFTER INSERT ON rejects BEGIN "
        "INSERT INTO rejects_clean VALUES (NEW.customer_id, NEW.branch_id, NEW.barcode); END"
    ),
)
_V2_DDL = (
    (
        "CREATE TABLE ingest_key_ids (id INTEGER PRIMARY KEY AUTOINCREMENT, key_id TEXT NOT NULL UNIQUE,"
        " customer_id INTEGER NOT NULL, branch_id INTEGER NOT NULL, algorithm TEXT NOT NULL DEFAULT 'hmac-sha256-v1',"
        " status TEXT NOT NULL DEFAULT 'active', created_at TEXT DEFAULT CURRENT_TIMESTAMP, retired_at TEXT,"
        " last_heartbeat_at TEXT, health_status TEXT, last_error_class TEXT, pending_outbox_count INTEGER,"
        " quarantined_count INTEGER, oldest_pending_event_at TEXT, last_success_at TEXT, watcher_last_active_at TEXT)"
    ),
    (
        "CREATE TABLE checkin_events (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,"
        " branch_id INTEGER NOT NULL, key_id TEXT NOT NULL, event_key TEXT NOT NULL, event_time TEXT NOT NULL, item_key TEXT,"
        " destination TEXT NOT NULL, bin TEXT NOT NULL, received_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    ),
    "CREATE UNIQUE INDEX checkin_events_uidx ON checkin_events (customer_id, branch_id, key_id, event_key)",
    (
        "CREATE TABLE reject_events (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,"
        " branch_id INTEGER NOT NULL, key_id TEXT NOT NULL, event_key TEXT NOT NULL, event_time TEXT NOT NULL,"
        " error_class TEXT NOT NULL, item_key TEXT, received_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    ),
    "CREATE UNIQUE INDEX reject_events_uidx ON reject_events (customer_id, branch_id, key_id, event_key)",
    (
        "CREATE TABLE acs_item_events (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,"
        " branch_id INTEGER NOT NULL, key_id TEXT NOT NULL, event_key TEXT NOT NULL, event_time TEXT NOT NULL,"
        " state TEXT NOT NULL, item_key TEXT NOT NULL, destination TEXT, is_ill BOOLEAN, is_branch_services BOOLEAN,"
        " is_collection_services BOOLEAN, ruleset_id TEXT, received_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    ),
    "CREATE UNIQUE INDEX acs_item_events_uidx ON acs_item_events (customer_id, branch_id, key_id, event_key)",
)


class Db:
    def __init__(self, engine, sent, state):
        self.engine, self.sent, self.state = engine, sent, state

    def rows(self, table, columns="*", where=""):
        self.state["paused"] = True  # the test's own queries are not the API's statements
        try:
            with self.engine.connect() as conn:
                return [tuple(r) for r in conn.execute(text(f"SELECT {columns} FROM {table} {where}"))]  # nosec B608 - test SQL
        finally:
            self.state["paused"] = False

    def count(self, table):
        return self.rows(table, "COUNT(*)")[0][0]

    def v2_rows(self):
        return sum(self.count(t) for t in ("checkin_events", "reject_events", "acs_item_events"))

    def v1_rows(self):
        return sum(self.count(t) for t in ("checkins", "rejects", "acs_events", "checkins_clean", "rejects_clean",
                                           "pipeline_status"))

    def v1_statements(self):
        return [s for s in self.sent if V1_TABLES.search(s)]

    def retire(self, key):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE ingest_key_ids SET status='retired', retired_at=CURRENT_TIMESTAMP WHERE key_id=:k"), {"k": key})


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False},
                           hide_parameters=main.engine.hide_parameters)

    @event.listens_for(engine, "connect")
    def _sha256(dbapi_connection, _record):
        dbapi_connection.create_function("sha256hex", 1, lambda v: hashlib.sha256(v.encode("utf-8")).hexdigest())

    ddl = [
        "CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT, status TEXT, operational_customer_id INTEGER)",
        "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, status TEXT, operational_branch_id INTEGER)",
        "CREATE TABLE collector_installations (id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER, status TEXT)",
        ("CREATE TABLE agent_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT, customer_id INTEGER,"
         " branch_id INTEGER, description TEXT, is_active BOOLEAN, last_used_at TEXT, installation_id INTEGER)"),
        *_V1_DDL, *_V2_DDL,
    ]
    with engine.begin() as conn:
        for statement in ddl:
            conn.execute(text(statement))
        conn.execute(text("INSERT INTO organizations VALUES (1, 'lib', 'active', :c)"), {"c": CUSTOMER})
        conn.execute(text("INSERT INTO organizations VALUES (2, 'other', 'active', :c)"), {"c": OTHER_CUSTOMER})
        conn.execute(text("INSERT INTO branches VALUES (1, 1, 'active', :b)"), {"b": BRANCH})
        conn.execute(text("INSERT INTO branches VALUES (2, 2, 'active', :b)"), {"b": OTHER_BRANCH})
        for token, customer, branch in ((TOKEN, CUSTOMER, BRANCH), (OTHER_TOKEN, OTHER_CUSTOMER, OTHER_BRANCH)):
            conn.execute(
                text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active)"
                     " VALUES (:h, :c, :b, 'test token', 1)"),
                {"h": hashlib.sha256(token.encode("utf-8")).hexdigest(), "c": customer, "b": branch})
        for key, customer, branch, state in ((KEY, CUSTOMER, BRANCH, "active"), (SECOND_KEY, CUSTOMER, BRANCH, "active"),
                                             (RETIRED_KEY, CUSTOMER, BRANCH, "retired"),
                                             (OTHER_TENANT_KEY, OTHER_CUSTOMER, OTHER_BRANCH, "active")):
            conn.execute(
                text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, status, retired_at) VALUES "
                     "(:k, :c, :b, :s, CASE WHEN :s = 'retired' THEN CURRENT_TIMESTAMP END)"),
                {"k": key, "c": customer, "b": branch, "s": state})

    sent: list[str] = []
    state = {"paused": False}

    @event.listens_for(engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if not state["paused"]:
            sent.append(statement)

    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", True)
    assert _HASH_EXPR in main._AGENT_TOKEN_LOOKUP_SQL
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, "sha256hex(:token)"))
    sent.clear()
    return Db(engine, sent, state)


def post_upload(body, token=TOKEN, **kwargs):
    return client.post("/v2/upload", json=body, headers={"Authorization": f"Bearer {token}"}, **kwargs)


def post_status(body, token=TOKEN):
    return client.post("/v2/status", json=body, headers={"Authorization": f"Bearer {token}"})


# =====================================================================================================================
# 1. Feature gating: SORTVIEW_V2_INGEST_ENABLED, default off
# =====================================================================================================================

def _flag_state(flag: str | None) -> dict:
    script = (
        "import json, main\n"
        "print(json.dumps({'enabled': main.V2_INGEST_ENABLED}))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "SORTVIEW_V2_INGEST_ENABLED"}
    env.update({"DATABASE_URL": "postgresql://user:pw@localhost/none", "PYTHONPATH": str(ROOT)})
    if flag is not None:
        env["SORTVIEW_V2_INGEST_ENABLED"] = flag
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False)  # nosec B603
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("flag", [None, "", "false", "0", "1", "yes", "on", "enabled", "TRUE-ish"])
def test_the_flag_is_off_unless_it_is_exactly_true(flag):
    assert _flag_state(flag) == {"enabled": False}


@pytest.mark.parametrize("flag", ["true", "TRUE", " True "])
def test_only_true_turns_it_on(flag):
    assert _flag_state(flag) == {"enabled": True}


@pytest.mark.parametrize("path,body", [("/v2/upload", upload()), ("/v2/status", status())])
def test_with_the_flag_off_both_v2_routes_answer_404_even_for_a_valid_token_key_and_payload(db, monkeypatch, path, body):
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", False)

    response = client.post(path, json=body, headers=AUTH)

    assert response.status_code == 404 and response.json() == {"detail": "Not Found"}
    assert db.v2_rows() == 0 and db.sent == []  # not even the token lookup ran


def test_with_the_flag_off_nothing_is_read_from_the_body(db, monkeypatch):
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", False)

    response = client.post("/v2/upload", content=b"x" * (main.V2_UPLOAD_MAX_BODY_BYTES + 10), headers=AUTH)

    assert response.status_code == 404  # 404, not 413: the flag is checked before the body is touched


def test_the_flag_alone_is_not_enough_an_active_matching_key_is_required(db):
    response = post_upload(upload(key=UNKNOWN_KEY))

    assert response.status_code == 403 and db.v2_rows() == 0


def test_the_v1_routes_do_not_depend_on_the_flag(db, monkeypatch):
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", False)
    v1 = {"checkins": [{"customer_id": CUSTOMER, "branch_id": BRANCH, "event_time": "2026-01-01 10:00:00", "barcode": "B-1",
                        "destination": "Main", "bin": "1"}]}

    response = client.post("/upload", json=v1, headers=AUTH)

    assert response.status_code == 200 and response.json()["checkins_inserted"] == 1


# =====================================================================================================================
# 2. Authentication and tenant scope: only the token decides
# =====================================================================================================================

def test_a_missing_or_wrong_token_is_a_401_and_stores_nothing(db):
    assert client.post("/v2/upload", json=upload()).status_code == 401
    assert client.post("/v2/upload", json=upload(), headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/v2/upload", json=upload(), headers={"Authorization": "Basic abc"}).status_code == 401
    assert db.v2_rows() == 0


def test_an_inactive_token_or_an_unusable_tenant_is_a_403_exactly_as_in_v1(db):
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE agent_tokens SET is_active = 0 WHERE customer_id = :c"), {"c": CUSTOMER})
    assert post_upload(upload()).status_code == 403

    with db.engine.begin() as conn:
        conn.execute(text("UPDATE agent_tokens SET is_active = 1 WHERE customer_id = :c"), {"c": CUSTOMER})
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE id = 1"))
    assert post_upload(upload()).status_code == 403
    assert db.v2_rows() == 0


def test_the_tenant_of_every_stored_row_is_the_tokens_own(db):
    response = post_upload(upload(checkins=[checkin(1)], rejects=[reject(2)], acs_items=[acs_hold(3)]))

    assert response.status_code == 200
    for table in ("checkin_events", "reject_events", "acs_item_events"):
        assert db.rows(table, "DISTINCT customer_id, branch_id, key_id") == [(CUSTOMER, BRANCH, KEY)], table


@pytest.mark.parametrize("field", ["customer_id", "branch_id"])
@pytest.mark.parametrize("value", [OTHER_CUSTOMER, OTHER_BRANCH, CUSTOMER, "20", None])
def test_a_payload_cannot_name_or_override_a_tenant(db, field, value):
    for body in (upload(**{field: value}),
                 upload(checkins=[checkin(**{field: value})]),
                 upload(checkins=[], rejects=[reject(**{field: value})]),
                 upload(checkins=[], acs_items=[acs_hold(**{field: value})])):
        response = post_upload(body)
        assert response.status_code == 422, body
        assert response.json()["detail"][0]["type"] == "extra_forbidden"
    assert db.v2_rows() == 0


def test_the_other_tenants_token_stores_under_its_own_scope_only(db):
    response = post_upload(upload(key=OTHER_TENANT_KEY, checkins=[checkin(1)]), token=OTHER_TOKEN)

    assert response.status_code == 200
    assert db.rows("checkin_events", "customer_id, branch_id, key_id") == [(OTHER_CUSTOMER, OTHER_BRANCH, OTHER_TENANT_KEY)]


# =====================================================================================================================
# 3. The key registry: an inactive, unknown or wrong-tenant key is rejected, identically
# =====================================================================================================================

@pytest.mark.parametrize("path,build", [("/v2/upload", upload), ("/v2/status", status)])
def test_unknown_retired_and_wrong_tenant_keys_are_rejected_with_one_generic_403(db, path, build):
    responses = [client.post(path, json=build(key=k), headers=AUTH) for k in (UNKNOWN_KEY, RETIRED_KEY, OTHER_TENANT_KEY)]

    assert {r.status_code for r in responses} == {403}
    assert len({r.text for r in responses}) == 1  # one body: no oracle for which of the three it was
    assert responses[0].json() == {"detail": main.V2_KEY_NOT_AUTHORIZED_DETAIL}
    assert db.v2_rows() == 0


def test_the_reason_is_logged_but_never_sent(db, caplog):
    with caplog.at_level(logging.INFO, logger="sortview.api"):
        for key in (UNKNOWN_KEY, RETIRED_KEY, OTHER_TENANT_KEY):
            post_upload(upload(key=key))

    log = caplog.text
    assert "unknown key" in log and "key status 'retired'" in log and "key belongs to another tenant" in log
    assert TOKEN not in log


def test_a_key_retired_after_use_stops_working(db):
    assert post_upload(upload(checkins=[checkin(1)])).status_code == 200

    db.retire(KEY)

    assert post_upload(upload(checkins=[checkin(2)])).status_code == 403
    assert post_status(status()).status_code == 403
    assert db.count("checkin_events") == 1


def test_two_active_keys_of_one_tenant_are_independent_identities(db):
    same = checkin(1)

    assert post_upload(upload(key=KEY, checkins=[same])).status_code == 200
    assert post_upload(upload(key=SECOND_KEY, checkins=[same])).status_code == 200  # the key_id is part of the identity

    assert db.count("checkin_events") == 2


# =====================================================================================================================
# 4. Upload: storage, idempotent resend, and conflicts
# =====================================================================================================================

def test_a_valid_upload_is_stored_and_counted(db):
    body = upload(checkins=[checkin(1), checkin(2)], rejects=[reject(3)], acs_items=[acs_hold(4), acs_hold(5), acs_hold(6)])

    response = post_upload(body)

    assert response.status_code == 200
    assert response.json() == {
        "status": "success", "contract_version": 2,
        "checkins_received": 2, "checkins_inserted": 2, "checkins_duplicates": 0,
        "rejects_received": 1, "rejects_inserted": 1, "rejects_duplicates": 0,
        "acs_items_received": 3, "acs_items_inserted": 3, "acs_items_duplicates": 0,
    }
    assert (db.count("checkin_events"), db.count("reject_events"), db.count("acs_item_events")) == (2, 1, 3)


def test_the_stored_row_is_exactly_the_event_in_utc(db):
    instant = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)
    local = instant.astimezone(timezone_of(-5)).isoformat()

    post_upload(upload(checkins=[checkin(1, event_time=local, bin="exception", destination="unknown")]))

    (row,) = db.rows("checkin_events", "event_key, event_time, item_key, destination, bin")
    assert row[0] == hmac_like(1) and row[2] == hmac_like(1001) and row[3:] == ("unknown", "exception")
    assert datetime.fromisoformat(row[1]) == instant and datetime.fromisoformat(row[1]).utcoffset() == timedelta(0)


def timezone_of(hours: int):
    from datetime import timezone
    return timezone(timedelta(hours=hours))


def test_an_acs_hold_stores_its_flags_and_ruleset(db):
    post_upload(upload(checkins=[], acs_items=[acs_hold(1, is_ill=True, is_collection_services=False, ruleset_id=None)]))

    assert db.rows("acs_item_events", "is_ill, is_branch_services, is_collection_services, ruleset_id, destination") == [
        (1, 0, 0, None, "library_express")]


def test_an_empty_request_is_a_400(db):
    response = post_upload(upload(checkins=[]))

    assert response.status_code == 400 and db.sent == []


def test_an_identical_resend_is_idempotent(db):
    body = upload(checkins=[checkin(1), checkin(2)], rejects=[reject(3)], acs_items=[acs_hold(4)])
    first = post_upload(body)

    again = post_upload(body)

    assert first.status_code == again.status_code == 200
    assert again.json()["checkins_inserted"] == 0 and again.json()["checkins_duplicates"] == 2
    assert again.json()["rejects_duplicates"] == 1 and again.json()["acs_items_duplicates"] == 1
    assert (db.count("checkin_events"), db.count("reject_events"), db.count("acs_item_events")) == (2, 1, 1)


def test_a_partial_overlap_inserts_only_the_new_events(db):
    post_upload(upload(checkins=[checkin(1), checkin(2)]))

    response = post_upload(upload(checkins=[checkin(2), checkin(3), checkin(4)]))

    assert response.json()["checkins_inserted"] == 2 and response.json()["checkins_duplicates"] == 1
    assert db.count("checkin_events") == 4


def test_an_identical_event_twice_in_one_request_is_stored_once_and_counted_as_a_duplicate(db):
    response = post_upload(upload(checkins=[checkin(1), checkin(1)]))

    assert response.status_code == 200 and response.json()["checkins_received"] == 2
    assert response.json()["checkins_inserted"] == 1 and response.json()["checkins_duplicates"] == 1
    assert db.count("checkin_events") == 1


def test_the_same_identity_under_another_tenant_is_a_different_event(db):
    post_upload(upload(checkins=[checkin(1)]))

    response = post_upload(upload(key=OTHER_TENANT_KEY, checkins=[checkin(1)]), token=OTHER_TOKEN)

    assert response.status_code == 200 and response.json()["checkins_inserted"] == 1
    assert db.count("checkin_events") == 2


CHANGES = [
    ("checkins", checkin, {"destination": "main"}), ("checkins", checkin, {"bin": "9"}),
    ("checkins", checkin, {"item_key": hmac_like(99)}), ("checkins", checkin, {"item_key": None}),
    ("checkins", checkin, {"event_time": when(days=3)}),
    ("rejects", reject, {"error_class": "routing_error"}), ("rejects", reject, {"item_key": None}),
    ("rejects", reject, {"event_time": when(days=3)}),
    ("acs_items", acs_hold, {"destination": "main"}), ("acs_items", acs_hold, {"is_ill": True}),
    ("acs_items", acs_hold, {"is_branch_services": True}), ("acs_items", acs_hold, {"is_collection_services": False}),
    ("acs_items", acs_hold, {"ruleset_id": "9e0f1a2b-3c4d-4e5f-8a6b-7c8d9e0f1a2b"}), ("acs_items", acs_hold, {"ruleset_id": None}),
    ("acs_items", acs_hold, {"item_key": hmac_like(98)}),
]


@pytest.mark.parametrize("kind,build,change", CHANGES, ids=lambda v: v if isinstance(v, str) else None)
def test_the_same_identity_with_different_content_is_a_conflict_not_a_silent_ignore(db, kind, build, change):
    original = build(1)
    assert post_upload(upload(**{"checkins": [], "rejects": [], "acs_items": [], kind: [original]})).status_code == 200
    changed = {**original, **change}

    response = post_upload(upload(**{"checkins": [], "rejects": [], "acs_items": [], kind: [changed]}))

    assert response.status_code == 409
    assert response.json() == {"code": "event_conflict", "conflicts": {kind: [0]},
                               "detail": "An event identity arrived with different content; nothing was stored"}
    table = {"checkins": "checkin_events", "rejects": "reject_events", "acs_items": "acs_item_events"}[kind]
    assert db.count(table) == 1  # the original is untouched


def test_a_conflict_stores_nothing_from_the_whole_request(db):
    post_upload(upload(checkins=[checkin(1)]))
    body = upload(checkins=[checkin(1, destination="main"), checkin(2), checkin(3)], rejects=[reject(4)], acs_items=[acs_hold(5)])

    response = post_upload(body)

    assert response.status_code == 409 and response.json()["conflicts"] == {"checkins": [0]}
    assert (db.count("checkin_events"), db.count("reject_events"), db.count("acs_item_events")) == (1, 0, 0)  # all or nothing
    assert db.rows("checkin_events", "destination") == [("westside",)]


def test_the_conflict_response_lists_positions_in_every_affected_list(db):
    post_upload(upload(checkins=[checkin(1), checkin(2)], rejects=[reject(3)]))
    body = upload(checkins=[checkin(9), checkin(1, bin="7"), checkin(2, bin="7")], rejects=[reject(3, error_class="other")])

    response = post_upload(body)

    assert response.status_code == 409
    assert response.json()["conflicts"] == {"checkins": [1, 2], "rejects": [0]}


def test_the_same_identity_twice_in_one_request_with_different_content_is_a_conflict(db):
    response = post_upload(upload(checkins=[checkin(1), checkin(2), checkin(1, destination="main")]))

    assert response.status_code == 409 and response.json()["conflicts"] == {"checkins": [2]}
    assert db.count("checkin_events") == 0


def test_the_conflict_body_and_log_carry_only_positions_and_counts(db, caplog):
    post_upload(upload(checkins=[checkin(1)]))

    with caplog.at_level(logging.INFO, logger="sortview.api"):
        response = post_upload(upload(checkins=[checkin(1, destination="main", bin="9")]))

    assert response.status_code == 409
    for text_ in (response.text, caplog.text):
        for secret in (hmac_like(1), hmac_like(1001), "westside", '"main"', "'main'", TOKEN):
            assert secret not in text_, secret
    assert "V2 upload rejected, event conflict" in caplog.text and "{'checkins': 1}" in caplog.text
    assert KEY in caplog.text  # the key_id is non-secret and identifies which key misbehaved


def test_a_conflict_after_a_resend_is_still_detected_against_the_stored_original(db):
    original = checkin(1)
    post_upload(upload(checkins=[original]))
    post_upload(upload(checkins=[original]))  # an identical resend, idempotent

    assert post_upload(upload(checkins=[{**original, "bin": "8"}])).status_code == 409
    assert post_upload(upload(checkins=[original])).status_code == 200  # ...and the original still resends cleanly


# --- bounds -----------------------------------------------------------------------------------------------------------

def test_exactly_one_thousand_events_in_total_are_stored(db):
    body = upload(checkins=[checkin(i) for i in range(400)], rejects=[reject(10_000 + i) for i in range(300)],
                  acs_items=[acs_hold(20_000 + i) for i in range(300)])

    response = post_upload(body)

    assert response.status_code == 200 and response.json()["checkins_inserted"] == 400
    assert db.v2_rows() == 1000


def test_more_than_one_thousand_events_in_total_are_a_422_and_nothing_is_stored(db):
    body = upload(checkins=[checkin(i) for i in range(400)], rejects=[reject(10_000 + i) for i in range(300)],
                  acs_items=[acs_hold(20_000 + i) for i in range(301)])

    response = post_upload(body)

    assert response.status_code == 422 and response.json()["detail"][0]["type"] == "too_many_events"
    assert db.v2_rows() == 0 and db.sent == []


def test_a_single_list_over_one_thousand_is_a_422(db):
    response = post_upload(upload(checkins=[checkin(i) for i in range(1001)]))

    assert response.status_code == 422 and response.json()["detail"][0]["type"] == "too_long"


# --- validation through the real endpoint ------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["2026-09-21T10:00:00", "2026-09-21", 1758448800, "2026-09-21 10:00:00+00:00", None, ""])
def test_a_naive_or_non_iso_timestamp_is_a_422_and_stores_nothing(db, value):
    for body in (upload(checkins=[checkin(event_time=value)]),
                 upload(checkins=[], rejects=[reject(event_time=value)]),
                 upload(checkins=[], acs_items=[acs_hold(event_time=value)])):
        assert post_upload(body).status_code == 422
    assert db.v2_rows() == 0


@pytest.mark.parametrize("label", ["Westside", "Library Express", "Main", "No Agency Destination", "DA(AH) TS(AH)-CATALOGING",
                                   "", "west side", "west-side"])
def test_a_raw_amh_destination_label_is_rejected(db, label):
    assert post_upload(upload(checkins=[checkin(destination=label)])).status_code == 422
    assert post_upload(upload(checkins=[], acs_items=[acs_hold(destination=label)])).status_code == 422
    assert db.v2_rows() == 0


@pytest.mark.parametrize("value", ["", "Bin 1", "BIN", "x" * 17, "1 2"])
def test_a_bin_that_is_not_a_short_normalized_code_is_rejected(db, value):
    assert post_upload(upload(checkins=[checkin(bin=value)])).status_code == 422


@pytest.mark.parametrize("value", ["Item Not Found", "", "free text CANARY", "Other"])
def test_a_reject_with_a_free_text_error_class_is_rejected(db, value):
    assert post_upload(upload(checkins=[], rejects=[reject(error_class=value)])).status_code == 422


@pytest.mark.parametrize("field,value", [("barcode", "CANARY-BARCODE-3003"), ("title", "CANARY-TITLE-3004"),
                                         ("patron_id", "CANARY-PATRON-3005"), ("raw_message", "64 CANARY-RAW-3006"),
                                         ("message", "CANARY-MESSAGE-3007"), ("source_event_id", "a" * 64)])
def test_a_v1_style_event_is_rejected_and_none_of_it_is_stored_or_echoed(db, caplog, field, value):
    with caplog.at_level(logging.DEBUG):
        response = post_upload(upload(checkins=[checkin(**{field: value})]))

    assert response.status_code == 422
    assert value not in response.text and value not in caplog.text
    assert db.v2_rows() == 0


# =====================================================================================================================
# 5. 422 hardening: caller-controlled field NAMES and values are never echoed
# =====================================================================================================================

@pytest.mark.parametrize("name", [CANARY_NAME, "CANARY_PATRON_3008", "patron_id", "jane.doe@example.org", "2300000000003",
                                  "x" * 80, "key with spaces"])
@pytest.mark.parametrize("where", ["envelope", "checkin", "reject", "acs_hold", "status"])
def test_an_unexpected_field_name_is_never_echoed_into_the_response_or_the_log(db, caplog, name, where):
    value = "CANARY-VALUE-3009"
    if where == "envelope":
        path, body = "/v2/upload", upload(**{name: value})
    elif where == "checkin":
        path, body = "/v2/upload", upload(checkins=[checkin(**{name: value})])
    elif where == "reject":
        path, body = "/v2/upload", upload(checkins=[], rejects=[reject(**{name: value})])
    elif where == "acs_hold":
        path, body = "/v2/upload", upload(checkins=[], acs_items=[acs_hold(**{name: value})])
    else:
        path, body = "/v2/status", status(**{name: value})

    with caplog.at_level(logging.DEBUG):
        response = client.post(path, json=body, headers=AUTH)

    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["type"] == "extra_forbidden" and error["loc"][-1] == "<key>" and error["msg"] == "Unexpected field"
    assert name not in response.text and value not in response.text
    assert name not in caplog.text and value not in caplog.text
    assert "extra_forbidden" in caplog.text  # ...but the kind and (server-side) location are still logged


def test_the_server_side_parents_of_an_unexpected_field_are_still_reported(db):
    (error,) = post_upload(upload(checkins=[checkin(**{CANARY_NAME: "x"})])).json()["detail"]

    assert error["loc"] == ["body", "checkins", 0, "<key>"]


def test_a_wrong_value_is_reported_by_field_and_kind_without_the_value(db, caplog):
    value = "CANARY-PATRON-CARD-2300000000003"
    with caplog.at_level(logging.DEBUG):
        response = post_upload(upload(checkins=[checkin(destination=value)]))

    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "checkins", 0, "destination"] and error["type"] == "string_pattern_mismatch"
    assert value not in response.text and value not in caplog.text


def test_a_body_that_is_not_json_is_a_422_without_its_content(db, caplog):
    junk = "CANARY-NOT-JSON-3010 {{{"
    with caplog.at_level(logging.DEBUG):
        response = client.post("/v2/upload", content=junk, headers={**AUTH, "content-type": "application/json"})

    assert response.status_code == 422 and junk not in response.text and junk not in caplog.text


def test_the_shared_helper_hides_the_last_element_of_an_extra_forbidden_location():
    error = {"type": "extra_forbidden", "loc": ("body", "checkins", 3, CANARY_NAME), "msg": "x", "input": "CANARY", "ctx": {}}

    assert ph.safe_validation_errors([error])[0]["loc"] == ["body", "checkins", 3, "<key>"]
    assert ph.safe_validation_errors([{**error, "type": "missing", "loc": ("body", "checkins", 3, "destination")}])[0]["loc"] \
        == ["body", "checkins", 3, "destination"]  # every other error type keeps the server's own field name


def test_the_v1_validation_response_is_unchanged_for_an_ordinary_missing_field(db):
    response = client.post("/upload", json={"checkins": [{"branch_id": BRANCH}]}, headers=AUTH)

    assert response.status_code == 422 and response.json()["code"] == "validation_error"
    assert response.json()["detail"][0]["loc"] == ["body", "checkins", 0, "customer_id"]


# =====================================================================================================================
# 6. The byte-size guard: Content-Length AND chunked
# =====================================================================================================================

def _chunks(payload: bytes, size: int = 4096):
    for start in range(0, len(payload), size):
        yield payload[start:start + size]


def test_a_body_over_the_limit_with_a_content_length_is_a_413_before_anything_runs(db):
    response = client.post("/v2/upload", content=b"x" * (main.V2_UPLOAD_MAX_BODY_BYTES + 1), headers=AUTH)

    assert response.status_code == 413 and response.json() == {"detail": "Request body too large"}
    assert db.sent == []


def test_a_chunked_body_over_the_limit_with_no_content_length_is_a_413_too(db):
    payload = b"x" * (main.V2_UPLOAD_MAX_BODY_BYTES + 1)

    response = client.post("/v2/upload", content=_chunks(payload), headers=AUTH)

    assert response.status_code == 413 and response.json() == {"detail": "Request body too large"}
    assert db.sent == []


def test_the_v1_middleware_alone_does_not_stop_a_chunked_body_which_is_why_v2_has_its_own_guard(db):
    payload = b"x" * (main.V2_UPLOAD_MAX_BODY_BYTES + 1)

    # the v1 route has no guard for a chunked body: it reads it all and only then fails validation
    assert client.post("/upload", content=_chunks(payload), headers={**AUTH, "content-type": "application/json"}).status_code == 422
    assert client.post("/v2/upload", content=_chunks(payload), headers=AUTH).status_code == 413


def test_a_valid_chunked_body_under_the_limit_is_accepted(db):
    payload = json.dumps(upload(checkins=[checkin(i) for i in range(50)])).encode()

    response = client.post("/v2/upload", content=_chunks(payload, 512), headers={**AUTH, "content-type": "application/json"})

    assert response.status_code == 200 and db.count("checkin_events") == 50


def test_a_body_exactly_at_the_limit_is_read_and_one_byte_more_is_not(db, monkeypatch):
    payload = json.dumps(upload(checkins=[checkin(1)])).encode()
    monkeypatch.setattr(main, "V2_UPLOAD_MAX_BODY_BYTES", len(payload))

    assert client.post("/v2/upload", content=payload, headers={**AUTH, "content-type": "application/json"}).status_code == 200
    assert client.post("/v2/upload", content=payload + b" ", headers={**AUTH, "content-type": "application/json"}).status_code == 413
    assert client.post("/v2/upload", content=_chunks(payload + b" ", 7), headers={**AUTH, "content-type": "application/json"}).status_code == 413


def test_the_status_route_has_its_own_much_smaller_limit(db):
    assert main.V2_STATUS_MAX_BODY_BYTES < main.V2_UPLOAD_MAX_BODY_BYTES
    big = b"x" * (main.V2_STATUS_MAX_BODY_BYTES + 1)

    assert client.post("/v2/status", content=big, headers=AUTH).status_code == 413
    assert client.post("/v2/status", content=_chunks(big), headers=AUTH).status_code == 413
    assert db.sent == []


def test_a_realistic_full_batch_fits_comfortably_inside_the_limit():
    body = json.dumps(upload(checkins=[checkin(i) for i in range(1000)]))

    assert len(body) < main.V2_UPLOAD_MAX_BODY_BYTES / 2


def _request_with(headers: list[tuple[bytes, bytes]], chunks: list[bytes]) -> Request:
    pending = list(chunks)

    async def receive():
        if pending:
            return {"type": "http.request", "body": pending.pop(0), "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    return Request({"type": "http", "headers": headers, "method": "POST", "path": "/v2/upload"}, receive)


def test_a_content_length_that_is_not_a_number_is_a_400():
    request = _request_with([(b"content-length", b"abc")], [b"{}"])

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main._read_bounded_body(request, 1000))

    assert caught.value.status_code == 400


def test_a_negative_or_oversized_content_length_is_refused_before_reading():
    for declared in (b"-5", b"1001"):
        request = _request_with([(b"content-length", declared)], [b"{}"])
        with pytest.raises(HTTPException) as caught:
            asyncio.run(main._read_bounded_body(request, 1000))
        assert caught.value.status_code in {400, 413}


def test_the_reader_stops_as_soon_as_the_running_total_passes_the_limit():
    request = _request_with([], [b"a" * 600, b"b" * 600, b"c" * 600])

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main._read_bounded_body(request, 1000))

    assert caught.value.status_code == 413


# =====================================================================================================================
# 7. No v2 request touches a v1 table or fires a v1 trigger
# =====================================================================================================================

def test_an_upload_never_touches_a_v1_table_or_fires_a_v1_trigger(db):
    body = upload(checkins=[checkin(1), checkin(2)], rejects=[reject(3)], acs_items=[acs_hold(4)])

    assert post_upload(body).status_code == 200
    assert post_upload(body).status_code == 200  # the resend path too
    assert post_upload(upload(checkins=[checkin(1, bin="9")])).status_code == 409  # ...and the conflict path

    assert db.v1_statements() == []
    assert db.v1_rows() == 0  # nothing in checkins/rejects/acs_events, and so nothing copied to the *_clean tables


def test_a_heartbeat_never_touches_a_v1_table(db):
    assert post_status(status(status="degraded", last_error_class="retryable_infra", pending_outbox_count=3)).status_code == 200

    assert db.v1_statements() == [] and db.v1_rows() == 0


def test_a_rejected_v2_request_touches_no_v1_table_either(db):
    post_upload(upload(key=UNKNOWN_KEY))
    post_upload(upload(checkins=[checkin(destination="Westside")]))
    post_status(status(status="ok"))

    assert db.v1_statements() == []


def test_the_v2_statements_only_ever_name_v2_and_auth_tables(db):
    post_upload(upload(checkins=[checkin(1)], rejects=[reject(2)], acs_items=[acs_hold(3)]))
    post_upload(upload(checkins=[checkin(1)]))
    post_status(status())

    named = {t for s in db.sent for t in re.findall(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+([a-z_]+)", s, re.IGNORECASE)}
    assert named <= {"agent_tokens", "organizations", "branches", "collector_installations", "ingest_key_ids",
                     "checkin_events", "reject_events", "acs_item_events"}


def test_a_v1_upload_still_works_with_the_same_token_and_never_writes_a_v2_table(db):
    v1 = {"checkins": [{"customer_id": CUSTOMER, "branch_id": BRANCH, "event_time": "2026-01-01 10:00:00", "barcode": "B-1",
                        "destination": "Main", "bin": "1"}]}

    response = client.post("/upload", json=v1, headers=AUTH)

    assert response.status_code == 200 and response.json()["checkins_inserted"] == 1
    assert db.v2_rows() == 0 and db.count("checkins") == 1


# =====================================================================================================================
# 8. The v2 heartbeat
# =====================================================================================================================

def _snapshot(db):
    return db.rows("ingest_key_ids", "health_status, last_error_class, pending_outbox_count, quarantined_count, "
                                     "oldest_pending_event_at, last_success_at, watcher_last_active_at, last_heartbeat_at",
                   f"WHERE key_id = '{KEY}'")[0]


def test_a_full_heartbeat_is_stored_on_the_key(db):
    response = post_status(status(status="degraded", last_error_class="retryable_infra", pending_outbox_count=12,
                                  quarantined_count=1, oldest_pending_event_at=when(hours=3), last_success_at=when(minutes=20),
                                  watcher_last_active_at=when(seconds=30)))

    assert response.status_code == 200 and response.json() == {"status": "success", "contract_version": 2}
    health, error_class, pending, quarantined, oldest, success, watcher, heartbeat = _snapshot(db)
    assert (health, error_class, pending, quarantined) == ("degraded", "retryable_infra", 12, 1)
    assert all(v is not None for v in (oldest, success, watcher, heartbeat))


def test_each_heartbeat_is_a_full_snapshot_an_omitted_field_becomes_null(db):
    post_status(status(status="error", last_error_class="auth_failure", pending_outbox_count=9, last_success_at=when(hours=1)))

    post_status(status(status="healthy"))

    assert _snapshot(db)[:7] == ("healthy", None, None, None, None, None, None)


def test_a_heartbeat_stores_no_event_and_no_v1_data(db):
    post_status(status())

    assert db.v2_rows() == 0 and db.v1_rows() == 0


FREE_TEXT_FIELDS = [
    ("last_error", "Traceback (most recent call last): CANARY-EXC"), ("last_error", "CANARY-RESPONSE-PREVIEW"),
    ("error", "boom"), ("message", "CANARY-MESSAGE"), ("detail", "x"), ("exception", "RuntimeError('x')"),
    ("destination_breakdown", {}), ("destination_breakdown", {"westside": 3}), ("details", {"a": 1}),
    ("installation_id", 7), ("collector_version", "1.0.4"), ("customer_id", CUSTOMER), ("branch_id", BRANCH),
    ("hostname", "CANARY-HOST"), ("checkins_rows", 5), ("last_run", when(hours=1)), ("last_failure_category", "auth_failure"),
    ("health_status", "healthy"),
]


@pytest.mark.parametrize("field,value", FREE_TEXT_FIELDS, ids=[f"{f}={type(v).__name__}" for f, v in FREE_TEXT_FIELDS])
def test_the_heartbeat_rejects_every_free_text_open_dictionary_and_unknown_field(db, caplog, field, value):
    with caplog.at_level(logging.DEBUG):
        response = post_status(status(**{field: value}))

    assert response.status_code == 422 and response.json()["detail"][0]["type"] == "extra_forbidden"
    assert response.json()["detail"][0]["loc"] == ["body", "<key>"]  # the caller's field name is not echoed
    assert "CANARY" not in response.text and "CANARY" not in caplog.text
    assert _snapshot(db)[0] is None  # nothing stored


@pytest.mark.parametrize("field,value", [
    ("status", "everything is fine"), ("status", "ok"), ("status", ""), ("status", "auth_failure"), ("status", "ERROR"),
    ("status", "failed"), ("last_error_class", "Traceback CANARY"), ("last_error_class", "error"), ("last_error_class", "healthy"),
    ("last_error_class", "timeout: HTTPSConnectionPool"), ("last_error_class", "RuntimeError"),
    ("pending_outbox_count", -1), ("pending_outbox_count", "many"), ("quarantined_count", 1.5),
    ("oldest_pending_event_at", "2026-09-21T10:00:00"), ("last_success_at", 1758448800), ("watcher_last_active_at", "now"),
])
def test_the_heartbeat_rejects_a_value_outside_its_enum_or_type(db, field, value):
    assert post_status(status(**{field: value})).status_code == 422
    assert _snapshot(db)[0] is None


def test_the_heartbeat_needs_a_status_and_the_contract_version(db):
    for missing in ("status", "contract_version", "key_id"):
        body = status()
        del body[missing]
        assert post_status(body).status_code == 422


def test_the_heartbeat_does_not_link_an_installation_or_change_a_v1_heartbeat_row(db):
    post_status(status())

    named = {t for s in db.sent for t in re.findall(r"\b(?:INTO|UPDATE)\s+([a-z_]+)", s, re.IGNORECASE)}
    assert named <= {"agent_tokens", "ingest_key_ids"}


def test_the_status_route_is_rate_limited_like_the_upload_route(db):
    limit = int(main.UPLOAD_RATE_LIMIT.split("/")[0])

    codes = [post_status(status()).status_code for _ in range(limit + 1)]

    assert codes[:limit] == [200] * limit and codes[limit] == 429


@pytest.mark.parametrize("value", ["healthy", "degraded", "error"])
def test_each_approved_overall_status_is_stored(db, value):
    assert post_status(status(status=value)).status_code == 200

    assert _snapshot(db)[0] == value


@pytest.mark.parametrize("value", ["retryable_infra", "auth_failure", "permanent_rejection", "source_unavailable",
                                   "configuration_error", "other"])
def test_each_approved_error_class_is_stored(db, value):
    assert post_status(status(status="error", last_error_class=value)).status_code == 200

    assert _snapshot(db)[:2] == ("error", value)


def test_auth_failure_is_an_error_class_and_is_refused_as_an_overall_status(db):
    assert post_status(status(status="auth_failure")).status_code == 422
    assert _snapshot(db)[0] is None

    assert post_status(status(status="error", last_error_class="auth_failure")).status_code == 200
    assert _snapshot(db)[:2] == ("error", "auth_failure")


SECRET_FIELD_NAMES = ["hmac_secret", "hmac_key", "secret", "key_material", "signing_key", "salt", "seed", "password", "algorithm"]


@pytest.mark.parametrize("name", SECRET_FIELD_NAMES)
@pytest.mark.parametrize("where", ["envelope", "checkin", "status"])
def test_a_field_that_could_carry_an_hmac_secret_is_refused_stored_nowhere_and_never_echoed(db, caplog, name, where):
    canary = "CANARY-HMAC-SECRET-3301"
    if where == "envelope":
        path, body = "/v2/upload", upload(**{name: canary})
    elif where == "checkin":
        path, body = "/v2/upload", upload(checkins=[checkin(**{name: canary})])
    else:
        path, body = "/v2/status", status(**{name: canary})

    with caplog.at_level(logging.DEBUG):
        response = client.post(path, json=body, headers=AUTH)

    assert response.status_code == 422
    assert canary not in response.text and canary not in caplog.text and name not in response.text.replace("<key>", "")
    assert db.v2_rows() == 0 and _snapshot(db)[0] is None


def test_a_key_id_that_is_really_key_material_is_refused_before_the_registry_is_even_asked(db):
    for smuggled in (hmac_like(7), "CANARY-HMAC-SECRET-3302", "c2VjcmV0LWtleS1tYXRlcmlhbC1oZXJl"):
        assert post_upload(upload(key=smuggled)).status_code == 422
        assert post_status(status(key=smuggled)).status_code == 422
    assert db.sent == []


def test_no_v2_response_carries_anything_but_counts_fixed_codes_and_positions(db):
    success = post_upload(upload(checkins=[checkin(1)]))
    conflict = post_upload(upload(checkins=[checkin(1, bin="9")]))
    refused = post_upload(upload(key=UNKNOWN_KEY))
    invalid = post_upload(upload(checkins=[checkin(destination="Westside")]))
    heartbeat = post_status(status())

    assert set(success.json()) == {"status", "contract_version", "checkins_received", "checkins_inserted", "checkins_duplicates",
                                   "rejects_received", "rejects_inserted", "rejects_duplicates", "acs_items_received",
                                   "acs_items_inserted", "acs_items_duplicates"}
    assert set(conflict.json()) == {"code", "detail", "conflicts"} and set(refused.json()) == {"detail"}
    assert set(invalid.json()) == {"code", "detail"} and set(heartbeat.json()) == {"status", "contract_version"}
    for response in (success, conflict, refused, invalid, heartbeat):
        body = response.text
        assert KEY not in body and TOKEN not in body and hmac_like(1) not in body  # not even the non-secret key_id is echoed
        assert not re.search(r"[0-9a-f]{32,}", body)  # no long hex string of any kind (a key, a digest, a secret)


# =====================================================================================================================
# 9. A failing database leaks nothing
# =====================================================================================================================

def test_a_database_failure_is_a_generic_500_and_leaks_no_event_value(db, caplog):
    with db.engine.begin() as conn:
        conn.execute(text("DROP TABLE checkin_events"))

    with caplog.at_level(logging.DEBUG):
        response = post_upload(upload(checkins=[checkin(1)]))

    assert response.status_code == 500 and response.json() == {"detail": "Internal server error"}
    for secret in (hmac_like(1), hmac_like(1001), "westside", TOKEN):
        assert secret not in response.text and secret not in caplog.text, secret


def _function_source(name: str) -> str:
    """The source of one top-level function of main.py, read from the file with `ast` (not `inspect`, which follows a
    decorated function's line numbers and can point at a neighbour if the file changed after it was imported)."""
    import ast

    path = Path(main.__file__)
    text_ = path.read_text(encoding="utf-8")
    (node,) = [n for n in ast.parse(text_).body if isinstance(n, ast.FunctionDef) and n.name == name]
    return ast.get_source_segment(text_, node) or ""


def test_the_v2_code_never_uses_model_dump_or_the_v1_models():
    for name in ("upload_v2", "status_v2", "_require_ingest_key"):
        source = _function_source(name)
        assert source, name
        assert "model_dump" not in source and "UploadRequest" not in source and "CheckinRow" not in source, name


def test_the_v1_handler_still_calls_the_v1_authentication_with_the_rows_own_ids():
    source = _function_source("upload")

    assert "authenticate_agent(" in source and "authenticate_agent_token" not in source
    assert "first_customer_id" in source and "first_branch_id" in source  # the v1 handler still authenticates the rows' own ids


# =====================================================================================================================
# 10. ACS item events: the contract through the real endpoint
# =====================================================================================================================

ACS_COLUMNS = "state, destination, is_ill, is_branch_services, is_collection_services, ruleset_id"


def test_all_three_states_are_stored_and_a_non_hold_stores_nothing_but_nulls_in_the_hold_only_columns(db):
    body = upload(checkins=[], acs_items=[acs_hold(1), acs_non_hold(2, "non_hold_101"), acs_non_hold(3, "other_code10")])

    response = post_upload(body)

    assert response.status_code == 200
    assert response.json()["acs_items_received"] == response.json()["acs_items_inserted"] == 3
    assert db.rows("acs_item_events", ACS_COLUMNS, "ORDER BY id") == [
        ("hold", "library_express", 0, 0, 1, "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d"),
        ("non_hold_101", None, None, None, None, None),   # NULL, not "" / 0 / "unknown"
        ("other_code10", None, None, None, None, None),
    ]


@pytest.mark.parametrize("field,value", [("destination", "main"), ("destination", "unknown"), ("destination", None),
                                         ("is_ill", False), ("is_ill", None), ("is_branch_services", False),
                                         ("is_collection_services", True), ("ruleset_id", "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d"),
                                         ("ruleset_id", None)])
@pytest.mark.parametrize("state", ["non_hold_101", "other_code10"])
def test_a_non_hold_carrying_any_hold_field_even_a_dummy_is_a_422_and_stores_nothing(db, state, field, value):
    response = post_upload(upload(checkins=[], acs_items=[acs_non_hold(1, state, **{field: value})]))

    assert response.status_code == 422 and response.json()["detail"][0]["type"] == "extra_forbidden"
    assert response.json()["detail"][0]["loc"][-1] == "<key>"
    assert db.v2_rows() == 0


@pytest.mark.parametrize("field", ["destination", "is_ill", "is_branch_services", "is_collection_services"])
def test_a_hold_missing_a_derived_field_is_a_422(db, field):
    event = acs_hold(1)
    del event[field]

    response = post_upload(upload(checkins=[], acs_items=[event]))

    assert response.status_code == 422 and response.json()["detail"][0]["type"] == "missing"
    assert response.json()["detail"][0]["loc"][-1] == field  # a server-side field name, safe to name
    assert db.v2_rows() == 0


@pytest.mark.parametrize("value", ["64", "message_64", "patron", "patron_info", "non_hold", "not_hold", "HOLD", "101", "10", "", None, 7])
def test_an_unknown_state_is_a_422_with_a_fixed_message_and_message_64_can_never_be_sent(db, caplog, value):
    with caplog.at_level(logging.DEBUG):
        response = post_upload(upload(checkins=[], acs_items=[acs_non_hold(1, value)]))

    (error,) = response.json()["detail"]
    assert response.status_code == 422 and error["type"] == "union_tag_invalid"
    assert error["loc"] == ["body", "acs_items", 0] and error["msg"] == "Input is not one of the allowed values"
    assert db.v2_rows() == 0
    if isinstance(value, str) and len(value) > 3:
        assert value not in response.text and value not in caplog.text  # the submitted tag is never echoed


def test_a_missing_state_is_a_422_and_a_raw_message_code_field_is_refused(db):
    no_state = acs_non_hold(1)
    del no_state["state"]
    with_code = {**acs_hold(2), "message_code": "10"}

    for candidate in (no_state, with_code):
        response = post_upload(upload(checkins=[], acs_items=[candidate]))
        assert response.status_code == 422
    assert db.v2_rows() == 0


def test_the_step_3_hold_only_list_is_no_longer_accepted(db):
    response = post_upload({"contract_version": 2, "key_id": KEY, "acs_holds": [acs_hold(1)]})

    assert response.status_code == 422 and response.json()["detail"][0]["type"] == "extra_forbidden"
    assert db.v2_rows() == 0


def test_acs_items_count_toward_the_thousand_event_total_through_the_endpoint(db):
    ok = upload(checkins=[checkin(i) for i in range(400)], rejects=[reject(10_000 + i) for i in range(300)],
                acs_items=[acs_non_hold(20_000 + i, "other_code10") for i in range(300)])
    too_many = upload(checkins=[checkin(i) for i in range(400)], rejects=[reject(10_000 + i) for i in range(300)],
                      acs_items=[acs_non_hold(20_000 + i, "other_code10") for i in range(301)])

    assert post_upload(ok).status_code == 200 and db.v2_rows() == 1000
    assert post_upload(too_many).status_code == 422


def test_an_identical_non_hold_resend_is_idempotent_and_a_state_change_under_one_identity_is_a_conflict(db):
    original = acs_non_hold(1, "non_hold_101")
    assert post_upload(upload(checkins=[], acs_items=[original])).status_code == 200

    again = post_upload(upload(checkins=[], acs_items=[original]))
    changed = post_upload(upload(checkins=[], acs_items=[{**original, "state": "other_code10"}]))

    assert again.status_code == 200 and again.json()["acs_items_duplicates"] == 1
    assert changed.status_code == 409 and changed.json()["conflicts"] == {"acs_items": [0]}
    assert db.rows("acs_item_events", "state") == [("non_hold_101",)]


def test_events_are_stored_in_send_order_so_id_is_a_deterministic_tiebreak_for_equal_times(db):
    same_instant, item = when(hours=2), hmac_like(777)
    events = [acs_hold(1, event_time=same_instant, item_key=item), acs_non_hold(2, "non_hold_101", event_time=same_instant, item_key=item),
              acs_hold(3, event_time=same_instant, item_key=item)]

    assert post_upload(upload(checkins=[], acs_items=events)).status_code == 200
    assert post_upload(upload(checkins=[], acs_items=[acs_non_hold(4, "other_code10", event_time=same_instant, item_key=item)])).status_code == 200

    stored = db.rows("acs_item_events", "id, event_key, state", "ORDER BY id")
    assert [r[1] for r in stored] == [hmac_like(n) for n in (1, 2, 3, 4)]  # send order, across requests too
    assert [r[0] for r in stored] == sorted(r[0] for r in stored)


def test_an_acs_conflict_is_reported_by_position_and_stores_nothing(db):
    post_upload(upload(checkins=[], acs_items=[acs_hold(1), acs_non_hold(2)]))

    response = post_upload(upload(checkins=[], acs_items=[acs_non_hold(9), acs_hold(1, destination="main"), acs_non_hold(2)]))

    assert response.status_code == 409 and response.json()["conflicts"] == {"acs_items": [1]}
    assert db.count("acs_item_events") == 2


# =====================================================================================================================
# 11. The exact sequences the dashboard retracts on: real dashboard code vs the persisted v2 event stream
# =====================================================================================================================
#
# For each sequence of ACS records this file computes what the REAL dashboard code says (Overview: metrics.build_acs_item_summary;
# Live Today: services.live_context_service.build_live_context) and what a plain reduction of the PERSISTED v2 stream says, after
# the same records went through POST /v2/upload. They must be equal. Dashboard aggregation is not implemented in this step; the
# small reducer below is the specification the later dashboard step must meet:
#
#   Overview  : states hold + non_hold_101 only; within the reporting window; the greatest (event_time, id) per item wins;
#               count an item only if that latest state is `hold`; other_code10 is ignored.
#   Live Today: all three states; rows of the latest date; the greatest (event_time, id) per item wins; count only a hold.

DAY0 = (datetime.now(UTC) - timedelta(days=5)).replace(hour=0, minute=0, second=0, microsecond=0)
SLUG_OF = {"Main": "main", "Westside": "westside", "Library Express": "library_express"}
TRANSIT_SLUGS = {"westside": "Westside", "library_express": "Library Express"}
CLASSIFIER = {"transit_labels": ["Westside", "Library Express"], "branch_services_names": [], "collection_services_names": [],
              "branch_services_da_patterns": [], "collection_services_da_patterns": []}
PALETTE = {k: "#000000" for k in ("danger_bg", "danger_border", "danger_text", "danger_title", "info_bg", "info_border",
                                  "info_text", "info_title")}


def at(hour, minute=0, day=0):
    return DAY0 + timedelta(days=day, hours=hour, minutes=minute)


def patron_record(pid, patron_type, when_):
    return {"when": when_, "message_code": "64", "barcode": None, "patron_id": pid, "destination": None,
            "raw_message": f"64 |AA{pid}|AECANARY NAME {pid}|PT{patron_type}|BDCANARY ADDRESS|BECANARY@example.invalid"}


def item_record(barcode, pid, destination, when_, prefix="101YNY"):
    return {"when": when_, "message_code": prefix[:2], "barcode": barcode, "patron_id": pid, "destination": destination,
            "raw_message": f"{prefix}|AB{barcode}|AJCANARY TITLE|AA{pid}|CT{destination}"}


def dashboard_frame(records):
    frame = pd.DataFrame([{k: (v.replace(tzinfo=None) if k == "when" else v) for k, v in r.items()} for r in records])
    return frame.rename(columns={"when": "datetime"})


def _patron_types(records):
    return {r["patron_id"]: r["raw_message"].split("|PT")[1].split("|")[0] for r in records if r["message_code"] == "64"}


def to_v2_events(records):
    """What the future collector derives from the same records: message-64 patron records stay local (no event); every code-10
    record becomes one event with its derived state; a hold also carries its derived, non-identifying classification."""
    types, events = _patron_types(records), []
    for n, r in enumerate(records):
        if r["message_code"] != "10":
            continue
        base = {"event_key": hmac.new(b"test-only-secret", f"event-{n}".encode(), "sha256").hexdigest(),
                "event_time": r["when"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "item_key": hmac.new(b"test-only-secret", r["barcode"].encode(), "sha256").hexdigest()}
        if r["raw_message"].startswith("101YNY"):
            events.append({"state": "hold", **base, "destination": SLUG_OF[r["destination"]],
                           "is_ill": types.get(r["patron_id"]) == "ILL", "is_branch_services": False, "is_collection_services": False})
        elif r["raw_message"].startswith("101"):
            events.append({"state": "non_hold_101", **base})
        else:
            events.append({"state": "other_code10", **base})
    return events


def _outcome(holds, ill, programming, collection, ill_main, ill_by_branch):
    # The dashboard's classifier returns `ill_by_branch == {}` when it finds no 101 item at all (its early-return path) and a
    # zero per transit label otherwise. Both mean "no ILL items there", so a missing label is compared as 0.
    by_branch = {label: dict(ill_by_branch).get(label, 0) for label in TRANSIT_SLUGS.values()}
    return {"holds": holds, "ill": ill, "programming": programming, "collection_services": collection,
            "ill_main": ill_main, "ill_by_branch": by_branch}


def dashboard_overview(records, first_day=None, last_day=None):
    frame = dashboard_frame(records)
    if first_day is not None:
        frame = frame[(frame["datetime"].dt.date >= (DAY0 + timedelta(days=first_day)).date())
                      & (frame["datetime"].dt.date <= (DAY0 + timedelta(days=last_day)).date())]
    summary = build_acs_item_summary(frame, **CLASSIFIER)
    return _outcome(summary["holds_total"], summary["ill_total"], summary["programming_total"],
                    summary["collection_services_total"], summary["ill_main"], summary["ill_by_branch"])


def dashboard_live(records, monkeypatch):
    monkeypatch.setattr(lcs, "get_today_metrics", lambda df, rj, today: {
        "today_df": pd.DataFrame({"datetime": pd.to_datetime([]), "destination": []}), "today_rejects_df": pd.DataFrame(),
        "today_checkins": 0, "today_rejects": 0, "today_total_transit": 0, "today_peak_hour": None,
        "today_peak_hour_count": 0, "today_peak_hour_pct": 0, "today_reject_rate": 0.0})
    monkeypatch.setattr(lcs, "_build_historical_baseline", lambda *a, **k: {
        "max_observed_hourly_throughput": 1, "historical_transit_pct_map": {}, "historical_daily_avg_reject": 0.0})
    context = lcs.build_live_context(
        pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), dashboard_frame(records), {}, 0,
        DAY0.date(), pd.Timestamp(DAY0), CLASSIFIER["transit_labels"], "Main", [], [], [], [], PALETTE)
    found = {}

    def walk(node, depth=0):
        if isinstance(node, dict) and depth < 3:
            for key, value in node.items():
                if key in ("today_holds", "today_ill", "today_programming", "today_collection_services", "today_ill_main",
                           "today_ill_by_branch"):
                    found[key] = value
                walk(value, depth + 1)

    walk(context)
    return _outcome(found["today_holds"], found["today_ill"], found["today_programming"], found["today_collection_services"],
                    found["today_ill_main"], found["today_ill_by_branch"])


def stream_rows(db):
    columns = ["id", "event_time", "state", "item_key", "destination", "is_ill", "is_branch_services", "is_collection_services"]
    return [{**dict(zip(columns, row)), "event_time": datetime.fromisoformat(row[1])}
            for row in db.rows("acs_item_events", ", ".join(columns), "ORDER BY id")]


def _latest_wins(rows, states):
    latest = {}
    for row in sorted(rows, key=lambda r: (r["event_time"], r["id"])):
        if row["state"] in states:
            latest[row["item_key"]] = row
    return list(latest.values())


def _summarize(latest):
    holds = [r for r in latest if r["state"] == "hold"]
    ill = [r for r in holds if r["is_ill"]]
    programming = [r for r in holds if r["is_branch_services"]]
    collection = [r for r in holds if r["is_collection_services"]]
    public = [r for r in holds if not (r["is_ill"] or r["is_branch_services"] or r["is_collection_services"])]
    return _outcome(len(public), len(ill), len(programming), len(collection),
                    sum(1 for r in ill if r["destination"] not in TRANSIT_SLUGS),
                    {label: sum(1 for r in ill if r["destination"] == slug) for slug, label in TRANSIT_SLUGS.items()})


def reduce_overview(rows, first_day=None, last_day=None):
    if first_day is not None:
        lo, hi = (DAY0 + timedelta(days=first_day)).date(), (DAY0 + timedelta(days=last_day)).date()
        rows = [r for r in rows if lo <= r["event_time"].date() <= hi]
    return _summarize(_latest_wins(rows, {"hold", "non_hold_101"}))


def reduce_live(rows):
    if not rows:
        return _summarize([])
    latest_date = max(r["event_time"].date() for r in rows)
    return _summarize(_latest_wins([r for r in rows if r["event_time"].date() == latest_date],
                                   {"hold", "non_hold_101", "other_code10"}))


ADULT, ILL_PATRON = patron_record("PA", "ADULT", at(8)), patron_record("PB", "ILL", at(8))

# name -> (records, expected Overview {holds, ill}, expected Live {holds, ill}); pinned from the investigation
SEQUENCES = {
    "hold -> later non_hold_101": (
        [ADULT, item_record("B1", "PA", "Main", at(9)), item_record("B1", "PA", "Main", at(11), "101YNN")],
        {"holds": 0, "ill": 0}, {"holds": 0, "ill": 0}),
    "hold -> later hold": (
        [ADULT, item_record("B1", "PA", "Main", at(9)), item_record("B1", "PA", "Main", at(11))],
        {"holds": 1, "ill": 0}, {"holds": 1, "ill": 0}),
    "non-hold -> later hold": (
        [ADULT, item_record("B1", "PA", "Main", at(9), "101YNN"), item_record("B1", "PA", "Main", at(11))],
        {"holds": 1, "ill": 0}, {"holds": 1, "ill": 0}),
    "hold -> later other_code10": (   # the case where Overview and Live Today DIFFER
        [ADULT, item_record("B1", "PA", "Main", at(9)), item_record("B1", "PA", "Main", at(11), "100NNY")],
        {"holds": 1, "ill": 0}, {"holds": 0, "ill": 0}),
    "hold -> later message-64 record": (
        [ADULT, item_record("B1", "PA", "Main", at(9)), patron_record("PA", "ADULT", at(11))],
        {"holds": 1, "ill": 0}, {"holds": 1, "ill": 0}),
    "ILL hold -> later non-hold": (
        [ADULT, ILL_PATRON, item_record("B1", "PB", "Westside", at(9)), item_record("B1", "PB", "Westside", at(11), "101YNN")],
        {"holds": 0, "ill": 0}, {"holds": 0, "ill": 0}),
    "ILL hold -> later non-ILL hold": (
        [ADULT, ILL_PATRON, item_record("B1", "PB", "Westside", at(9)), item_record("B1", "PA", "Main", at(11))],
        {"holds": 1, "ill": 0}, {"holds": 1, "ill": 0}),
    "ILL hold (transit destination), nothing later": (
        [ADULT, ILL_PATRON, item_record("B1", "PB", "Westside", at(9))],
        {"holds": 0, "ill": 1}, {"holds": 0, "ill": 1}),
    "ILL hold (main destination), nothing later": (
        [ADULT, ILL_PATRON, item_record("B1", "PB", "Main", at(9))],
        {"holds": 0, "ill": 1}, {"holds": 0, "ill": 1}),
    "two items, one retracted": (
        [ADULT, item_record("B1", "PA", "Main", at(9)), item_record("B2", "PA", "Main", at(9, 5)),
         item_record("B1", "PA", "Main", at(10), "101YNN")],
        {"holds": 1, "ill": 0}, {"holds": 1, "ill": 0}),
}


@pytest.mark.parametrize("name", list(SEQUENCES))
def test_the_real_dashboard_still_behaves_as_the_investigation_found(name, monkeypatch):
    records, overview, live = SEQUENCES[name]

    actual_overview, actual_live = dashboard_overview(records), dashboard_live(records, monkeypatch)

    assert {k: actual_overview[k] for k in ("holds", "ill")} == overview
    assert {k: actual_live[k] for k in ("holds", "ill")} == live


@pytest.mark.parametrize("name", list(SEQUENCES))
def test_the_persisted_v2_stream_reproduces_both_dashboard_paths_exactly(db, monkeypatch, name):
    records, _overview, _live = SEQUENCES[name]
    events = to_v2_events(records)

    response = post_upload(upload(checkins=[], acs_items=events))

    assert response.status_code == 200
    rows = stream_rows(db)
    assert reduce_overview(rows) == dashboard_overview(records)   # every field: holds, ill, programming, collection, ill_main, ill_by_branch
    assert reduce_live(rows) == dashboard_live(records, monkeypatch)


def test_a_message_64_record_never_becomes_a_cloud_event(db):
    records, _o, _l = SEQUENCES["hold -> later message-64 record"]

    events = to_v2_events(records)
    assert post_upload(upload(checkins=[], acs_items=events)).status_code == 200

    assert len(records) == 3 and len(events) == 1  # two patron records and one item record: only the item record is an event
    assert [r["state"] for r in stream_rows(db)] == ["hold"]
    assert db.count("acs_item_events") == 1


def test_overview_ignores_other_code10_but_live_today_lets_it_retract_a_hold(db, monkeypatch):
    records, overview, live = SEQUENCES["hold -> later other_code10"]
    post_upload(upload(checkins=[], acs_items=to_v2_events(records)))
    rows = stream_rows(db)

    assert [r["state"] for r in rows] == ["hold", "other_code10"]
    assert reduce_overview(rows)["holds"] == overview["holds"] == 1
    assert reduce_live(rows)["holds"] == live["holds"] == 0


@pytest.mark.parametrize("first_day,last_day,expected_holds", [(0, 0, 1), (0, 2, 0), (2, 2, 0), (1, 1, 0)])
def test_overview_windows_see_the_retraction_only_when_both_records_are_inside_the_window(db, first_day, last_day, expected_holds):
    records = [ADULT, item_record("B1", "PA", "Main", at(9)), item_record("B1", "PA", "Main", at(9, day=2), "101YNN")]
    post_upload(upload(checkins=[], acs_items=to_v2_events(records)))

    dashboard = dashboard_overview(records, first_day, last_day)

    assert dashboard["holds"] == expected_holds
    assert reduce_overview(stream_rows(db), first_day, last_day) == dashboard


def test_ties_at_one_instant_resolve_to_the_last_event_sent(db):
    # Same item, same second. The dashboard's pandas sort is not guaranteed stable for exact ties; the v2 stream is: the greatest
    # id (send order, which is source-file order) wins. The later dashboard step relies on this rule.
    records = [ADULT, item_record("B1", "PA", "Main", at(9)), item_record("B1", "PA", "Main", at(9), "101YNN")]
    post_upload(upload(checkins=[], acs_items=to_v2_events(records)))

    assert reduce_overview(stream_rows(db))["holds"] == 0
    assert reduce_live(stream_rows(db))["holds"] == 0


def test_the_stream_holds_no_field_the_privacy_contract_forbids(db):
    records, _o, _l = SEQUENCES["ILL hold -> later non-ILL hold"]
    post_upload(upload(checkins=[], acs_items=to_v2_events(records)))

    with db.engine.connect() as conn:
        columns = list(conn.execute(text("SELECT * FROM acs_item_events")).keys())
        stored = " ".join(str(v) for row in conn.execute(text("SELECT * FROM acs_item_events")) for v in row)

    assert set(columns) == {"id", "customer_id", "branch_id", "key_id", "event_key", "event_time", "state", "item_key",
                            "destination", "is_ill", "is_branch_services", "is_collection_services", "ruleset_id", "received_at"}
    for canary in ("CANARY", "B1", "PA", "PB", "101YNY", "101YNN", "AJ", "AA", "example.invalid"):
        assert canary not in stored, canary  # no barcode, patron id, name, title, message code or raw SIP2 anywhere in the rows
