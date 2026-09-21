"""scripts/migrate_admin_lock_hashes.py -- the parts that need no database (the SQL is exercised for real in
tests/test_admin_lock_migration_postgres.py).

The script converts legacy PLAINTEXT organization admin-lock passwords to hashes. What is proved here:

  * every state is classified as the application would treat it, and anything unexpected FAILS CLOSED;
  * the expected-result functions change exactly one key and mutate nothing;
  * the guards (--execute needs --confirm-database and --row-id; the confirmation must match the target) refuse BEFORE
    any connection is attempted;
  * nothing a run prints can contain a value from the data, and a database failure prints only a safe summary.

Every value is a SYNTHETIC canary.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import DataError

from services import admin_lock_service as lock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "migrate_admin_lock_hashes.py"

PW = "CANARY-ADMIN-PASSWORD-8101"
DB_PASSWORD = "CANARY-DB-PASSWORD-8102"
DB_URL = f"postgresql://svc_user:{DB_PASSWORD}@canary-db-host.example.invalid:5432/sortview_prod"
REAL_LOOKING_HASH = lock.hash_admin_password(PW)


@pytest.fixture(scope="module")
def migrate():
    spec = importlib.util.spec_from_file_location("migrate_admin_lock_hashes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_admin_lock_hashes"] = module  # dataclasses resolve their string annotations through sys.modules
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("migrate_admin_lock_hashes", None)


# --- classification --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("settings, state", [
    ({}, "no_lock"),
    ({"library_name": "x"}, "no_lock"),
    ({"security": {}}, "no_lock"),
    ({"security": {"admin_enabled": True}}, "no_lock"),
    ({"security": {"admin_enabled": True, "admin_password": ""}}, "no_lock"),  # the old empty default is not a credential
    ({"security": {"admin_password": None}}, "no_lock"),
    ({"security": {"admin_password": PW}}, "plaintext_only"),
    ({"security": {"admin_enabled": False, "admin_password": PW}}, "plaintext_only"),  # a disabled lock is still a stored secret
    ({"security": {"admin_password": " "}}, "plaintext_only"),  # the app treats whitespace as a password
    ({"security": {"admin_password": PW, "admin_password_hash": ""}}, "plaintext_only"),
    ({"security": {"admin_password": PW, "admin_password_hash": None}}, "plaintext_only"),
    ({"security": {"admin_password_hash": REAL_LOOKING_HASH}}, "hash_only"),
    ({"security": {"admin_password": "", "admin_password_hash": REAL_LOOKING_HASH}}, "hash_only"),
    ({"security": {"admin_password": PW, "admin_password_hash": REAL_LOOKING_HASH}}, "both"),
])
def test_each_state_is_classified_as_the_application_treats_it(migrate, settings, state):
    assert migrate.classify(settings).state == state
    # ...and the application agrees about whether a password is set at all
    security = settings.get("security")
    assert lock.has_admin_password(security) == (state in ("plaintext_only", "hash_only", "both"))


@pytest.mark.parametrize("settings, reason", [
    (None, "settings_json is not a JSON object"),
    ([], "settings_json is not a JSON object"),
    ("text", "settings_json is not a JSON object"),
    ({"security": None}, "security is not a JSON object"),
    ({"security": "oops"}, "security is not a JSON object"),
    ({"security": [PW]}, "security is not a JSON object"),
    ({"security": {"admin_password": 12345}}, "admin_password is not a string"),
    ({"security": {"admin_password": True}}, "admin_password is not a string"),
    ({"security": {"admin_password": [PW]}}, "admin_password is not a string"),
    ({"security": {"admin_password_hash": 5}}, "admin_password_hash is not a string"),
    ({"security": {"admin_password_hash": {"x": 1}}}, "admin_password_hash is not a string"),
    ({"security": {"admin_password_hash": "not-a-hash"}}, "admin_password_hash is not a recognised hash format"),
    ({"security": {"admin_password": PW, "admin_password_hash": "plain$x$y"}}, "admin_password_hash is not a recognised hash format"),
])
def test_anything_unexpected_fails_closed_with_a_fixed_reason_and_no_value(migrate, settings, reason):
    result = migrate.classify(settings)

    assert result.state == "unexpected_shape" and result.reason == reason
    assert PW not in result.reason and "not-a-hash" not in result.reason  # a reason never quotes the data


def test_the_migration_only_ever_targets_what_the_application_would_have_accepted(migrate):
    # A plaintext_only row is exactly what verify_admin_password would accept as a legacy password.
    for settings in ({"security": {"admin_password": PW}}, {"security": {"admin_password": " "}}):
        assert migrate.classify(settings).state == "plaintext_only"
        assert lock.is_legacy_plaintext(settings["security"])
    # ...and a non-string is NOT a password to the application, so the migration must not guess: it stops.
    assert lock.has_admin_password({"admin_password": 12345}) is False
    assert migrate.classify({"security": {"admin_password": 12345}}).state == "unexpected_shape"


# --- the expected results --------------------------------------------------------------------------------------------

OLD = {
    "library_name": "Acme", "n": 1.5, "flag": True, "nested": {"list": [1, {"a": None}]},
    "security": {"admin_enabled": False, "admin_password": PW, "custom": [1, 2]},
}


def test_migrating_replaces_only_the_plaintext_key_with_the_hash(migrate):
    before = repr(OLD)

    new = migrate.expected_after_migrating(OLD, "scrypt:x$y$z")

    assert repr(OLD) == before  # the input is not mutated
    assert new["security"] == {"admin_enabled": False, "admin_password_hash": "scrypt:x$y$z", "custom": [1, 2]}
    assert {k: v for k, v in new.items() if k != "security"} == {k: v for k, v in OLD.items() if k != "security"}
    assert PW not in repr(new)


def test_dropping_a_stale_key_removes_only_that_key(migrate):
    old = {"a": 1, "security": {"admin_password": PW, "admin_password_hash": "scrypt:x$y$z", "admin_enabled": True}}

    new = migrate.expected_after_dropping_stale(old)

    assert new == {"a": 1, "security": {"admin_password_hash": "scrypt:x$y$z", "admin_enabled": True}}
    assert "admin_password" in old["security"]  # not mutated


def test_the_hash_the_migration_writes_verifies_the_original_password_the_way_the_app_checks_it(migrate):
    new = migrate.expected_after_migrating(OLD, lock.hash_admin_password(PW))

    assert lock.verify_admin_password(PW, new["security"]) is True
    assert lock.verify_admin_password(PW + "x", new["security"]) is False


def test_refusals_stop_on_unexpected_shapes_and_on_any_branch_level_credential(migrate):
    def row(table, state, reason=""):
        return migrate.Row(table, 1, 1, "s", {}, migrate.Classification(state, reason))

    clean_org = [row("organization_settings", s) for s in ("no_lock", "plaintext_only", "hash_only", "both")]
    assert migrate.refusals(clean_org, [row("branch_settings", "no_lock")]) == []

    stop = migrate.refusals([row("organization_settings", "unexpected_shape", "security is not a JSON object")], [])
    assert len(stop) == 1 and "security is not a JSON object" in stop[0]

    for state in ("plaintext_only", "hash_only", "both", "unexpected_shape"):
        assert len(migrate.refusals(clean_org, [row("branch_settings", state)])) == 1, state


def test_a_row_object_never_prints_the_document_it_holds(migrate):
    row = migrate.Row("organization_settings", 7, 3, "acme", {"security": {"admin_password": PW}},
                      migrate.classify({"security": {"admin_password": PW}}))

    assert PW not in repr(row) and PW not in str(row)
    assert "settings_row_id=7" in migrate._describe(row) and PW not in migrate._describe(row)


# --- the guards refuse before any connection ----------------------------------------------------------------------------

@pytest.fixture
def no_connections(migrate, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the script tried to create a database engine")

    monkeypatch.setattr(migrate, "create_engine", refuse)


def _main(migrate, argv, monkeypatch, url=DB_URL):
    if url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", url)
    out = io.StringIO()
    return migrate.main(argv, out=out), out.getvalue()


@pytest.mark.parametrize("argv", [
    ["--execute"],
    ["--execute", "--confirm-database", "sortview_prod"],
    ["--execute", "--row-id", "1"],
])
def test_execute_needs_the_confirmation_and_explicit_row_ids(migrate, monkeypatch, no_connections, argv):
    code, output = _main(migrate, argv, monkeypatch)

    assert code == 1 and "--execute needs --confirm-database" in output
    assert DB_PASSWORD not in output and "svc_user" not in output


def test_the_confirmation_must_equal_the_target_database_name(migrate, monkeypatch, no_connections):
    code, output = _main(migrate, ["--execute", "--confirm-database", "some_other_db", "--row-id", "1"], monkeypatch)

    assert code == 1 and "does not match the target database" in output
    assert DB_PASSWORD not in output and "svc_user" not in output


def test_without_a_database_url_or_with_a_garbled_one_it_stops_without_echoing_it(migrate, monkeypatch, no_connections):
    code, output = _main(migrate, [], monkeypatch, url=None)
    assert code == 1 and "DATABASE_URL is not set" in output

    code, output = _main(migrate, [], monkeypatch, url=f"not a url {DB_PASSWORD}")
    assert code == 1 and "could not be parsed" in output and DB_PASSWORD not in output


def test_the_target_line_shows_host_and_database_but_never_credentials(migrate, monkeypatch):
    class Unreachable:
        url = None

        def connect(self):
            raise DataError("SELECT 1", {"p": PW}, Exception(f"password {DB_PASSWORD} row {PW}"))

        def dispose(self):
            pass

    monkeypatch.setattr(migrate, "create_engine", lambda *_a, **_k: Unreachable())

    code, output = _main(migrate, [], monkeypatch)

    assert "Target: host=canary-db-host.example.invalid database=sortview_prod" in output
    assert DB_PASSWORD not in output and "svc_user" not in output
    # a failure prints the safe summary (type, location), never the driver's text, the statement or the values
    assert code == 1 and "FAILED: error_type=sqlalchemy.exc.DataError" in output
    assert PW not in output and "SELECT 1" not in output and "Nothing was committed" in output


def test_a_failure_while_executing_exits_2_and_says_nothing_was_committed(migrate, monkeypatch):
    class Boom:
        def begin(self):
            raise RuntimeError(f"could not connect: {DB_PASSWORD}")

        def dispose(self):
            pass

    monkeypatch.setattr(migrate, "create_engine", lambda *_a, **_k: Boom())

    code, output = _main(migrate, ["--execute", "--confirm-database", "sortview_prod", "--row-id", "1"], monkeypatch)

    assert code == 2 and "FAILED: error_type=builtins.RuntimeError" in output and DB_PASSWORD not in output


# --- structural guards -----------------------------------------------------------------------------------------------

_SECRET_NAMES = {"plaintext", "password_hash", "settings", "old", "after", "expected", "security", "record"}


def _printed_names(source: str) -> list[str]:
    """Names of variables that appear inside any print(...) call's arguments."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
            found += [n.id for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Name) and n.id in _SECRET_NAMES]
            found += [n.attr for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Attribute) and n.attr in _SECRET_NAMES]
    return found


