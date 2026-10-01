"""scripts/purge_tenant_data.py -- the tenant data purge: what it refuses, what it counts, what it deletes and what it keeps.

The tool's gates, inventory and deletes are plain portable SQL taken from the lifecycle policy registry, so they run here
against SQLite (never production): every refusal, the tenant scoping and the delete order are exercised with the tool's
own statements. `main()` itself only ever runs against PostgreSQL -- that, the real foreign-key topology, the read-only
dry run, the role check and the append-only evidence are covered in tests/test_tenant_lifecycle_postgres.py.

Two tenants with every purgeable table populated for both, so a scoping mistake in any single DELETE shows up as a
missing row for the tenant that was NOT purged:

    org 1 "lib-a"  cancelled  customer 10, branches 1 and 4   -- fully cut off, ready to purge
    org 2 "lib-b"  active     customer 20, branch 2
"""

from __future__ import annotations

import importlib.util
import inspect
import io
import json
import re
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from src.services import data_lifecycle_policy as policy

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "purge_tenant_data.py"

CANARY_PATRON = "CANARY-PATRON-21234000111"
CANARY_BARCODE = "CANARY-BARCODE-31234000999"
CANARY_TITLE = "CANARY-TITLE The Secret Garden"
CANARY_TOKEN_HASH = "canarytokenhash" * 4

_KEYED = "customer_id INTEGER, branch_id INTEGER"
_DDL = [
    "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)",
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT, status TEXT, operational_customer_id INTEGER)",
    "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, operational_branch_id INTEGER)",
    "CREATE TABLE branch_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id INTEGER)",
    "CREATE TABLE organization_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER)",
    "CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER, status TEXT)",
    "CREATE TABLE memberships (id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER, user_id INTEGER)",
    "CREATE TABLE collector_installations (id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER, status TEXT)",
    """CREATE TABLE collector_enrollment_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, installation_id INTEGER, used_at TEXT, revoked_at TEXT)""",
    """CREATE TABLE agent_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT, customer_id INTEGER, branch_id INTEGER,
        is_active BOOLEAN, installation_id INTEGER)""",
    f"CREATE TABLE ingest_key_ids (id INTEGER PRIMARY KEY AUTOINCREMENT, {_KEYED}, status TEXT)",
    f"CREATE TABLE v2_cutovers (id INTEGER PRIMARY KEY AUTOINCREMENT, {_KEYED})",
    f"CREATE TABLE pipeline_status ({_KEYED})",
    f"CREATE TABLE checkins (id INTEGER PRIMARY KEY AUTOINCREMENT, {_KEYED}, barcode TEXT, title TEXT)",
    f"CREATE TABLE rejects (id INTEGER PRIMARY KEY AUTOINCREMENT, {_KEYED}, barcode TEXT)",
    f"CREATE TABLE acs_events (id INTEGER PRIMARY KEY AUTOINCREMENT, {_KEYED}, patron_id TEXT, barcode TEXT)",
    f"CREATE TABLE checkins_clean (id INTEGER, {_KEYED}, barcode TEXT)",
    f"CREATE TABLE rejects_clean (id INTEGER, {_KEYED}, barcode TEXT)",
    f"CREATE TABLE checkin_events (id INTEGER PRIMARY KEY AUTOINCREMENT, {_KEYED})",
    f"CREATE TABLE reject_events (id INTEGER PRIMARY KEY AUTOINCREMENT, {_KEYED})",
    f"CREATE TABLE acs_item_events (id INTEGER PRIMARY KEY AUTOINCREMENT, {_KEYED})",
    # retained / separately governed / global
    "CREATE TABLE app_users (id INTEGER PRIMARY KEY, email TEXT)",
    "CREATE TABLE auth_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER)",
    "CREATE TABLE password_reset_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER)",
    "CREATE TABLE auth_audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, email TEXT)",
    "CREATE TABLE plans (id INTEGER PRIMARY KEY, code TEXT)",
    "CREATE TABLE feature_entitlements (id INTEGER PRIMARY KEY, plan_id INTEGER)",
    "CREATE TABLE bin_routing_map (bin TEXT PRIMARY KEY)",
    "CREATE TABLE alembic_version (version_num TEXT)",
    """CREATE TABLE tenant_lifecycle_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, organization_id INTEGER NOT NULL,
        organization_slug TEXT NOT NULL, operational_customer_id INTEGER, actor_user_id INTEGER,
        actor_label TEXT NOT NULL, occurred_at TEXT DEFAULT CURRENT_TIMESTAMP, details TEXT NOT NULL)""",
]

