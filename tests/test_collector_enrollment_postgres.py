"""Collector enrollment against a REAL PostgreSQL: the real migration chain,
real row locking and real concurrent transactions.

SQLite (tests/test_collector_enrollment.py) covers the logic and SQL; it cannot
prove `SELECT ... FOR UPDATE` serializes two redemptions, that a loser waits for
the winner's COMMIT, that ON DELETE CASCADE / the partial indexes / the UNIQUE
constraint behave, or that the timestamptz comparisons work. Those need a real
server.

OPT-IN AND SAFE BY CONSTRUCTION. The tests run only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION
server the tests may create and drop databases on, e.g.

    SORTVIEW_TEST_POSTGRES_URL=postgresql://postgres:@127.0.0.1:5432/postgres

They never touch that database's own contents: each run creates a brand-new
throwaway database (sortview_enroll_test_<random>), applies the project's real
Alembic migrations to it (in a subprocess, so Alembic's logging setup cannot
disturb the rest of the test session), and drops it afterwards. The URL's host
must be local (localhost / 127.0.0.1 / ::1) unless
SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also set, so a production URL left in an
environment variable cannot be used by accident.
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
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

import main
from src.services import collector_enrollment_service as enrollment

ROOT_DIR = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL enrollment tests)"
)

_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
# pgcrypto's digest() is not present on every throwaway server; PostgreSQL 11+'s
# built-in sha256() computes the same hex digest.
_BUILTIN_SHA256_EXPR = "encode(sha256(convert_to(:token, 'UTF8')), 'hex')"
PUBLIC = {"detail": "Invalid or expired enrollment code"}

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


# --- a throwaway, fully migrated database ------------------------------------------------

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
    name = f"sortview_enroll_test_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    test_url = admin.set(database=name)
    env = {**os.environ, "DATABASE_URL": test_url.render_as_string(hide_password=False)}
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT_DIR, env=env, capture_output=True, text=True, check=False,
    )
    try:
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        yield test_url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


@pytest.fixture
def pg(pg_url, monkeypatch):
    engine = create_engine(pg_url, pool_size=12, max_overflow=8)
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE collector_enrollment_codes, agent_tokens, collector_installations, "
            "pipeline_status, branches, organizations, customers, app_users RESTART IDENTITY CASCADE"
        ))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Lib A'), (11, 'Lib B')"))
        conn.execute(text(
            "INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES "
            "(1, 'lib-a', 'Lib A', 'active', 10), (2, 'lib-b', 'Lib B', 'trial', 11)"
        ))
        conn.execute(text(
            "INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) VALUES "
            "(1, 1, 'main', 'Main', 'active', 1), (2, 2, 'main', 'Main', 'active', 2)"
        ))
        conn.execute(text(
            "INSERT INTO collector_installations (id, organization_id, branch_id, name, status) VALUES "
            "(101, 1, 1, 'Main AMH Sorter', 'provisioning'), (102, 1, 1, 'Second Sorter', 'active'), "
            "(103, 1, 1, 'Old Sorter', 'inactive'), (201, 2, 2, 'Lib B Sorter', 'provisioning')"
        ))
        conn.execute(text(
            "INSERT INTO app_users (id, email, full_name) VALUES (5, 'admin@example.test', 'Admin')"
        ))
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(
        main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR)
    )
    yield engine
    engine.dispose()


def _generate(engine, installation_id, **kwargs):
    with engine.begin() as conn:
        return enrollment.create_enrollment_code(conn, installation_id, **kwargs)


def _redeem(engine, code, **kwargs):
    with engine.begin() as conn:
        return enrollment.redeem_enrollment_code(conn, code, **kwargs)


def _rows(engine, sql, **params):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]


# --- the real migration --------------------------------------------------------------------

def test_the_real_migration_created_the_tables_columns_and_indexes(pg):
    columns = {r["column_name"]: r for r in _rows(
        pg, "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'collector_enrollment_codes'")}
    assert columns["code_hash"]["is_nullable"] == "NO" and columns["expires_at"]["data_type"] == "timestamp with time zone"
    assert columns["used_at"]["is_nullable"] == "YES" and columns["created_by_user_id"]["is_nullable"] == "YES"
    token_column = _rows(pg, "SELECT is_nullable, data_type FROM information_schema.columns "
                             "WHERE table_name = 'agent_tokens' AND column_name = 'installation_id'")
    assert token_column == [{"is_nullable": "YES", "data_type": "bigint"}]
    indexes = {r["indexname"] for r in _rows(pg, "SELECT indexname FROM pg_indexes")}
    assert {"ix_collector_enrollment_codes_installation_id", "ix_collector_enrollment_codes_unused",
            "ix_agent_tokens_installation_id", "uq_collector_enrollment_codes_code_hash"} <= indexes


def test_code_hash_is_unique(pg):
    _generate(pg, 101)
    stored = _rows(pg, "SELECT code_hash FROM collector_enrollment_codes")[0]["code_hash"]

    with pytest.raises(IntegrityError), pg.begin() as conn:
        conn.execute(text("INSERT INTO collector_enrollment_codes (installation_id, code_hash, expires_at) "
                          "VALUES (102, :h, now() + interval '1 hour')"), {"h": stored})


def test_deleting_an_installation_cascades_to_its_codes_and_bound_tokens_but_not_legacy_tokens(pg):
    _redeem(pg, _generate(pg, 101)["enrollment_code"])
    with pg.begin() as conn:
        conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id) "
                          "VALUES ('legacy', 10, 1)"))

    with pg.begin() as conn:
        conn.execute(text("DELETE FROM collector_installations WHERE id = 101"))

    assert _rows(pg, "SELECT id FROM collector_enrollment_codes WHERE installation_id = 101") == []
    assert [r["token_hash"] for r in _rows(pg, "SELECT token_hash FROM agent_tokens")] == ["legacy"]


def test_created_by_user_is_recorded_and_survives_user_removal_as_null(pg):
    _generate(pg, 101, created_by_user_id=5)
    assert _rows(pg, "SELECT created_by_user_id FROM collector_enrollment_codes")[0]["created_by_user_id"] == 5

    with pg.begin() as conn:
        conn.execute(text("DELETE FROM app_users WHERE id = 5"))

    assert _rows(pg, "SELECT created_by_user_id FROM collector_enrollment_codes")[0]["created_by_user_id"] is None


# --- service behavior on real PostgreSQL ------------------------------------------------------

def test_generate_and_redeem_round_trip_on_postgresql(pg):
    installation_before = _rows(pg, "SELECT * FROM collector_installations ORDER BY id")
    generated = _generate(pg, 101, created_by_user_id=5)

    issued = _redeem(pg, generated["enrollment_code"], hostname="AMH-PC", collector_version="1.0.4")

    assert set(issued) == {"customer_id", "branch_id", "installation_id", "agent_token"}
    assert (issued["customer_id"], issued["branch_id"], issued["installation_id"]) == (10, 1, 101)
    (token,) = _rows(pg, "SELECT * FROM agent_tokens")
    assert (token["customer_id"], token["branch_id"], token["installation_id"]) == (10, 1, 101)
    assert token["is_active"] is True
    assert token["token_hash"] == hashlib.sha256(issued["agent_token"].encode()).hexdigest()
    (code_row,) = _rows(pg, "SELECT * FROM collector_enrollment_codes")
    assert code_row["used_at"] is not None and code_row["revoked_at"] is None
    assert code_row["code_hash"] == hashlib.sha256(generated["enrollment_code"].encode()).hexdigest()
    # installed_at / last_seen_at / collector_version / status / updated_at all untouched.
    assert _rows(pg, "SELECT * FROM collector_installations ORDER BY id") == installation_before


def test_an_expired_code_is_refused_on_postgresql_and_the_boundary_is_exclusive(pg):
    now = datetime.now(UTC)
    code = _generate(pg, 101, now=now, ttl=timedelta(minutes=30))["enrollment_code"]

    with pytest.raises(enrollment.EnrollmentError) as expired:
        _redeem(pg, code, now=now + timedelta(minutes=30))
    assert expired.value.reason == "expired"
    assert _rows(pg, "SELECT * FROM agent_tokens") == []
    assert _redeem(pg, code, now=now + timedelta(minutes=29))["installation_id"] == 101


@pytest.mark.parametrize(("installation_id", "reason"), [(103, "installation_status_inactive")])
def test_generation_refuses_unusable_installations_on_postgresql(pg, installation_id, reason):
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _generate(pg, installation_id)

    assert excinfo.value.reason == reason


def test_a_failure_after_the_claim_rolls_the_claim_back_on_postgresql(pg, monkeypatch):
    code = _generate(pg, 101)["enrollment_code"]
    with pg.begin() as conn:
        conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id) VALUES ('taken', 10, 1)"))
    monkeypatch.setattr(enrollment, "new_agent_token", lambda: ("raw", "taken"))

    with pytest.raises(IntegrityError):
        _redeem(pg, code)

    assert _rows(pg, "SELECT used_at FROM collector_enrollment_codes")[0]["used_at"] is None
    monkeypatch.undo()
    assert _redeem(pg, code)["installation_id"] == 101


def test_generation_and_redemption_really_take_row_locks(pg):
    statements: list[str] = []

    @event.listens_for(pg, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, *_args):
        statements.append(" ".join(statement.split()))

    code = _generate(pg, 101)["enrollment_code"]
    _redeem(pg, code)

    assert any("FROM collector_installations ci" in s and s.endswith("FOR UPDATE OF ci") for s in statements)
    assert any("FROM collector_enrollment_codes WHERE code_hash" in s and s.endswith("FOR UPDATE") for s in statements)


# --- concurrency ----------------------------------------------------------------------------------

def test_a_second_redemption_blocks_on_the_row_lock_until_the_first_commits(pg):
    code = _generate(pg, 101)["enrollment_code"]
    a_holding, release_a, b_done = threading.Event(), threading.Event(), threading.Event()
    outcome: dict[str, object] = {}

    def redeemer_a():
        with pg.begin() as conn:
            outcome["a"] = enrollment.redeem_enrollment_code(conn, code)
            a_holding.set()  # token inserted, code claimed -- NOT yet committed
            release_a.wait(20)

    def redeemer_b():
        try:
            with pg.begin() as conn:
                outcome["b"] = enrollment.redeem_enrollment_code(conn, code)
        except enrollment.EnrollmentError as exc:
            outcome["b"] = exc.reason
        finally:
            b_done.set()

    thread_a = threading.Thread(target=redeemer_a)
    thread_a.start()
    assert a_holding.wait(20)
    thread_b = threading.Thread(target=redeemer_b)
    thread_b.start()

    # B is stuck behind A's row lock: it cannot finish while A is uncommitted.
    assert not b_done.wait(1.5), "the second redemption did not block on the row lock"
    release_a.set()  # A commits
    assert b_done.wait(20)
    thread_a.join(20)
    thread_b.join(20)

    assert isinstance(outcome["a"], dict) and outcome["b"] == "already_used"  # B saw A's COMMIT
    assert len(_rows(pg, "SELECT id FROM agent_tokens")) == 1


def test_many_simultaneous_redemptions_issue_exactly_one_token(pg):
    code = _generate(pg, 101)["enrollment_code"]
    workers = 10
    barrier = threading.Barrier(workers)
    results: list[object] = []
    lock = threading.Lock()

    def worker():
        barrier.wait(20)
        try:
            value = _redeem(pg, code)
        except enrollment.EnrollmentError as exc:
            value = exc.reason
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, str)]
    assert len(results) == workers and len(successes) == 1
    assert set(failures) <= {"already_used", "lost_redemption_race"} and len(failures) == workers - 1
    assert len(_rows(pg, "SELECT id FROM agent_tokens")) == 1
    (row,) = _rows(pg, "SELECT used_at FROM collector_enrollment_codes")
    assert row["used_at"] is not None


def test_concurrent_generations_for_one_installation_leave_exactly_one_usable_code(pg):
    workers = 6
    barrier = threading.Barrier(workers)
    codes: list[str] = []
    lock = threading.Lock()

    def worker():
        barrier.wait(20)
        result = _generate(pg, 101)
        with lock:
            codes.append(result["enrollment_code"])

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert len(set(codes)) == workers
    rows = _rows(pg, "SELECT used_at, revoked_at FROM collector_enrollment_codes")
    assert len(rows) == workers
    usable = [r for r in rows if r["used_at"] is None and r["revoked_at"] is None]
    assert len(usable) == 1  # the installation-row lock serialized the revoke-then-insert steps
    redeemable = []
    for code in codes:
        try:
            redeemable.append(_redeem(pg, code)["installation_id"])
        except enrollment.EnrollmentError:
            pass
    assert redeemable == [101]  # exactly one of the six codes still works


# --- end to end through the real endpoints on real PostgreSQL ---------------------------------------

def test_enroll_then_heartbeat_end_to_end_on_postgresql(pg):
    code = _generate(pg, 101)["enrollment_code"]

    enrolled = client.post("/collector/enroll", json={"enrollment_code": code, "hostname": "AMH-PC",
                                                       "collector_version": "1.0.4"})

    assert enrolled.status_code == 200
    body = enrolled.json()
    assert set(body) == {"customer_id", "branch_id", "installation_id", "agent_token"}
    (before,) = _rows(pg, "SELECT status, installed_at, last_seen_at FROM collector_installations WHERE id = 101")
    assert before == {"status": "provisioning", "installed_at": None, "last_seen_at": None}

    heartbeat = client.post(
        "/upload-pipeline-status",
        json={"customer_id": body["customer_id"], "branch_id": body["branch_id"], "status": "completed",
              "installation_id": body["installation_id"], "collector_version": "1.0.4"},
        headers={"Authorization": f"Bearer {body['agent_token']}"},
    )

    assert heartbeat.status_code == 200
    (after,) = _rows(pg, "SELECT status, installed_at, last_seen_at, collector_version "
                         "FROM collector_installations WHERE id = 101")
    assert after["status"] == "active" and after["installed_at"] is not None
    assert after["last_seen_at"] is not None and after["collector_version"] == "1.0.4"
    assert len(_rows(pg, "SELECT 1 FROM pipeline_status")) == 1


def test_an_enrolled_token_cannot_claim_a_different_installation_on_postgresql(pg):
    body = client.post("/collector/enroll",
                       json={"enrollment_code": _generate(pg, 101)["enrollment_code"]}).json()
    before = _rows(pg, "SELECT * FROM collector_installations ORDER BY id")

    rejected = client.post(
        "/upload-pipeline-status",
        json={"customer_id": 10, "branch_id": 1, "status": "completed", "installation_id": 102},
        headers={"Authorization": f"Bearer {body['agent_token']}"},
    )

    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "Collector installation is not authorized to report status"}
    assert _rows(pg, "SELECT * FROM collector_installations ORDER BY id") == before
    assert _rows(pg, "SELECT 1 FROM pipeline_status") == []


def test_a_legacy_null_installation_token_still_works_on_postgresql(pg):
    raw = "legacy-token-value"
    with pg.begin() as conn:
        conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, is_active) "
                          "VALUES (:h, 10, 1, TRUE)"), {"h": hashlib.sha256(raw.encode()).hexdigest()})

    for extra in ({}, {"installation_id": 101}, {"installation_id": 102}):
        response = client.post(
            "/upload-pipeline-status",
            json={"customer_id": 10, "branch_id": 1, "status": "completed", **extra},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert response.status_code == 200, extra


def test_the_endpoint_refuses_every_bad_code_with_one_generic_400_on_postgresql(pg):
    used = _generate(pg, 101)["enrollment_code"]
    _redeem(pg, used)
    inactive_code = enrollment.new_enrollment_code()
    with pg.begin() as conn:
        conn.execute(
            text("INSERT INTO collector_enrollment_codes (installation_id, code_hash, expires_at) "
                 "VALUES (103, :h, now() + interval '1 hour')"),
            {"h": enrollment.hash_enrollment_code(inactive_code)},
        )

    bodies = set()
    for code in (enrollment.new_enrollment_code(), "junk", used, inactive_code):
        main.limiter.reset()
        response = client.post("/collector/enroll", json={"enrollment_code": code})
        assert response.status_code == 400
        bodies.add(response.text)

    assert len(bodies) == 1 and PUBLIC["detail"] in bodies.pop()
    assert len(_rows(pg, "SELECT id FROM agent_tokens")) == 1  # only the legitimate first redemption


# --- a bound token dies with its installation, on real PostgreSQL ---------------------------

GENERIC_403 = {"detail": "Agent is not currently authorized to upload data"}
_UPLOAD_BODY = {"checkins": [{"customer_id": 10, "branch_id": 1, "barcode": "B-100",
                              "event_time": "2026-09-20T09:00:00"}]}


def _enrolled_token(pg, installation_id=101):
    response = client.post("/collector/enroll", json={"enrollment_code": _generate(pg, installation_id)["enrollment_code"]})
    assert response.status_code == 200
    return response.json()["agent_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _token_last_used(pg):
    return _rows(pg, "SELECT last_used_at FROM agent_tokens ORDER BY id DESC LIMIT 1")[0]["last_used_at"]


@pytest.mark.parametrize("status", ["inactive", "retired"])
def test_a_bound_token_cannot_upload_or_heartbeat_once_its_installation_is_inactive_or_retired(pg, status):
    token = _enrolled_token(pg)
    assert client.post("/upload", json=_UPLOAD_BODY, headers=_auth(token)).status_code == 200
    assert len(_rows(pg, "SELECT id FROM checkins")) == 1
    used_before = _token_last_used(pg)

    with pg.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET status = :s WHERE id = 101"), {"s": status})
    more = {"checkins": [{**_UPLOAD_BODY["checkins"][0], "barcode": "B-200"}]}

    upload = client.post("/upload", json=more, headers=_auth(token))
    heartbeats = [
        client.post("/upload-pipeline-status", json={"customer_id": 10, "branch_id": 1, "status": "completed", **extra},
                    headers=_auth(token))
        for extra in ({}, {"installation_id": 101})
    ]

    for response in (upload, *heartbeats):
        assert (response.status_code, response.json()) == (403, GENERIC_403)
    assert len(_rows(pg, "SELECT id FROM checkins")) == 1  # the refused upload wrote nothing
    assert _rows(pg, "SELECT status FROM collector_installations WHERE id = 101")[0]["status"] == status
    assert _token_last_used(pg) == used_before  # authentication never got as far as marking use


def test_reactivating_the_installation_restores_the_same_token_on_postgresql(pg):
    token = _enrolled_token(pg)
    with pg.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET status = 'retired' WHERE id = 101"))
    assert client.post("/upload", json=_UPLOAD_BODY, headers=_auth(token)).status_code == 403

    with pg.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET status = 'active' WHERE id = 101"))

    assert client.post("/upload", json=_UPLOAD_BODY, headers=_auth(token)).status_code == 200
    assert len(_rows(pg, "SELECT id FROM agent_tokens")) == 1


def test_legacy_tokens_still_upload_while_every_installation_of_their_scope_is_retired(pg):
    raw = "legacy-upload-token"
    with pg.begin() as conn:
        conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, is_active) "
                          "VALUES (:h, 10, 1, TRUE)"), {"h": hashlib.sha256(raw.encode()).hexdigest()})
        conn.execute(text("UPDATE collector_installations SET status = 'retired' WHERE organization_id = 1"))

    assert client.post("/upload", json=_UPLOAD_BODY, headers=_auth(raw)).status_code == 200
    assert client.post("/upload-pipeline-status", json={"customer_id": 10, "branch_id": 1, "status": "ok"},
                       headers=_auth(raw)).status_code == 200


def test_deleting_the_installation_deletes_the_bound_token_so_it_is_unknown_not_legacy(pg):
    token = _enrolled_token(pg)
    with pg.begin() as conn:
        conn.execute(text("DELETE FROM collector_installations WHERE id = 101"))

    response = client.post("/upload", json=_UPLOAD_BODY, headers=_auth(token))

    assert response.status_code == 401  # the token row is gone (ON DELETE CASCADE), not demoted to a legacy token
    assert _rows(pg, "SELECT id FROM agent_tokens") == []


def test_a_bound_token_whose_installation_moved_to_another_tenant_is_refused_on_postgresql(pg):
    token = _enrolled_token(pg)  # bound to 101 (org 1 / branch 1)
    with pg.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET organization_id = 2, branch_id = 2 WHERE id = 101"))

    response = client.post("/upload-pipeline-status", json={"customer_id": 10, "branch_id": 1, "status": "ok"},
                           headers=_auth(token))

    assert (response.status_code, response.json()) == (403, GENERIC_403)