def test_no_print_in_the_script_can_show_a_password_a_hash_or_a_document():
    assert _printed_names(SCRIPT.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("source", [
    "print(plaintext)",
    "print(f'x {password_hash}')",
    "print(row.settings)",
    "print('a', old['security'])",
], ids=["plaintext", "hash", "document-attr", "document-var"])
def test_control_the_print_guard_flags_each_way_of_leaking(source):
    assert _printed_names(source) != []


def test_the_updates_send_only_ids_and_the_new_hash_never_the_plaintext(migrate):
    import re

    for statement in (migrate.MIGRATE_UPDATE, migrate.DROP_STALE_UPDATE, migrate.LOCK_SELECTED_ORG_ROWS,
                      migrate.SELECT_ONE_SETTINGS, migrate.SELECT_ORG_ROWS, migrate.SELECT_BRANCH_ROWS):
        assert set(re.findall(r"(?<![:\w]):(\w+)", statement)) <= {"settings_id", "password_hash", "ids"}
    assert ":password_hash" in migrate.MIGRATE_UPDATE and ":password_hash" not in migrate.DROP_STALE_UPDATE
    # the old state is re-tested on the server, not by sending the old value back
    assert "admin_password}') <> ''" in migrate.MIGRATE_UPDATE and "jsonb_typeof" in migrate.MIGRATE_UPDATE


def test_the_script_uses_the_applications_own_hash_helper_and_no_reversible_encryption():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "from services.admin_lock_service import" in source and "hash_admin_password" in source
    for forbidden in ("pgp_sym_encrypt", "encrypt(", "crypt(", "Fernet", "AES", "base64"):
        assert forbidden not in source, forbidden