_EVENT_TABLES = ("checkins", "rejects", "acs_events", "checkins_clean", "rejects_clean", "checkin_events",
                 "reject_events", "acs_item_events", "ingest_key_ids", "v2_cutovers", "pipeline_status")
_ALL_TABLES = tuple(entry.table for entry in policy.DATABASE_SURFACES)


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("purge_tenant_data_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["purge_tenant_data_under_test"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("purge_tenant_data_under_test", None)


def _run(engine, sql, **params):
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _rows(engine, table):
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(text(f"SELECT * FROM {table}")).mappings().all()]  # nosec B608
    return sorted(rows, key=lambda row: json.dumps(row, default=str, sort_keys=True))


def _snapshot(engine):
    return {table: _rows(engine, table) for table in _ALL_TABLES}


def _seed_tenant(engine, org_id, slug, status, customer, branches, *, cut_off):
    _run(engine, "INSERT INTO customers VALUES (:c, :n)", c=customer, n=slug)
    _run(engine, "INSERT INTO organizations VALUES (:o, :s, :st, :c)", o=org_id, s=slug, st=status, c=customer)
    _run(engine, "INSERT INTO organization_settings (organization_id) VALUES (:o)", o=org_id)
    _run(engine, "INSERT INTO subscriptions (organization_id, status) VALUES (:o, 'active')", o=org_id)
    installation = org_id * 100 + 1
    _run(engine, "INSERT INTO collector_installations VALUES (:i, :o, :b, :s)",
         i=installation, o=org_id, b=branches[0], s="retired" if cut_off else "active")
    _run(engine, "INSERT INTO collector_enrollment_codes (installation_id, used_at, revoked_at) VALUES (:i, NULL, :r)",
         i=installation, r="2026-01-01" if cut_off else None)
    for bound in (installation, None):  # one enrollment-issued token, one legacy unbound token
        _run(engine, "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, is_active, installation_id) "
                     "VALUES (:h, :c, :b, :a, :i)",
             h=f"{CANARY_TOKEN_HASH}-{org_id}-{bound}", c=customer, b=branches[0], a=not cut_off, i=bound)
    for branch in branches:
        _run(engine, "INSERT INTO branches VALUES (:b, :o, :b)", b=branch, o=org_id)
        _run(engine, "INSERT INTO branch_settings (branch_id) VALUES (:b)", b=branch)
        scope = {"c": customer, "b": branch}
        _run(engine, "INSERT INTO checkins (customer_id, branch_id, barcode, title) VALUES (:c, :b, :x, :t)",
             **scope, x=CANARY_BARCODE, t=CANARY_TITLE)
        _run(engine, "INSERT INTO checkins_clean VALUES (1, :c, :b, :x)", **scope, x=CANARY_BARCODE)
        _run(engine, "INSERT INTO rejects (customer_id, branch_id, barcode) VALUES (:c, :b, :x)", **scope, x=CANARY_BARCODE)
        _run(engine, "INSERT INTO rejects_clean VALUES (1, :c, :b, :x)", **scope, x=CANARY_BARCODE)
        _run(engine, "INSERT INTO acs_events (customer_id, branch_id, patron_id, barcode) VALUES (:c, :b, :p, :x)",
             **scope, p=CANARY_PATRON, x=CANARY_BARCODE)
        for table in ("checkin_events", "reject_events", "acs_item_events", "v2_cutovers", "pipeline_status"):
            _run(engine, f"INSERT INTO {table} (customer_id, branch_id) VALUES (:c, :b)", **scope)  # nosec B608
        _run(engine, "INSERT INTO ingest_key_ids (customer_id, branch_id, status) VALUES (:c, :b, :s)",
             **scope, s="retired" if cut_off else "active")
    if cut_off:
        _run(engine, "INSERT INTO tenant_lifecycle_events (event_type, organization_id, organization_slug, "
                     "operational_customer_id, actor_label, details) VALUES ('access_cutoff', :o, :s, :c, 'admin', '{}')",
             o=org_id, s=slug, c=customer)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in _DDL:
            conn.execute(text(statement))
    _seed_tenant(engine, 1, "lib-a", "cancelled", 10, (1, 4), cut_off=True)
    _seed_tenant(engine, 2, "lib-b", "active", 20, (2,), cut_off=False)
    # Users are global: one belongs only to the purged tenant, one to both, with sessions and audit history.
    for user_id, email, orgs in ((1, "only-a@example.invalid", (1,)), (2, "both@example.invalid", (1, 2))):
        _run(engine, "INSERT INTO app_users VALUES (:u, :e)", u=user_id, e=email)
        _run(engine, "INSERT INTO auth_sessions (user_id) VALUES (:u)", u=user_id)
        _run(engine, "INSERT INTO password_reset_tokens (user_id) VALUES (:u)", u=user_id)
        _run(engine, "INSERT INTO auth_audit_log (user_id, email) VALUES (:u, :e)", u=user_id, e=email)
        for org_id in orgs:
            _run(engine, "INSERT INTO memberships (organization_id, user_id) VALUES (:o, :u)", o=org_id, u=user_id)
    _run(engine, "INSERT INTO plans VALUES (1, 'pro')")
    _run(engine, "INSERT INTO feature_entitlements VALUES (1, 1)")
    _run(engine, "INSERT INTO bin_routing_map VALUES ('7')")
    _run(engine, "INSERT INTO alembic_version VALUES ('head')")
    yield engine
    engine.dispose()


