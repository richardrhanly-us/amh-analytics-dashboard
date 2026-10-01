"""Migration c8d5f2a47e91 (collector run / schedule diagnostics on ingest_key_ids), rendered as offline PostgreSQL SQL --
no database is touched -- plus the things about it that must stay true across the codebase: its value lists match the
API's, it grants nothing, and the deployment order is written down where an operator will read it.

What needs a real server (the column types as PostgreSQL reports them, the CHECK constraints refusing bad values, row
level security and the runtime role) is in tests/test_ingest_v2_postgres.py and tests/test_rls_phase1_postgres.py.
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

from scripts import runtime_role_privileges as privileges
from src.services import data_lifecycle_policy as policy
from src.services import ingest_v2_models as models

ROOT = Path(__file__).resolve().parent.parent
REVISION = "c8d5f2a47e91"
PREVIOUS_HEAD = "a7c4e19d5b02"
COLUMNS = ("collector_last_run_at", "collector_next_run_at", "collector_run_duration_ms", "collector_schedule_status")


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


def test_the_migration_extends_the_lifecycle_migration_and_the_history_stays_linear():
    script = _script_directory()

    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    assert REVISION in {r.revision for r in script.walk_revisions()} and len(script.get_heads()) == 1


def test_it_adds_exactly_four_nullable_columns_of_the_approved_types_to_ingest_key_ids():
    sql = _render("upgrade")

    assert "alter table ingest_key_ids add column collector_last_run_at timestamptz" in sql
    assert "alter table ingest_key_ids add column collector_next_run_at timestamptz" in sql
    assert "alter table ingest_key_ids add column collector_run_duration_ms integer" in sql
    assert "alter table ingest_key_ids add column collector_schedule_status text" in sql
    assert sql.count("add column") == 4
    assert "not null" not in sql and " default " not in sql  # NULLable, no default: a catalog-only change, no rewrite


def test_the_duration_and_the_schedule_status_are_constrained_in_the_database():
    sql = _render("upgrade")

    assert ("check ( collector_run_duration_ms is null or (collector_run_duration_ms >= 0 "
            "and collector_run_duration_ms <= 86400000) )") in sql
    assert ("check ( collector_schedule_status is null or collector_schedule_status in "
            "('healthy', 'task_missing', 'task_disabled', 'no_next_run', 'query_failed') )") in sql
    assert sql.count("add constraint") == 2


def test_the_migrations_value_lists_are_the_apis():
    module = _module()

    assert module.SCHEDULE_STATUSES == models.SCHEDULE_STATUSES
    assert module.MAX_RUN_DURATION_MS == models.MAX_RUN_DURATION_MS == 86_400_000


def test_it_touches_nothing_but_ingest_key_ids_and_no_existing_column_row_policy_or_grant():
    sql = _render("upgrade")

    assert set(re.findall(r"alter table (\w+)", sql)) == {"ingest_key_ids"}
    for forbidden in ("grant", "revoke", "create policy", "row level security", "create table", "drop ", "create index",
                      "create trigger", "insert into", "update ", "delete from", "truncate", "alter column", "rename",
                      "pipeline_status"):
        assert forbidden not in sql, forbidden


def test_the_runtime_role_needs_no_new_privilege_for_the_new_columns():
    # The baseline grants are per TABLE; a column added later is covered by them. Nothing about the role changes.
    assert privileges.BASELINE["ingest_key_ids"] == {"SELECT", "INSERT", "UPDATE"}
    assert "ingest_key_ids" in privileges.RLS_TABLES
    assert policy.surface("ingest_key_ids").category == policy.REVOKE  # and its lifecycle classification is unchanged
    assert "NO GRANT, NO POLICY." in _docstring()


def test_the_downgrade_removes_exactly_what_the_upgrade_added():
    sql = _render("downgrade")

    for column in COLUMNS:
        assert f"alter table ingest_key_ids drop column if exists {column}" in sql
    assert "drop constraint if exists ingest_key_ids_collector_schedule_status_chk" in sql
    assert "drop constraint if exists ingest_key_ids_collector_run_duration_ms_chk" in sql
    assert sql.count("drop column") == 4 and sql.count("drop constraint") == 2
    assert sql.index("drop constraint") < sql.index("drop column")
    assert set(re.findall(r"alter table (\w+)", sql)) == {"ingest_key_ids"}


def test_the_deployment_order_is_documented_in_the_migration_and_for_operators():
    docstring = _docstring()
    assert "DEPLOY ORDER." in docstring
    steps = [docstring.index(step) for step in (
        "1. apply this migration", "2. deploy the API and dashboard", "3. only then deploy a Collector release")]
    assert steps == sorted(steps)
    assert "Step 3 must never come first" in docstring and "rejected whole (422)" in docstring

    for name in ("contract-v2-design.md", "collector-v2.md"):
        document = " ".join((ROOT / "docs" / name).read_text(encoding="utf-8").split())
        assert "c8d5f2a47e91" in document, name
        order = [document.index(step) for step in (
            "apply migration `c8d5f2a47e91`", "deploy the API and dashboard", "deploy the new Collector release")]
        assert order == sorted(order), name
        assert "must not be deployed against the old API" in document, name


def test_the_documents_describe_each_field_as_the_code_implements_it():
    design = " ".join((ROOT / "docs" / "contract-v2-design.md").read_text(encoding="utf-8").split())
    collector = " ".join((ROOT / "docs" / "collector-v2.md").read_text(encoding="utf-8").split())

    for column in COLUMNS:
        assert f"`{column}`" in design and f"`{column}`" in collector, column
    for status in models.SCHEDULE_STATUSES:
        assert status in design and status in collector, status
    # Last run: Task Scheduler's LastRunTime, however the run was started -- never "scheduled runs only".
    assert "Windows Task Scheduler `LastRunTime`" in collector
    assert "Start-ScheduledTask" in collector
    for narrowing in ("scheduled-only", "scheduled runs only", "only scheduled runs"):
        assert narrowing not in collector.lower() and narrowing not in design.lower()
    # Next run: what Windows reported, never computed.
    assert "never computed from the cadence" in collector
    # Duration: monotonic; includes the scheduler query; excludes only the heartbeat POST.
    assert "time.perf_counter()" in collector and "Windows Task Scheduler query" in collector
    assert "excludes only the heartbeat POST" in collector
    # The dashboard's overdue rule: two cadences, thirty minutes.
    assert "two cadences (30 minutes)" in collector and "MultipleInstancesPolicy=IgnoreNew" in collector
