"""C6a R2 on a REAL PostgreSQL: replacement redemption as the RUNTIME ROLE, under row level security, with real locks.

docs/c6a-replacement-machine-design.md, 14.4a. The world is built through the real paths: the source is enrolled by the
ordinary Contract v2 enrollment over HTTP, the replacement is authorized and its code issued by R1's service. What only a
real server can prove:

  * the whole cutover commits as the non-owning, NOBYPASSRLS runtime role, with ingest_key_ids under row level security;
  * after commit the source's token and key are refused and the replacement's accepted, one active key, the cutover and
    every stored event unchanged; the audit row as stored (JSONB);
  * the corrected lock order (decision A): an upload or heartbeat from the source ALREADY HOLDING its token is waited for
    and is never deadlocked with -- the case the original 14.4 order deadlocked on;
  * every race of the test matrix ends in one consistent outcome with no deadlock;
  * a failure at any write, or a guarded write that changes no row, leaves everything as it was and the code unused;
  * lost-response recovery through R1's real authorization, without replaying a used code or a second active key.

OPT-IN AND SAFE BY CONSTRUCTION, as tests/test_collector_enrollment_v2_postgres.py, whose throwaway database and runtime
role this module reuses: runs only when SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION,
local server. Every value is SYNTHETIC.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_collector_enrollment_v2_postgres import (  # noqa: F401 -- `cluster` is a fixture
    _BUILTIN_SHA256_EXPR,
    _HASH_EXPR,
    ADMIN_URL,
    ORG_A_CREATED,
    ORG_B_CREATED,
    SETTINGS_A,
    SETTINGS_B,
    V2_KEYS,
    cluster,
)
from test_ingest_v2_api import checkin

import database
import main
from services import admin_installation_service as admin
from services import platform_admin_service
from services.admin_installation_service import InstallationAdminError
from src.services import auth_service as src_auth_service
from src.services import collector_enrollment_service as enrollment
from src.services.collector_enrollment_service import EnrollmentError, EnrollmentRefused
from src.services.ingest_v2_models import StatusV2Request, UploadV2Request
from src.services.ingest_v2_service import record_heartbeat, store_events

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL tests)")

ACTOR = {"id": 7, "email": "admin@example.invalid"}
SOURCE = 101
client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


@pytest.fixture
def world(cluster, monkeypatch):  # noqa: F811 -- the imported fixture, requested by name
    """lib-a (org 1, customer 10, branch 1, primary): installation 101 enrolled on Contract v2 over HTTP, Active, with
    one stored event; 102, a second Contract 2 installation at the branch; an authorized pending replacement of 101 with
    an unused code, by R1's service. lib-b (org 2, customer 11, branch 2): enrolled, and never touched."""
    monkeypatch.setenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED", "true")  # 14.11: these tests replace machines
    owner, runtime, role = cluster
    with owner.begin() as conn:
        conn.execute(text(
            "TRUNCATE auth_audit_log, tenant_lifecycle_events, v2_cutovers, ingest_key_ids, checkin_events, "
            "reject_events, acs_item_events, checkins, rejects, acs_events, collector_enrollment_codes, agent_tokens, "
            "collector_installations, organization_settings, branch_settings, pipeline_status, branches, organizations, "
            "customers, app_users RESTART IDENTITY CASCADE"
        ))
        conn.execute(text("INSERT INTO app_users (id, email, full_name, is_platform_admin) "
                          "VALUES (7, 'admin@example.invalid', 'Test Admin', TRUE)"))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Lib A'), (11, 'Lib B')"))
        conn.execute(text(
            "INSERT INTO organizations (id, slug, name, status, operational_customer_id, created_at) VALUES "
            "(1, 'lib-a', 'Lib A', 'active', 10, :a), (2, 'lib-b', 'Lib B', 'active', 11, :b)"
        ), {"a": ORG_A_CREATED, "b": ORG_B_CREATED})
        conn.execute(text(
            "INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) VALUES "
            "(1, 1, 'main', 'Main', TRUE, 'active', 1), (2, 2, 'main', 'Main', TRUE, 'active', 2)"
        ))
        conn.execute(text(
            "INSERT INTO organization_settings (organization_id, settings_json) VALUES "
            "(1, CAST(:a AS JSONB)), (2, CAST(:b AS JSONB))"
        ), {"a": json.dumps(SETTINGS_A), "b": json.dumps(SETTINGS_B)})
        conn.execute(text(
            "INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract) VALUES "
            "(101, 1, 1, 'Main Sorter', 'provisioning', 2), (102, 1, 1, 'Second Sorter', 'provisioning', 2), "
            "(201, 2, 2, 'Lib B Sorter', 'provisioning', 2)"
        ))
        conn.execute(text("SELECT setval('collector_installations_id_seq', 1000)"))
    monkeypatch.setattr(main, "engine", runtime)
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", True)
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR))
    monkeypatch.setattr(database, "_engine", runtime)  # R1's services run as the runtime role too

    source = _enroll(runtime, SOURCE)
    _enroll(runtime, 201)
    assert _status(source["agent_token"], source["key_id"]).status_code == 200  # 101: provisioning -> active
    assert _upload(source["agent_token"], source["key_id"]).status_code == 200
    replacement = admin.authorize_replacement(
        _library(), SOURCE, name="Replacement Sorter", confirm_lost_response=False, actor=ACTOR)["installation"]["id"]
    code = admin.issue_enrollment_code(_library(), replacement, actor=ACTOR)["enrollment_code"]
    return {"owner": owner, "runtime": runtime, "role": role, "source": source, "replacement": replacement,
            "code": code}