def _refusals(tool, engine, organization_id=1, slug="lib-a"):
    with engine.connect() as conn:
        return tool.refusals(conn, organization_id, slug)


def _purge(tool, engine, organization_id=1, slug="lib-a"):
    with engine.begin() as conn:
        tenant, problems = tool.refusals(conn, organization_id, slug, lock=True)
        assert problems == [], problems
        return tool.execute_purge(conn, tenant, operator="test-operator", schema_revision="a7c4e19d5b02")


# ======================================================================================================================
# the gates
# ======================================================================================================================

def test_a_fully_cut_off_tenant_with_an_unambiguous_mapping_is_accepted(tool, db):
    tenant, problems = _refusals(tool, db)

    assert problems == []
    assert (tenant.organization_id, tenant.slug, tenant.customer_id, tenant.branch_ids) == (1, "lib-a", 10, (1, 4))


def test_a_tenant_that_is_not_cancelled_is_refused(tool, db):
    for status in ("active", "trial", "suspended"):
        _run(db, "UPDATE organizations SET status = :s WHERE id = 2", s=status)
        _tenant, problems = _refusals(tool, db, 2, "lib-b")
        assert len(problems) == 1 and "not 'cancelled'" in problems[0] and status in problems[0]


def test_an_unknown_organization_or_a_wrong_slug_is_refused_without_naming_the_real_slug(tool, db):
    tenant, problems = _refusals(tool, db, 999, "lib-a")
    assert tenant is None and problems == ["organization 999 does not exist"]

    tenant, problems = _refusals(tool, db, 1, "lib-b")
    assert tenant is None and len(problems) == 1 and "does not match" in problems[0] and "lib-a" not in problems[0]


@pytest.mark.parametrize(("undo", "expected"), [
    ("UPDATE agent_tokens SET is_active = 1 WHERE customer_id = 10 AND installation_id IS NULL", "active agent token"),
    ("UPDATE agent_tokens SET is_active = 1 WHERE installation_id = 101", "active agent token"),
    ("UPDATE collector_installations SET status = 'inactive' WHERE id = 101", "installation(s) not retired"),
    ("UPDATE collector_enrollment_codes SET revoked_at = NULL WHERE installation_id = 101", "enrollment code"),
    ("UPDATE ingest_key_ids SET status = 'active' WHERE customer_id = 10 AND branch_id = 4", "active ingest key"),
])
def test_an_incomplete_access_cutoff_is_refused(tool, db, undo, expected):
    _run(db, undo)

    _tenant, problems = _refusals(tool, db)

    assert len(problems) == 1 and "access cutoff is incomplete" in problems[0] and expected in problems[0]


