"""C6a M3b on a REAL PostgreSQL: scripts/issue_ingest_key.py and scripts/create_agent_token.py against real enrollment, offboarding,
credential recovery and row level security, and every lock order that can race them.

A race here is made deterministic rather than hoped for, as in tests/test_key_stamping_and_recovery_postgres.py: one transaction
is held open (or paused behind a row another connection has locked), the other is started, and pg_stat_activity shows it really
waiting before the first is released.

OPT-IN AND SAFE BY CONSTRUCTION, the same convention as that file: it runs only when SORTVIEW_TEST_POSTGRES_URL points at a
maintenance database on a NON-PRODUCTION server it may create and drop databases on. A throwaway database and role are created and
dropped. The host must be local unless SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also set. The API's own DATABASE_URL is never read.
Every value is SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from scripts import create_agent_token, issue_ingest_key
from scripts import runtime_role_privileges as privileges
from services import admin_installation_service as service
from services import collector_enrollment_service as enrollment
from services import collector_rules, ingest_v2_service, platform_admin_service

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL tests)")

ACTOR = {"id": 7, "email": "admin@example.invalid"}
# org 1 "acme": customer 10, branches 1 and 3. org 2 "other": customer 20, branch 2.
CUSTOMER, BRANCH, OTHER_BRANCH = 10, 1, 3
OWNER, SECOND = 101, 102
OWNED_KEY = "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"
UNOWNED_KEY = "8d9e0f1a-2b3c-4d4e-9f5a-6b7c8d9e0f1a"
READY = {"collector_timezone": "America/Chicago", "collector_rules": collector_rules.rules_review_marker(
    reviewed_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC), reviewed_by_user_id=5)}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- a throwaway database and role -----------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cluster():
    admin = make_url(ADMIN_URL)
    host = admin.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(f"refusing to run against non-local PostgreSQL host {host!r}")
    name = f"sortview_m3b_test_{secrets.token_hex(4)}"
    role = f"sortview_m3b_role_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    url = admin.set(database=name)
    owner = create_engine(url, hide_parameters=True, pool_size=8, max_overflow=4)
    runtime = None
    role_created = False
    try:
        migrated = subprocess.run(  # nosec B603
            [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
            env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
            capture_output=True, text=True, check=False,
        )
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        with owner.begin() as conn:
            conn.execute(text(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT "  # nosec B608
                f"PASSWORD '{RUNTIME_ROLE_PASSWORD}'"
            ))
            role_created = True
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO {role}'))  # nosec B608
            for line in privileges.provisioning_sql(role).splitlines():
                if line and not line.startswith("--") and line not in ("BEGIN;", "COMMIT;"):
                    conn.execute(text(line))
        runtime = create_engine(url.set(username=role, password=RUNTIME_ROLE_PASSWORD), hide_parameters=True,
                                pool_size=8, max_overflow=4)
        yield owner, runtime, url.render_as_string(hide_password=False)
    finally:
        if runtime is not None:
            runtime.dispose()
        if role_created:
            with owner.begin() as conn:
                conn.execute(text(f"DROP OWNED BY {role}"))  # nosec B608
                conn.execute(text(f"DROP ROLE IF EXISTS {role}"))  # nosec B608
        owner.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


@pytest.fixture
def db(cluster, monkeypatch):
    """Two libraries with installations, one legacy unbound token, and no key yet."""
    owner, runtime, _url = cluster
    with owner.begin() as conn:
        conn.execute(text(
            "TRUNCATE auth_audit_log, tenant_lifecycle_events, v2_cutovers, ingest_key_ids, checkin_events, "
            "collector_enrollment_codes, agent_tokens, collector_installations, organization_settings, branch_settings, "
            "pipeline_status, branches, organizations, customers, app_users RESTART IDENTITY CASCADE"
        ))
        conn.execute(text("INSERT INTO app_users (id, email, full_name, is_platform_admin) "
                          "VALUES (7, 'admin@example.invalid', 'Test Admin', TRUE)"))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Acme'), (20, 'Other')"))
        conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id, created_at) VALUES "
                          "(1, 'acme', 'Acme', 'active', 10, '2026-01-01T00:00:00+00:00'), "
                          "(2, 'other', 'Other', 'active', 20, '2026-01-01T00:00:00+00:00')"))
        conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id, "
                          "onboarding_status) VALUES (1, 1, 'main', 'Main', TRUE, 'active', 1, 'ready'), "
                          "(3, 1, 'north', 'North', FALSE, 'active', 3, 'ready'), (2, 2, 'main', 'Other Main', TRUE, 'active', 2, 'ready')"))
        conn.execute(text("INSERT INTO organization_settings (organization_id, settings_json) VALUES "
                          "(1, CAST(:s AS JSONB)), (2, CAST(:s AS JSONB))"), {"s": json.dumps(READY)})
        conn.execute(text("INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract) VALUES "
                          "(101, 1, 1, 'Owner', 'active', 2), (102, 1, 1, 'Second', 'active', 2), (301, 1, 3, 'North', 'active', 2), "
                          "(201, 2, 2, 'Other', 'active', 2)"))
        conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active) "
                          "VALUES (:h, 10, 1, 'synthetic legacy', TRUE)"), {"h": _hash("CANARY-legacy-unbound-token")})
    for module in (service, platform_admin_service):
        monkeypatch.setattr(module, "get_engine", lambda: runtime)
    return owner, runtime


def _insert_key(conn, key, installation_id, *, status="active", customer=CUSTOMER, branch=BRANCH):
    conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, status, retired_at, installation_id) VALUES "
                      "(:k, :c, :b, :s, CASE WHEN :s = 'retired' THEN now() END, :i)"),
                 {"k": key, "c": customer, "b": branch, "s": status, "i": installation_id})


@pytest.fixture
def owned(db):
    """OWNER owns the branch's one active key, as a real enrollment leaves it, with a bound token."""
    owner, runtime = db
    with owner.begin() as conn:
        _insert_key(conn, OWNED_KEY, OWNER)
        conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active, installation_id) "
                          "VALUES (:h, 10, 1, 'synthetic owner', TRUE, :i)"), {"h": _hash("CANARY-owner-token"), "i": OWNER})
    return owner, runtime


