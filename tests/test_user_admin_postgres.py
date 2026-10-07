"""R8C: administering an organization's members on a REAL PostgreSQL -- the migrated schema, real row locks, real
transactions, real audit rows, real sessions.

Nothing between a call and the rows is replaced: services.user_admin_service, services.auth_service,
services.session_service and every reader that decides access run their own statements against the database.

What only a real server can prove:

  * memberships.removed_at is what the migration says it is, existing rows survive it untouched, and a downgrade really
    does bring removed memberships back;
  * a removal is an UPDATE of one row, the audit row beside it is written in the same transaction, and the person's
    sessions go on validating while every access reader answers "no membership";
  * two changes that would each be fine alone, and together would leave an organization with NO active owner, cannot
    both happen -- whichever order they arrive in, however close together;
  * deactivating an account locks the organizations it owns in ascending id order, so two deactivations cannot deadlock;
  * the organization's activity is selected by the audit row's own jsonb metadata, and by nothing else.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_account_postgres.py: runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION, local server. The module creates its
own throwaway databases (migrated with the project's real Alembic chain) and drops them afterward. Production is never
touched.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from werkzeug.security import check_password_hash

import database
from services import (
    access_service,
    efficiency_settings_service,
    entitlement_service,
    session_service,
    tenant_resolution_service,
    user_admin_service,
)
from services.efficiency_settings import validate_organization_efficiency_settings
from services.user_admin_service import (
    add_organization_member,
    change_organization_member_role,
    list_org_users,
    list_recent_org_auth_events,
    remove_organization_member,
    set_global_account_active,
)

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
REVISION, PREVIOUS_HEAD = "e3a7b1c9d4f2", "16b41d730e15"

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL user administration tests)"
)

# Organization ids are deliberately NOT in the order they are created or joined: lock order must come from the id.
ACME, BETA, GAMMA = 30, 20, 10
ACME_CUSTOMER, BETA_CUSTOMER = 8101, 8202
ACME_MAIN, BETA_MAIN = 31, 21
OWNER, SECOND, ADMIN, VIEWER, BOTH, BETA_OWNER, STRANGER, PLATFORM = 101, 102, 103, 105, 106, 201, 501, 900
PASSWORD = "synthetic-Temp-Password-1"
ROUNDS = 8  # how many times each race is run: a lost update would need only one unlucky interleaving


def _email(user_id: int) -> str:
    return f"user{user_id}@example.invalid"


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
    """A brand-new database on the test server, migrated to `revision` and dropped when the context exits."""

    def __init__(self, revision: str):
        self.revision = revision
        self.admin = make_url(ADMIN_URL)
        _guard(self.admin)
        self.name = f"sortview_members_test_{secrets.token_hex(4)}"
        self.admin_engine = create_engine(self.admin, isolation_level="AUTOCOMMIT")

    def __enter__(self):
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{self.name}"'))  # nosec B608 - generated name, no user input
        self.url = self.admin.set(database=self.name)
        self.migrate("upgrade", self.revision)
        self.engine = create_engine(self.url, hide_parameters=True)
        return self

    def migrate(self, *args: str) -> None:
        done = _alembic(self.url, *args)
        assert done.returncode == 0, done.stderr[-2000:]

    def __exit__(self, *_exc):
        self.engine.dispose()
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))  # nosec B608
        self.admin_engine.dispose()


@pytest.fixture(scope="module")
def engine():
    with Throwaway("head") as throwaway:
        yield throwaway.engine


def rows(engine, sql, **params):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params)]


def run(engine, sql, **params) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _seed_organization(conn, organization_id, slug, status="active", customer=None) -> None:
    if customer is not None:
        conn.execute(text("INSERT INTO customers (id, name) VALUES (:c, :n)"), {"c": customer, "n": slug})
    conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES (:i, :s, :n, :st, :c)"),
                 {"i": organization_id, "s": slug, "n": f"{slug.title()} Library", "st": status, "c": customer})


def _seed_member(conn, organization_id, user_id, role) -> None:
    conn.execute(text("INSERT INTO memberships (organization_id, user_id, role) VALUES (:o, :u, :r)"),
                 {"o": organization_id, "u": user_id, "r": role})


@pytest.fixture
def db(engine, monkeypatch):
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE organization_settings, branch_settings, password_reset_tokens, auth_sessions, auth_audit_log, memberships, "
            "branches, organizations, app_users, customers RESTART IDENTITY CASCADE"
        ))
        _seed_organization(conn, ACME, "acme", customer=ACME_CUSTOMER)
        _seed_organization(conn, BETA, "beta", customer=BETA_CUSTOMER)
        _seed_organization(conn, GAMMA, "gamma")
        for branch_id, organization_id in ((ACME_MAIN, ACME), (BETA_MAIN, BETA)):
            conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) "
                              "VALUES (:b, :o, 'main', 'Main', TRUE, 'active', :b)"), {"b": branch_id, "o": organization_id})
        for user_id in (OWNER, SECOND, ADMIN, VIEWER, BOTH, BETA_OWNER, STRANGER, PLATFORM):
            conn.execute(text("INSERT INTO app_users (id, email, password_hash, is_platform_admin) VALUES (:i, :e, 'CANARY-HASH', :p)"),
                         {"i": user_id, "e": _email(user_id), "p": user_id == PLATFORM})
        for organization_id, user_id, role in ((ACME, OWNER, "owner"), (ACME, SECOND, "owner"), (ACME, ADMIN, "admin"),
                                               (ACME, VIEWER, "viewer"), (ACME, BOTH, "admin"), (BETA, BETA_OWNER, "owner"),
                                               (BETA, BOTH, "viewer")):
            _seed_member(conn, organization_id, user_id, role)
        # Ids were given explicitly; move the sequences past them so a created account gets a fresh one.
        conn.execute(text("SELECT setval(pg_get_serial_sequence('app_users', 'id'), 5000)"))
    # The one flat engine every service uses.
    monkeypatch.setattr(database, "_engine", engine)
    return engine


def _membership(db, organization_id, user_id):
    found = rows(db, "SELECT id, role, removed_at FROM memberships WHERE organization_id = :o AND user_id = :u", o=organization_id, u=user_id)
    assert len(found) <= 1
    return found[0] if found else None


def _active_owners(db, organization_id) -> list[int]:
    return [r[0] for r in rows(db, """
        SELECT m.user_id FROM memberships m JOIN app_users u ON u.id = m.user_id
        WHERE m.organization_id = :o AND m.role = 'owner' AND m.removed_at IS NULL AND u.is_active ORDER BY 1""", o=organization_id)]


def _audit(db):
    # Everything recorded except the sessions a test itself opened (session_service records those).
    return rows(db, "SELECT event_type, user_id, email, is_success, metadata FROM auth_audit_log "
                    "WHERE event_type <> 'session_created' ORDER BY id")


# =====================================================================================================================
# The migration
# =====================================================================================================================

def test_the_column_is_a_nullable_timestamptz_with_no_default_and_the_table_is_otherwise_as_it_was(db):
    assert rows(db, "SELECT data_type, is_nullable, column_default FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND table_name = 'memberships' AND column_name = 'removed_at'") == [("timestamp with time zone", "YES", None)]
    constraints = rows(db, "SELECT contype::text, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'public.memberships'::regclass")
    assert ("u", "UNIQUE (organization_id, user_id)") in constraints
    assert not [d for _, d in rows(db, "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'memberships'") if "removed_at" in d]
    assert rows(db, "SELECT count(*) FROM memberships WHERE removed_at IS NOT NULL") == [(0,)]


def test_upgrading_leaves_every_membership_active_and_downgrading_brings_removed_ones_back():
    with Throwaway(PREVIOUS_HEAD) as old:
        with old.engine.begin() as conn:
            _seed_organization(conn, ACME, "acme")
            for user_id, role in ((OWNER, "owner"), (VIEWER, "viewer")):
                conn.execute(text("INSERT INTO app_users (id, email) VALUES (:i, :e)"), {"i": user_id, "e": _email(user_id)})
                _seed_member(conn, ACME, user_id, role)
        before = rows(old.engine, "SELECT id, organization_id, user_id, role, created_at FROM memberships ORDER BY id")
        assert "removed_at" not in [r[0] for r in rows(old.engine, "SELECT column_name FROM information_schema.columns WHERE table_name = 'memberships'")]

        old.migrate("upgrade", REVISION)

        assert rows(old.engine, "SELECT id, organization_id, user_id, role, created_at FROM memberships ORDER BY id") == before
        assert rows(old.engine, "SELECT count(*) FROM memberships WHERE removed_at IS NOT NULL") == [(0,)]  # no backfill
        run(old.engine, "UPDATE memberships SET removed_at = now() WHERE user_id = :u", u=VIEWER)

        old.migrate("downgrade", PREVIOUS_HEAD)

        # The row is still there and nothing says it was removed: to the previous version it is an active membership.
        assert rows(old.engine, "SELECT id, organization_id, user_id, role, created_at FROM memberships ORDER BY id") == before
        assert "removed_at" not in [r[0] for r in rows(old.engine, "SELECT column_name FROM information_schema.columns WHERE table_name = 'memberships'")]

        old.migrate("upgrade", REVISION)
        assert rows(old.engine, "SELECT count(*) FROM memberships WHERE removed_at IS NOT NULL") == [(0,)]


# =====================================================================================================================
# One membership of one organization
# =====================================================================================================================

def test_removal_is_one_updated_row_with_its_audit_row_and_the_account_and_sessions_are_untouched(db):
    token = session_service.create_session(BOTH)["token"]
    accounts = rows(db, "SELECT id, email, password_hash, is_active, is_platform_admin FROM app_users ORDER BY id")
    others = rows(db, "SELECT * FROM memberships WHERE NOT (organization_id = :o AND user_id = :u) ORDER BY id", o=ACME, u=BOTH)
    membership_id = _membership(db, ACME, BOTH)[0]

    assert remove_organization_member("acme", BOTH, actor_user_id=OWNER) == {"ok": True, "message": "User removed from this organization."}

    assert rows(db, "SELECT id, role, removed_at IS NOT NULL, removed_at BETWEEN now() - interval '1 minute' AND now() + interval '1 minute' "
                    "FROM memberships WHERE organization_id = :o AND user_id = :u", o=ACME, u=BOTH) == [(membership_id, "admin", True, True)]
    assert rows(db, "SELECT * FROM memberships WHERE NOT (organization_id = :o AND user_id = :u) ORDER BY id", o=ACME, u=BOTH) == others
    assert rows(db, "SELECT id, email, password_hash, is_active, is_platform_admin FROM app_users ORDER BY id") == accounts
    assert rows(db, "SELECT count(*) FROM auth_sessions WHERE user_id = :u AND revoked_at IS NULL", u=BOTH) == [(1,)]
    assert session_service.validate_session(token)["id"] == BOTH  # still signed in ...
    assert _audit(db) == [("membership_removed", BOTH, _email(BOTH), True,
                           {"org_slug": "acme", "previous_role": "admin", "role": None, "actor_user_id": OWNER, "actor_email": _email(OWNER)})]


def test_a_removed_member_is_refused_by_every_reader_that_decides_access_and_keeps_the_other_organization(db):
    acme_settings = validate_organization_efficiency_settings({})
    assert tenant_resolution_service.resolve_operational_tenant(BOTH, "acme", "main") is not None
    assert efficiency_settings_service.read_organization_efficiency("acme", user_id=BOTH) is not None  # an admin of acme

    assert remove_organization_member("acme", BOTH, actor_user_id=OWNER)["ok"]

    # ... and is, on the very next read, a member of nothing in acme.
    assert access_service.user_can_access_org(BOTH, "acme") is False
    assert entitlement_service.get_org_role_for_user(BOTH, "acme") is None
    assert tenant_resolution_service.resolve_operational_tenant(BOTH, "acme", "main") is None
    assert efficiency_settings_service.read_organization_efficiency("acme", user_id=BOTH) is None
    assert efficiency_settings_service.replace_organization_efficiency("acme", acme_settings, user_id=BOTH) is None
    assert efficiency_settings_service.read_sorter_efficiency("acme", "main", user_id=BOTH) is None
    assert BOTH not in [u["user_id"] for u in list_org_users("acme")]
    assert remove_organization_member("acme", VIEWER, actor_user_id=BOTH)["code"] == "not_permitted"
    # beta is exactly as it was.
    assert [m["organization_slug"] for m in access_service.get_user_memberships(BOTH)] == ["beta"]
    assert access_service.user_can_access_org(BOTH, "beta") is True
    assert entitlement_service.get_org_role_for_user(BOTH, "beta") == "viewer"
    resolved = tenant_resolution_service.resolve_operational_tenant(BOTH, "beta", "main")
    assert (resolved.operational_customer_id, resolved.operational_branch_id) == (BETA_CUSTOMER, BETA_MAIN)


def test_the_member_lookup_finds_only_an_active_member_of_that_organization_by_address(db):
    # R8D: how the customer API turns an address into the member to act on.
    find = user_admin_service.find_active_member_id

    assert find("acme", _email(BOTH)) == BOTH and find("beta", f"  {_email(BOTH).upper()} ") == BOTH
    assert find("acme", _email(BETA_OWNER)) is None and find("gamma", _email(OWNER)) is None
    assert find("acme", _email(STRANGER)) is None and find("acme", "nobody@example.invalid") is None
    assert remove_organization_member("acme", BOTH, actor_user_id=OWNER)["ok"]
    assert find("acme", _email(BOTH)) is None and find("beta", _email(BOTH)) == BOTH


def test_adding_back_reuses_the_one_row_with_the_role_given_now(db):
    assert remove_organization_member("acme", SECOND, actor_user_id=OWNER)["ok"]
    membership_id = _membership(db, ACME, SECOND)[0]

    assert add_organization_member("acme", _email(SECOND), PASSWORD, "", "owner", actor_user_id=ADMIN)["code"] == "owner_required"
    assert _membership(db, ACME, SECOND)[2] is not None  # still removed
    assert add_organization_member("acme", _email(SECOND), PASSWORD, "", "viewer", actor_user_id=ADMIN)["ok"]

    assert _membership(db, ACME, SECOND) == (membership_id, "viewer", None)
    assert rows(db, "SELECT count(*) FROM memberships WHERE organization_id = :o AND user_id = :u", o=ACME, u=SECOND) == [(1,)]
    assert rows(db, "SELECT password_hash FROM app_users WHERE id = :u", u=SECOND) == [("CANARY-HASH",)]  # the password given was ignored


def test_a_new_address_gets_a_real_account_and_the_record_holds_no_password(db):
    result = add_organization_member("acme", "New.Person@Example.invalid", PASSWORD, "New Person", "manager", actor_user_id=ADMIN)

    assert result["ok"]
    [(user_id, password_hash, is_active)] = rows(db, "SELECT id, password_hash, is_active FROM app_users WHERE email = 'new.person@example.invalid'")
    assert check_password_hash(password_hash, PASSWORD) and is_active
    assert _membership(db, ACME, user_id)[1:] == ("manager", None)
    recorded = repr(rows(db, "SELECT event_type, message, metadata::text FROM auth_audit_log"))
    assert PASSWORD not in recorded and password_hash not in recorded
    # Creating the account is the ACCOUNT's event; only the membership is acme's.
    assert [(e["event_type"], e["metadata"]["role"]) for e in list_recent_org_auth_events("acme")] == [("membership_added", "manager")]


def test_a_new_account_and_its_membership_are_one_transaction(db):
    statements: list[str] = []

    def record(conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().startswith(("INSERT INTO app_users", "INSERT INTO memberships", "INSERT INTO auth_audit_log")):
            statements.append((statement.split()[2], id(conn.connection.dbapi_connection)))

    event.listen(db, "before_cursor_execute", record)
    try:
        assert add_organization_member("acme", "pat@example.invalid", PASSWORD, "Pat", "viewer", actor_user_id=OWNER)["ok"]
    finally:
        event.remove(db, "before_cursor_execute", record)

    # The account, its record, the membership and its record: in that order, all on ONE connection.
    assert [table for table, _ in statements] == ["app_users", "auth_audit_log", "memberships", "auth_audit_log"]
    assert len({connection for _, connection in statements}) == 1
    assert [e[0] for e in _audit(db)] == ["user_create_success", "membership_added"]


@pytest.mark.parametrize("fails_at", ["INSERT INTO memberships", "membership_added"], ids=["the-membership-insert", "the-membership-record"])
def test_if_the_membership_cannot_be_completed_no_account_is_left_behind(db, monkeypatch, fails_at):
    """The account has been INSERTed -- and then the membership's own INSERT, or its audit record, fails."""
    from services import auth_service

    seen: list[str] = []

    def fail_the_insert(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().startswith("INSERT INTO app_users"):
            seen.append("account inserted")
        if fails_at in statement:
            raise RuntimeError("the membership could not be written")

    really_record = auth_service.log_auth_event_with_connection

    def fail_the_record(conn, event_type, *args, **kwargs):
        if event_type == fails_at:
            raise RuntimeError("the membership could not be recorded")
        really_record(conn, event_type, *args, **kwargs)

    before = (rows(db, "SELECT id, email FROM app_users ORDER BY id"), rows(db, "SELECT * FROM memberships ORDER BY id"), _audit(db))
    event.listen(db, "before_cursor_execute", fail_the_insert)
    monkeypatch.setattr(auth_service, "log_auth_event_with_connection", fail_the_record)
    try:
        with pytest.raises(RuntimeError):
            add_organization_member("acme", "orphan@example.invalid", PASSWORD, "Orphan", "viewer", actor_user_id=OWNER)
    finally:
        event.remove(db, "before_cursor_execute", fail_the_insert)

    assert seen == ["account inserted"]  # the account really was written inside the transaction ...
    assert rows(db, "SELECT count(*) FROM app_users WHERE email = 'orphan@example.invalid'") == [(0,)]  # ... and went with it
    assert (rows(db, "SELECT id, email FROM app_users ORDER BY id"), rows(db, "SELECT * FROM memberships ORDER BY id"), _audit(db)) == before

    # Nothing is left in the way: the same address can be added straight afterwards.
    monkeypatch.undo()
    monkeypatch.setattr(database, "_engine", db)
    assert add_organization_member("acme", "orphan@example.invalid", PASSWORD, "Orphan", "viewer", actor_user_id=OWNER)["ok"]
    assert rows(db, "SELECT count(*) FROM app_users WHERE email = 'orphan@example.invalid'") == [(1,)]


def _add_while_another_transaction_is_creating(db, email, finish):
    """Another transaction has INSERTed the account for `email` and not yet committed. add_organization_member is
    started and really waits on that row; then the other transaction is finished with `finish` (commit / rollback)."""
    from services import auth_service

    answers: list = []

    def add():
        try:
            answers.append(add_organization_member("acme", email, PASSWORD, "Our Name", "viewer", actor_user_id=OWNER))
        except BaseException as error:  # a raw database error here is exactly the regression
            answers.append(error)

    other = db.connect()
    transaction = other.begin()
    try:
        theirs = auth_service.create_user_with_connection(other, email, "synthetic-Their-Password-9", "Their Name")
        assert theirs is not None
        thread = threading.Thread(target=add)
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive() and answers == []  # waiting for the other creation to be decided
        getattr(transaction, finish)()
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        other.close()
    return theirs, answers


def test_an_account_created_by_another_transaction_at_the_same_moment_is_attached_as_it_is(db):
    email = "same.moment@example.invalid"

    theirs, answers = _add_while_another_transaction_is_creating(db, email, "commit")

    assert answers == [{"ok": True, "message": "The user was added to this organization. If they already had a SortView account, "
                                               "their existing password is unchanged."}]
    [(user_id, password_hash, full_name, is_active)] = rows(db, "SELECT id, password_hash, full_name, is_active FROM app_users WHERE lower(email) = :e", e=email)
    assert user_id == theirs["id"] and full_name == "Their Name" and is_active is True
    assert check_password_hash(password_hash, "synthetic-Their-Password-9") and not check_password_hash(password_hash, PASSWORD)
    assert rows(db, "SELECT organization_id, role, removed_at FROM memberships WHERE user_id = :u", u=user_id) == [(ACME, "viewer", None)]
    # ONE creation was recorded -- theirs -- and this operation recorded only what it did: the membership.
    audit = _audit(db)
    assert [(e[0], e[1]) for e in audit] == [("user_create_success", user_id), ("membership_added", user_id)]
    assert audit[1][4]["actor_user_id"] == OWNER


def test_if_the_other_creation_is_rolled_back_this_one_creates_the_account_itself(db):
    email = "rolled.back@example.invalid"

    theirs, answers = _add_while_another_transaction_is_creating(db, email, "rollback")

    assert [a["ok"] for a in answers] == [True]
    [(user_id, password_hash, full_name)] = rows(db, "SELECT id, password_hash, full_name FROM app_users WHERE lower(email) = :e", e=email)
    assert user_id != theirs["id"] and full_name == "Our Name" and check_password_hash(password_hash, PASSWORD)
    assert rows(db, "SELECT organization_id, role FROM memberships WHERE user_id = :u", u=user_id) == [(ACME, "viewer")]
    assert [(e[0], e[1]) for e in _audit(db)] == [("user_create_success", user_id), ("membership_added", user_id)]


def test_two_organizations_adding_the_same_new_address_at_once_share_one_account(db):
    for round_number in range(ROUNDS):
        email = f"shared{round_number}@example.invalid"

        results = _race(
            lambda email=email: add_organization_member("acme", email, PASSWORD, "Pat", "viewer", actor_user_id=OWNER),
            lambda email=email: add_organization_member("beta", email, "synthetic-Other-Password-7", "Pat", "manager", actor_user_id=BETA_OWNER),
        )

        assert [r["ok"] for r in results] == [True, True], results  # no raw unique-constraint error, no refusal
        [(user_id, password_hash)] = rows(db, "SELECT id, password_hash FROM app_users WHERE lower(email) = :e", e=email)
        # Whichever created it, its password is the creator's and was not then overwritten by the other.
        assert check_password_hash(password_hash, PASSWORD) != check_password_hash(password_hash, "synthetic-Other-Password-7")
        assert rows(db, "SELECT organization_id, role FROM memberships WHERE user_id = :u AND removed_at IS NULL ORDER BY 1 DESC",
                    u=user_id) == [(ACME, "viewer"), (BETA, "manager")]
        events = [e[0] for e in _audit(db) if e[1] == user_id]
        assert sorted(events) == ["membership_added", "membership_added", "user_create_success"], events


def test_a_refused_add_creates_no_account(db):
    for actor, role in ((ADMIN, "owner"), (VIEWER, "viewer"), (BETA_OWNER, "viewer"), (OWNER, "root")):
        assert not add_organization_member("acme", "nobody@example.invalid", PASSWORD, "", role, actor_user_id=actor)["ok"]

    assert rows(db, "SELECT count(*) FROM app_users WHERE email = 'nobody@example.invalid'") == [(0,)] and _audit(db) == []


def test_an_existing_account_is_added_exactly_as_it_is_even_when_it_is_switched_off(db):
    run(db, "UPDATE app_users SET is_active = FALSE WHERE id = :u", u=STRANGER)
    account = rows(db, "SELECT * FROM app_users WHERE id = :u", u=STRANGER)

    assert add_organization_member("acme", _email(STRANGER).upper(), "a-password-that-must-be-ignored", "Other Name", "viewer", actor_user_id=OWNER)["ok"]

    assert rows(db, "SELECT * FROM app_users WHERE id = :u", u=STRANGER) == account  # hash, name, is_active = FALSE: untouched
    assert _membership(db, ACME, STRANGER)[1:] == ("viewer", None)
    assert rows(db, "SELECT count(*) FROM app_users") == [(8,)] and [e[0] for e in _audit(db)] == ["membership_added"]


def test_the_public_create_user_still_creates_and_records_and_refuses_a_duplicate(db):
    from services import auth_service

    created = auth_service.create_user("Solo@Example.invalid", PASSWORD, " Solo Person ")

    assert (created["email"], created["full_name"], created["is_active"]) == ("solo@example.invalid", "Solo Person", True)
    assert [(e[0], e[1]) for e in _audit(db)] == [("user_create_success", created["id"])]
    with pytest.raises(auth_service.UserAlreadyExistsError):
        auth_service.create_user("solo@example.invalid", PASSWORD)
    assert [e[0] for e in _audit(db)] == ["user_create_success", "user_create_failed"]


def test_a_refused_change_writes_nothing_at_all(db):
    before = rows(db, "SELECT * FROM memberships ORDER BY id"), _audit(db)
    run(db, "UPDATE memberships SET role = 'admin' WHERE organization_id = :o AND user_id = :u", o=ACME, u=SECOND)
    before = rows(db, "SELECT * FROM memberships ORDER BY id"), _audit(db)

    assert remove_organization_member("acme", OWNER, actor_user_id=OWNER)["code"] == "last_owner"
    assert change_organization_member_role("acme", OWNER, "viewer", actor_user_id=OWNER)["code"] == "last_owner"
    assert change_organization_member_role("acme", OWNER, "viewer", actor_user_id=ADMIN)["code"] == "owner_required"
    assert remove_organization_member("acme", ADMIN, actor_user_id=BETA_OWNER)["code"] == "not_permitted"

    assert (rows(db, "SELECT * FROM memberships ORDER BY id"), _audit(db)) == before


def test_the_record_is_in_the_same_transaction_as_the_change(db, monkeypatch):
    from services import auth_service

    def fail(*_args, **_kwargs):
        raise RuntimeError("the audit log could not be written")

    monkeypatch.setattr(auth_service, "log_auth_event_with_connection", fail)

    with pytest.raises(RuntimeError):
        remove_organization_member("acme", VIEWER, actor_user_id=OWNER)
    with pytest.raises(RuntimeError):
        change_organization_member_role("acme", VIEWER, "admin", actor_user_id=OWNER)

    assert _membership(db, ACME, VIEWER)[1:] == ("viewer", None)  # no change without its record


# =====================================================================================================================
# What happened in this organization
# =====================================================================================================================

def test_activity_is_selected_by_the_rows_own_metadata_and_never_leaks_across_organizations(db):
    from services import auth_service

    auth_service.log_auth_event("login_success", True, user_id=BOTH, email=_email(BOTH), message="Login successful.")
    auth_service.log_auth_event("password_changed", True, user_id=BOTH, email=_email(BOTH), message="Password changed.")
    assert change_organization_member_role("beta", BOTH, "admin", actor_user_id=BETA_OWNER)["ok"]
    assert change_organization_member_role("acme", BOTH, "viewer", actor_user_id=OWNER)["ok"]
    assert set_global_account_active(VIEWER, False, actor_user_id=PLATFORM)["ok"]  # VIEWER is a member of acme

    acme, beta = list_recent_org_auth_events("acme"), list_recent_org_auth_events("beta")

    assert [(e["event_type"], e["metadata"]["org_slug"], e["metadata"]["role"]) for e in acme] == [("membership_role_updated", "acme", "viewer")]
    assert [(e["event_type"], e["metadata"]["org_slug"], e["metadata"]["role"]) for e in beta] == [("membership_role_updated", "beta", "admin")]
    assert list_recent_org_auth_events("gamma") == []
    assert len(_audit(db)) == 5  # all five were recorded; each organization is shown only its own one


def test_membership_activity_is_filtered_by_kind_before_the_limit_is_applied(db):
    from services import auth_service

    kinds = user_admin_service.MEMBERSHIP_EVENT_TYPES
    for role in ("manager", "admin", "viewer"):  # the oldest three events: membership changes in acme
        assert change_organization_member_role("acme", VIEWER, role, actor_user_id=OWNER)["ok"]
    for _ in range(12):                          # twelve NEWER events of another kind, also attributed to acme
        auth_service.log_auth_event("user_status_updated", True, user_id=VIEWER, email=_email(VIEWER), metadata={"org_slug": "acme"})
    assert change_organization_member_role("beta", BOTH, "admin", actor_user_id=BETA_OWNER)["ok"]

    assert {e["event_type"] for e in list_recent_org_auth_events("acme", limit=10)} == {"user_status_updated"}  # unchanged default
    filtered = list_recent_org_auth_events("acme", limit=10, event_types=kinds)
    assert [(e["event_type"], e["metadata"]["org_slug"], e["metadata"]["role"]) for e in filtered] == [
        ("membership_role_updated", "acme", "viewer"), ("membership_role_updated", "acme", "admin"), ("membership_role_updated", "acme", "manager")]
    assert [e["metadata"]["role"] for e in list_recent_org_auth_events("acme", limit=2, event_types=kinds)] == ["viewer", "admin"]
    assert list_recent_org_auth_events("acme", limit=10, event_types=()) == []


# =====================================================================================================================
# The whole account
# =====================================================================================================================

def test_switching_an_account_off_revokes_its_real_sessions_and_is_refused_for_a_sole_owner_anywhere(db):
    with db.begin() as conn:
        _seed_member(conn, BETA, OWNER, "owner")     # OWNER co-owns beta with BETA_OWNER ...
        _seed_member(conn, GAMMA, OWNER, "owner")    # ... and is gamma's ONLY owner
    token, other = session_service.create_session(OWNER)["token"], session_service.create_session(SECOND)["token"]

    assert set_global_account_active(OWNER, False, actor_user_id=PLATFORM)["code"] == "last_owner"
    assert session_service.validate_session(token) is not None and _audit(db) == []

    with db.begin() as conn:
        _seed_member(conn, GAMMA, SECOND, "owner")
    assert set_global_account_active(OWNER, False, actor_user_id=OWNER)["code"] == "platform_admin_required"
    assert set_global_account_active(OWNER, False, actor_user_id=PLATFORM)["ok"]

    assert rows(db, "SELECT is_active FROM app_users WHERE id = :u", u=OWNER) == [(False,)]
    assert session_service.validate_session(token) is None and session_service.validate_session(other) is not None
    assert rows(db, "SELECT count(*) FROM memberships WHERE user_id = :u AND removed_at IS NULL", u=OWNER) == [(3,)]  # no membership removed
    assert _audit(db) == [("user_status_updated", OWNER, _email(OWNER), True,
                           {"scope": "account", "previous_is_active": True, "is_active": False,
                            "actor_user_id": PLATFORM, "actor_email": _email(PLATFORM)})]
    assert (_active_owners(db, ACME), _active_owners(db, BETA), _active_owners(db, GAMMA)) == ([SECOND], [BETA_OWNER], [SECOND])


def test_deactivation_locks_the_organizations_the_account_owns_in_ascending_id_order(db):
    with db.begin() as conn:
        for organization_id in (BETA, GAMMA):  # joined in an order that is neither ascending nor the creation order
            _seed_member(conn, organization_id, OWNER, "owner")
            _seed_member(conn, organization_id, SECOND, "owner")
    locked: list[int] = []

    def record(_conn, _cursor, statement, parameters, _context, _executemany):
        if "FOR UPDATE" in statement and "organizations" in statement:
            locked.append(parameters["organization_id"])

    event.listen(db, "before_cursor_execute", record)
    try:
        assert set_global_account_active(OWNER, False, actor_user_id=PLATFORM)["ok"]
    finally:
        event.remove(db, "before_cursor_execute", record)

    assert locked == sorted([ACME, BETA, GAMMA]) == [GAMMA, BETA, ACME]


def test_deactivation_refuses_with_concurrent_change_when_the_account_comes_to_own_another_organization_meanwhile(db):
    """No hook in the service: another administrator's transaction really holds acme's row, so the deactivation really
    waits there -- having read that OWNER owns acme alone -- while OWNER is really made an owner of beta."""
    token = session_service.create_session(OWNER)["token"]
    answers: list = []
    holder = db.connect()
    transaction = holder.begin()
    try:
        holder.execute(text("SELECT id FROM organizations WHERE id = :o FOR UPDATE"), {"o": ACME})
        thread = threading.Thread(target=lambda: answers.append(set_global_account_active(OWNER, False, actor_user_id=PLATFORM)))
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive() and answers == []  # it has read "owns acme" and is waiting for acme's row
        with db.begin() as conn:                    # committed while it waits: OWNER now owns beta as well
            _seed_member(conn, BETA, OWNER, "owner")
        transaction.commit()
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        holder.close()

    assert answers == [{"ok": False, "code": "concurrent_change",
                        "message": "This account's organizations changed while it was being updated. Nothing was changed; try again."}]
    assert rows(db, "SELECT is_active FROM app_users WHERE id = :u", u=OWNER) == [(True,)]
    assert session_service.validate_session(token) is not None
    assert rows(db, "SELECT count(*) FROM auth_sessions WHERE user_id = :u AND revoked_at IS NULL", u=OWNER) == [(1,)]
    assert _audit(db) == []
    # Tried again, it sees both organizations, and beta has BETA_OWNER and acme has SECOND: it goes through.
    assert set_global_account_active(OWNER, False, actor_user_id=PLATFORM)["ok"]
    assert session_service.validate_session(token) is None


# =====================================================================================================================
# Two changes at once
# =====================================================================================================================

def _race(*changes) -> list:
    """Starts every change at the same instant, each on its own connection, and returns what each answered."""
    barrier = threading.Barrier(len(changes))
    results: list = [None] * len(changes)

    def go(index, change):
        try:
            barrier.wait(timeout=10)
            results[index] = change()
        except BaseException as error:  # reported to the test: a deadlock or a timeout is a failure, not a refusal
            results[index] = error

    threads = [threading.Thread(target=go, args=(i, c)) for i, c in enumerate(changes)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "a change never finished: deadlock?"
    assert not [r for r in results if isinstance(r, BaseException)], results
    return results


def _two_owners_again(db) -> None:
    run(db, "UPDATE memberships SET role = 'owner', removed_at = NULL WHERE organization_id = :o AND user_id IN (:a, :b)", o=ACME, a=OWNER, b=SECOND)
    run(db, "UPDATE app_users SET is_active = TRUE WHERE id IN (:a, :b)", a=OWNER, b=SECOND)


RACES = {
    "both-step-down": (lambda: change_organization_member_role("acme", OWNER, "admin", actor_user_id=OWNER),
                       lambda: change_organization_member_role("acme", SECOND, "admin", actor_user_id=SECOND)),
    "both-leave": (lambda: remove_organization_member("acme", OWNER, actor_user_id=OWNER),
                   lambda: remove_organization_member("acme", SECOND, actor_user_id=SECOND)),
    "each-removes-the-other": (lambda: remove_organization_member("acme", SECOND, actor_user_id=OWNER),
                               lambda: remove_organization_member("acme", OWNER, actor_user_id=SECOND)),
    "each-demotes-the-other": (lambda: change_organization_member_role("acme", SECOND, "viewer", actor_user_id=OWNER),
                               lambda: change_organization_member_role("acme", OWNER, "viewer", actor_user_id=SECOND)),
    "one-leaves-as-the-other-steps-down": (lambda: remove_organization_member("acme", OWNER, actor_user_id=OWNER),
                                           lambda: change_organization_member_role("acme", SECOND, "manager", actor_user_id=SECOND)),
    "one-leaves-as-the-others-account-is-switched-off": (lambda: remove_organization_member("acme", OWNER, actor_user_id=OWNER),
                                                         lambda: set_global_account_active(SECOND, False, actor_user_id=PLATFORM)),
    "both-accounts-switched-off": (lambda: set_global_account_active(OWNER, False, actor_user_id=PLATFORM),
                                   lambda: set_global_account_active(SECOND, False, actor_user_id=PLATFORM)),
}


@pytest.mark.parametrize("race", RACES, ids=list(RACES))
def test_two_changes_that_would_together_leave_no_owner_never_both_happen(db, race):
    for _ in range(ROUNDS):
        _two_owners_again(db)

        results = _race(*RACES[race])

        owners = _active_owners(db, ACME)
        assert len(owners) == 1, (race, results, owners)   # exactly one went through: not both, and not neither
        assert sorted(r["ok"] for r in results) == [False, True], results
        refused = next(r for r in results if not r["ok"])
        # The loser was refused for the right reason: because of the owner rule, or -- when the winner took away the
        # loser's own authority first -- because they may no longer administer at all.
        assert refused["code"] in {"last_owner", "not_permitted", "member_not_found", "owner_required"}, refused


def test_a_change_waits_for_the_organizations_row_and_then_decides_on_what_it_finds(db):
    answers: list = []
    holder = db.connect()
    transaction = holder.begin()
    try:
        # Another administrator's change is in flight: it holds acme's row and has demoted SECOND, uncommitted.
        holder.execute(text("SELECT id FROM organizations WHERE slug = 'acme' FOR UPDATE"))
        holder.execute(text("UPDATE memberships SET role = 'admin' WHERE organization_id = :o AND user_id = :u"), {"o": ACME, "u": SECOND})
        thread = threading.Thread(target=lambda: answers.append(remove_organization_member("acme", OWNER, actor_user_id=OWNER)))
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive() and answers == []  # waiting on the lock: it has not counted two owners and gone ahead
        transaction.commit()
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        holder.close()

    assert [a.get("code") for a in answers] == ["last_owner"]
    assert _active_owners(db, ACME) == [OWNER]


def test_changes_to_different_organizations_do_not_wait_for_each_other(db):
    holder = db.connect()
    transaction = holder.begin()
    try:
        holder.execute(text("SELECT id FROM organizations WHERE slug = 'beta' FOR UPDATE"))
        assert remove_organization_member("acme", VIEWER, actor_user_id=OWNER)["ok"]  # returns: acme's row is free
    finally:
        transaction.rollback()
        holder.close()


def test_two_deactivations_across_the_same_organizations_neither_deadlock_nor_orphan_one(db):
    # OWNER and SECOND are the only two owners of all three organizations.
    run(db, "UPDATE memberships SET removed_at = now() WHERE organization_id = :o AND user_id = :u", o=BETA, u=BETA_OWNER)
    with db.begin() as conn:
        for organization_id in (GAMMA, BETA):
            _seed_member(conn, organization_id, SECOND, "owner")
        for organization_id in (BETA, GAMMA):
            _seed_member(conn, organization_id, OWNER, "owner")

    for _ in range(ROUNDS):
        run(db, "UPDATE app_users SET is_active = TRUE WHERE id IN (:a, :b)", a=OWNER, b=SECOND)

        results = _race(lambda: set_global_account_active(OWNER, False, actor_user_id=PLATFORM),
                        lambda: set_global_account_active(SECOND, False, actor_user_id=PLATFORM))

        assert sorted(r["ok"] for r in results) == [False, True], results
        assert next(r for r in results if not r["ok"])["code"] == "last_owner"
        owners = [_active_owners(db, organization_id) for organization_id in (ACME, BETA, GAMMA)]
        assert owners[0] == owners[1] == owners[2] and len(owners[0]) == 1, owners


def test_the_service_locks_only_on_postgresql_and_this_is_postgresql(db):
    with db.connect() as conn:
        assert user_admin_service._is_postgresql(conn) is True