def test_a_cancelled_tenant_with_no_recorded_cutoff_is_refused(tool, db):
    _run(db, "UPDATE organizations SET status = 'cancelled' WHERE id = 2")  # set by hand: nothing was revoked or recorded

    _tenant, problems = _refusals(tool, db, 2, "lib-b")

    assert any("no access_cutoff lifecycle event" in p for p in problems)
    assert any("active agent token" in p for p in problems)


def test_a_missing_operational_mapping_is_refused(tool, db):
    _run(db, "UPDATE organizations SET operational_customer_id = NULL WHERE id = 1")

    _tenant, problems = _refusals(tool, db)

    assert any("no operational customer mapping" in p for p in problems)


def test_an_ambiguous_operational_mapping_is_refused(tool, db):
    _run(db, "UPDATE organizations SET operational_customer_id = 10 WHERE id = 2")  # two organizations, one customer

    _tenant, problems = _refusals(tool, db)

    assert any("mapped by 2 organizations" in p for p in problems)


def test_a_dangling_customer_or_an_inconsistent_branch_mapping_is_refused(tool, db):
    _run(db, "UPDATE branches SET operational_branch_id = 2 WHERE id = 4")
    _tenant, problems = _refusals(tool, db)
    assert any("operational id that is not their own id" in p for p in problems)

    _run(db, "UPDATE branches SET operational_branch_id = 4 WHERE id = 4")
    _run(db, "DELETE FROM customers WHERE id = 10")
    _tenant, problems = _refusals(tool, db)
    assert any("customer 10 does not exist" in p for p in problems)


# --- unattributable rows: the purge fails closed, it never guesses ----------------------------------------------------

@pytest.mark.parametrize(("customer_id", "branch_id", "summary"), [
    (None, None, "1 with every key NULL, 0 partially keyed"),
    (10, None, "0 with every key NULL, 1 partially keyed"),
    (None, 1, "0 with every key NULL, 1 partially keyed"),
    (20, None, "0 with every key NULL, 1 partially keyed"),  # even one that names ANOTHER tenant's customer
])
def test_a_null_or_partially_keyed_acs_event_makes_the_purge_refuse(tool, db, customer_id, branch_id, summary):
    _run(db, "INSERT INTO acs_events (customer_id, branch_id, patron_id) VALUES (:c, :b, :p)",
         c=customer_id, b=branch_id, p=CANARY_PATRON)

    _tenant, problems = _refusals(tool, db)

    # (A row naming this tenant's branch with no customer is ALSO reported as a pairing conflict; hence problems[0].)
    problem = problems[0]
    assert problem.startswith("acs_events: 1 row(s) have a NULL tenant key") and summary in problem
    assert "by hand" in problem
    assert all(p.startswith("acs_events:") and CANARY_PATRON not in p for p in problems)  # counts, never the row


@pytest.mark.parametrize("table", ["checkins_clean", "rejects_clean"])
def test_a_null_keyed_row_in_a_clean_copy_makes_the_purge_refuse_too(tool, db, table):
    _run(db, f"INSERT INTO {table} VALUES (9, NULL, NULL, :x)", x=CANARY_BARCODE)  # nosec B608

    _tenant, problems = _refusals(tool, db)

    assert len(problems) == 1 and problems[0].startswith(f"{table}: 1 row(s) have a NULL tenant key")


def test_the_nullable_tables_are_exactly_the_ones_the_schema_leaves_unconstrained():
    assert {e.table for e in policy.DATABASE_SURFACES if e.nullable_tenant_keys} == {
        "acs_events", "checkins_clean", "rejects_clean",
    }


