"""C6a R2: redeeming a REPLACEMENT installation's enrollment code (docs/c6a-replacement-machine-design.md, 14.4a).

Runs the real service SQL and the real endpoints against SQLite, as tests/test_collector_enrollment_v2.py does for an
ordinary Contract v2 enrollment (whose tests are the proof that it is unchanged). The replacement is seeded as R1's
authorization leaves it: a `provisioning` Contract 2 installation naming its source, with write-once attribution. What
needs a real server -- the lock order, row level security, the runtime role and true concurrency -- is in
tests/test_replacement_redemption_postgres.py.

Every value is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool
from test_collector_enrollment_v2 import SETTINGS_A, V2_KEYS
from test_ingest_v2_api import _V1_DDL, _V2_DDL, checkin, v1_payload

import main
from src.services import auth_service
from src.services import collector_enrollment_service as enrollment
from src.services.collector_enrollment_service import EnrollmentError, EnrollmentRefused

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = {"detail": "Invalid or expired enrollment code"}
_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
SOURCE, REPLACEMENT, AUTHORIZER = 101, 501, 7

client = TestClient(main.app)

_DDL = [
    "CREATE TABLE customers (id INTEGER PRIMARY KEY)",
    ("CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT, status TEXT, operational_customer_id INTEGER, "
     "created_at TEXT)"),
    ("CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, slug TEXT, is_primary BOOLEAN NOT NULL "
     "DEFAULT 1, status TEXT, operational_branch_id INTEGER, onboarding_status TEXT NOT NULL DEFAULT 'pending', "
     "onboarding_message TEXT, onboarding_updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"),
    "CREATE TABLE organization_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER UNIQUE, settings_json TEXT)",
    "CREATE TABLE branch_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id INTEGER UNIQUE, settings_json TEXT)",
    """CREATE TABLE collector_installations (
        id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER, name TEXT, hostname TEXT,
        collector_version TEXT, status TEXT, installed_at TEXT, last_seen_at TEXT, created_at TEXT, updated_at TEXT,
        required_contract INTEGER NOT NULL DEFAULT 1, replaces_installation_id INTEGER,
        replacement_authorized_by_user_id INTEGER, replacement_authorized_at TEXT)""",
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
    """CREATE TABLE auth_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, email TEXT, event_type TEXT NOT NULL,
        is_success BOOLEAN NOT NULL, message TEXT, metadata TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    *_V1_DDL, *_V2_DDL,
]


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED", "true")  # 14.11: these tests replace machines
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _register_sha256(dbapi_connection, _record):
        dbapi_connection.create_function("sha256hex", 1, lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest())

    with engine.begin() as conn:
        for statement in _DDL:
            conn.execute(text(statement))
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", True)
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, "sha256hex(:token)"))
    return engine


def _run(engine, sql, **params):
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _table(engine, name):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(f"SELECT * FROM {name} ORDER BY 1")).mappings().all()]  # nosec B608


def _row(engine, name, row_id):
    (found,) = [r for r in _table(engine, name) if r["id"] == row_id]
    return found


def _written(engine) -> dict:
    """Everything a redemption can write, the audit log and the events included."""
    return {name: _table(engine, name) for name in (
        "collector_enrollment_codes", "agent_tokens", "ingest_key_ids", "v2_cutovers", "collector_installations",
        "auth_audit_log", "checkin_events",
    )}


def _code(engine, installation_id) -> str:
    with engine.begin() as conn:
        return enrollment.create_enrollment_code(conn, installation_id)["enrollment_code"]


def _redeem(engine, code, **kwargs):
    with engine.begin() as conn:
        return enrollment.redeem_enrollment_code(conn, code, **kwargs)


def _post(code, **body):
    return client.post("/collector/enroll", json={"enrollment_code": code, "contract_version": 2, **body})


