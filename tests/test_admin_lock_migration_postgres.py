"""The admin-lock migration and the read-only inventory SQL, against a REAL PostgreSQL.

The migration is written in JSONB operators (`#-`, `jsonb_set`, `jsonb_exists`), `SELECT ... FOR UPDATE` and a
guarded UPDATE whose WHERE clause re-tests the old state on the server. SQLite cannot run any of that, so these tests
run the script's real statements on a real server: that it changes exactly what it should and nothing else, that the
plaintext is never sent back to the database, that a re-run is a no-op, and that every failure rolls back.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_collector_enrollment_postgres.py. They run only
when SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server the tests may create and
drop databases on, e.g.

    SORTVIEW_TEST_POSTGRES_URL=postgresql://postgres:@127.0.0.1:5432/postgres

Each run creates a brand-new throwaway database, applies the project's real Alembic migrations to it (in a subprocess),
and drops it afterwards. The host must be local (localhost / 127.0.0.1 / ::1) unless
SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also set, so a production URL left in an environment variable cannot be used.

Every value is a SYNTHETIC canary.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError

from services import admin_lock_service as lock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "migrate_admin_lock_hashes.py"
INVENTORY_SQL = ROOT / "scripts" / "admin_lock_inventory.sql"
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL migration tests)"
)

PW_A, PW_B, PW_STALE, PW_EXISTING = (f"CANARY-ADMIN-PASSWORD-{n}" for n in (8201, 8202, 8203, 8204))
HASH_EXISTING = lock.hash_admin_password(PW_EXISTING)
ALL_SECRETS = (PW_A, PW_B, PW_STALE, PW_EXISTING, HASH_EXISTING)


def docs() -> dict[int, object]:
    """organization_settings.id -> settings_json, one organization each."""
    return {
        1: {"library_name": "Plain A", "n": 1.5, "flag": True, "nested": {"list": [1, {"a": None}]},
            "transit": {"destinations": [{"key": "west", "enabled": True}]},
            "security": {"admin_enabled": True, "admin_password": PW_A}},
        2: {"library_name": "Plain B", "security": {"admin_enabled": False, "admin_password": PW_B, "custom": [1, 2]}},
        3: {"library_name": "Hashed", "security": {"admin_enabled": True, "admin_password_hash": HASH_EXISTING}},
        4: {"library_name": "Both", "security": {"admin_enabled": True, "admin_password": PW_STALE,
                                                 "admin_password_hash": HASH_EXISTING}},
        5: {"library_name": "No security block"},
        6: {"library_name": "Empty legacy key", "security": {"admin_enabled": True, "admin_password": ""}},
    }


UNEXPECTED_DOCS = {
    "security-is-a-string": {"security": "oops"},
    "security-is-null": {"security": None},
    "password-is-a-number": {"security": {"admin_password": 12345}},
    "hash-is-not-a-string": {"security": {"admin_password_hash": 5}},
    "hash-has-no-known-format": {"security": {"admin_password_hash": "not-a-hash"}},
    "document-is-an-array": [1, 2],
}


# --- a throwaway, fully migrated database ---------------------------------------------------------------------------------

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
    name = f"sortview_adminlock_test_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    test_url = admin.set(database=name)
    env = {**os.environ, "DATABASE_URL": test_url.render_as_string(hide_password=False)}
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    try:
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        yield test_url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


def _seed(engine, extra: dict[int, object] | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE branch_settings, organization_settings, branches, organizations RESTART IDENTITY CASCADE"))
        rows = {**docs(), **(extra or {})}
        for org_id, document in rows.items():
            conn.execute(text("INSERT INTO organizations (id, slug, name) VALUES (:id, :slug, :name)"),
                         {"id": org_id, "slug": f"org-{org_id}", "name": f"Org {org_id}"})
            conn.execute(text("INSERT INTO organization_settings (id, organization_id, settings_json) "
                              "VALUES (:id, :id, CAST(:doc AS jsonb))"), {"id": org_id, "doc": json.dumps(document)})
        for org_id in (1, 2):
            conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, is_primary) "
                              "VALUES (:id, :id, 'main', 'Main', TRUE)"), {"id": org_id})
            conn.execute(text("INSERT INTO branch_settings (id, branch_id, settings_json) "
                              "VALUES (:id, :id, CAST(:doc AS jsonb))"), {"id": org_id, "doc": json.dumps({"branch_name": "Main"})})


@pytest.fixture
def engine(pg_url):
    engine = create_engine(pg_url)
    _seed(engine)
    yield engine
    engine.dispose()


def snapshot(engine) -> dict[int, tuple]:
    with engine.connect() as conn:
        return {r[0]: (r[1], r[2]) for r in conn.execute(text(
            "SELECT id, settings_json, updated_at FROM organization_settings ORDER BY id"))}


@pytest.fixture(scope="module")
def migrate():
    spec = importlib.util.spec_from_file_location("migrate_admin_lock_hashes_pg", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_admin_lock_hashes_pg"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("migrate_admin_lock_hashes_pg", None)


@pytest.fixture
def run(migrate, pg_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_url.render_as_string(hide_password=False))

    def call(*argv):
        out = io.StringIO()
        return migrate.main(list(argv), out=out), out.getvalue()

    return call


@pytest.fixture
def sent():
    """Every statement (and its bound parameters) any engine sends to the server during the test."""
    log: list[tuple[str, object]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _executemany):
        log.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", capture)
    yield log
    event.remove(Engine, "before_cursor_execute", capture)


def _no_secret(text_: str, *extra: str) -> list[str]:
    return [s for s in (*ALL_SECRETS, *extra) if s in text_]


def _execute_args(pg_url, *ids: int) -> list[str]:
    return ["--execute", "--confirm-database", pg_url.database, *[a for i in ids for a in ("--row-id", str(i))]]


# --- dry run --------------------------------------------------------------------------------------------------------------

def test_a_dry_run_reports_states_and_ids_changes_nothing_and_prints_no_secret(run, engine, sent, pg_url):
    before = snapshot(engine)

    code, output = run()

    assert code == 0
    assert "organization_settings: 6 rows -> no_lock 2, plaintext_only 2, hash_only 1, both 1, unexpected_shape 0" in output
    for row_id, slug in ((1, "org-1"), (2, "org-2")):
        assert f"would hash the plaintext: settings_row_id={row_id} (organization_id={row_id}, slug={slug})" in output
    assert "would drop the stale plaintext key: settings_row_id=4" in output
    assert f"--execute --confirm-database {pg_url.database} --row-id 1 --row-id 2 --row-id 4" in output
    assert _no_secret(output) == []
    assert snapshot(engine) == before  # not even updated_at moved
    assert not [s for s, _ in sent if re.match(r"\s*(UPDATE|INSERT|DELETE)", s, re.IGNORECASE)]
    assert any(s.strip().upper().startswith("SET TRANSACTION READ ONLY") for s, _ in sent)  # read-only transaction


def test_the_dry_run_transaction_really_is_read_only(engine):
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        with pytest.raises(Exception, match="read-only"):
            conn.execute(text("UPDATE organization_settings SET settings_json = settings_json"))


# --- execute --------------------------------------------------------------------------------------------------------------

def test_execute_migrates_only_the_approved_rows_and_preserves_every_other_setting(run, engine, sent, pg_url):
    before = snapshot(engine)

    code, output = run(*_execute_args(pg_url, 1, 2))
    after = snapshot(engine)

    assert code == 0 and "hashed: settings_row_id=1" in output and "hashed: settings_row_id=2" in output
    for row_id, password in ((1, PW_A), (2, PW_B)):
        old, new = before[row_id][0], after[row_id][0]
        assert lock.LEGACY_PLAINTEXT_KEY not in new["security"]  # the plaintext is gone from the row
        stored = new["security"][lock.HASH_KEY]
        assert stored != password and password not in json.dumps(new)
        assert lock.verify_admin_password(password, new["security"]) is True  # the same password still unlocks
        assert lock.verify_admin_password(password + "x", new["security"]) is False
        assert new == run_expected(old, stored)  # every other setting, including nested/float/bool/null, is identical
        assert after[row_id][1] > before[row_id][1]  # updated_at moved
    assert after[2][0]["security"]["custom"] == [1, 2] and after[2][0]["security"]["admin_enabled"] is False
    for untouched in (3, 4, 5, 6):  # not approved, or nothing to do: byte-for-byte unchanged, updated_at included
        assert after[untouched] == before[untouched]
    assert _no_secret(output, after[1][0]["security"][lock.HASH_KEY]) == []


def run_expected(old, password_hash):
    return {**old, "security": {**{k: v for k, v in old["security"].items() if k != lock.LEGACY_PLAINTEXT_KEY},
                                 lock.HASH_KEY: password_hash}}


def test_the_plaintext_is_never_sent_back_to_the_database(run, engine, sent, pg_url):
    run(*_execute_args(pg_url, 1, 2, 4))

    wire = " ".join(f"{statement} {parameters}" for statement, parameters in sent)
    assert [p for p in (PW_A, PW_B, PW_STALE, PW_EXISTING) if p in wire] == []
    updates = [(s, p) for s, p in sent if re.match(r"\s*UPDATE organization_settings", s)]
    assert len(updates) == 3
    hashed = [p for s, p in updates if "password_hash" in str(p)]
    assert len(hashed) == 2 and all(str(p["password_hash"]).startswith("scrypt:") for p in hashed)


def test_a_both_row_only_loses_its_stale_plaintext_key_and_keeps_its_hash(run, engine, pg_url):
    before = snapshot(engine)

    code, output = run(*_execute_args(pg_url, 4))
    after = snapshot(engine)

    assert code == 0 and "dropped stale plaintext: settings_row_id=4" in output
    assert after[4][0] == {**before[4][0], "security": {"admin_enabled": True, lock.HASH_KEY: HASH_EXISTING}}
    assert lock.verify_admin_password(PW_EXISTING, after[4][0]["security"]) is True  # the hash that already won still wins
    assert lock.verify_admin_password(PW_STALE, after[4][0]["security"]) is False
    assert _no_secret(output) == []


def test_running_it_again_is_a_safe_no_op(run, engine, pg_url):
    assert run(*_execute_args(pg_url, 1, 2, 4))[0] == 0
    settled = snapshot(engine)

    code, output = run(*_execute_args(pg_url, 1, 2, 4))  # the same approved ids, again
    assert code == 0 and "skipped" in output and "already hash_only" in output and "0 row(s) changed" in output
    assert snapshot(engine) == settled

    code, output = run()  # and a fresh dry run reports nothing left to migrate
    assert code == 0 and "Nothing to migrate." in output and "plaintext_only 0" in output and "both 0" in output


def test_execute_refuses_without_all_three_guards_and_changes_nothing(run, engine, pg_url):
    before = snapshot(engine)

    for args in (["--execute"], ["--execute", "--row-id", "1"], ["--execute", "--confirm-database", pg_url.database],
                 ["--execute", "--confirm-database", "some_other_db", "--row-id", "1"]):
        code, _ = run(*args)
        assert code == 1, args

    assert snapshot(engine) == before


def test_an_unknown_row_id_aborts_everything_including_the_valid_rows(run, engine, pg_url):
    before = snapshot(engine)

    code, output = run(*_execute_args(pg_url, 1, 999))

    assert code == 2 and "row ids not found" in output and "rolled back" in output
    assert snapshot(engine) == before


@pytest.mark.parametrize("name", list(UNEXPECTED_DOCS))
def test_an_unexpected_json_shape_fails_closed_and_blocks_even_the_good_rows(run, pg_url, name):
    engine = create_engine(pg_url)
    try:
        _seed(engine, {7: UNEXPECTED_DOCS[name]})
        before = snapshot(engine)

        code, output = run()
        assert code == 2 and "STOP: unexpected JSON shape in organization_settings settings_row_id=7" in output
        assert "Fail closed" in output

        code, output = run(*_execute_args(pg_url, 1, 2))
        assert code == 2 and "REFUSED" in output
        assert snapshot(engine) == before  # the perfectly good rows 1 and 2 were NOT migrated either
        assert _no_secret(output) == []
    finally:
        engine.dispose()


def test_a_credential_stored_at_branch_level_blocks_execute(run, engine, pg_url):
    with engine.begin() as conn:  # branch-level values override the organization's, so this needs a human decision
        conn.execute(text("UPDATE branch_settings SET settings_json = CAST(:d AS jsonb) WHERE id = 1"),
                     {"d": json.dumps({"security": {"admin_password": PW_STALE}})})
    before = snapshot(engine)

    code, output = run()
    assert code == 2 and "STOP: branch_settings settings_row_id=1" in output and _no_secret(output) == []

    code, output = run(*_execute_args(pg_url, 1))
    assert code == 2 and "REFUSED" in output
    assert snapshot(engine) == before


def test_a_failure_part_way_rolls_back_every_row_in_the_transaction(run, migrate, engine, pg_url, monkeypatch):
    before = snapshot(engine)
    real = migrate.hash_admin_password
    calls = []

    def second_hash_is_wrong(password):
        calls.append(1)
        return real(password) if len(calls) == 1 else "scrypt:32768:8:1$bad$bad"  # does not verify the password

    monkeypatch.setattr(migrate, "hash_admin_password", second_hash_is_wrong)

    code, output = run(*_execute_args(pg_url, 1, 2))

    assert code == 2 and "did not verify" in output and "rolled back" in output
    assert snapshot(engine) == before  # row 1 had already been updated inside the transaction; it was rolled back
    assert _no_secret(output) == []


def test_a_row_whose_json_is_not_exactly_the_expected_result_is_rolled_back(run, migrate, engine, pg_url, monkeypatch):
    before = snapshot(engine)
    monkeypatch.setattr(migrate, "expected_after_migrating", lambda old, password_hash: {**old, "unexpected": True})

    code, output = run(*_execute_args(pg_url, 1))

    assert code == 2 and "not exactly the expected result" in output
    assert snapshot(engine) == before


def test_an_update_that_matches_no_row_aborts_the_run_and_rolls_back(run, migrate, engine, pg_url, monkeypatch):
    before = snapshot(engine)
    # As if the row's state had changed between the check and the update (the row lock prevents it; this proves the guard).
    monkeypatch.setattr(migrate, "MIGRATE_UPDATE", migrate.MIGRATE_UPDATE + " AND FALSE")

    code, output = run(*_execute_args(pg_url, 1, 2))

    assert code == 2 and "did not match exactly one row" in output and "rolled back" in output
    assert snapshot(engine) == before


def test_the_guarded_updates_match_nothing_once_the_old_state_is_gone(migrate, engine):
    def touched(statement, settings_id, **params):
        with engine.connect() as conn, conn.begin() as tx:
            count = conn.execute(text(statement), {"settings_id": settings_id, **params}).rowcount
            tx.rollback()
        return count

    hashed = {"password_hash": "scrypt:32768:8:1$s$h"}
    assert touched(migrate.MIGRATE_UPDATE, 1, **hashed) == 1       # plaintext_only: matches
    assert touched(migrate.MIGRATE_UPDATE, 2, **hashed) == 1
    assert touched(migrate.MIGRATE_UPDATE, 3, **hashed) == 0       # already hash_only
    assert touched(migrate.MIGRATE_UPDATE, 4, **hashed) == 0       # has a hash: not this update's row
    assert touched(migrate.MIGRATE_UPDATE, 5, **hashed) == 0       # no security block
    assert touched(migrate.MIGRATE_UPDATE, 6, **hashed) == 0       # empty legacy key is not a credential
    assert touched(migrate.DROP_STALE_UPDATE, 4) == 1              # both: matches
    for other in (1, 2, 3, 5, 6):
        assert touched(migrate.DROP_STALE_UPDATE, other) == 0, other


def test_the_row_lock_makes_a_concurrent_change_wait_so_the_guard_cannot_be_raced(migrate, pg_url):
    a, b = create_engine(pg_url), create_engine(pg_url)
    try:
        with a.connect() as holder, holder.begin() as holding:
            holder.execute(text(migrate.LOCK_SELECTED_ORG_ROWS), {"ids": [1]})  # what --execute does first
            with b.connect() as other, other.begin() as waiting:
                other.execute(text("SET LOCAL lock_timeout = '300ms'"))
                with pytest.raises(OperationalError, match="lock"):
                    other.execute(text("UPDATE organization_settings SET settings_json = settings_json WHERE id = 1"))
                waiting.rollback()
            holding.rollback()
    finally:
        a.dispose()
        b.dispose()


def test_the_application_still_reads_the_migrated_lock_as_it_reads_a_new_one(run, engine, pg_url):
    assert run(*_execute_args(pg_url, 1))[0] == 0

    security = snapshot(engine)[1][0]["security"]

    assert lock.has_admin_password(security) is True and lock.is_legacy_plaintext(security) is False
    assert lock.public_security_view(security) == {"admin_enabled": True, "admin_password_set": True}
    assert lock.verify_admin_password(PW_A, security) is True
    assert lock.verify_admin_password("wrong", security) is False


# --- the read-only inventory SQL ------------------------------------------------------------------------------------------

def _inventory_queries() -> dict[str, str]:
    source = INVENTORY_SQL.read_text(encoding="utf-8")
    parts = re.split(r"^-- @@ (\w+) @@\s*$", source, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def test_the_inventory_returns_only_safe_metadata_and_agrees_with_the_script(migrate, pg_url):
    engine = create_engine(pg_url)
    try:
        extra = {7 + i: document for i, document in enumerate(UNEXPECTED_DOCS.values())}
        _seed(engine, extra)
        queries = _inventory_queries()
        assert set(queries) == {"detail", "summary"}

        with engine.connect() as conn, conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            detail = conn.execute(text(queries["detail"])).mappings().all()
            summary = conn.execute(text(queries["summary"])).mappings().all()
            expected = {r["settings_id"]: migrate.classify(r["settings"]).state
                        for r in conn.execute(text(migrate.SELECT_ORG_ROWS)).mappings()}

        assert list(detail[0]) == ["table_name", "settings_row_id", "owner_id", "owner_slug", "state", "empty_legacy_key_present"]
        assert list(summary[0]) == ["table_name", "state", "row_count"]
        rendered = json.dumps([dict(r) for r in detail] + [dict(r) for r in summary], default=str)
        assert _no_secret(rendered, "scrypt:", "oops", "12345", "not-a-hash") == []  # no password, hash or document text

        org_states = {r["settings_row_id"]: r["state"] for r in detail if r["table_name"] == "organization_settings"}
        assert org_states == expected  # SQL and Python classify every row the same way, unexpected shapes included
        assert {org_states[i] for i in range(1, 7)} == {"plaintext_only", "hash_only", "both", "no_lock"}
        assert [org_states[7 + i] for i in range(len(UNEXPECTED_DOCS))] == ["unexpected_shape"] * len(UNEXPECTED_DOCS)
        assert next(r for r in detail if r["settings_row_id"] == 6 and r["table_name"] == "organization_settings")[
            "empty_legacy_key_present"] is True
        counts = {(r["table_name"], r["state"]): r["row_count"] for r in summary}
        assert counts[("organization_settings", "plaintext_only")] == 2 and counts[("branch_settings", "no_lock")] == 2
    finally:
        engine.dispose()