@pytest.mark.parametrize("table", _EVENT_TABLES)
def test_a_row_claimed_by_two_tenants_makes_the_purge_refuse(tool, db, table):
    # This tenant's customer with the OTHER tenant's branch ...
    _run(db, f"INSERT INTO {table} (customer_id, branch_id) VALUES (10, 2)")  # nosec B608
    _tenant, problems = _refusals(tool, db)
    assert len(problems) == 1 and problems[0].startswith(f"{table}: 1 row(s) pair this tenant's customer")

    # ... and the other tenant's customer with THIS tenant's branch.
    _run(db, f"UPDATE {table} SET customer_id = 20, branch_id = 4 WHERE customer_id = 10 AND branch_id = 2")  # nosec B608
    _tenant, problems = _refusals(tool, db)
    assert len(problems) == 1 and problems[0].startswith(f"{table}: 1 row(s) pair this tenant's customer")


# ======================================================================================================================
# inventory: counts only, and it writes nothing
# ======================================================================================================================

def test_the_inventory_counts_exactly_the_tenants_rows_in_purge_order(tool, db):
    with db.connect() as conn:
        tenant, _problems = tool.refusals(conn, 1, "lib-a")
        counts = tool.inventory(conn, tenant)

    assert list(counts) == [entry.table for entry in policy.purge_plan()]
    assert counts == {
        "checkins_clean": 2, "rejects_clean": 2, "checkins": 2, "rejects": 2, "acs_events": 2,
        "checkin_events": 2, "reject_events": 2, "acs_item_events": 2, "ingest_key_ids": 2, "v2_cutovers": 2,
        "pipeline_status": 2, "agent_tokens": 2, "collector_enrollment_codes": 1, "collector_installations": 1,
        "branch_settings": 2, "branches": 2, "organization_settings": 1, "subscriptions": 1, "memberships": 2,
        "organizations": 1, "customers": 1,
    }


def test_the_gates_and_the_inventory_write_nothing_and_issue_only_selects(tool, db):
    before = _snapshot(db)
    statements: list[str] = []

    @event.listens_for(db, "before_cursor_execute")
    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lstrip().lower())

    with db.connect() as conn:
        tenant, _problems = tool.refusals(conn, 1, "lib-a")
        tool.inventory(conn, tenant)

    assert statements and all(s.startswith("select") for s in statements)
    assert _snapshot(db) == before


# ======================================================================================================================
# the purge
# ======================================================================================================================

def test_the_purge_removes_every_row_of_the_tenant_and_not_one_row_of_another(tool, db):
    other_before = {
        table: [r for r in _rows(db, table) if r.get("customer_id") == 20 or r.get("organization_id") == 2
                or r.get("branch_id") == 2 or r.get("installation_id") == 201]
        for table in (e.table for e in policy.purge_plan())
    }
    other_before["organizations"] = [r for r in _rows(db, "organizations") if r["id"] == 2]
    other_before["customers"] = [r for r in _rows(db, "customers") if r["id"] == 20]
    other_before["branches"] = [r for r in _rows(db, "branches") if r["organization_id"] == 2]

    deleted = _purge(tool, db)

    with db.connect() as conn:
        assert not any(tool.inventory(conn, tool.Tenant(1, "lib-a", "cancelled", 10, (1, 4))).values())
    for table, expected in other_before.items():
        assert _rows(db, table) == expected, table  # the other tenant's rows are ALL that is left, unchanged
    assert sum(deleted.values()) == 36 and deleted["acs_events"] == 2 and deleted["customers"] == 1


def test_the_purge_deletes_in_the_registrys_order(tool, db):
    statements: list[str] = []

    @event.listens_for(db, "before_cursor_execute")
    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().lower().startswith("delete"):
            statements.append(statement.split()[2])

    _purge(tool, db)

    assert statements == [entry.table for entry in policy.purge_plan()]


def test_every_delete_is_scoped_to_the_one_tenant(tool, db):
    statements: list[tuple[str, object]] = []

    @event.listens_for(db, "before_cursor_execute")
    def _record(_conn, _cursor, statement, parameters, _context, _executemany):
        if statement.lstrip().lower().startswith("delete"):
            statements.append((statement, parameters))

    _purge(tool, db)

    assert len(statements) == len(policy.purge_plan())
    for statement, parameters in statements:
        assert " where " in statement.lower()
        assert set(parameters) <= {1, 10}  # only organization 1 / customer 10 are ever bound


