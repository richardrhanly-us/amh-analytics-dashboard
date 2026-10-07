"""Migration e3a7b1c9d4f2 (memberships.removed_at), rendered as offline PostgreSQL SQL -- no database is touched -- plus
what must stay true around it: it grants nothing, the runtime role still cannot DELETE a membership, and the deployment
order and the cost of a downgrade are written down where an operator will read them.

What needs a real server (the column as PostgreSQL reports it, existing rows surviving with it NULL, the unique
constraint, and the downgrade bringing removed memberships back) is in tests/test_user_admin_postgres.py.
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

ROOT = Path(__file__).resolve().parent.parent
REVISION = "e3a7b1c9d4f2"
PREVIOUS_HEAD = "16b41d730e15"


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


def test_the_migration_is_the_one_new_head_and_the_history_stays_linear():
    script = _script_directory()

    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    assert script.get_heads() == [REVISION]
    assert [r.revision for r in script.walk_revisions() if r.down_revision == PREVIOUS_HEAD] == [REVISION]


def test_it_adds_exactly_one_nullable_timestamptz_column_to_memberships():
    sql = _render("upgrade")

    assert "alter table memberships add column removed_at timestamptz" in sql
    assert sql.count("add column") == 1 and sql.count("timestamptz") == 1
    assert "not null" not in sql and " default " not in sql  # NULLable, no default: catalog-only, no rewrite


def test_it_backfills_nothing_and_touches_nothing_else():
    sql = _render("upgrade")

    assert set(re.findall(r"alter table (\w+)", sql)) == {"memberships"}
    # No statement writes a row, so every existing membership is exactly as active as it was.
    for forbidden in ("update ", "insert into", "delete from", "truncate", "now()", "current_timestamp", "grant", "revoke",
                      "create policy", "row level security", "create table", "drop ", "create index", "create unique",
                      "create trigger", "alter column", "rename", "add constraint", "primary key", "references",
                      "app_users", "organizations", "is_active"):
        assert forbidden not in sql, forbidden


def test_the_runtime_role_needs_no_new_privilege_and_still_cannot_delete_a_membership():
    # The baseline grants are per TABLE; a column added later is covered by them.
    assert privileges.BASELINE["memberships"] == {"SELECT", "INSERT", "UPDATE"}
    assert privileges.NEVER_GRANTED == ("DELETE", "TRUNCATE")
    assert not set(privileges.NEVER_GRANTED) & privileges.BASELINE["memberships"]
    assert "memberships" not in privileges.RLS_TABLES  # unchanged: access to it is decided by the queries that read it
    assert "NO GRANT, NO POLICY." in _docstring() and "It still cannot delete a membership." in _docstring()


def test_the_downgrade_drops_exactly_the_column_and_says_what_that_costs():
    sql = _render("downgrade")

    assert "alter table memberships drop column removed_at" in sql and sql.count("drop column") == 1
    assert set(re.findall(r"alter table (\w+)", sql)) == {"memberships"}
    for forbidden in ("drop table", "drop constraint", "drop index", "cascade", "delete from", "update ", "grant", "revoke"):
        assert forbidden not in sql, forbidden

    docstring = _docstring()
    assert "DOWNGRADE drops the column -- AND WITH IT THE FACT THAT ANYONE WAS REMOVED." in docstring
    assert "becomes an active membership again" in docstring
    assert "WHERE removed_at IS NOT NULL" in docstring  # how to list them first
    assert "Removed memberships become active again" in (_module().downgrade.__doc__ or "")


def test_the_migration_says_why_and_in_what_order_to_deploy():
    docstring = _docstring()

    for heading in ("WHY A COLUMN AND NOT A DELETE.", "NO BACKFILL.", "UNIQUE (organization_id, user_id) STAYS.", "NO INDEX.",
                    "DEPLOY ORDER."):
        assert heading in docstring, heading
    assert "Apply this migration BEFORE deploying the application that reads the column" in docstring
    assert "never a second row" in docstring
