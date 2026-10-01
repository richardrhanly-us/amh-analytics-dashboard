"""Permanent library offboarding -- the access cutoff (platform_admin_service.offboard_library) and the four provisioning
guards that keep a cancelled tenant from being re-provisioned.

Runs the REAL service SQL against SQLite (the approach of tests/test_collector_enrollment.py), so the joins, the scoping
predicates and the single-transaction behaviour are genuinely exercised, together with the real consumers of the result:
main.authenticate_agent, enrollment redemption, session validation and access_service. What needs PostgreSQL itself --
row level security on ingest_key_ids, real privileges, the append-only trigger -- is in
tests/test_tenant_lifecycle_postgres.py.

Three tenants, with operational customer ids that never equal the SaaS organization ids:

    org 1 "lib-a"   active     customer 10, branch 1   -- the one being offboarded
    org 2 "lib-b"   active     customer 20, branch 2   -- must be untouched by everything
    org 3 "lib-c"   cancelled  customer 30, branch 3   -- already offboarded, for the multi-organization session cases

Suspension itself is covered, unchanged, by tests/test_library_suspension.py; this file adds only that suspension and
offboarding stay separate.
"""

from __future__ import annotations

import hashlib
import inspect
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

import main
from src.services import (
    access_service,
    data_lifecycle_policy,
    ingest_v2_service,
    platform_admin_service,
    session_service,
    tenant_service,
)
from src.services import collector_enrollment_service as enrollment

_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"

# Values that must never reach lifecycle evidence. Each is stored somewhere in the tenant's rows below.
CANARY_BARCODE = "CANARY-BARCODE-31234000999"
CANARY_HOSTNAME = "CANARY-HOSTNAME-AMH-PC"
CANARY_EMAIL = "canary.staff@example.invalid"

ACTOR = {"actor_user_id": 900, "actor_label": "operator@example.invalid"}

_DDL = [
    "CREATE TABLE customers (id INTEGER PRIMARY KEY)",
    """CREATE TABLE organizations (
        id INTEGER PRIMARY KEY, slug TEXT, name TEXT, status TEXT, operational_customer_id INTEGER, updated_at TEXT)""",
    "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, status TEXT, operational_branch_id INTEGER)",
    """CREATE TABLE collector_installations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER, branch_id INTEGER, name TEXT, hostname TEXT,
        collector_version TEXT, status TEXT, installed_at TEXT, last_seen_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE agent_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE, customer_id INTEGER NOT NULL,
        branch_id INTEGER NOT NULL, description TEXT, is_active BOOLEAN NOT NULL DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_used_at TEXT, installation_id INTEGER)""",
    """CREATE TABLE collector_enrollment_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, installation_id INTEGER NOT NULL, code_hash TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL, used_at TEXT, revoked_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_by_user_id INTEGER)""",
    """CREATE TABLE ingest_key_ids (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key_id TEXT NOT NULL UNIQUE, customer_id INTEGER NOT NULL,
        branch_id INTEGER NOT NULL, algorithm TEXT NOT NULL, status TEXT NOT NULL, retired_at TEXT)""",
    """CREATE TABLE v2_cutovers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, branch_id INTEGER, cutover_at TEXT, set_by TEXT,
        set_at TEXT DEFAULT CURRENT_TIMESTAMP, note TEXT)""",
    """CREATE TABLE app_users (
        id INTEGER PRIMARY KEY, email TEXT, full_name TEXT, is_active BOOLEAN NOT NULL DEFAULT 1,
        is_platform_admin BOOLEAN NOT NULL DEFAULT 0)""",
    "CREATE TABLE memberships (id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER, user_id INTEGER, role TEXT)",
    """CREATE TABLE auth_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL, revoked_at TEXT, last_seen_at TEXT)""",
    "CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, organization_id INTEGER, status TEXT, ends_at TEXT)",
    "CREATE TABLE checkins (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, branch_id INTEGER, barcode TEXT)",
    """CREATE TABLE tenant_lifecycle_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, organization_id INTEGER NOT NULL,
        organization_slug TEXT NOT NULL, operational_customer_id INTEGER, actor_user_id INTEGER,
        actor_label TEXT NOT NULL, occurred_at TEXT DEFAULT CURRENT_TIMESTAMP, details TEXT NOT NULL)""",
]