# --- helpers -----------------------------------------------------------------------------------------------------------

def _library(slug="lib-a"):
    return admin.get_library(slug)


def _enroll(runtime, installation_id):
    with runtime.begin() as conn:
        code = enrollment.create_enrollment_code(conn, installation_id)["enrollment_code"]
    response = client.post("/collector/enroll", json={"enrollment_code": code, "contract_version": 2})
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _upload(token, key_id, n=1):
    return client.post("/v2/upload", json={"contract_version": 2, "key_id": key_id, "checkins": [checkin(n)]},
                       headers=_bearer(token))


def _status(token, key_id):
    return client.post("/v2/status", json={"contract_version": 2, "key_id": key_id, "status": "healthy"},
                       headers=_bearer(token))


def _rows(engine, sql, **params):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def _state(owner):
    """Everything a redemption can write, the events and the audit log included, as the table owner sees it."""
    return {
        "codes": _rows(owner, "SELECT id, installation_id, used_at, revoked_at FROM collector_enrollment_codes ORDER BY id"),
        "tokens": _rows(owner, "SELECT id, installation_id, is_active FROM agent_tokens ORDER BY id"),
        "keys": _rows(owner, "SELECT key_id, installation_id, status, retired_at FROM ingest_key_ids ORDER BY id"),
        "installations": _rows(owner, "SELECT id, status FROM collector_installations ORDER BY id"),
        "cutovers": _rows(owner, "SELECT * FROM v2_cutovers ORDER BY id"),
        "events": _rows(owner, "SELECT * FROM checkin_events ORDER BY id"),
        "audit": _rows(owner, "SELECT event_type, metadata FROM auth_audit_log ORDER BY id"),
    }


def _active_keys(owner, customer_id=10):
    return [r["installation_id"] for r in _rows(
        owner, "SELECT installation_id FROM ingest_key_ids WHERE customer_id = :c AND status = 'active'", c=customer_id)]


def _redeem(runtime, code):
    with runtime.begin() as conn:
        return enrollment.redeem_enrollment_code(conn, code, hostname="NEW-PC", contract_version=2)


def _outcome(call):
    try:
        return ("ok", call())
    except Exception as exc:  # each race records what happened; a deadlock is asserted absent below
        return ("error", exc)


def _race(*calls):
    barrier = threading.Barrier(len(calls))

    def run(call):
        barrier.wait()
        return _outcome(call)

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        outcomes = list(pool.map(run, calls))
    for kind, value in outcomes:
        assert not (kind == "error" and "deadlock" in str(value).lower()), value
    return outcomes


