"""scripts/issue_ingest_key.py -- issuing and retiring Contract v2 `key_id`s (docs/contract-v2-design.md).

The tool is safe by default (a dry run touches no database), issues only for a fully mapped operational tenant, and only ever
prints the non-secret key_id. These tests run it against a throwaway SQLite FILE (never production): the SQL is the tool's own.
"""

from __future__ import annotations

import importlib.util
import io
import re
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.services import ingest_v2_service as service
from src.services.ingest_v2_models import UUID4_PATTERN

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "issue_ingest_key.py"
CUSTOMER, BRANCH = 10, 1


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("issue_ingest_key_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["issue_ingest_key_under_test"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("issue_ingest_key_under_test", None)


@pytest.fixture
def url(tmp_path):
    path = tmp_path / "keys.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE organizations (id INTEGER PRIMARY KEY, operational_customer_id INTEGER)"))
        conn.execute(text("CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, operational_branch_id INTEGER)"))
        conn.execute(text(
            "CREATE TABLE ingest_key_ids (id INTEGER PRIMARY KEY AUTOINCREMENT, key_id TEXT NOT NULL UNIQUE,"
            " customer_id INTEGER NOT NULL, branch_id INTEGER NOT NULL, algorithm TEXT NOT NULL, status TEXT NOT NULL,"
            " created_at TEXT DEFAULT CURRENT_TIMESTAMP, retired_at TEXT)"))
        conn.execute(text("INSERT INTO organizations VALUES (1, :c)"), {"c": CUSTOMER})
        conn.execute(text("INSERT INTO branches VALUES (1, 1, :b)"), {"b": BRANCH})
    engine.dispose()
    return f"sqlite:///{path}"


def run(tool, *argv):
    out = io.StringIO()
    code = tool.main(list(argv), out=out)
    return code, out.getvalue()


def keys(url):
    engine = create_engine(url)
    with engine.connect() as conn:
        rows = [tuple(r) for r in conn.execute(text("SELECT key_id, customer_id, branch_id, algorithm, status, retired_at FROM ingest_key_ids"))]
    engine.dispose()
    return rows


# --- safe by default -----------------------------------------------------------------------------------------------------

def test_a_dry_run_touches_no_database(tool, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a dry run must not create an engine")

    monkeypatch.setattr(tool, "create_engine", refuse)

    issue_code, issue_out = run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH))
    retire_code, retire_out = run(tool, "retire", "--key-id", "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e")

    assert issue_code == retire_code == 0
    assert "DRY RUN" in issue_out and "Nothing was written" in issue_out and "--execute" in issue_out
    assert "DRY RUN" in retire_out and "Nothing was written" in retire_out


def test_a_dry_run_issues_no_key_id(tool):
    _code, out = run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH))

    assert not re.search(UUID4_PATTERN.strip("^$"), out)