# Everything a cutoff must leave exactly as it was, row for row.
_UNTOUCHED_BY_CUTOFF = ("customers", "branches", "memberships", "subscriptions", "checkins", "app_users", "v2_cutovers")
_ALL_TABLES = (
    "customers", "organizations", "branches", "collector_installations", "agent_tokens", "collector_enrollment_codes",
    "ingest_key_ids", "v2_cutovers", "app_users", "memberships", "auth_sessions", "subscriptions", "checkins",
    "tenant_lifecycle_events",
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _register_functions(dbapi_connection, _record):
        dbapi_connection.create_function("sha256hex", 1, _sha256)
        # tenant_service and platform_admin_service's suspend path say NOW(); SQLite has no such function.
        dbapi_connection.create_function("now", 0, lambda: "2026-01-01 00:00:00")

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _drop_row_locks(_conn, _cursor, statement, parameters, _context, _executemany):
        # set_library_active_status locks with a literal FOR UPDATE, which SQLite cannot parse and does not need.
        return statement.replace("for update", "").replace("FOR UPDATE", ""), parameters

    with engine.begin() as conn:
        for statement in _DDL:
            conn.execute(text(statement))

    for module in (platform_admin_service, tenant_service, session_service, access_service):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setattr(session_service, "_log_auth_event", lambda **_kwargs: None)
    monkeypatch.setattr(
        main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, "sha256hex(:token)")
    )
    access_service.get_user_memberships.clear()
    yield engine
    access_service.get_user_memberships.clear()
    engine.dispose()


def _run(engine, sql, **params):
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _rows(engine, table):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY 1")).mappings().all()]  # nosec B608


def _snapshot(engine, tables=_ALL_TABLES):
    return {table: _rows(engine, table) for table in tables}


def _token(engine, raw, customer_id, branch_id, *, active=True, installation_id=None):
    _run(engine,
         "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active, installation_id) "
         "VALUES (:h, :c, :b, 'test', :a, :i)",
         h=_sha256(raw), c=customer_id, b=branch_id, a=active, i=installation_id)


def _session(user_id) -> str:
    return session_service.create_session(user_id)["token"]