def _wait_until_waiting_for_a_lock(owner, role, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waiting = _rows(owner, "SELECT count(*) AS n FROM pg_stat_activity WHERE usename = :r AND wait_event_type = 'Lock'",
                        r=role)[0]["n"]
        if waiting:
            return
        time.sleep(0.05)
    pytest.fail("the redemption never waited for the lock the request holds")


# =====================================================================================================================
# the cutover, as the runtime role under row level security
# =====================================================================================================================

def test_the_cutover_commits_as_the_runtime_role_under_row_level_security(world):
    owner, runtime, source = world["owner"], world["runtime"], world["source"]
    replacement = world["replacement"]
    with runtime.connect() as conn:  # no tenant context: row level security hides every key from the runtime role
        assert conn.execute(text("SELECT count(*) FROM ingest_key_ids")).scalar() == 0
    before = _state(owner)

    response = client.post("/collector/enroll", json={"enrollment_code": world["code"], "contract_version": 2})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == V2_KEYS and body["installation_id"] == replacement
    after = _state(owner)
    assert after["cutovers"] == before["cutovers"] and after["events"] == before["events"]
    assert {i["id"]: i["status"] for i in after["installations"]}[SOURCE] == "retired"
    assert {i["id"]: i["status"] for i in after["installations"]}[replacement] == "provisioning"
    assert [t["is_active"] for t in after["tokens"] if t["installation_id"] == SOURCE] == [False]
    keys = {k["key_id"]: k for k in after["keys"]}
    assert keys[source["key_id"]]["status"] == "retired" and keys[source["key_id"]]["retired_at"] is not None
    assert _active_keys(owner) == [replacement] and _active_keys(owner, 11) == [201]
    assert [k for k in after["keys"] if k["installation_id"] == 201] == [k for k in before["keys"] if k["installation_id"] == 201]
    (audit,) = _rows(owner, "SELECT user_id, email, is_success, metadata FROM auth_audit_log "
                            "WHERE event_type = 'collector_replacement_redeemed'")
    assert (audit["user_id"], audit["email"], audit["is_success"]) == (None, None, True)
    assert audit["metadata"]["authorized_by_user_id"] == 7 and audit["metadata"]["retired_key_id"] == source["key_id"]
    assert audit["metadata"]["key_id"] == body["key_id"] and audit["metadata"]["target_org_slug"] == "lib-a"
    assert body["agent_token"] not in json.dumps(audit["metadata"]) and world["code"] not in json.dumps(audit["metadata"])


def test_after_commit_the_source_is_refused_and_the_replacement_accepted(world):
    owner, source = world["owner"], world["source"]
    body = client.post("/collector/enroll", json={"enrollment_code": world["code"], "contract_version": 2}).json()
    events = _state(owner)["events"]

    assert _upload(source["agent_token"], source["key_id"], n=2).status_code == 403
    assert _status(source["agent_token"], source["key_id"]).status_code == 403
    assert _upload(body["agent_token"], source["key_id"], n=3).status_code == 403
    assert _state(owner)["events"] == events

    assert _upload(body["agent_token"], body["key_id"], n=4).status_code == 200
    assert _status(body["agent_token"], body["key_id"]).status_code == 200


# =====================================================================================================================
# decision A: a request from the source already holding its token is waited for, never deadlocked with
# =====================================================================================================================

def _redeem_in_background(runtime, code):
    result = {}

    def redeem():
        result.update(dict([_outcome(lambda: _redeem(runtime, code))]))

    thread = threading.Thread(target=redeem)
    thread.start()
    return thread, result


def test_an_upload_in_progress_holding_the_source_token_is_waited_for_not_deadlocked(world):
    """The upload has authenticated (its token row is locked) and checked its key; the redemption starts and must wait
    for the token. The upload then inserts events, which takes the branch FOR KEY SHARE: under the original 14.4 order
    the redemption would already hold the branch FOR UPDATE, and the two would deadlock."""
    owner, runtime, role, source = world["owner"], world["runtime"], world["role"], world["source"]
    payload = UploadV2Request.model_validate({"contract_version": 2, "key_id": source["key_id"], "checkins": [checkin(9)]})

    with runtime.connect() as upload:
        transaction = upload.begin()
        token_row = main.authenticate_agent_token(upload, f"Bearer {source['agent_token']}")
        main._require_ingest_key(upload, token_row, source["key_id"])
        thread, result = _redeem_in_background(runtime, world["code"])
        _wait_until_waiting_for_a_lock(owner, role)

        stored = store_events(upload, customer_id=10, branch_id=1, key_id=source["key_id"],
                              checkins=payload.checkins, rejects=payload.rejects, acs_items=payload.acs_items)
        transaction.commit()
    thread.join(timeout=30)

    assert stored["checkins_inserted"] == 1  # the request already past its checks finishes (the M2 limitation)
    assert "ok" in result, result.get("error")
    assert _active_keys(owner) == [world["replacement"]]
    # The event from before the commit is kept, under the source's key; nothing after it is accepted.
    assert len(_rows(owner, "SELECT 1 FROM checkin_events WHERE key_id = :k", k=source["key_id"])) == 2
    assert _upload(source["agent_token"], source["key_id"], n=10).status_code == 403


def test_a_heartbeat_in_progress_holding_the_source_token_is_waited_for_not_deadlocked(world):
    owner, runtime, role, source = world["owner"], world["runtime"], world["role"], world["source"]
    data = StatusV2Request.model_validate({"contract_version": 2, "key_id": source["key_id"], "status": "healthy"})

    with runtime.connect() as heartbeat:
        transaction = heartbeat.begin()
        token_row = main.authenticate_agent_token(heartbeat, f"Bearer {source['agent_token']}")
        main._require_ingest_key(heartbeat, token_row, source["key_id"])
        thread, result = _redeem_in_background(runtime, world["code"])
        _wait_until_waiting_for_a_lock(owner, role)

        assert record_heartbeat(heartbeat, customer_id=10, branch_id=1, data=data)  # the key row
        main.record_installation_heartbeat(heartbeat, token_row, SOURCE, None)    # then the installation
        transaction.commit()
    thread.join(timeout=30)

    assert "ok" in result, result.get("error")
    assert _rows(owner, "SELECT status FROM collector_installations WHERE id = 101") == [{"status": "retired"}]
    assert _active_keys(owner) == [world["replacement"]]


# =====================================================================================================================
# races: one consistent outcome, no deadlock
# =====================================================================================================================

def test_two_redemptions_of_one_code_redeem_it_once(world):
    owner, runtime = world["owner"], world["runtime"]

    outcomes = _race(lambda: _redeem(runtime, world["code"]), lambda: _redeem(runtime, world["code"]))

    assert sorted(kind for kind, _ in outcomes) == ["error", "ok"]
    (error,) = [value for kind, value in outcomes if kind == "error"]
    assert isinstance(error, EnrollmentError) and error.reason == "already_used"
    assert _active_keys(owner) == [world["replacement"]]


def test_redemption_racing_cancellation_ends_one_way_or_the_other(world):
    owner, runtime, replacement = world["owner"], world["runtime"], world["replacement"]

    redeemed, cancelled = _race(lambda: _redeem(runtime, world["code"]),
                                lambda: admin.cancel_replacement(_library(), replacement, actor=ACTOR))

    if redeemed[0] == "ok":
        assert isinstance(cancelled[1], InstallationAdminError) and cancelled[1].code == "not_a_pending_replacement"
        assert _active_keys(owner) == [replacement]
    else:
        assert cancelled[0] == "ok" and isinstance(redeemed[1], EnrollmentError)
        assert _active_keys(owner) == [SOURCE]


def test_redemption_racing_a_recovery_token_for_the_source_leaves_the_source_with_no_live_token(world):
    owner, runtime = world["owner"], world["runtime"]

    redeemed, recovered = _race(lambda: _redeem(runtime, world["code"]),
                                lambda: admin.issue_recovery_token(_library(), SOURCE, actor=ACTOR))

    assert redeemed[0] == "ok"
    assert recovered[0] == "ok" or isinstance(recovered[1], InstallationAdminError)
    assert _rows(owner, "SELECT count(*) AS n FROM agent_tokens WHERE installation_id = 101 AND is_active") == [{"n": 0}]
    assert _active_keys(owner) == [world["replacement"]]


def test_redemption_racing_offboarding_leaves_nothing_live(world):
    owner, runtime = world["owner"], world["runtime"]

    _race(lambda: _redeem(runtime, world["code"]),
          lambda: platform_admin_service.offboard_library(1, "lib-a", actor_user_id=7, actor_label="test"))

    assert _rows(owner, "SELECT status FROM organizations WHERE id = 1") == [{"status": "cancelled"}]
    assert _rows(owner, "SELECT count(*) AS n FROM agent_tokens WHERE customer_id = 10 AND is_active") == [{"n": 0}]
    assert _active_keys(owner) == []
    assert _active_keys(owner, 11) == [201]


@pytest.mark.parametrize("status", ["inactive", "retired"])
def test_redemption_racing_a_status_change_of_the_source(world, status):
    owner, runtime = world["owner"], world["runtime"]

    redeemed, _changed = _race(lambda: _redeem(runtime, world["code"]),
                               lambda: admin.change_status(_library(), SOURCE, status=status, actor=ACTOR))

    assert redeemed[0] == "ok"  # deactivation leaves an eligible source; retiring a key owner is refused
    assert _rows(owner, "SELECT status FROM collector_installations WHERE id = 101") == [{"status": "retired"}]
    assert _active_keys(owner) == [world["replacement"]]


def test_redemption_racing_deactivation_of_the_replacement(world):
    owner, runtime, replacement = world["owner"], world["runtime"], world["replacement"]

    redeemed, deactivated = _race(lambda: _redeem(runtime, world["code"]),
                                  lambda: admin.change_status(_library(), replacement, status="inactive", actor=ACTOR))

    assert deactivated[0] == "ok"
    if redeemed[0] == "ok":
        assert _active_keys(owner) == [replacement]
    else:
        assert isinstance(redeemed[1], EnrollmentError) and _active_keys(owner) == [SOURCE]


def test_redemption_racing_an_ordinary_enrollment_at_the_branch(world):
    owner, runtime = world["owner"], world["runtime"]
    with runtime.begin() as conn:  # a code R1's administration would refuse; the enrollment must refuse it anyway
        ordinary = enrollment.create_enrollment_code(conn, 102)["enrollment_code"]

    redeemed, enrolled = _race(lambda: _redeem(runtime, world["code"]), lambda: _redeem(runtime, ordinary))

    assert redeemed[0] == "ok"
    assert isinstance(enrolled[1], EnrollmentRefused) and enrolled[1].reason == "active_ingest_key_exists"
    assert _active_keys(owner) == [world["replacement"]]


def test_redemption_racing_the_revocation_of_an_unbound_token(world):
    owner, runtime = world["owner"], world["runtime"]
    with owner.begin() as conn:
        conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active) "
                          "VALUES ('synthetic-unbound-hash', 10, 1, 'legacy', TRUE)"))
    (token,) = admin.list_unbound_tokens(_library())

    redeemed, revoked = _race(lambda: _redeem(runtime, world["code"]),
                              lambda: admin.revoke_unbound_token(_library(), token["id"], actor=ACTOR))

    assert revoked[0] == "ok"
    if redeemed[0] == "ok":
        assert _active_keys(owner) == [world["replacement"]]
    else:
        assert isinstance(redeemed[1], EnrollmentRefused) and redeemed[1].reason == "unbound_tokens_active"
        assert _active_keys(owner) == [SOURCE]
        assert _redeem(runtime, world["code"])["installation_id"] == world["replacement"]  # the code was not used