def test_execute_without_a_url_or_environment_variable_is_refused(tool, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    code, out = run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute")

    assert code == 2 and "REFUSED" in out and "DATABASE_URL" in out


# --- issue -----------------------------------------------------------------------------------------------------------------

def test_execute_registers_a_new_active_key_for_the_tenant(tool, url):
    code, out = run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute", url)

    assert code == 0
    ((key_id, customer, branch, algorithm, state, retired),) = keys(url)
    assert re.fullmatch(UUID4_PATTERN, key_id)  # server-issued: a random lower-case UUIDv4
    assert (customer, branch, algorithm, state, retired) == (CUSTOMER, BRANCH, "hmac-sha256-v1", "active", None)
    assert f"ISSUED key_id={key_id}" in out


def test_each_issue_gives_a_new_key(tool, url):
    run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute", url)
    run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute", url)

    assert len({k[0] for k in keys(url)}) == 2


def test_an_unmapped_tenant_is_refused_and_nothing_is_written(tool, url):
    for customer, branch in ((999, BRANCH), (CUSTOMER, 999), (999, 999)):
        code, out = run(tool, "issue", "--customer-id", str(customer), "--branch-id", str(branch), "--execute", url)
        assert code == 2 and "REFUSED" in out and "Nothing was written" in out

    assert keys(url) == []


def test_the_execute_flag_can_use_the_environment_variable(tool, url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", url)

    code, _out = run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute")

    assert code == 0 and len(keys(url)) == 1


def test_the_tool_prints_only_the_key_id_and_the_tenant_no_secret_and_no_url(tool, url):
    _code, out = run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute", url)

    assert url not in out and "sqlite" not in out
    assert "secret" in out.lower() and "never leaves the collector" in out  # it says where the secret is, never what it is


# --- retire ----------------------------------------------------------------------------------------------------------------

def test_retiring_marks_the_key_retired_with_a_timestamp(tool, url):
    _code, out = run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute", url)
    key_id = re.search(r"key_id=(\S+)", out).group(1)

    code, retire_out = run(tool, "retire", "--key-id", key_id, "--execute", url)

    assert code == 0 and f"RETIRED key_id={key_id}" in retire_out
    ((_k, _c, _b, _a, state, retired),) = keys(url)
    assert state == "retired" and retired is not None


def test_retiring_twice_or_retiring_an_unknown_key_is_refused(tool, url):
    _code, out = run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute", url)
    key_id = re.search(r"key_id=(\S+)", out).group(1)
    run(tool, "retire", "--key-id", key_id, "--execute", url)

    again_code, again_out = run(tool, "retire", "--key-id", key_id, "--execute", url)
    unknown_code, _ = run(tool, "retire", "--key-id", "8d9e0f1a-2b3c-4d4e-9f5a-6b7c8d9e0f1a", "--execute", url)

    assert again_code == unknown_code == 2 and "REFUSED" in again_out


@pytest.mark.parametrize("bad", ["not-a-key", "", "3F2B8C1E-4D5A-4B6C-8D7E-9F0A1B2C3D4E", "'; DROP TABLE ingest_key_ids; --"])
def test_a_malformed_key_id_is_refused_before_any_database_is_used(tool, url, bad):
    code, out = run(tool, "retire", "--key-id", bad, "--execute", url)

    assert code == 2 and "REFUSED" in out and (bad == "" or bad not in out)
    assert keys(url) == []


def test_a_retired_key_can_never_be_reactivated_by_the_tool(tool):
    assert not hasattr(service, "reactivate_ingest_key")
    source = SCRIPT.read_text(encoding="utf-8")
    assert "status = 'active'" not in source.replace("status 'active'", "")  # only ever issues new keys


# --- the service functions the tool and the API share ---------------------------------------------------------------------

def test_the_service_refuses_to_issue_for_an_unmapped_tenant(url):
    engine = create_engine(url)
    with engine.begin() as conn, pytest.raises(ValueError):
        service.issue_ingest_key(conn, 5, 5)
    engine.dispose()


def test_ingest_key_problem_distinguishes_the_reasons_for_the_log_only(url):
    engine = create_engine(url)
    with engine.begin() as conn:
        key = service.issue_ingest_key(conn, CUSTOMER, BRANCH)
        assert service.ingest_key_problem(conn, CUSTOMER, BRANCH, key) is None
        assert service.ingest_key_problem(conn, CUSTOMER, BRANCH, "8d9e0f1a-2b3c-4d4e-9f5a-6b7c8d9e0f1a") == "unknown key"
        assert service.ingest_key_problem(conn, 20, 2, key) == "key belongs to another tenant"
        service.retire_ingest_key(conn, key)
        assert service.ingest_key_problem(conn, CUSTOMER, BRANCH, key) == "key status 'retired'"
    engine.dispose()


# --- the tool manages only the non-secret key_id --------------------------------------------------------------------------

def test_the_only_long_token_the_tool_prints_is_the_key_id(tool, url):
    _code, out = run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute", url)
    key_id = re.search(r"key_id=(\S+)", out).group(1)

    long_tokens = re.findall(r"[A-Za-z0-9_+/-]{20,}", out)

    assert long_tokens == [key_id] and re.fullmatch(UUID4_PATTERN, key_id)  # nothing else that could be a secret or a digest


def test_the_registry_row_the_tool_writes_holds_an_identifier_a_tenant_an_algorithm_name_and_a_state_only(tool, url):
    run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--execute", url)
    engine = create_engine(url)
    with engine.connect() as conn:
        (row,) = conn.execute(text("SELECT * FROM ingest_key_ids")).mappings().all()
    engine.dispose()

    populated = {k: v for k, v in row.items() if v is not None}
    assert set(populated) == {"id", "key_id", "customer_id", "branch_id", "algorithm", "status", "created_at"}
    assert populated["algorithm"] == "hmac-sha256-v1"  # an algorithm NAME, not key material


def test_the_dry_run_and_the_refusals_print_no_secret_either(tool, url):
    outputs = [
        run(tool, "issue", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH))[1],
        run(tool, "issue", "--customer-id", "999", "--branch-id", "999", "--execute", url)[1],
        run(tool, "retire", "--key-id", "8d9e0f1a-2b3c-4d4e-9f5a-6b7c8d9e0f1a", "--execute", url)[1],
    ]

    for out in outputs:
        assert not re.search(r"[A-Za-z0-9_+/-]{20,}", out.replace("8d9e0f1a-2b3c-4d4e-9f5a-6b7c8d9e0f1a", "")), out


def test_the_tool_has_no_command_that_reads_or_accepts_a_secret(tool):
    parser = tool._parser()
    actions = {a.dest for sub in parser._subparsers._group_actions for choice in sub.choices.values() for a in choice._actions}

    assert actions == {"help", "execute", "customer_id", "branch_id", "key_id"}  # no --secret, --key, --hmac, --password ...