@pytest.fixture
def world(db):
    for org_id, slug, status, customer, branch in (
        (1, "lib-a", "active", 10, 1), (2, "lib-b", "active", 20, 2), (3, "lib-c", "cancelled", 30, 3),
    ):
        _run(db, "INSERT INTO customers VALUES (:c)", c=customer)
        _run(db, "INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES (:i, :s, :s, :st, :c)",
             i=org_id, s=slug, st=status, c=customer)
        _run(db, "INSERT INTO branches VALUES (:b, :o, 'active', :b)", b=branch, o=org_id)
        _run(db, "INSERT INTO subscriptions VALUES (:o, :o, 'active', NULL)", o=org_id)
        _run(db, "INSERT INTO checkins (customer_id, branch_id, barcode) VALUES (:c, :b, :code)",
             c=customer, b=branch, code=CANARY_BARCODE)

    # org 1: one installation in every status; org 2: one live installation.
    for installation_id, org_id, branch, status in (
        (101, 1, 1, "provisioning"), (102, 1, 1, "active"), (103, 1, 1, "inactive"), (104, 1, 1, "retired"),
        (201, 2, 2, "active"),
    ):
        _run(db, "INSERT INTO collector_installations (id, organization_id, branch_id, name, hostname, status) "
                 "VALUES (:i, :o, :b, :n, :h, :s)",
             i=installation_id, o=org_id, b=branch, n=f"Sorter {installation_id}", h=CANARY_HOSTNAME, s=status)

    _token(db, "a-bound", 10, 1, installation_id=102)        # enrollment-issued, bound to a live installation
    _token(db, "a-legacy", 10, 1)                            # legacy: installation_id NULL
    _token(db, "a-already-revoked", 10, 1, active=False)     # revoked long before the cutoff
    _token(db, "b-bound", 20, 2, installation_id=201)
    _token(db, "b-legacy", 20, 2)

    for key_id, customer, branch, status in (
        ("11111111-1111-4111-8111-111111111111", 10, 1, "active"),
        ("22222222-2222-4222-8222-222222222222", 20, 2, "active"),
    ):
        _run(db, "INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, algorithm, status) "
                 "VALUES (:k, :c, :b, 'hmac-sha256-v1', :s)", k=key_id, c=customer, b=branch, s=status)

    # Users: who keeps a session when org 1 is cancelled?
    users = {
        1: ("only-a", False, [1]),            # no other organization           -> revoked
        2: ("a-and-b", False, [1, 2]),        # also in an active organization  -> kept
        3: ("platform-admin", True, [1]),     # platform admin                  -> kept
        4: ("a-and-cancelled-c", False, [1, 3]),  # only other org is cancelled -> revoked
        5: ("only-b", False, [2]),            # not a member of org 1 at all    -> kept
    }
    for user_id, (name, is_admin, orgs) in users.items():
        _run(db, "INSERT INTO app_users (id, email, full_name, is_platform_admin) VALUES (:i, :e, :n, :a)",
             i=user_id, e=CANARY_EMAIL if user_id == 1 else f"{name}@example.invalid", n=name, a=is_admin)
        for org_id in orgs:
            _run(db, "INSERT INTO memberships (organization_id, user_id, role) VALUES (:o, :u, 'viewer')",
                 o=org_id, u=user_id)
    return db


def _offboard(organization_id=1, slug="lib-a"):
    return platform_admin_service.offboard_library(organization_id, slug, **ACTOR)


def _auth_status(engine, raw_token, customer_id, branch_id) -> int:
    with engine.begin() as conn:
        try:
            main.authenticate_agent(conn, f"Bearer {raw_token}", customer_id, branch_id)
        except HTTPException as exc:
            return exc.status_code
    return 200


def _events(engine):
    return [{**row, "details": json.loads(row["details"])} for row in _rows(engine, "tenant_lifecycle_events")]


def _org_status(engine, organization_id=1):
    return next(o["status"] for o in _rows(engine, "organizations") if o["id"] == organization_id)


# ======================================================================================================================
# the cutoff itself
# ======================================================================================================================

def test_offboarding_cancels_the_organization(world):
    result = _offboard()

    assert _org_status(world) == "cancelled"
    assert result["status"] == "cancelled" and result["changed"] is True and result["status_before"] == "active"


def test_offboarding_deactivates_every_token_of_the_tenant_including_legacy_unbound_ones(world):
    assert _auth_status(world, "a-bound", 10, 1) == 200 and _auth_status(world, "a-legacy", 10, 1) == 200

    result = _offboard()

    tokens = {t["token_hash"]: bool(t["is_active"]) for t in _rows(world, "agent_tokens")}
    assert tokens[_sha256("a-bound")] is False
    assert tokens[_sha256("a-legacy")] is False  # installation_id NULL: reachable only through customer_id
    assert result["counts"]["agent_tokens_deactivated"] == 2
    assert _auth_status(world, "a-bound", 10, 1) == 403 and _auth_status(world, "a-legacy", 10, 1) == 403


def test_offboarding_retires_every_installation_of_the_tenant(world):
    result = _offboard()

    statuses = {i["id"]: i["status"] for i in _rows(world, "collector_installations")}
    assert {statuses[101], statuses[102], statuses[103], statuses[104]} == {"retired"}
    assert result["counts"]["installations_retired"] == 3  # 104 was already retired