def _authorize(engine, replacement_id, source_id, *, status="provisioning"):
    """A replacement as R1's authorization leaves it."""
    _run(engine,
         "INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract, "
         "replaces_installation_id, replacement_authorized_by_user_id, replacement_authorized_at) "
         "VALUES (:id, 1, 1, :name, :status, 2, :source, :by, '2026-11-12T10:00:00+00:00')",
         id=replacement_id, name=f"Replacement {replacement_id}", status=status, source=source_id, by=AUTHORIZER)


def _active_keys(engine):
    return sorted((k for k in _table(engine, "ingest_key_ids") if k["status"] == "active"), key=lambda k: k["customer_id"])


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _upload(token, key_id, n=1):
    return client.post("/v2/upload", json={"contract_version": 2, "key_id": key_id, "checkins": [checkin(n)]},
                       headers=_bearer(token))


def _status(token, key_id):
    return client.post("/v2/status", json={"contract_version": 2, "key_id": key_id, "status": "healthy"},
                       headers=_bearer(token))


@pytest.fixture
def world(db):
    """org 1 "lib-a" / customer 10 / branch 1 "main": installation 101 enrolled on Contract v2 through the ordinary
    enrollment (its token, its key, the branch's cutover), active, with one stored event; and 501, an authorized,
    pending replacement of 101, with an unused code. org 2 / customer 11 / branch 2: another library, untouched."""
    for org, customer, branch in ((1, 10, 1), (2, 11, 2)):
        _run(db, "INSERT INTO customers VALUES (:c)", c=customer)
        _run(db, "INSERT INTO organizations VALUES (:o, :slug, 'active', :c, '2025-03-04T05:06:07+00:00')",
             o=org, slug=f"lib-{'ab'[org - 1]}", c=customer)
        _run(db, "INSERT INTO branches (id, organization_id, slug, status, operational_branch_id) "
                 "VALUES (:b, :o, 'main', 'active', :b)", b=branch, o=org)
        _run(db, "INSERT INTO organization_settings (organization_id, settings_json) VALUES (:o, :j)",
             o=org, j=json.dumps(SETTINGS_A))
    _run(db, "INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract) "
             "VALUES (101, 1, 1, 'Sorter', 'provisioning', 2), (201, 2, 2, 'Other Sorter', 'provisioning', 2)")
    source = _post(_code(db, SOURCE)).json()
    other = _post(_code(db, 201)).json()
    assert _status(source["agent_token"], source["key_id"]).status_code == 200  # 101: provisioning -> active
    assert _upload(source["agent_token"], source["key_id"]).status_code == 200  # one event, from the source
    _authorize(db, REPLACEMENT, SOURCE)
    code = _code(db, REPLACEMENT)
    return {"db": db, "source": source, "other": other, "code": code}


# =====================================================================================================================
# a successful replacement
# =====================================================================================================================

def test_redemption_moves_the_branchs_identity_to_the_replacement_and_changes_nothing_else(world):
    db, source = world["db"], world["source"]
    before = _written(db)

    response = _post(world["code"], hostname="NEW-PC", collector_version="1.0.14")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == V2_KEYS
    assert (body["customer_id"], body["branch_id"], body["installation_id"]) == (10, 1, REPLACEMENT)
    assert body["contract_version"] == 2 and body["contract_mode"] == "v2" and body["timezone"] == "America/New_York"
    assert body["key_id"] != source["key_id"] and body["agent_token"] != source["agent_token"]

    # The source: retired, its token inactive, its key retired. The replacement: still provisioning, owns the new key.
    assert _row(db, "collector_installations", SOURCE)["status"] == "retired"
    assert _row(db, "collector_installations", REPLACEMENT)["status"] == "provisioning"
    tokens = {t["installation_id"]: t for t in _table(db, "agent_tokens") if t["customer_id"] == 10}
    assert tokens[SOURCE]["is_active"] in (0, False) and tokens[REPLACEMENT]["is_active"] in (1, True)
    keys = {k["key_id"]: k for k in _table(db, "ingest_key_ids") if k["customer_id"] == 10}
    assert keys[source["key_id"]]["status"] == "retired" and keys[source["key_id"]]["retired_at"] is not None
    assert (keys[body["key_id"]]["status"], keys[body["key_id"]]["installation_id"]) == ("active", REPLACEMENT)
    assert [(k["customer_id"], k["installation_id"]) for k in _active_keys(db)] == [(10, REPLACEMENT), (11, 201)]

    # Never written: the cutover, the events, the other library.
    after = _written(db)
    assert after["v2_cutovers"] == before["v2_cutovers"]
    assert after["checkin_events"] == before["checkin_events"]
    for table in ("agent_tokens", "ingest_key_ids"):
        assert [r for r in after[table] if r["customer_id"] == 11] == [r for r in before[table] if r["customer_id"] == 11]
    assert [i for i in after["collector_installations"] if i["organization_id"] == 2] == [
        i for i in before["collector_installations"] if i["organization_id"] == 2]
    assert [c["used_at"] is not None for c in after["collector_enrollment_codes"]] == [True, True, True]