# =====================================================================================================================
# one transaction
# =====================================================================================================================

def _fail(*_args, **_kwargs):
    raise RuntimeError("injected failure")


@pytest.mark.parametrize("target, name", [
    (enrollment, "_claim_code"),
    (enrollment, "issue_ingest_key"),
    (enrollment, "_insert_bound_token"),
    (src_auth_service, "log_auth_event_with_connection"),
])
def test_a_failure_at_any_write_rolls_everything_back_and_the_code_stays_unused(world, monkeypatch, target, name):
    owner, runtime = world["owner"], world["runtime"]
    before = _state(owner)
    original = getattr(target, name)
    monkeypatch.setattr(target, name, _fail)

    with pytest.raises(RuntimeError, match="injected"):
        _redeem(runtime, world["code"])

    assert _state(owner) == before
    monkeypatch.setattr(target, name, original)
    assert _redeem(runtime, world["code"])["installation_id"] == world["replacement"]


@pytest.mark.parametrize("statement", ["_RETIRE_SOURCE_SQL", "_RETIRE_SOURCE_KEY_SQL"])
def test_a_guarded_write_that_changes_no_row_rolls_everything_back(world, monkeypatch, statement):
    owner, runtime = world["owner"], world["runtime"]
    before = _state(owner)
    monkeypatch.setattr(enrollment, statement, getattr(enrollment, statement) + "  AND 1 = 0")

    with pytest.raises(enrollment.ReplacementCutoverError):
        _redeem(runtime, world["code"])

    assert _state(owner) == before