def test_an_unused_enrollment_code_cannot_be_redeemed_after_offboarding(world):
    with world.begin() as conn:
        code = enrollment.create_enrollment_code(conn, 101)["enrollment_code"]

    result = _offboard()

    assert result["counts"]["enrollment_codes_revoked"] == 1
    assert all(c["revoked_at"] is not None for c in _rows(world, "collector_enrollment_codes"))
    tokens_before = _rows(world, "agent_tokens")
    with pytest.raises(enrollment.EnrollmentError) as refused, world.begin() as conn:
        enrollment.redeem_enrollment_code(conn, code)
    assert refused.value.reason == "revoked"
    assert _rows(world, "agent_tokens") == tokens_before  # no token was issued


def test_a_new_enrollment_code_cannot_be_generated_for_an_offboarded_tenant(world):
    _offboard()

    with pytest.raises(enrollment.EnrollmentError), world.begin() as conn:
        enrollment.create_enrollment_code(conn, 101)
    assert _rows(world, "collector_enrollment_codes") == []


def test_offboarding_retires_the_tenants_active_ingest_keys(world):
    result = _offboard()

    keys = {k["customer_id"]: k for k in _rows(world, "ingest_key_ids")}
    assert keys[10]["status"] == "retired" and keys[10]["retired_at"] is not None
    assert result["counts"]["ingest_keys_retired"] == 1


def test_a_token_revoked_before_the_cutoff_stays_revoked_and_is_not_claimed_by_it(world):
    revoked_id = next(t["id"] for t in _rows(world, "agent_tokens") if t["token_hash"] == _sha256("a-already-revoked"))

    _offboard()

    (cutoff,) = _events(world)
    assert revoked_id not in cutoff["details"]["agent_token_ids"]  # so a reversal can never switch it back on
    assert _auth_status(world, "a-already-revoked", 10, 1) == 403
    # Nor does anything the Super Admin can still do bring it back: reactivation is refused outright.
    with pytest.raises(RuntimeError, match="cancelled"):
        platform_admin_service.set_library_active_status(1, is_active=True)
    assert _auth_status(world, "a-already-revoked", 10, 1) == 403


def test_another_tenant_is_untouched(world):
    before = {
        table: [row for row in _rows(world, table) if row.get("customer_id") == 20 or row.get("organization_id") == 2]
        for table in ("agent_tokens", "collector_installations", "ingest_key_ids", "subscriptions", "checkins")
    }
    org_b_before = next(o for o in _rows(world, "organizations") if o["id"] == 2)

    _offboard()

    after = {
        table: [row for row in _rows(world, table) if row.get("customer_id") == 20 or row.get("organization_id") == 2]
        for table in before
    }
    assert after == before
    assert next(o for o in _rows(world, "organizations") if o["id"] == 2) == org_b_before
    assert _auth_status(world, "b-bound", 20, 2) == 200 and _auth_status(world, "b-legacy", 20, 2) == 200


def test_offboarding_deletes_nothing_and_leaves_data_structure_and_billing_alone(world):
    before = _snapshot(world)

    _offboard()

    after = _snapshot(world)
    for table in _UNTOUCHED_BY_CUTOFF:
        assert after[table] == before[table], table
    for table in _ALL_TABLES:  # nothing is ever deleted; the only new row is the evidence
        expected = len(before[table]) + (1 if table == "tenant_lifecycle_events" else 0)
        assert len(after[table]) == expected, table


def test_offboarding_issues_no_delete_statement(world):
    statements: list[str] = []

    @event.listens_for(world, "before_cursor_execute")
    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(statement.lower().split()))

    _offboard()

    assert statements and not any(s.startswith(("delete", "truncate", "drop")) for s in statements)
    source = inspect.getsource(platform_admin_service)
    assert "delete from" not in source.lower()


# ======================================================================================================================
# sessions: multi-organization semantics
# ======================================================================================================================