def test_the_redemption_is_audited_as_a_system_action_naming_the_authorizer_and_ids_only(world, caplog, monkeypatch):
    """SQLite cannot hold the JSONB the audit insert casts to, so the call is recorded here; the stored row is checked
    on PostgreSQL."""
    db, source = world["db"], world["source"]
    caplog.set_level(logging.INFO)
    calls = []
    original = auth_service.log_auth_event_with_connection

    def recording(conn, **kwargs):
        calls.append(kwargs)
        return original(conn, **kwargs)

    monkeypatch.setattr(auth_service, "log_auth_event_with_connection", recording)

    body = _post(world["code"]).json()

    (audit,) = _table(db, "auth_audit_log")
    (call,) = calls
    source_token_id = next(t["id"] for t in _table(db, "agent_tokens") if t["installation_id"] == SOURCE)
    (code_id,) = [c["id"] for c in _table(db, "collector_enrollment_codes") if c["installation_id"] == REPLACEMENT]
    assert (audit["user_id"], audit["email"], audit["event_type"], audit["is_success"]) == (
        None, None, "collector_replacement_redeemed", 1)
    assert "user_id" not in call and "email" not in call  # a system action: both NULL
    audit["metadata"] = json.dumps(call["metadata"])
    assert json.loads(audit["metadata"]) == {
        "target_org_slug": "lib-a", "branch_slug": "main", "installation_id": REPLACEMENT,
        "replaces_installation_id": SOURCE, "authorized_by_user_id": AUTHORIZER, "retired_key_id": source["key_id"],
        "key_id": body["key_id"], "enrollment_code_id": code_id, "deactivated_token_ids": [source_token_id],
    }
    # No secret anywhere: not the code, not either token, not a hash.
    token_hash = hashlib.sha256(body["agent_token"].encode()).hexdigest()
    written = audit["metadata"] + caplog.text
    for secret in (world["code"], body["agent_token"], source["agent_token"], token_hash):
        assert secret not in written


def test_the_response_is_exactly_the_field_set_the_setup_engine_accepts(world):
    script = (ROOT / "collector" / "deploy" / "setup-collector.ps1").read_text(encoding="utf-8")
    base = re.search(r'\$script:ExpectedResponseKeys = @\(([^)]*)\)', script).group(1)
    extra = re.search(r'\$script:V2ResponseKeys = \$script:ExpectedResponseKeys \+ @\(([^)]*)\)', script).group(1)
    accepted = set(re.findall(r'"([a-z_]+)"', base)) | set(re.findall(r'"([a-z_]+)"', extra))

    body = _post(world["code"]).json()

    assert len(accepted) == 9 and set(body) == accepted


# =====================================================================================================================
# the source's credentials stop working; the replacement's work
# =====================================================================================================================