def test_a_suspended_library_consumes_no_code_and_changes_no_credential(world):
    owner, runtime = world["owner"], world["runtime"]
    with owner.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE id = 1"))
    before = _state(owner)

    with pytest.raises(EnrollmentError) as refused:
        _redeem(runtime, world["code"])

    assert refused.value.reason == "organization_status_suspended"
    assert _state(owner) == before


# =====================================================================================================================
# lost response, through R1's real authorization
# =====================================================================================================================

def test_a_lost_response_is_recovered_without_replaying_the_code_or_a_second_active_key(world):
    owner, runtime, lost_installation = world["owner"], world["runtime"], world["replacement"]
    lost = _redeem(runtime, world["code"])  # the computer never received this
    with pytest.raises(EnrollmentError):
        _redeem(runtime, world["code"])  # a used code is never redeemed again

    with pytest.raises(InstallationAdminError) as unconfirmed:
        admin.authorize_replacement(_library(), lost_installation, name="Third", confirm_lost_response=False, actor=ACTOR)
    assert unconfirmed.value.code == "lost_response_confirmation_required"
    third = admin.authorize_replacement(
        _library(), lost_installation, name="Third", confirm_lost_response=True, actor=ACTOR)["installation"]["id"]
    recovered = _redeem(runtime, admin.issue_enrollment_code(_library(), third, actor=ACTOR)["enrollment_code"])

    assert recovered["key_id"] not in (lost["key_id"], world["source"]["key_id"])
    assert recovered["agent_token"] not in (lost["agent_token"], world["source"]["agent_token"])
    assert _active_keys(owner) == [third]
    assert _rows(owner, "SELECT count(*) AS n FROM v2_cutovers WHERE customer_id = 10") == [{"n": 1}]
    assert _upload(lost["agent_token"], lost["key_id"]).status_code == 403
    assert _upload(recovered["agent_token"], recovered["key_id"]).status_code == 200


# =====================================================================================================================
# machine replacement turned off (14.11)
# =====================================================================================================================

def test_while_replacement_is_off_redemption_is_refused_unused_and_the_source_runs_on(world, monkeypatch):
    owner, runtime, source = world["owner"], world["runtime"], world["source"]
    before = _state(owner)
    monkeypatch.delenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED")

    with pytest.raises(EnrollmentRefused) as refused:
        _redeem(runtime, world["code"])
    response = client.post("/collector/enroll", json={"enrollment_code": world["code"], "contract_version": 2})

    assert (refused.value.code, refused.value.reason) == ("enrollment_not_ready", "replacement_disabled")
    assert response.status_code == 409 and response.json()["code"] == "enrollment_not_ready"
    assert _state(owner) == before  # the code unused; no token, key, installation, cutover, event or audit row changed
    assert _status(source["agent_token"], source["key_id"]).status_code == 200
    assert _upload(source["agent_token"], source["key_id"], n=7).status_code == 200

    monkeypatch.setenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED", "true")
    assert _redeem(runtime, world["code"])["installation_id"] == world["replacement"]
