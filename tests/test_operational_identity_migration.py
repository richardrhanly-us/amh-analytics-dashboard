"""Tests for migration d679b80d3a64 (enforce operational identity integrity).

The upgrade/downgrade are rendered as offline PostgreSQL SQL (Alembic
as_sql mode -- no database is touched) so the exact DDL can be asserted.
"""

from __future__ import annotations

import importlib.util
import io
import os

import pytest
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVISION = "d679b80d3a64"
PREVIOUS_HEAD = "fb984ee6c56c"


def _script_directory():
    cfg = Config(os.path.join(ROOT_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_migration():
    revision = _script_directory().get_revision(REVISION)
    assert revision is not None
    spec = importlib.util.spec_from_file_location("migration_d679b80d3a64", revision.path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(function_name: str) -> str:
    module = _load_migration()
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer},
    )
    with Operations.context(context):
        getattr(module, function_name)()
    return " ".join(buffer.getvalue().lower().split())


# --- chain -------------------------------------------------------------------

def test_migration_chains_from_the_previous_head_and_stays_in_a_single_linear_chain():
    script = _script_directory()

    revision = script.get_revision(REVISION)

    assert revision is not None
    assert revision.down_revision == PREVIOUS_HEAD
    # Later migrations may extend the chain; it must stay one linear history.
    assert REVISION in {r.revision for r in script.walk_revisions()}
    assert len(script.get_heads()) == 1


# --- upgrade DDL -------------------------------------------------------------

def test_upgrade_adds_foreign_key_from_operational_customer_id_to_customers():
    sql = _render("upgrade")

    assert (
        "alter table organizations add constraint "
        "fk_organizations_operational_customer_id foreign key(operational_customer_id) "
        "references customers (id)"
    ) in sql


def test_upgrade_adds_partial_unique_index_on_non_null_operational_customer_id():
    sql = _render("upgrade")

    assert (
        "create unique index uq_organizations_operational_customer_id "
        "on organizations (operational_customer_id) "
        "where operational_customer_id is not null"
    ) in sql


def test_upgrade_adds_check_that_operational_branch_id_equals_id():
    sql = _render("upgrade")

    assert (
        "alter table branches add constraint ck_branches_operational_branch_id_matches_id "
        "check (operational_branch_id is null or operational_branch_id = id)"
    ) in sql


def test_upgrade_adds_no_index_on_branches_and_no_new_tables_or_keys():
    # The CHECK plus branches' primary key already make operational_branch_id
    # unique and non-dangling; a unique index on it would be redundant.
    sql = _render("upgrade")

    assert sql.count("create unique index") == 1
    assert "create index" not in sql
    assert "create table" not in sql
    assert "drop " not in sql
    assert "primary key" not in sql
    assert "operational_branches" not in sql


def test_upgrade_changes_no_data():
    sql = _render("upgrade")

    for statement in ("insert into", "update ", "delete from"):
        assert statement not in sql


# --- downgrade DDL -----------------------------------------------------------

def test_downgrade_removes_exactly_what_upgrade_added():
    sql = _render("downgrade")

    assert "alter table branches drop constraint ck_branches_operational_branch_id_matches_id" in sql
    assert "drop index uq_organizations_operational_customer_id" in sql
    assert (
        "alter table organizations drop constraint fk_organizations_operational_customer_id"
    ) in sql
    assert "drop table" not in sql


@pytest.mark.parametrize("function_name", ["upgrade", "downgrade"])
def test_migration_renders_without_a_database(function_name):
    assert _render(function_name)
