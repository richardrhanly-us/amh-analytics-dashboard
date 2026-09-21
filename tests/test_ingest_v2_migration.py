"""Tests for migration d3f1a8c95b27 (Privacy Contract v2 ingest tables).

The upgrade/downgrade are rendered as offline PostgreSQL SQL (Alembic as_sql mode -- no database is touched), so the exact
DDL can be asserted: that it is purely additive, that it creates exactly the four approved tables, that the event tables
physically lack every prohibited legacy column, that time is TIMESTAMPTZ, and that its CHECK patterns are the API's.
The same migration is run on a real PostgreSQL by tests/test_ingest_v2_postgres.py.
"""

from __future__ import annotations

import importlib.util
import io
import os
import re

import pytest
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from src.services import ingest_v2_models as models

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVISION = "d3f1a8c95b27"
PREVIOUS_HEAD = "b4e91d7a3c58"
NEW_TABLES = ("ingest_key_ids", "checkin_events", "reject_events", "acs_hold_events")
EVENT_TABLES = ("checkin_events", "reject_events", "acs_hold_events")
V1_OBJECTS = ("checkins", "rejects", "acs_events", "checkins_clean", "rejects_clean", "checkins_routed", "pipeline_status",
              "agent_tokens", "organizations", "branches", "customers", "collector_installations", "bin_routing_map")
PROHIBITED_COLUMNS = ("barcode", "barcode_key", "title", "patron_id", "patron", "raw_message", "message", "error_message",
                      "source_event_id", "source_file", "call_number", "shelf_code", "collection_code", "flag_1", "is_problem")


