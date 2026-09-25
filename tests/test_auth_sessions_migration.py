"""Tests for migration 0d1dcae29e32 (auth_sessions, persistent login).

Rendered as offline PostgreSQL SQL (Alembic as_sql mode -- no database is
touched) so the exact DDL, including the guarded runtime-role grant, can be
asserted. _role_exists is monkeypatched directly (rather than faking a real
bind) since op.get_bind() returns None in as_sql mode and _role_exists's real
implementation requires a live connection -- see 67d06f4ccd24, which has no
offline test for exactly this reason.
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
REVISION = "0d1dcae29e32"
PREVIOUS_HEAD = "f2a91c7d4e83"


def _script_directory():
    cfg = Config(os.path.join(ROOT_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_migration():
    revision = _script_directory().get_revision(REVISION)
    assert revision is not None
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", revision.path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(function_name: str, monkeypatch, role_exists: bool) -> str:
    module = _load_migration()
    monkeypatch.setattr(module, "_role_exists", lambda bind, role_name: role_exists)

    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        getattr(module, function_name)()
    return " ".join(buffer.getvalue().lower().split())


def test_migration_extends_the_previous_head_and_the_history_stays_linear():
    script = _script_directory()

    revision = script.get_revision(REVISION)

    assert revision is not None and revision.down_revision == PREVIOUS_HEAD
    assert REVISION in {r.revision for r in script.walk_revisions()}
    assert len(script.get_heads()) == 1


def test_upgrade_creates_the_auth_sessions_table_with_the_required_columns(monkeypatch):
    sql = _render("upgrade", monkeypatch, role_exists=False)

    assert "create table auth_sessions" in sql
    assert "id bigserial not null" in sql
    assert "user_id bigint not null" in sql
    assert "token_hash varchar(64) not null" in sql
    assert "created_at timestamp with time zone default current_timestamp not null" in sql
    assert "expires_at timestamp with time zone not null" in sql
    assert "revoked_at timestamp with time zone," in sql
    assert "last_seen_at timestamp with time zone" in sql


def test_upgrade_constrains_the_table(monkeypatch):
    sql = _render("upgrade", monkeypatch, role_exists=False)

    assert "constraint uq_auth_sessions_token_hash unique (token_hash)" in sql
    assert "foreign key(user_id) references app_users (id) on delete cascade" in sql
    assert "primary key (id)" in sql


def test_upgrade_indexes_user_id_only(monkeypatch):
    sql = _render("upgrade", monkeypatch, role_exists=False)

    assert (
        "create index ix_auth_sessions_user_id "
        "on auth_sessions (user_id)"
    ) in sql

    assert "ix_auth_sessions_token_hash" not in sql
    assert sql.count("create index") == 1


def test_upgrade_grants_select_insert_update_but_never_delete_when_role_exists(monkeypatch):
    sql = _render("upgrade", monkeypatch, role_exists=True)

    assert "grant select, insert, update on table public.auth_sessions to sortview_app" in sql
    assert "grant usage on sequence public.auth_sessions_id_seq to sortview_app" in sql
    # No SELECT on the sequence (USAGE alone is sufficient for nextval()) and
    # no DELETE in either grant's privilege list. "delete on" (not a bare
    # "delete" substring check) so this doesn't false-positive on the FK's
    # own unrelated "on delete cascade" clause earlier in the same DDL.
    assert "select on sequence" not in sql
    assert "delete on" not in sql


def test_upgrade_skips_all_grants_when_role_does_not_exist(monkeypatch):
    sql = _render("upgrade", monkeypatch, role_exists=False)

    assert "grant" not in sql


def test_upgrade_never_touches_existing_data(monkeypatch):
    sql = _render("upgrade", monkeypatch, role_exists=False)

    for forbidden in (
        "insert into",
        "update ",
        "delete from",
        "drop ",
        "alter column",
        "truncate",
    ):
        assert forbidden not in sql, forbidden


def test_downgrade_revokes_before_dropping_when_role_exists(monkeypatch):
    sql = _render("downgrade", monkeypatch, role_exists=True)

    assert "revoke select, insert, update on table public.auth_sessions from sortview_app" in sql
    assert "revoke usage on sequence public.auth_sessions_id_seq from sortview_app" in sql
    assert "drop index ix_auth_sessions_user_id" in sql
    assert "drop table auth_sessions" in sql

    # Ordering: both REVOKEs happen before the index/table are dropped.
    revoke_table_pos = sql.index("revoke select, insert, update on table public.auth_sessions")
    revoke_seq_pos = sql.index("revoke usage on sequence public.auth_sessions_id_seq")
    drop_index_pos = sql.index("drop index ix_auth_sessions_user_id")
    drop_table_pos = sql.index("drop table auth_sessions")

    assert revoke_table_pos < drop_index_pos
    assert revoke_seq_pos < drop_index_pos
    assert drop_index_pos < drop_table_pos


def test_downgrade_skips_revokes_when_role_does_not_exist(monkeypatch):
    sql = _render("downgrade", monkeypatch, role_exists=False)

    assert "revoke" not in sql
    assert "drop index ix_auth_sessions_user_id" in sql
    assert "drop table auth_sessions" in sql