def test_sessions_are_revoked_only_for_users_left_with_no_usable_organization(world):
    tokens = {user_id: _session(user_id) for user_id in (1, 2, 3, 4, 5)}

    result = _offboard()

    valid = {user_id: session_service.validate_session(token) is not None for user_id, token in tokens.items()}
    assert valid == {
        1: False,  # only belonged to the cancelled organization
        2: True,   # still a member of an active organization
        3: True,   # platform admin
        4: False,  # their only other organization was already cancelled
        5: True,   # never a member of the cancelled organization
    }
    assert result["counts"]["sessions_revoked"] == 2
    assert len(_rows(world, "auth_sessions")) == 5  # revoked in place, never deleted


def test_a_multi_organization_user_keeps_the_other_organization_but_cannot_reach_the_cancelled_one(world):
    token = _session(2)

    _offboard()

    assert session_service.validate_session(token) is not None
    access_service.get_user_memberships.clear()
    assert [m["organization_slug"] for m in access_service.get_user_memberships(2)] == ["lib-b"]
    assert access_service.get_org_access_mode("lib-a") == "blocked"
    assert access_service.get_org_access_mode("lib-b") == "full"


def test_a_suspended_other_organization_still_counts_as_usable(world):
    _run(world, "UPDATE organizations SET status = 'suspended' WHERE id = 2")
    token = _session(2)

    _offboard()

    assert session_service.validate_session(token) is not None  # suspended = read-only access, not none


# ======================================================================================================================
# idempotence, refusals, separation from suspension
# ======================================================================================================================

def test_offboarding_is_idempotent(world):
    first = _offboard()
    state_after_first = _snapshot(world, [t for t in _ALL_TABLES if t != "tenant_lifecycle_events"])

    second = _offboard()

    assert first["changed"] is True
    assert second["changed"] is False and second["status_before"] == "cancelled"
    assert not any(second["counts"].values())
    assert _snapshot(world, [t for t in _ALL_TABLES if t != "tenant_lifecycle_events"]) == state_after_first
    assert [e["event_type"] for e in _events(world)] == ["access_cutoff", "access_cutoff"]  # the repeat is on the record


def test_the_evidence_tells_the_first_cutoff_from_a_repeat_sweep_that_changed_nothing(world):
    _offboard()
    _offboard()

    first, repeat = (e["details"] for e in _events(world))
    assert (first["status_before"], first["repeat_sweep"], first["changed"]) == ("active", False, True)
    assert first["counts"] == {
        "agent_tokens_deactivated": 2, "installations_retired": 3, "enrollment_codes_revoked": 0,
        "ingest_keys_retired": 1, "sessions_revoked": 0,
    }
    assert (repeat["status_before"], repeat["repeat_sweep"], repeat["changed"]) == ("cancelled", True, False)
    assert set(repeat["counts"]) == set(first["counts"]) and not any(repeat["counts"].values())
    for id_list in ("agent_token_ids", "installations", "enrollment_code_ids", "ingest_key_row_ids", "session_ids"):
        assert repeat[id_list] == [], id_list  # a repeat claims no row as its own


def test_a_repeat_sweep_that_finds_something_live_again_says_so(world):
    _offboard()
    _token(world, "a-issued-by-hand-afterwards", 10, 1)  # e.g. a token inserted by an operator after the cutoff

    result = _offboard()

    repeat = _events(world)[1]["details"]
    assert (repeat["status_before"], repeat["repeat_sweep"], repeat["changed"]) == ("cancelled", True, True)
    assert repeat["counts"]["agent_tokens_deactivated"] == 1 and len(repeat["agent_token_ids"]) == 1
    assert result["changed"] is True and _auth_status(world, "a-issued-by-hand-afterwards", 10, 1) == 403


@pytest.mark.parametrize("slug", ["lib-b", "LIB-A", "", "lib-a-typo"])
def test_a_wrong_confirmation_slug_changes_nothing(world, slug):
    before = _snapshot(world)

    with pytest.raises(ValueError, match="does not match"):
        _offboard(slug=slug)

    assert _snapshot(world) == before


def test_an_unknown_organization_is_refused_and_nothing_is_written(world):
    before = _snapshot(world)

    with pytest.raises(RuntimeError, match="not found"):
        _offboard(organization_id=999, slug="lib-a")

    assert _snapshot(world) == before