def test_after_cutover_the_source_is_refused_everywhere_and_the_replacement_is_accepted(world):
    db, source = world["db"], world["source"]
    body = _post(world["code"]).json()
    events_before = _table(db, "checkin_events")

    # The source's token (inactive) on both v2 routes and the v1 heartbeat.
    assert _upload(source["agent_token"], source["key_id"], n=2).status_code == 403
    assert _status(source["agent_token"], source["key_id"]).status_code == 403
    assert client.post("/upload", json=v1_payload(customer=10, branch=1),
                       headers=_bearer(source["agent_token"])).status_code == 403
    # The source's (retired) key, even with the replacement's own token.
    assert _upload(body["agent_token"], source["key_id"], n=3).status_code == 403
    assert _table(db, "checkin_events") == events_before

    # The replacement: its token and key work, and its first contact makes it Active.
    assert _upload(body["agent_token"], body["key_id"], n=4).status_code == 200
    assert _status(body["agent_token"], body["key_id"]).status_code == 200
    assert _row(db, "collector_installations", REPLACEMENT)["status"] == "active"
    # The branch's v1 upload stays closed: the cutover was never moved.
    v1 = client.post("/upload", json=v1_payload(customer=10, branch=1), headers=_bearer(body["agent_token"]))
    assert v1.status_code == 403 and v1.json() == {"detail": main.V1_UPLOAD_CLOSED_DETAIL}


# =====================================================================================================================
# refusals: enrollment_not_ready, nothing consumed
# =====================================================================================================================

def _refused_with(db, code, reason):
    before = _written(db)
    with pytest.raises(EnrollmentRefused) as refused:
        _redeem(db, code, contract_version=2)
    assert (refused.value.code, refused.value.reason) == ("enrollment_not_ready", reason)
    assert _written(db) == before  # the code unused, nothing else changed
    response = _post(code)
    assert response.status_code == 409 and response.json()["code"] == "enrollment_not_ready"
    assert _written(db) == before


@pytest.mark.parametrize("change, reason", [
    ("UPDATE collector_installations SET status = 'retired' WHERE id = 101", "replacement_source_retired"),
    ("UPDATE collector_installations SET required_contract = 1 WHERE id = 101", "replacement_source_not_contract_2"),
    ("UPDATE ingest_key_ids SET status = 'retired' WHERE installation_id = 101", "replacement_source_not_key_owner"),
    (("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, status) "
      "VALUES ('4e5f6071-8293-4a4b-8d5e-6f7a8b9cadb4', 10, 1, 'active')"), "replacement_source_not_key_owner"),
    ("DELETE FROM v2_cutovers WHERE customer_id = 10", "replacement_branch_not_enrolled"),
    ("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, is_active) VALUES ('h', 10, 1, 1)",
     "unbound_tokens_active"),
    ("UPDATE branches SET is_primary = 0 WHERE id = 1", "no_active_primary_branch"),
    (("INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract, "
      "replaces_installation_id, replacement_authorized_by_user_id, replacement_authorized_at) "
      "VALUES (502, 1, 1, 'Second', 'inactive', 2, 101, 8, '2026-11-12')"), "replacement_already_pending"),
])
def test_each_eligibility_failure_is_enrollment_not_ready_and_consumes_nothing(world, change, reason):
    _run(world["db"], change)

    _refused_with(world["db"], world["code"], reason)


@pytest.mark.parametrize("status", ["provisioning", "active", "inactive"])
def test_a_source_in_any_enrolled_status_is_replaced(world, status):
    db = world["db"]
    _run(db, "UPDATE collector_installations SET status = :s WHERE id = 101", s=status)

    assert _post(world["code"]).status_code == 200
    assert _row(db, "collector_installations", SOURCE)["status"] == "retired"


def test_an_installation_that_is_no_longer_the_source_s_only_key_owner_is_never_retired(world):
    db = world["db"]
    _run(db, "UPDATE ingest_key_ids SET installation_id = 201 WHERE installation_id = 101")  # forged: another owner

    _refused_with(db, world["code"], "replacement_source_not_key_owner")