def _script_directory():
    cfg = Config(os.path.join(ROOT_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_migration():
    revision = _script_directory().get_revision(REVISION)
    assert revision is not None
    spec = importlib.util.spec_from_file_location("migration_d3f1a8c95b27", revision.path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(function_name: str) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        getattr(_load_migration(), function_name)()
    return " ".join(buffer.getvalue().lower().split())


def _table_ddl(sql: str, table: str) -> str:
    """The text of one CREATE TABLE statement (through its closing parenthesis and semicolon)."""
    match = re.search(rf"create table {table} \((.*?)\);", sql)
    assert match, table
    return match.group(1)


def _column_names(ddl: str) -> list[str]:
    """Column names of a CREATE TABLE body (constraint clauses skipped)."""
    names = []
    depth, current = 0, ""
    for char in ddl:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            names.append(current.strip())
            current = ""
        else:
            current += char
    names.append(current.strip())
    return [part.split()[0] for part in names if part and part.split()[0] not in {"constraint", "primary", "unique", "check"}]


# --- chain ---------------------------------------------------------------------------------------------------------

def test_the_migration_chains_from_the_previous_head_and_is_the_single_head():
    script = _script_directory()
    revision = script.get_revision(REVISION)

    assert revision is not None and revision.down_revision == PREVIOUS_HEAD
    assert script.get_heads() == [REVISION]  # one linear history, and this migration is its head
    assert [r.revision for r in script.walk_revisions()][:2] == [REVISION, PREVIOUS_HEAD]


# --- purely additive -------------------------------------------------------------------------------------------------

def test_upgrade_creates_exactly_the_four_approved_tables():
    sql = _render("upgrade")

    assert re.findall(r"create table (\w+)", sql) == ["ingest_key_ids", "checkin_events", "reject_events", "acs_hold_events"]
    assert sorted(re.findall(r"create table (\w+)", sql)) == sorted(NEW_TABLES)


def test_upgrade_is_additive_only_it_alters_drops_and_replaces_nothing():
    sql = _render("upgrade")

    for statement in ("alter table", "drop ", "truncate", "delete from", "insert into", "update ", "create trigger",
                      "create or replace", "create view", "create function", "rename ", "add column", "add constraint"):
        assert statement not in sql, statement


def test_upgrade_never_names_a_v1_table_except_as_a_foreign_key_target():
    sql = _render("upgrade")

    for name in V1_OBJECTS:
        for match in re.finditer(rf"\b{name}\b", sql):
            context = sql[max(0, match.start() - 12):match.end() + 6]
            assert "references " + name in context, f"{name} appears outside a REFERENCES clause: {context!r}"
    referenced = set(re.findall(r"references (\w+)\(", sql))
    assert referenced == {"customers", "branches"}  # the same two tables v1's checkins references, and nothing else


def test_upgrade_touches_no_existing_index_constraint_or_trigger():
    sql = _render("upgrade")

    for name in ("checkins_unique_event_scoped", "rejects_unique_event_scoped", "acs_events_unique_scoped",
                 "trg_sync_checkins_to_clean", "trg_sync_rejects_to_clean", "checkins_source_event_id_uidx"):
        assert name not in sql, name


def test_upgrade_uses_plain_transactional_ddl_because_every_table_is_new_and_empty():
    sql = _render("upgrade")

    assert "concurrently" not in sql and "commit" not in sql


# --- time is timestamptz -----------------------------------------------------------------------------------------------

def test_every_timestamp_column_is_timestamptz():
    sql = _render("upgrade")

    assert "timestamp without time zone" not in sql
    assert not re.search(r"\btimestamp\b(?! with time zone)", sql)  # never a bare TIMESTAMP
    for table in EVENT_TABLES:
        ddl = _table_ddl(sql, table)
        assert "event_time timestamptz not null" in ddl and "received_at timestamptz not null default now()" in ddl


# --- the event tables physically lack every prohibited legacy column ----------------------------------------------------

@pytest.mark.parametrize("table", NEW_TABLES)
def test_no_v2_table_has_a_prohibited_or_free_text_column(table):
    columns = set(_column_names(_table_ddl(_render("upgrade"), table)))

    assert columns.isdisjoint(PROHIBITED_COLUMNS), columns & set(PROHIBITED_COLUMNS)


def test_the_event_tables_have_exactly_the_approved_columns():
    sql = _render("upgrade")
    tenant = ["id", "customer_id", "branch_id", "key_id"]

    assert _column_names(_table_ddl(sql, "checkin_events")) == [*tenant, "event_key", "event_time", "item_key", "destination", "bin", "received_at"]
    assert _column_names(_table_ddl(sql, "reject_events")) == [*tenant, "event_key", "event_time", "error_class", "item_key", "received_at"]
    assert _column_names(_table_ddl(sql, "acs_hold_events")) == [
        *tenant, "event_key", "event_time", "item_key", "destination", "is_ill", "is_branch_services",
        "is_collection_services", "ruleset_id", "received_at"]


def test_the_registry_holds_no_key_material_only_the_algorithm_and_lifecycle_and_the_latest_heartbeat():
    columns = _column_names(_table_ddl(_render("upgrade"), "ingest_key_ids"))

    assert columns == ["id", "key_id", "customer_id", "branch_id", "algorithm", "status", "created_at", "retired_at",
                       "last_heartbeat_at", "health_status", "last_error_class", "pending_outbox_count", "quarantined_count",
                       "oldest_pending_event_at", "last_success_at", "watcher_last_active_at"]
    assert not {"secret", "hmac_key", "key", "key_material", "salt", "seed", "token", "password"} & set(columns)


def test_the_registry_records_the_non_secret_algorithm_and_a_lifecycle_state():
    ddl = _table_ddl(_render("upgrade"), "ingest_key_ids")

    assert "algorithm text not null default 'hmac-sha256-v1'" in ddl
    assert "check (algorithm in ('hmac-sha256-v1'))" in ddl
    assert "check (status in ('active', 'retired'))" in ddl
    assert "(status = 'active' and retired_at is null) or (status = 'retired' and retired_at is not null)" in ddl


SECRET_WORDS = ("secret", "hmac", "salt", "seed", "password", "passphrase", "private", "material", "credential", "token", "signing")


@pytest.mark.parametrize("table", NEW_TABLES)
def test_no_v2_column_can_hold_an_hmac_secret_or_key_material(table):
    for column in _column_names(_table_ddl(_render("upgrade"), table)):
        assert not any(word in column for word in SECRET_WORDS), (table, column)


def test_the_only_key_columns_are_the_opaque_identifier_and_the_two_hmac_outputs():
    sql = _render("upgrade")
    named_key = {(t, c) for t in NEW_TABLES for c in _column_names(_table_ddl(sql, t)) if "key" in c}

    assert named_key == {("ingest_key_ids", "key_id"), ("checkin_events", "key_id"), ("checkin_events", "event_key"),
                         ("checkin_events", "item_key"), ("reject_events", "key_id"), ("reject_events", "event_key"),
                         ("reject_events", "item_key"), ("acs_hold_events", "key_id"), ("acs_hold_events", "event_key"),
                         ("acs_hold_events", "item_key")}


# --- dedup and indexes ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("table", EVENT_TABLES)
def test_each_event_table_is_unique_on_tenant_key_id_and_event_key(table):
    sql = _render("upgrade")

    assert re.search(rf"create unique index {table}_event_identity_uidx on {table} \(customer_id, branch_id, key_id, event_key\)", sql)


def test_the_registry_key_id_is_unique_and_scope_lookups_are_indexed():
    sql = _render("upgrade")

    assert "create unique index ingest_key_ids_key_id_uidx on ingest_key_ids (key_id)" in sql
    assert "create index ingest_key_ids_scope_idx on ingest_key_ids (customer_id, branch_id, status)" in sql


@pytest.mark.parametrize("table", EVENT_TABLES)
def test_each_event_table_has_the_scope_time_and_item_indexes(table):
    sql = _render("upgrade")

    assert f"create index {table}_scope_time_idx on {table} (customer_id, branch_id, event_time)" in sql
    assert re.search(rf"create index {table}_scope_item_idx on {table} \(customer_id, branch_id, item_key\)", sql)


def test_there_is_no_foreign_key_from_an_event_table_to_the_registry():
    sql = _render("upgrade")

    assert "references ingest_key_ids" not in sql


# --- the CHECK constraints are the API's patterns --------------------------------------------------------------------------

def test_the_migrations_patterns_are_exactly_the_apis():
    migration = _load_migration()

    assert migration.UUID4 == models.UUID4_PATTERN
    assert migration.HMAC_HEX == models.HMAC_HEX_PATTERN
    assert migration.DESTINATION == models.DESTINATION_PATTERN
    assert migration.BIN == models.BIN_PATTERN
    assert migration.ERROR_CLASS == models.ERROR_CLASS_PATTERN


def test_the_database_checks_use_those_patterns():
    sql = _render("upgrade")
    m = _load_migration()

    for table in EVENT_TABLES:
        ddl = _table_ddl(sql, table)
        assert f"key_id ~ '{m.UUID4}'" in ddl and f"event_key ~ '{m.HMAC_HEX}'" in ddl
    assert f"destination ~ '{m.DESTINATION}'" in _table_ddl(sql, "checkin_events")
    assert f"bin ~ '{m.BIN}'" in _table_ddl(sql, "checkin_events")
    assert f"destination ~ '{m.DESTINATION}'" in _table_ddl(sql, "acs_hold_events")
    assert f"error_class ~ '{m.ERROR_CLASS}'" in _table_ddl(sql, "reject_events")
    assert f"ruleset_id is null or ruleset_id ~ '{m.UUID4}'" in _table_ddl(sql, "acs_hold_events")
    assert f"item_key is null or item_key ~ '{m.HMAC_HEX}'" in _table_ddl(sql, "checkin_events")
    assert f"item_key ~ '{m.HMAC_HEX}'" in _table_ddl(sql, "acs_hold_events")  # a hold's item_key is required


def test_the_api_enum_values_all_satisfy_the_databases_error_class_pattern():
    for value in models.ERROR_CLASSES:
        assert re.fullmatch(models.ERROR_CLASS_PATTERN, value), value


def test_the_registry_heartbeat_checks_match_the_api_enums_and_bounds():
    ddl = _table_ddl(_render("upgrade"), "ingest_key_ids")

    def in_list(column: str) -> set[str]:
        match = re.search(rf"{column} in \(([^)]*)\)", ddl)
        assert match, column
        return set(re.findall(r"'([^']*)'", match.group(1)))

    assert in_list("health_status") == set(models.HEALTH_STATUSES) == {"healthy", "degraded", "error"}  # the SAME set, no extras
    assert in_list("last_error_class") == set(models.LAST_ERROR_CLASSES)
    assert "auth_failure" not in in_list("health_status") and "auth_failure" in in_list("last_error_class")
    assert f"<= {models.MAX_COUNTER}" in ddl and ddl.count(f"<= {models.MAX_COUNTER}") == 2


# --- downgrade -----------------------------------------------------------------------------------------------------------

def test_downgrade_drops_exactly_the_four_new_tables_and_nothing_else():
    sql = _render("downgrade")

    assert sorted(re.findall(r"drop table if exists (\w+)", sql)) == sorted(NEW_TABLES)
    assert len(re.findall(r"\bdrop\b", sql)) == 4
    for name in V1_OBJECTS:
        assert not re.search(rf"\b{name}\b", sql), name


def test_the_docstring_states_the_downgrade_is_destructive_once_data_exists():
    doc = _load_migration().__doc__.lower()

    assert "destroys v2 data" in doc and "purely additive" in doc