def test_a_failure_part_way_rolls_the_whole_cutoff_back(world, monkeypatch):
    before = _snapshot(world)

    def explode(*_args, **_kwargs):
        raise RuntimeError("evidence could not be written")

    monkeypatch.setattr(platform_admin_service, "record_tenant_lifecycle_event", explode)

    with pytest.raises(RuntimeError, match="evidence could not be written"):
        _offboard()

    assert _snapshot(world) == before  # no cancelled organization without its revocations and its evidence


def test_a_suspended_library_can_be_offboarded_and_can_never_be_reactivated_afterwards(world):
    platform_admin_service.set_library_active_status(1, is_active=False)
    assert _org_status(world) == "suspended"

    result = _offboard()

    assert result["status_before"] == "suspended" and _org_status(world) == "cancelled"
    for is_active in (True, False):
        with pytest.raises(RuntimeError, match="cancelled"):
            platform_admin_service.set_library_active_status(1, is_active=is_active)
    assert _org_status(world) == "cancelled"
    assert _auth_status(world, "a-legacy", 10, 1) == 403


def test_suspension_still_changes_only_the_organization_status(world):
    before = _snapshot(world)

    platform_admin_service.set_library_active_status(1, is_active=False)

    after = _snapshot(world)
    assert _org_status(world) == "suspended"
    for table in _ALL_TABLES:
        if table != "organizations":
            assert after[table] == before[table], table  # tokens, installations, keys, sessions: all as they were
    platform_admin_service.set_library_active_status(1, is_active=True)
    assert _org_status(world) == "active" and _auth_status(world, "a-legacy", 10, 1) == 200  # and it is reversible


def test_suspension_and_offboarding_are_separate_code_paths():
    suspend = inspect.getsource(platform_admin_service.set_library_active_status)
    offboard = inspect.getsource(platform_admin_service.offboard_library)

    # Suspend/reactivate can only ever write 'active' or 'suspended', and never reaches the cutoff.
    assert "offboard" not in suspend and 'new_status = "cancelled"' not in suspend
    assert {'new_status = "active"', 'new_status = "suspended"'} <= {
        line.strip() for line in suspend.splitlines() if line.strip().startswith("new_status =")
    }
    assert "set_library_active_status" not in offboard


# ======================================================================================================================
# evidence
# ======================================================================================================================

def test_the_cutoff_records_who_did_it_and_exactly_which_rows_it_changed(world):
    _offboard()

    (cutoff,) = _events(world)
    assert cutoff["event_type"] == "access_cutoff"
    assert (cutoff["organization_id"], cutoff["organization_slug"], cutoff["operational_customer_id"]) == (1, "lib-a", 10)
    assert (cutoff["actor_user_id"], cutoff["actor_label"]) == (900, "operator@example.invalid")
    details = cutoff["details"]
    assert details["status_before"] == "active" and details["status_after"] == "cancelled"
    assert details["installations"] == [
        {"id": 101, "status_before": "provisioning"},
        {"id": 102, "status_before": "active"},
        {"id": 103, "status_before": "inactive"},
    ]
    assert len(details["agent_token_ids"]) == 2 and all(isinstance(i, int) for i in details["agent_token_ids"])


def test_lifecycle_evidence_carries_no_token_code_patron_item_or_host_material(world):
    with world.begin() as conn:
        code = enrollment.create_enrollment_code(conn, 101)["enrollment_code"]
    session_token = _session(1)

    _offboard()

    stored = json.dumps(_rows(world, "tenant_lifecycle_events"))
    for secret in (
        "a-bound", "a-legacy", _sha256("a-bound"), _sha256("a-legacy"), code, enrollment.hash_enrollment_code(code),
        session_token, _sha256(session_token), CANARY_BARCODE, CANARY_HOSTNAME, CANARY_EMAIL,
        "11111111-1111-4111-8111-111111111111",
    ):
        assert secret not in stored