def _rows(engine, sql, **params):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def _state(owner):
    """Everything these operations can change, read as the owner."""
    return {
        "tokens": _rows(owner, "SELECT id, token_hash, is_active, installation_id FROM agent_tokens ORDER BY id"),
        "keys": _rows(owner, "SELECT key_id, status, retired_at, installation_id FROM ingest_key_ids ORDER BY id"),
        "installations": _rows(owner, "SELECT id, status FROM collector_installations ORDER BY id"),
        "codes": _rows(owner, "SELECT id, used_at, revoked_at FROM collector_enrollment_codes ORDER BY id"),
        "cutovers": _rows(owner, "SELECT id, set_by FROM v2_cutovers ORDER BY id"),
    }


def _active_keys(owner, branch=BRANCH):
    return _rows(owner, "SELECT key_id, installation_id FROM ingest_key_ids WHERE customer_id = :c AND branch_id = :b "
                        "AND status = 'active' ORDER BY id", c=CUSTOMER, b=branch)


def _live_tokens(owner):
    return _rows(owner, "SELECT id FROM agent_tokens WHERE is_active AND customer_id = :c", c=CUSTOMER)


def _script_token_rows(owner):
    return _rows(owner, "SELECT is_active, installation_id FROM agent_tokens WHERE token_hash = :h", h=_hash("CANARY-script-token"))


# --- making a race deterministic -----------------------------------------------------------------------------------------

_HELD: list = []
_STARTED: list = []


@pytest.fixture(autouse=True)
def _nothing_left_holding_a_lock():
    """A test that fails part-way must not leave a row locked: the next test's TRUNCATE would wait for it forever."""
    yield
    while _HELD:
        _HELD.pop().close()
    while _STARTED:
        _STARTED.pop().thread.join(20)


class Background:
    """Runs `work` on its own thread, keeping what it returned or raised."""

    def __init__(self, work):
        self.result = self.error = None
        self.thread = threading.Thread(target=self._run, args=(work,), daemon=True)
        self.thread.start()
        _STARTED.append(self)

    def _run(self, work):
        try:
            self.result = work()
        except Exception as exc:  # the test asserts on it
            self.error = exc

    def finish(self):
        self.thread.join(20)
        assert not self.thread.is_alive(), "still running: a lock was never released"
        assert not (isinstance(self.error, DBAPIError) and "deadlock" in str(self.error).lower()), "deadlock detected"
        return self