def test_lifecycle_evidence_survives_the_purge_and_records_it(tool, db):
    _purge(tool, db)

    events = _rows(db, "tenant_lifecycle_events")
    assert [e["event_type"] for e in events] == ["access_cutoff", "purge_executed"]
    purge = events[1]
    assert (purge["organization_id"], purge["organization_slug"], purge["operational_customer_id"]) == (1, "lib-a", 10)
    assert purge["actor_label"] == "test-operator" and purge["actor_user_id"] is None
    details = json.loads(purge["details"])
    assert details["schema_revision"] == "a7c4e19d5b02" and details["branch_ids"] == [1, 4]
    assert details["rows_deleted"]["acs_events"] == 2 and sum(details["rows_deleted"].values()) == 36


def test_the_purge_never_issues_a_statement_against_a_retained_table(tool, db):
    touched: set[str] = set()

    @event.listens_for(db, "before_cursor_execute")
    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        words = statement.split()
        if words[0].lower() in ("delete", "update"):
            touched.add(words[2] if words[0].lower() == "delete" else words[1])

    _purge(tool, db)

    assert not touched & set(policy.retained_tables())
    assert "tenant_lifecycle_events" in policy.retained_tables()


def test_global_accounts_sessions_and_the_security_audit_log_are_left_exactly_as_they_were(tool, db):
    kept = ("app_users", "auth_sessions", "password_reset_tokens", "auth_audit_log", "plans", "feature_entitlements",
            "bin_routing_map", "alembic_version")
    before = {table: _rows(db, table) for table in kept}

    _purge(tool, db)

    assert {table: _rows(db, table) for table in kept} == before
    # ... while the purged tenant's memberships are gone, including the one user who belonged to nothing else.
    assert [(m["organization_id"], m["user_id"]) for m in _rows(db, "memberships")] == [(2, 2)]


def test_evidence_and_refusals_carry_no_patron_item_or_token_material(tool, db):
    _run(db, "INSERT INTO acs_events (customer_id, branch_id, patron_id, barcode) VALUES (NULL, NULL, :p, :x)",
         p=CANARY_PATRON, x=CANARY_BARCODE)
    _tenant, problems = _refusals(tool, db)
    _run(db, "DELETE FROM acs_events WHERE customer_id IS NULL")
    _purge(tool, db)

    out = io.StringIO()
    tool._print_tenant(tool.Tenant(1, "lib-a", "cancelled", 10, (1, 4)), out)
    tool._print_counts("Rows deleted, per table:", {"acs_events": 2}, out)
    tool._print_refusals(problems, out)
    tool._print_retained(out)
    everything = out.getvalue() + json.dumps(_rows(db, "tenant_lifecycle_events")) + tool._PROVIDER_HISTORY_NOTICE

    for secret in (CANARY_PATRON, CANARY_BARCODE, CANARY_TITLE, CANARY_TOKEN_HASH, "only-a@example.invalid"):
        assert secret not in everything


def test_a_purge_that_would_leave_a_row_behind_raises_so_the_transaction_rolls_back(tool, db, monkeypatch):
    before = _snapshot(db)
    plan = tuple(entry for entry in policy.purge_plan() if entry.table != "acs_events")  # a plan that forgets a table
    real_plan = tool.purge_plan
    calls = {"n": 0}

    def forgetful_plan():
        calls["n"] += 1
        return plan if calls["n"] == 1 else real_plan()  # the deletes skip acs_events; the survivor check does not

    monkeypatch.setattr(tool, "purge_plan", forgetful_plan)

    with pytest.raises(RuntimeError, match="left rows behind in: acs_events"), db.begin() as conn:
        tenant = tool.load_tenant(conn, 1)
        tool.execute_purge(conn, tenant, operator="test-operator", schema_revision="a7c4e19d5b02")

    assert _snapshot(db) == before


# ======================================================================================================================
# the command line: safe by default
# ======================================================================================================================

def _main(tool, *argv):
    out = io.StringIO()
    return tool.main(list(argv), out=out), out.getvalue()