def test_an_old_installer_is_refused_without_consuming_the_code(world):
    db = world["db"]
    before = _written(db)

    response = client.post("/collector/enroll", json={"enrollment_code": world["code"]})

    assert response.status_code == 409 and response.json()["code"] == "collector_upgrade_required"
    assert _written(db) == before


def test_a_suspended_library_is_refused_generically_consuming_nothing_and_changing_no_credential(world):
    db = world["db"]
    _run(db, "UPDATE organizations SET status = 'suspended' WHERE id = 1")
    before = _written(db)

    with pytest.raises(EnrollmentError) as refused:
        _redeem(db, world["code"], contract_version=2)
    response = _post(world["code"])

    assert refused.value.reason == "organization_status_suspended"
    assert response.status_code == 400 and response.json() == PUBLIC
    assert _written(db) == before


@pytest.mark.parametrize("status", ["inactive", "retired"])
def test_a_replacement_that_is_no_longer_enrollable_is_refused_generically(world, status):
    db = world["db"]
    _run(db, "UPDATE collector_installations SET status = :s WHERE id = 501", s=status)
    before = _written(db)

    assert _post(world["code"]).status_code == 400
    assert _written(db) == before


def test_a_used_replacement_code_is_never_redeemed_again(world):
    db = world["db"]
    first = _post(world["code"])
    after_first = _written(db)

    second = _post(world["code"])

    assert first.status_code == 200
    assert second.status_code == 400 and second.json() == PUBLIC
    assert _written(db) == after_first


# =====================================================================================================================
# lost responses and chains (14.6)
# =====================================================================================================================

def test_a_lost_response_is_recovered_by_replacing_the_unconfirmed_replacement(world):
    """501 is redeemed but its computer never got the response: it is provisioning, owns the only key, holds nothing.
    Support authorizes 502 to replace 501 (R1 asks for the lost-response confirmation). The used code is not replayed,
    no secret is reused, and the branch never has two active keys."""
    db = world["db"]
    lost = _post(world["code"]).json()  # the response the computer never received
    assert _row(db, "collector_installations", REPLACEMENT)["status"] == "provisioning"
    assert _post(world["code"]).status_code == 400  # replaying the used code: refused

    _authorize(db, 502, REPLACEMENT)
    recovered = _post(_code(db, 502)).json()

    assert recovered["installation_id"] == 502
    assert len({world["source"]["key_id"], lost["key_id"], recovered["key_id"]}) == 3
    assert len({world["source"]["agent_token"], lost["agent_token"], recovered["agent_token"]}) == 3
    assert [(k["customer_id"], k["installation_id"]) for k in _active_keys(db)] == [(10, 502), (11, 201)]
    assert [_row(db, "collector_installations", i)["status"] for i in (SOURCE, REPLACEMENT, 502)] == [
        "retired", "retired", "provisioning"]
    assert _upload(lost["agent_token"], lost["key_id"]).status_code == 403
    assert _upload(recovered["agent_token"], recovered["key_id"]).status_code == 200
    assert len(_table(db, "v2_cutovers")) == 2  # lib-a's and lib-b's, never another


def test_a_chain_of_replacements_keeps_one_active_key_and_one_cutover(world):
    db = world["db"]
    second = _post(world["code"]).json()
    assert _status(second["agent_token"], second["key_id"]).status_code == 200  # 501 becomes Active
    _authorize(db, 502, REPLACEMENT)
    third = _post(_code(db, 502)).json()

    assert [(k["customer_id"], k["installation_id"]) for k in _active_keys(db)] == [(10, 502), (11, 201)]
    retired = [k["installation_id"] for k in _table(db, "ingest_key_ids") if k["status"] == "retired"]
    assert sorted(retired) == [SOURCE, REPLACEMENT]
    assert _upload(second["agent_token"], second["key_id"]).status_code == 403
    assert _upload(third["agent_token"], third["key_id"]).status_code == 200
    assert [a["event_type"] for a in _table(db, "auth_audit_log")] == ["collector_replacement_redeemed"] * 2


