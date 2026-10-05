"""Migration 16b41d730e15 (server-stamped report times on pipeline_status), rendered as offline PostgreSQL SQL -- no
database is touched -- plus the things about it that must stay true across the codebase: it grants nothing, it changes
no policy, no collector payload gains a field, and the deployment order is written down where an operator will read it.

What needs a real server (the column types as PostgreSQL reports them, existing rows surviving with both columns NULL,
the primary key, row level security and the runtime role's privileges, and the downgrade) is in
tests/test_pipeline_status_report_stamps_migration_postgres.py.
"""

from __future__ import annotations

import importlib.util
import io
import os
import re
from pathlib import Path

from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

import main
from scripts import runtime_role_privileges as privileges
from src.services import data_lifecycle_policy as policy

ROOT = Path(__file__).resolve().parent.parent
REVISION = "16b41d730e15"
PREVIOUS_HEAD = "c8d5f2a47e91"
COLUMNS = ("status_reported_at", "health_status_reported_at")


def _script_directory() -> ScriptDirectory:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _module():
    revision = _script_directory().get_revision(REVISION)
    assert revision is not None
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", revision.path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(function_name: str) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        getattr(_module(), function_name)()
    return " ".join(buffer.getvalue().lower().split())


def _docstring() -> str:
    return " ".join((_module().__doc__ or "").split())


def test_the_migration_extends_the_collector_diagnostics_migration_and_the_history_stays_linear():
    script = _script_directory()

    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    assert REVISION in {r.revision for r in script.walk_revisions()} and len(script.get_heads()) == 1
    # Exactly one revision was added on top of the previous head: nothing else chains from it.
    assert [r.revision for r in script.walk_revisions() if r.down_revision == PREVIOUS_HEAD] == [REVISION]


def test_it_adds_exactly_two_nullable_timestamptz_columns_to_pipeline_status():
    sql = _render("upgrade")

    assert "alter table pipeline_status add column status_reported_at timestamptz" in sql
    assert "alter table pipeline_status add column health_status_reported_at timestamptz" in sql
    assert sql.count("add column") == 2
    assert sql.count("timestamptz") == 2 and "timestamp without" not in sql
    assert "not null" not in sql and " default " not in sql  # NULLable, no default: a catalog-only change, no rewrite


def test_it_backfills_nothing_and_stamps_nothing():
    sql = _render("upgrade")

    # No statement writes a row: both columns are NULL for every existing row after the upgrade.
    for forbidden in ("update ", "insert into", "set ", "now()", "current_timestamp", "updated_at", "last_attempt",
                      "last_run", "at time zone", "using "):
        assert forbidden not in sql, forbidden
    assert re.findall(r"alter table \w+ (\w+ \w+)", sql) == ["add column", "add column"]


def test_it_touches_nothing_but_pipeline_status_and_no_existing_column_row_index_policy_or_grant():
    sql = _render("upgrade")

    assert set(re.findall(r"alter table (\w+)", sql)) == {"pipeline_status"}
    for forbidden in ("grant", "revoke", "create policy", "row level security", "create table", "drop ", "create index",
                      "create unique", "create trigger", "delete from", "truncate", "alter column", "rename",
                      "add constraint", "primary key", "references", "ingest_key_ids", "v2_cutovers"):
        assert forbidden not in sql, forbidden


def test_the_runtime_role_needs_no_new_privilege_for_the_new_columns():
    # The baseline grants are per TABLE; a column added later is covered by them. Nothing about the role changes.
    assert privileges.BASELINE["pipeline_status"] == {"SELECT", "INSERT", "UPDATE"}
    assert not set(privileges.NEVER_GRANTED) & privileges.BASELINE["pipeline_status"]
    assert "NO GRANT, NO POLICY." in _docstring()
    assert "no column-level grants" in _docstring()
    # The privilege model is still stated per relation: it has no notion of a column to grant on.
    assert all(isinstance(grants, frozenset) for grants in privileges.BASELINE.values())


def test_pipeline_status_stays_outside_row_level_security():
    # Unchanged by this migration, and stated: a read of the table is scoped by its own tenant filter alone.
    assert "pipeline_status" not in privileges.RLS_TABLES
    assert len(privileges.RLS_TABLES) == 7
    assert "outside row level security" in _docstring()
    assert "scoped only by its own customer_id and branch_id filter" in _docstring()
    assert policy.surface("pipeline_status").category == policy.PURGE  # and its lifecycle classification is unchanged


def test_no_collector_or_agent_payload_gains_a_field():
    # Server-stamped means exactly that: no request can carry, set or clear either value.
    for column in COLUMNS:
        assert column not in main.PipelineStatusRequest.model_fields, column
        assert column not in main._PIPELINE_STATUS_LEGACY_FIELDS, column
        assert column not in main._PIPELINE_STATUS_HEARTBEAT_FIELDS, column
        assert column not in main._PIPELINE_STATUS_UPDATABLE_FIELDS, column
    assert "never part of a collector's or an agent's payload" in _docstring()


def test_the_downgrade_removes_exactly_what_the_upgrade_added():
    sql = _render("downgrade")

    for column in COLUMNS:
        assert f"alter table pipeline_status drop column if exists {column}" in sql
    assert sql.count("drop column") == 2
    assert set(re.findall(r"alter table (\w+)", sql)) == {"pipeline_status"}
    assert set(re.findall(r"drop column if exists (\w+)", sql)) == set(COLUMNS)
    for forbidden in ("drop table", "drop constraint", "drop index", "cascade", "updated_at", "last_attempt", "last_run",
                      "grant", "revoke", "policy"):
        assert forbidden not in sql, forbidden


def test_the_migration_says_why_there_is_no_backfill_default_or_index():
    docstring = _docstring()

    assert "NO BACKFILL, NO DEFAULT." in docstring and "NO INDEX." in docstring
    assert "TIMESTAMP WITHOUT TIME ZONE" in docstring               # why the existing columns cannot be used
    assert "Those three columns are NOT changed by this migration" in docstring
    assert "THIS MIGRATION STAMPS NOTHING." in docstring


def test_the_deployment_order_is_documented_in_the_migration():
    docstring = _docstring()

    assert "DEPLOY ORDER." in docstring
    steps = [docstring.index(step) for step in ("1. apply this migration", "2. only then deploy the API")]
    assert steps == sorted(steps)
    assert "Step 2 must never come first" in docstring
    assert "Roll the API back first" in docstring