def test_nothing_connects_without_a_database_url(tool, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(tool, "create_engine", lambda *_a, **_k: pytest.fail("must not create an engine"))

    code, out = _main(tool, "--organization-id", "1", "--confirm-slug", "lib-a")

    assert code == 2 and "REFUSED" in out and "DATABASE_URL" in out


@pytest.mark.parametrize("extra", [
    ["--execute"],
    ["--execute", "--operator", "rhanly"],
    ["--execute", "--confirm-purge", "lib-a"],
    ["--execute", "--confirm-purge", "lib-b", "--operator", "rhanly"],
    ["--execute", "--confirm-purge", "lib-a", "--operator", "   "],
])
def test_execute_needs_the_slug_twice_and_a_named_operator_before_anything_connects(tool, monkeypatch, extra):
    monkeypatch.setattr(tool, "create_engine", lambda *_a, **_k: pytest.fail("must not create an engine"))

    code, out = _main(tool, "--organization-id", "1", "--confirm-slug", "lib-a", "--database-url", "postgresql://x/y", *extra)

    assert code == 2 and "REFUSED" in out and "Nothing was written" in out


def test_the_tool_refuses_anything_that_is_not_postgresql_and_never_prints_the_url(tool, tmp_path):
    url = f"sqlite:///{tmp_path / 'not-postgres.db'}"

    code, out = _main(tool, "--organization-id", "1", "--confirm-slug", "lib-a", "--database-url", url, "--execute",
                      "--confirm-purge", "lib-a", "--operator", "rhanly")

    assert code == 2 and "PostgreSQL only" in out and url not in out and "sqlite" not in out


def test_ownership_is_required_of_every_table_the_tool_names_anywhere(tool):
    required = set(tool.ownership_tables())

    assert required == {entry.table for entry in policy.purge_plan()} | {"tenant_lifecycle_events", "alembic_version"}
    # Every table the tool's own SQL names literally (the gates; the plan's tables arrive through the registry) is one it
    # must own: no check can run against a table whose rows row level security could be hiding.
    source = SCRIPT.read_text(encoding="utf-8")
    named = set(re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_]+)\b(?!\()", source))  # (?!\() : not a function call
    assert {"organizations", "branches", "customers", "agent_tokens", "tenant_lifecycle_events", "alembic_version"} <= named
    assert {name for name in named if not name.startswith("pg_")} <= required
    for entry in policy.purge_plan():  # and the registry's own sub-selects name only plan tables
        assert set(re.findall(r"\bFROM\s+([a-z_]+)\b", entry.purge_where)) <= required


def test_ownership_is_checked_before_the_tenant_is_looked_up_and_is_not_satisfied_by_delete_privilege(tool):
    source = inspect.getsource(tool.environment_problems)

    assert source.index("table_owners(") < source.index("expected_schema_revision()") < source.index("has_table_privilege")
    code_only = "\n".join(line for line in source.split('"""')[2].splitlines() if not line.strip().startswith("#"))
    assert "rolsuper" not in code_only and "rolbypassrls" not in code_only  # no alternate accepted path
    main = inspect.getsource(tool.main)
    assert main.index("environment_problems(conn)") < main.index("refusals(conn") < main.index("inventory(conn")
    assert "if not problems:" in main  # the tenant is not loaded, and nothing is counted, once the environment is refused


def test_the_tool_documents_who_may_run_it_and_from_where(tool):
    doc = tool.__doc__

    assert "city-owned machine" in doc and "never permitted" in doc
    assert "refuses to run as the application's runtime" in doc
    assert tool.RUNTIME_ROLE == "sortview_app"


def test_the_completion_message_never_claims_provider_history_is_erased(tool):
    source = SCRIPT.read_text(encoding="utf-8")
    notice = tool._PROVIDER_HISTORY_NOTICE

    assert "LIVE DATABASE PURGE COMPLETE" in source
    assert notice.startswith("PROVIDER HISTORY AGED OUT: NOT VERIFIED")
    assert "NOT yet physically erased" in notice and "retention window" in notice and "branch" in notice
    assert "PROVIDER HISTORY AGED OUT: YES" not in source.upper().replace("NOT VERIFIED", "")