def test_counts_ids_and_status_names_are_accepted_as_details():
    details = {
        "status_before": "active", "schema_revision": "a7c4e19d5b02", "counts": {"agent_tokens_deactivated": 2},
        "agent_token_ids": [4, 9], "installations": [{"id": 101, "status_before": "provisioning"}], "flag": True,
    }

    assert data_lifecycle_policy.validate_lifecycle_details(details) == details


@pytest.mark.parametrize("details", [
    {"agent_token_ids": ["k3Jx-raw-token-value"]},
    {"token_hash": 5},
    {"barcode": 31234000999},
    {"patron_id": 7},
    {"counts": {"raw_message": 1}},
    {"note": "free text about the tenant"},
    {"status_before": "Not A Status!"},
    {"status_before": "a" * 64},
    {"value": 1.5},
])
def test_details_that_could_carry_a_payload_are_refused(details):
    with pytest.raises(ValueError):
        data_lifecycle_policy.validate_lifecycle_details(details)


def test_an_event_needs_a_known_type_and_a_named_actor(world):
    for overrides in ({"event_type": "deleted_everything"}, {"actor_label": "   "}):
        arguments = {
            "event_type": "access_cutoff", "organization_id": 1, "organization_slug": "lib-a",
            "operational_customer_id": 10, "actor_user_id": None, "actor_label": "operator", "details": {},
            **overrides,
        }
        with pytest.raises(ValueError), world.begin() as conn:
            data_lifecycle_policy.record_tenant_lifecycle_event(conn, **arguments)
    assert _rows(world, "tenant_lifecycle_events") == []


# ======================================================================================================================
# provisioning guards: a cancelled tenant cannot be re-provisioned, whatever the page shows
# ======================================================================================================================

def test_a_cancelled_tenant_cannot_gain_an_installation(world):
    _offboard()
    before = _rows(world, "collector_installations")

    with pytest.raises(RuntimeError, match="cancelled"):
        tenant_service.create_collector_installation(organization_id=1, branch_id=1, name="New Sorter")

    assert _rows(world, "collector_installations") == before


@pytest.mark.parametrize("status", ["provisioning", "active", "inactive", "retired"])
def test_a_cancelled_tenants_installation_cannot_be_updated(world, status):
    _offboard()
    before = _rows(world, "collector_installations")

    with pytest.raises(RuntimeError, match="cancelled"):
        tenant_service.update_collector_installation(
            installation_id=102, organization_id=1, name="Revived", hostname=None, collector_version=None, status=status,
        )

    assert _rows(world, "collector_installations") == before


def test_a_cancelled_tenant_cannot_be_issued_an_ingest_key(world):
    _offboard()
    before = _rows(world, "ingest_key_ids")

    with pytest.raises(ValueError, match="cancelled"), world.begin() as conn:
        ingest_v2_service.issue_ingest_key(conn, 10, 1)

    assert _rows(world, "ingest_key_ids") == before


def test_a_cancelled_tenant_cannot_be_given_a_v2_cutover(world):
    _offboard()

    for cutover_at in ("2026-10-01T00:00:00+00:00", None):  # a set, and a rollback
        with pytest.raises(ValueError, match="cancelled"), world.begin() as conn:
            ingest_v2_service.record_v2_cutover(conn, 10, 1, cutover_at, "operator")

    assert _rows(world, "v2_cutovers") == []


def test_the_guards_do_not_get_in_the_way_of_a_live_or_suspended_tenant(world):
    _run(world, "UPDATE organizations SET status = 'suspended' WHERE id = 2")

    created = tenant_service.create_collector_installation(organization_id=2, branch_id=2, name="Second Sorter")
    updated = tenant_service.update_collector_installation(
        installation_id=201, organization_id=2, name="Renamed", hostname=None, collector_version=None, status="inactive",
    )
    with world.begin() as conn:
        key_id = ingest_v2_service.issue_ingest_key(conn, 20, 2)
        cutover_id = ingest_v2_service.record_v2_cutover(conn, 20, 2, None, "operator")

    assert created["name"] == "Second Sorter" and updated["status"] == "inactive" and key_id and cutover_id