# =====================================================================================================================
# one transaction: any failure after the first write leaves everything as it was, the code unused
# =====================================================================================================================

def _fail(*_args, **_kwargs):
    raise RuntimeError("injected failure")


@pytest.mark.parametrize("target, name", [
    (enrollment, "_claim_code"),
    (enrollment, "issue_ingest_key"),
    (enrollment, "_insert_bound_token"),
    (auth_service, "log_auth_event_with_connection"),
])
def test_a_failure_at_any_write_rolls_the_whole_replacement_back(world, monkeypatch, target, name):
    db = world["db"]
    before = _written(db)
    monkeypatch.setattr(target, name, _fail)

    response = _post(world["code"])

    assert response.status_code == 500 and response.json() == {"detail": "Internal server error"}
    assert _written(db) == before


@pytest.mark.parametrize("statement", ["_RETIRE_SOURCE_SQL", "_RETIRE_SOURCE_KEY_SQL"])
def test_a_guarded_write_that_changes_no_row_is_an_internal_error_not_a_refusal_and_rolls_back(
        world, monkeypatch, statement):
    db = world["db"]
    before = _written(db)
    monkeypatch.setattr(enrollment, statement, getattr(enrollment, statement) + "  AND 1 = 0")

    with pytest.raises(enrollment.ReplacementCutoverError):
        _redeem(db, world["code"], contract_version=2)
    response = _post(world["code"])

    assert response.status_code == 500
    assert _written(db) == before


# =====================================================================================================================
# an ordinary code is untouched by all of this
# =====================================================================================================================

def test_an_unlinked_code_on_an_enrolled_branch_is_still_refused_as_before(world):
    db = world["db"]
    _run(db, "INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract) "
             "VALUES (102, 1, 1, 'Second machine', 'provisioning', 2)")
    code = _code(db, 102)
    before = _written(db)

    with pytest.raises(EnrollmentRefused) as refused:
        _redeem(db, code, contract_version=2)

    assert (refused.value.code, refused.value.reason) == ("enrollment_not_ready", "active_ingest_key_exists")
    assert _written(db) == before


# =====================================================================================================================
# machine replacement turned off (14.11): a replacement's code is refused unused; nothing else is affected
# =====================================================================================================================

def test_while_replacement_is_off_a_replacement_code_is_refused_unused_and_works_once_it_is_on(world, monkeypatch):
    monkeypatch.delenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED")

    _refused_with(world["db"], world["code"], "replacement_disabled")

    monkeypatch.setenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED", "true")
    assert _post(world["code"]).json()["installation_id"] == REPLACEMENT


def test_while_replacement_is_off_ordinary_enrollment_uploads_and_heartbeats_are_unaffected(world, monkeypatch):
    db, source = world["db"], world["source"]
    monkeypatch.delenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED")
    _run(db, "INSERT INTO customers VALUES (12)")
    _run(db, "INSERT INTO organizations VALUES (3, 'lib-c', 'active', 12, '2025-03-04T05:06:07+00:00')")
    _run(db, "INSERT INTO branches (id, organization_id, slug, status, operational_branch_id) "
             "VALUES (3, 3, 'main', 'active', 3)")
    _run(db, "INSERT INTO organization_settings (organization_id, settings_json) VALUES (3, :j)", j=json.dumps(SETTINGS_A))
    _run(db, "INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract) "
             "VALUES (301, 3, 3, 'Third Sorter', 'provisioning', 2)")

    enrolled = _post(_code(db, 301))

    assert enrolled.status_code == 200 and enrolled.json()["installation_id"] == 301
    third = enrolled.json()
    assert _status(third["agent_token"], third["key_id"]).status_code == 200
    assert _upload(third["agent_token"], third["key_id"], n=2).status_code == 200
    # The source of the pending replacement runs on.
    assert _status(source["agent_token"], source["key_id"]).status_code == 200
    assert _upload(source["agent_token"], source["key_id"], n=3).status_code == 200