def _waiting_on_a_lock(owner, count, *, unless=None):
    """Until `count` sessions of this database are waiting on a lock, or `unless` has finished instead."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with owner.connect() as conn:
            waiting = conn.execute(text("SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database() "
                                        "AND wait_event_type = 'Lock'")).scalar()
        if waiting >= count or (unless is not None and not unless.thread.is_alive()):
            return
        time.sleep(0.02)
    pytest.fail(f"never saw {count} session(s) waiting on a lock")


class Paused:
    """Holds a row FOR UPDATE, so a transaction that reaches it waits there, holding what it has so far."""

    def __init__(self, owner, sql, **params):
        self.conn = owner.connect()
        _HELD.append(self.conn)
        self.transaction = self.conn.begin()
        assert self.conn.execute(text(sql + " FOR UPDATE"), params).first() is not None

    def release(self):
        self.transaction.rollback()


def _pause_at_a_token(owner):
    return Paused(owner, "SELECT id FROM agent_tokens WHERE token_hash = :h", h=_hash("CANARY-legacy-unbound-token"))


class Open:
    """A transaction left open until commit() or rollback()."""

    def __init__(self, engine):
        self.conn = engine.connect()
        _HELD.append(self.conn)
        self.transaction = self.conn.begin()

    def commit(self):
        self.transaction.commit()

    def rollback(self):
        self.transaction.rollback()


def _tenant_context(conn, customer=CUSTOMER, branch=BRANCH):
    enrollment.set_tenant_context(conn, customer, branch)


def _issue_unowned(engine, customer=CUSTOMER, branch=BRANCH):
    with engine.begin() as conn:
        _tenant_context(conn, customer, branch)
        return ingest_v2_service.issue_ingest_key(conn, customer, branch, installation_id=None)


def _retire(engine, key_id, *, context=True):
    with engine.begin() as conn:
        if context:
            _tenant_context(conn)
        return ingest_v2_service.retire_ingest_key(conn, key_id)


def _new_code(runtime, installation_id=OWNER):
    with runtime.begin() as conn:
        return enrollment.create_enrollment_code(conn, installation_id)["enrollment_code"]


def _redeem(runtime, code):
    with runtime.begin() as conn:
        return enrollment.redeem_enrollment_code(conn, code, contract_version=2)


def _offboard():
    return platform_admin_service.offboard_library(1, "acme", actor_user_id=7, actor_label="test")


def _script_transaction(conn, customer=CUSTOMER, branch=BRANCH):
    """What scripts/create_agent_token.py does in its one transaction."""
    create_agent_token.validate_token_scope(conn, customer, branch)
    conn.execute(text(create_agent_token._INSERT_SQL), {"token_hash": _hash("CANARY-script-token"), "customer_id": customer,
                                                        "branch_id": branch, "description": "synthetic script token"})


def _token_script(engine, **scope):
    with engine.begin() as conn:
        _script_transaction(conn, **scope)


def _run_tool(*argv):
    out = io.StringIO()
    return issue_ingest_key.main(list(argv), out=out), out.getvalue()


# =====================================================================================================================
# issuing: one active key, and never an unowned key at a branch with an enrollment or an owner behind it
# =====================================================================================================================

@pytest.mark.parametrize("role", ["owner", "runtime"])
def test_a_second_active_key_is_refused_for_either_role_and_writes_nothing(db, role):
    owner, runtime = db
    engine = owner if role == "owner" else runtime
    first = _issue_unowned(engine)
    before = _state(owner)

    with pytest.raises(ValueError, match="already has an active key"):
        _issue_unowned(engine)

    assert _state(owner) == before and _active_keys(owner) == [{"key_id": first, "installation_id": None}]


def test_a_branch_with_a_retired_owned_key_is_refused_and_writes_nothing(db):
    owner, runtime = db
    with owner.begin() as conn:
        _insert_key(conn, OWNED_KEY, OWNER, status="retired")
    before = _state(owner)

    with pytest.raises(ValueError, match="owned by an installation"):
        _issue_unowned(runtime)

    assert _state(owner) == before


def test_an_active_owned_key_keeps_its_existing_refusal(owned):
    _, runtime = owned

    with pytest.raises(ValueError, match="the branch has an active key owned by an installation"):
        _issue_unowned(runtime)


def test_a_real_enrollment_with_a_null_owner_still_refuses_a_replacement_key(db):
    """A redemption from before M2 left its key unowned: only its cutover row still says an enrollment happened."""
    owner, runtime = db
    key_id = _redeem(runtime, _new_code(runtime))["key_id"]
    with owner.begin() as conn:
        conn.execute(text("UPDATE ingest_key_ids SET installation_id = NULL WHERE key_id = :k"), {"k": key_id})
    assert _retire(owner, key_id) is True
    before = _state(owner)

    with pytest.raises(ValueError, match="enrolled"):
        _issue_unowned(runtime)

    assert _state(owner) == before


def test_the_set_by_the_guard_reads_is_the_one_a_real_enrollment_writes(db):
    owner, runtime = db
    _redeem(runtime, _new_code(runtime))

    assert _rows(owner, "SELECT set_by FROM v2_cutovers") == [{"set_by": ingest_v2_service.ENROLLMENT_CUTOVER_SET_BY}]
    assert ingest_v2_service.ENROLLMENT_CUTOVER_SET_BY == enrollment.INITIAL_CUTOVER_SET_BY


def test_another_branch_is_not_refused_by_this_branchs_history(owned):
    owner, runtime = owned
    with owner.begin() as conn:
        ingest_v2_service.record_v2_cutover(conn, CUSTOMER, BRANCH, datetime(2026, 10, 1, tzinfo=UTC), enrollment.INITIAL_CUTOVER_SET_BY)

    assert _issue_unowned(runtime, CUSTOMER, OTHER_BRANCH)
    assert _issue_unowned(runtime, 20, 2)


def test_retire_then_issue_stays_possible_for_a_branch_with_only_legacy_keys(db):
    owner, runtime = db
    with owner.begin() as conn:
        ingest_v2_service.record_v2_cutover(conn, CUSTOMER, BRANCH, datetime(2026, 10, 1, tzinfo=UTC), "an operator")
    first = _issue_unowned(runtime)

    assert _retire(runtime, first) is True
    second = _issue_unowned(runtime)

    assert _active_keys(owner) == [{"key_id": second, "installation_id": None}] and second != first


def test_issuing_without_a_tenant_context_still_fails_on_row_level_security(db):
    owner, runtime = db

    with pytest.raises(DBAPIError, match="row-level security"), runtime.begin() as conn:
        ingest_v2_service.issue_ingest_key(conn, CUSTOMER, BRANCH, installation_id=None)

    assert _active_keys(owner) == []


def test_two_issues_at_once_leave_one_active_key(db):
    owner, runtime = db
    held = Open(runtime)
    _tenant_context(held.conn)
    first = ingest_v2_service.issue_ingest_key(held.conn, CUSTOMER, BRANCH, installation_id=None)

    second = Background(lambda: _issue_unowned(runtime))
    _waiting_on_a_lock(owner, 1)
    held.commit()

    assert isinstance(second.finish().error, ValueError) and "already has an active key" in str(second.error)
    assert _active_keys(owner) == [{"key_id": first, "installation_id": None}]


def test_a_waiting_issue_succeeds_when_the_first_rolls_back(db):
    owner, runtime = db
    held = Open(runtime)
    _tenant_context(held.conn)
    ingest_v2_service.issue_ingest_key(held.conn, CUSTOMER, BRANCH, installation_id=None)

    second = Background(lambda: _issue_unowned(runtime))
    _waiting_on_a_lock(owner, 1)
    held.rollback()

    assert second.finish().error is None
    assert _active_keys(owner) == [{"key_id": second.result, "installation_id": None}]


def test_an_enrollment_that_commits_first_refuses_the_waiting_issue(db):
    owner, runtime = db
    code = _new_code(runtime)
    held = Open(runtime)
    enrolled = enrollment.redeem_enrollment_code(held.conn, code, contract_version=2)

    issue = Background(lambda: _issue_unowned(runtime))
    _waiting_on_a_lock(owner, 1)
    held.commit()

    assert isinstance(issue.finish().error, ValueError)
    assert _active_keys(owner) == [{"key_id": enrolled["key_id"], "installation_id": OWNER}]


def test_an_issue_that_commits_first_refuses_the_waiting_enrollment_and_consumes_nothing(db):
    owner, runtime = db
    code = _new_code(runtime)
    held = Open(runtime)
    _tenant_context(held.conn)
    key = ingest_v2_service.issue_ingest_key(held.conn, CUSTOMER, BRANCH, installation_id=None)

    redeem = Background(lambda: _redeem(runtime, code))
    _waiting_on_a_lock(owner, 1)
    held.commit()

    assert isinstance(redeem.finish().error, enrollment.EnrollmentRefused) and redeem.error.reason == "active_ingest_key_exists"
    assert _active_keys(owner) == [{"key_id": key, "installation_id": None}]
    assert _rows(owner, "SELECT used_at FROM collector_enrollment_codes") == [{"used_at": None}]


def test_an_issue_reaching_an_offboarding_in_progress_is_refused(db):
    owner, runtime = db
    pause = _pause_at_a_token(owner)
    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 1)

    issue = Background(lambda: _issue_unowned(runtime))
    _waiting_on_a_lock(owner, 2)
    pause.release()

    assert offboard.finish().error is None
    assert isinstance(issue.finish().error, ValueError) and "cancelled" in str(issue.error)
    assert _active_keys(owner) == []


# =====================================================================================================================
# retiring: an owned key, never; an unowned one, as before
# =====================================================================================================================

@pytest.mark.parametrize("role", ["owner", "runtime"])
def test_retiring_an_owned_key_is_refused_for_either_role_and_changes_nothing(owned, role):
    owner, runtime = owned
    before = _state(owner)

    with pytest.raises(ValueError, match="owned by an installation"):
        _retire(owner if role == "owner" else runtime, OWNED_KEY)

    assert _state(owner) == before


def test_an_unowned_key_still_retires_and_a_second_attempt_or_an_unknown_key_is_false(db):
    owner, _ = db
    with owner.begin() as conn:
        _insert_key(conn, UNOWNED_KEY, None)

    assert _retire(owner, UNOWNED_KEY) is True
    assert _retire(owner, UNOWNED_KEY) is False
    assert _retire(owner, OWNED_KEY) is False


def test_a_retired_owned_key_answers_as_any_other_retired_key(db):
    owner, _ = db
    with owner.begin() as conn:
        _insert_key(conn, OWNED_KEY, OWNER, status="retired")

    assert _retire(owner, OWNED_KEY) is False


def test_the_runtime_role_cannot_retire_a_key_it_cannot_see(db):
    owner, runtime = db
    with owner.begin() as conn:
        _insert_key(conn, UNOWNED_KEY, None)
        _insert_key(conn, OWNED_KEY, None, branch=OTHER_BRANCH)
    before = _state(owner)

    assert _retire(runtime, UNOWNED_KEY, context=False) is False
    assert _retire(runtime, OWNED_KEY) is False  # another branch's key, from this branch's context

    assert _state(owner) == before


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
def test_a_retirement_waiting_behind_an_ownership_change_rereads_the_owner(db, outcome):
    owner, _ = db
    with owner.begin() as conn:
        _insert_key(conn, UNOWNED_KEY, None)
    held = Open(owner)
    held.conn.execute(text("UPDATE ingest_key_ids SET installation_id = :i WHERE key_id = :k"), {"i": OWNER, "k": UNOWNED_KEY})

    retire = Background(lambda: _retire(owner, UNOWNED_KEY))
    _waiting_on_a_lock(owner, 1)

    if outcome == "commit":
        held.commit()
        assert isinstance(retire.finish().error, ValueError) and "owned by an installation" in str(retire.error)
        assert _active_keys(owner) == [{"key_id": UNOWNED_KEY, "installation_id": OWNER}]
    else:
        held.rollback()
        assert retire.finish().error is None and retire.result is True
        assert _active_keys(owner) == []


def test_a_retirement_waiting_behind_an_offboarding_write_finds_no_active_key(db):
    owner, _ = db
    with owner.begin() as conn:
        _insert_key(conn, UNOWNED_KEY, None)
    held = Open(owner)
    held.conn.execute(text("UPDATE ingest_key_ids SET status = 'retired', retired_at = now() WHERE key_id = :k"), {"k": UNOWNED_KEY})

    retire = Background(lambda: _retire(owner, UNOWNED_KEY))
    _waiting_on_a_lock(owner, 1)
    held.commit()

    assert retire.finish().error is None and retire.result is False


def test_an_owned_key_is_refused_at_once_during_an_uncommitted_offboarding_write(owned):
    owner, _ = owned
    held = Open(owner)
    held.conn.execute(text("UPDATE ingest_key_ids SET status = 'retired', retired_at = now() WHERE key_id = :k"), {"k": OWNED_KEY})

    retire = Background(lambda: _retire(owner, OWNED_KEY))  # no wait: the row it can see does not match
    assert "owned by an installation" in str(retire.finish().error)

    held.commit()
    assert _retire(owner, OWNED_KEY) is False


def test_a_real_offboarding_retires_owned_keys_as_before(owned):
    owner, _ = owned

    _offboard()

    assert _active_keys(owner) == [] and _retire(owner, OWNED_KEY) is False


def test_an_issue_during_an_uncommitted_retirement_is_refused_and_succeeds_after_it(db):
    owner, runtime = db
    with owner.begin() as conn:
        _insert_key(conn, UNOWNED_KEY, None)
    held = Open(owner)
    assert ingest_v2_service.retire_ingest_key(held.conn, UNOWNED_KEY) is True

    with pytest.raises(ValueError, match="already has an active key"):
        _issue_unowned(runtime)
    held.commit()

    assert _active_keys(owner) == [] and _issue_unowned(runtime)


# =====================================================================================================================
# the issue_ingest_key tool, end to end
# =====================================================================================================================

def test_the_two_step_bypass_is_closed_through_the_tool(cluster, owned):
    owner, _, url = cluster
    before = _state(owner)

    retire_code, retire_out = _run_tool("retire", "--key-id", OWNED_KEY, "--execute", url)
    issue_code, issue_out = _run_tool("issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute", url)

    assert retire_code == issue_code == 2
    assert "REFUSED" in retire_out and "owned by an installation" in retire_out and "Nothing was changed" in retire_out
    assert "REFUSED" in issue_out and "Nothing was written" in issue_out
    assert _state(owner) == before

    with owner.begin() as conn:  # a key retired before this guard existed, by the old tool or by hand
        conn.execute(text("UPDATE ingest_key_ids SET status = 'retired', retired_at = now() WHERE key_id = :k"), {"k": OWNED_KEY})
    before = _state(owner)

    issue_code, issue_out = _run_tool("issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute", url)

    assert issue_code == 2 and "owned by an installation" in issue_out and _state(owner) == before


def test_the_tool_still_retires_and_replaces_a_legacy_key(cluster, db):
    owner, _ = db
    url = cluster[2]
    with owner.begin() as conn:
        _insert_key(conn, UNOWNED_KEY, None)

    second_code, _second_out = _run_tool("issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute", url)
    retire_code, _retire_out = _run_tool("retire", "--key-id", UNOWNED_KEY, "--execute", url)
    issue_code, _issue_out = _run_tool("issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute", url)

    assert (second_code, retire_code, issue_code) == (2, 0, 0) and len(_active_keys(owner)) == 1


# =====================================================================================================================
# the token script
# =====================================================================================================================

@pytest.mark.parametrize("role", ["owner", "runtime"])
def test_an_unbound_token_is_refused_beside_an_active_owned_key_for_either_role(owned, role):
    """As the runtime role the key is under row level security: with no tenant context this check would read nothing and pass."""
    owner, runtime = owned
    before = _state(owner)

    with pytest.raises(create_agent_token.ScopeError, match="active ingest key owned by an installation"):
        _token_script(owner if role == "owner" else runtime)

    assert _state(owner) == before


@pytest.mark.parametrize(("key_status", "key_owner"), [
    pytest.param(None, None, id="no-key"),
    pytest.param("active", None, id="active-null-owner"),
    pytest.param("retired", None, id="retired-null-owner"),
    pytest.param("retired", OWNER, id="only-a-retired-owned-key"),
])
def test_a_token_is_still_allowed_with_no_active_owned_key(db, key_status, key_owner):
    owner, runtime = db
    if key_status is not None:
        with owner.begin() as conn:
            _insert_key(conn, UNOWNED_KEY, key_owner, status=key_status)

    _token_script(runtime)

    assert _script_token_rows(owner) == [{"is_active": True, "installation_id": None}]


def test_another_branchs_owned_key_does_not_refuse_this_branch(db):
    owner, runtime = db
    with owner.begin() as conn:
        _insert_key(conn, OWNED_KEY, OWNER)

    _token_script(runtime, branch=OTHER_BRANCH)

    assert _script_token_rows(owner) == [{"is_active": True, "installation_id": None}]


def test_the_tenant_context_does_not_outlive_the_scripts_transaction(owned):
    _, runtime = owned
    engine_conn = runtime.connect()
    _HELD.append(engine_conn)
    with engine_conn.begin(), pytest.raises(create_agent_token.ScopeError):
        _script_transaction(engine_conn)

    assert not engine_conn.execute(text("SELECT current_setting('app.operational_customer_id', true)")).scalar()
    engine_conn.rollback()


def test_the_real_script_refuses_shows_no_token_and_writes_nothing(owned, cluster, monkeypatch, capsys):
    owner, _, url = cluster
    before = _state(owner)
    monkeypatch.setattr(create_agent_token, "generate_token", lambda: ("CANARY-RAW-TOKEN", "CANARYHASH"))
    monkeypatch.setattr(create_agent_token, "create_engine", lambda target, **_kwargs: create_engine(target))  # sslmode: a local cluster
    monkeypatch.setattr(sys, "argv", ["create_agent_token.py", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH),
                                      "--description", "synthetic", "--execute", url])

    with pytest.raises(SystemExit) as refused:
        create_agent_token.main()

    assert "REFUSED" in str(refused.value) and "Nothing was written" in str(refused.value)
    assert "CANARY-RAW-TOKEN" not in capsys.readouterr().out
    assert _state(owner) == before


def test_an_enrollment_in_flight_makes_the_token_script_wait_and_then_refuses_it(db):
    # The race the branch lock exists for: both hold the organization FOR SHARE, which does not conflict, and the key is uncommitted.
    owner, runtime = db
    code = _new_code(runtime)
    held = Open(runtime)
    enrollment.redeem_enrollment_code(held.conn, code, contract_version=2)

    script = Background(lambda: _token_script(runtime))
    _waiting_on_a_lock(owner, 1, unless=script)
    assert script.thread.is_alive(), "the token script did not wait for the enrollment"
    held.commit()

    assert isinstance(script.finish().error, create_agent_token.ScopeError)
    assert _script_token_rows(owner) == []


def test_a_token_script_in_flight_makes_the_enrollment_wait_and_the_owned_key_refuses_that_token(db):
    owner, runtime = db
    code = _new_code(runtime)
    held = Open(runtime)
    _script_transaction(held.conn)

    redeem = Background(lambda: _redeem(runtime, code))
    _waiting_on_a_lock(owner, 1, unless=redeem)
    assert redeem.thread.is_alive(), "the enrollment did not wait for the token script"
    held.commit()

    assert redeem.finish().error is None
    assert _script_token_rows(owner) == [{"is_active": True, "installation_id": None}]  # nothing revokes an unbound token
    with owner.connect() as conn:
        assert ingest_v2_service.ingest_key_problem(conn, CUSTOMER, BRANCH, redeem.result["key_id"], token_installation_id=None) == (
            "key is owned by another installation")


def test_offboarding_waits_for_the_token_script_and_leaves_no_active_token(db):
    owner, runtime = db
    held = Open(runtime)
    _script_transaction(held.conn)

    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 1)
    held.commit()

    assert offboard.finish().error is None
    assert _live_tokens(owner) == []


def test_the_token_script_reaching_an_offboarding_in_progress_is_refused_and_writes_nothing(db):
    owner, runtime = db
    pause = _pause_at_a_token(owner)
    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 1)

    script = Background(lambda: _token_script(runtime))
    _waiting_on_a_lock(owner, 2)
    pause.release()

    assert offboard.finish().error is None
    assert isinstance(script.finish().error, create_agent_token.ScopeError) and "cancelled" in str(script.error)
    assert _script_token_rows(owner) == [] and _live_tokens(owner) == []


def test_the_token_script_waits_for_a_recovery_in_progress_and_is_refused(owned):
    owner, runtime = owned
    pause = Paused(owner, "SELECT id FROM agent_tokens WHERE token_hash = :h", h=_hash("CANARY-owner-token"))
    library = service.get_library("acme")
    recover = Background(lambda: service.issue_recovery_token(library, OWNER, actor=ACTOR))
    _waiting_on_a_lock(owner, 1)

    script = Background(lambda: _token_script(runtime))
    _waiting_on_a_lock(owner, 2)
    pause.release()

    assert recover.finish().error is None and recover.result["revoked_previous_count"] == 1
    assert isinstance(script.finish().error, create_agent_token.ScopeError) and "owned by an installation" in str(script.error)
    assert _script_token_rows(owner) == []
    assert _rows(owner, "SELECT is_active FROM agent_tokens WHERE token_hash = :h", h=_hash("CANARY-legacy-unbound-token")) == [
        {"is_active": True}]  # recovery does not touch a token bound to nothing


def test_a_recovery_after_a_refused_token_script_still_works(owned):
    owner, runtime = owned
    with pytest.raises(create_agent_token.ScopeError):
        _token_script(runtime)

    assert service.issue_recovery_token(service.get_library("acme"), OWNER, actor=ACTOR)["agent_token"]
    assert len(_active_keys(owner)) == 1


# =====================================================================================================================
# the lock order: nothing new deadlocks, and nothing live waits
# =====================================================================================================================

def test_a_retirement_a_token_script_and_an_offboarding_all_finish(db):
    # The retirement holds the branch and waits at its installation; the script holds the organization FOR SHARE and waits for the
    # branch; offboarding waits for the organization. Each releases the next.
    owner, runtime = db
    pause = Paused(owner, "SELECT id FROM collector_installations WHERE id = :i", i=SECOND)
    library = service.get_library("acme")
    retire = Background(lambda: service.change_status(library, SECOND, status="retired", actor=ACTOR))
    _waiting_on_a_lock(owner, 1)
    script = Background(lambda: _token_script(runtime))
    _waiting_on_a_lock(owner, 2)
    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 3)
    pause.release()

    assert retire.finish().error is None
    assert script.finish().error is None
    assert offboard.finish().error is None
    assert _live_tokens(owner) == [] and _active_keys(owner) == []


def test_an_enrollment_a_token_script_and_an_offboarding_all_finish(db):
    owner, runtime = db
    code = _new_code(runtime)
    held = Open(runtime)
    enrollment.redeem_enrollment_code(held.conn, code, contract_version=2)
    script = Background(lambda: _token_script(runtime))
    _waiting_on_a_lock(owner, 1)
    offboard = Background(_offboard)
    _waiting_on_a_lock(owner, 2)
    held.commit()

    assert isinstance(script.finish().error, create_agent_token.ScopeError)
    assert offboard.finish().error is None
    assert _live_tokens(owner) == [] and _active_keys(owner) == [] and _script_token_rows(owner) == []


def test_two_token_scripts_and_an_issue_at_one_branch_all_finish(db):
    owner, runtime = db
    held = Open(runtime)
    create_agent_token.validate_token_scope(held.conn, CUSTOMER, BRANCH)
    issue = Background(lambda: _issue_unowned(runtime))
    _waiting_on_a_lock(owner, 1)

    def second_script():
        with runtime.begin() as conn:
            create_agent_token.validate_token_scope(conn, CUSTOMER, BRANCH)

    script = Background(second_script)
    _waiting_on_a_lock(owner, 2)
    held.rollback()

    assert issue.finish().error is None and script.finish().error is None
    assert len(_active_keys(owner)) == 1


def test_an_event_insert_does_not_wait_for_a_token_script_holding_the_branch(owned):
    owner, runtime = owned
    held = Open(runtime)
    with owner.begin() as conn:
        conn.execute(text("UPDATE ingest_key_ids SET status = 'retired', retired_at = now() WHERE key_id = :k"), {"k": OWNED_KEY})
    _script_transaction(held.conn)  # only a retired owned key, so this one is allowed

    with runtime.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '3s'"))
        _tenant_context(conn)
        conn.execute(text("INSERT INTO checkin_events (customer_id, branch_id, key_id, event_key, event_time, destination, bin) "
                          "VALUES (10, 1, :k, :e, now(), 'westside', '3')"), {"k": OWNED_KEY, "e": "ab" * 32})

    held.commit()


def test_a_heartbeat_write_does_not_wait_for_a_token_script_holding_the_branch(db):
    owner, runtime = db
    with owner.begin() as conn:
        _insert_key(conn, UNOWNED_KEY, None)
    held = Open(runtime)
    _script_transaction(held.conn)

    with runtime.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '3s'"))
        _tenant_context(conn)
        assert conn.execute(text("UPDATE ingest_key_ids SET last_heartbeat_at = now() WHERE key_id = :k"), {"k": UNOWNED_KEY}).rowcount == 1
        conn.execute(text("UPDATE collector_installations SET last_seen_at = now() WHERE id = :i"), {"i": OWNER})

    held.commit()


def test_a_first_upload_marking_the_branch_ready_waits_and_finishes(db):
    owner, runtime = db
    with owner.begin() as conn:
        conn.execute(text("UPDATE branches SET onboarding_status = 'pending' WHERE id = :b"), {"b": BRANCH})
    held = Open(runtime)
    _script_transaction(held.conn)

    def first_upload():
        with runtime.begin() as conn:
            return conn.execute(text("UPDATE branches SET onboarding_status = 'ready' WHERE id = :b AND onboarding_status = 'pending'"),
                                {"b": BRANCH}).rowcount

    upload = Background(first_upload)
    _waiting_on_a_lock(owner, 1)
    held.commit()

    assert upload.finish().error is None and upload.result == 1


# =====================================================================================================================
# legacy NULL-owner behavior is preserved
# =====================================================================================================================

def test_nothing_here_revokes_an_unbound_token_or_changes_a_legacy_keys_use(db):
    owner, runtime = db
    first = _issue_unowned(runtime)
    with pytest.raises(ValueError):
        _issue_unowned(runtime)
    _token_script(runtime)

    active = _rows(owner, "SELECT installation_id FROM agent_tokens WHERE is_active AND customer_id = :c", c=CUSTOMER)
    with owner.connect() as conn:
        problem = ingest_v2_service.ingest_key_problem(conn, CUSTOMER, BRANCH, first, token_installation_id=None)

    assert active == [{"installation_id": None}, {"installation_id": None}]
    assert problem is None
