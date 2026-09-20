"""Tests for migration b4e91d7a3c58 (collector enrollment codes + agent_tokens.installation_id).

Rendered as offline PostgreSQL SQL (Alembic as_sql mode -- no database is
touched) so the exact DDL can be asserted. The migration is also applied for real,
up and down, in tests/test_collector_enrollment_postgres.py when a local
PostgreSQL is configured.
"""

from __future__ import annotations

import importlib.util
import io
import os

from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVISION = "b4e91d7a3c58"
PREVIOUS_HEAD = "d679b80d3a64"


def _script_directory():
    cfg = Config(os.path.join(ROOT_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_migration():
    revision = _script_directory().get_revision(REVISION)
    assert revision is not None
    spec = importlib.util.spec_from_file_location("migration_b4e91d7a3c58", revision.path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(function_name: str) -> str:
    module = _load_migration()
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        getattr(module, function_name)()
    return " ".join(buffer.getvalue().lower().split())


def test_migration_extends_the_previous_head_and_the_history_stays_linear():
    script = _script_directory()

    revision = script.get_revision(REVISION)

    assert revision is not None and revision.down_revision == PREVIOUS_HEAD
    # Later migrations may extend the chain; it must stay one linear history.
    assert REVISION in {r.revision for r in script.walk_revisions()}
    assert len(script.get_heads()) == 1


def test_upgrade_creates_the_enrollment_codes_table_with_the_required_columns():
    sql = _render("upgrade")

    assert "create table collector_enrollment_codes" in sql
    assert "id bigserial not null" in sql
    assert "installation_id bigint not null" in sql
    assert "code_hash text not null" in sql
    assert "expires_at timestamp with time zone not null" in sql
    assert "used_at timestamp with time zone," in sql and "revoked_at timestamp with time zone," in sql
    assert "created_at timestamp with time zone default current_timestamp not null" in sql
    assert "created_by_user_id bigint" in sql


def test_upgrade_constrains_the_code_table():
    sql = _render("upgrade")

    assert "constraint uq_collector_enrollment_codes_code_hash unique (code_hash)" in sql
    assert "foreign key(installation_id) references collector_installations (id) on delete cascade" in sql
    assert "foreign key(created_by_user_id) references app_users (id) on delete set null" in sql
    assert "primary key (id)" in sql


def test_upgrade_indexes_the_installation_and_the_unused_codes():
    sql = _render("upgrade")

    assert "create index ix_collector_enrollment_codes_installation_id on collector_enrollment_codes (installation_id)" in sql
    assert ("create index ix_collector_enrollment_codes_unused on collector_enrollment_codes (installation_id) "
            "where used_at is null and revoked_at is null") in sql


def test_upgrade_adds_a_nullable_installation_id_to_agent_tokens_with_no_default():
    sql = _render("upgrade")

    assert "alter table agent_tokens add column installation_id bigint" in sql
    added = sql.split("alter table agent_tokens add column installation_id bigint", 1)[1].split(";", 1)[0]
    assert "not null" not in added and "default" not in added  # existing tokens keep NULL: fully backward compatible
    assert ("constraint fk_agent_tokens_installation_id foreign key(installation_id) "
            "references collector_installations (id) on delete cascade") in sql
    assert ("create index ix_agent_tokens_installation_id on agent_tokens (installation_id) "
            "where installation_id is not null") in sql


def test_upgrade_never_changes_existing_data_or_existing_columns():
    sql = _render("upgrade")

    for forbidden in ("insert into", "update ", "delete from", "drop ", "alter column", "truncate"):
        assert forbidden not in sql, forbidden


def test_downgrade_reverses_the_upgrade():
    sql = _render("downgrade")

    assert "drop index ix_agent_tokens_installation_id" in sql
    assert "alter table agent_tokens drop column installation_id" in sql
    assert "drop index ix_collector_enrollment_codes_unused" in sql
    assert "drop index ix_collector_enrollment_codes_installation_id" in sql
    assert "drop table collector_enrollment_codes" in sql
