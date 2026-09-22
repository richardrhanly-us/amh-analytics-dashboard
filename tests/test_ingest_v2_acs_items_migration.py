"""Tests for migration e5a2c7b93d14 (acs_hold_events -> acs_item_events), the Contract v2 ACS item-event amendment.

The upgrade/downgrade are rendered as offline PostgreSQL SQL (Alembic as_sql mode -- no database is touched), so the exact DDL
is asserted: that it is a rename-and-extend of ONE table, that it creates and drops no table, that it touches no v1 object, and
that its checks are the API's. The same migration is run against a real PostgreSQL, over Step 3 data, by
tests/test_ingest_v2_postgres.py.
"""

from __future__ import annotations

import importlib.util
import io
import os
import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from src.services import ingest_v2_models as models

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVISION = "e5a2c7b93d14"
STEP3 = "d3f1a8c95b27"
V1_OBJECTS = ("checkins", "rejects", "acs_events", "checkins_clean", "rejects_clean", "checkins_routed", "pipeline_status",
              "agent_tokens", "organizations", "branches", "customers", "collector_installations", "bin_routing_map")
HOLD_ONLY = ("destination", "is_ill", "is_branch_services", "is_collection_services")
FORBIDDEN_WORDS = ("message_code", "raw_message", "patron", "barcode", "title", "message_type", "free_text", "call_number")


def _script_directory():
    cfg = Config(os.path.join(ROOT_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load():
    revision = _script_directory().get_revision(REVISION)
    assert revision is not None
    spec = importlib.util.spec_from_file_location("migration_e5a2c7b93d14", revision.path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(function_name: str) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        getattr(_load(), function_name)()
    return " ".join(buffer.getvalue().lower().split())


# --- chain -------------------------------------------------------------------------------------------------------------

def test_the_amendment_is_a_new_revision_on_top_of_step_3_and_the_chain_stays_a_single_head():
    script = _script_directory()
    revision = script.get_revision(REVISION)

    assert revision is not None and revision.down_revision == STEP3
    # Not asserting REVISION itself is the current head: later migrations
    # (e.g. the RLS phase 1 migration) legitimately extend the chain past
    # it. What must still hold, and is what this test actually protects
    # against, is that the amendment never forked the history -- there is
    # still exactly one head overall.
    assert len(script.get_heads()) == 1
    # REVISION's own ancestry (not the tree's current head) sits directly
    # on Step 3 with nothing else merged in between -- walking from
    # REVISION specifically keeps this valid regardless of what's added on
    # top of it later.
    assert [r.revision for r in script.walk_revisions(base="base", head=REVISION)][:2] == [REVISION, STEP3]


def test_step_3s_revision_still_creates_the_old_table_it_was_not_rewritten():
    text = Path(_script_directory().get_revision(STEP3).path).read_text(encoding="utf-8")

    assert "CREATE TABLE acs_hold_events" in text and "acs_item_events" not in text


# --- upgrade: one table renamed and extended, nothing else ----------------------------------------------------------------

def test_upgrade_creates_and_drops_no_table_and_touches_no_v1_object():
    sql = _render("upgrade")

    for forbidden in ("create table", "drop table", "truncate", "delete from", "create trigger", "create view", "create function",
                      "create or replace"):
        assert forbidden not in sql, forbidden
    for name in V1_OBJECTS:
        assert not re.search(rf"\b{name}\b", sql), name


def test_upgrade_renames_the_table_and_every_object_that_carries_its_name():
    sql = _render("upgrade")

    assert "alter table acs_hold_events rename to acs_item_events" in sql
    assert "alter sequence acs_hold_events_id_seq rename to acs_item_events_id_seq" in sql
    for old, new in (("acs_hold_events_pkey", "acs_item_events_pkey"),
                     ("acs_hold_events_event_identity_uidx", "acs_item_events_event_identity_uidx"),
                     ("acs_hold_events_scope_time_idx", "acs_item_events_scope_time_idx")):
        assert f"alter index {old} rename to {new}" in sql
    for suffix in ("customer_id_fkey", "branch_id_fkey", "key_id_format_chk", "event_key_format_chk", "item_key_format_chk",
                   "destination_format_chk", "ruleset_id_format_chk"):
        assert f"rename constraint acs_hold_events_{suffix} to acs_item_events_{suffix}" in sql


def test_upgrade_adds_exactly_one_column_state_and_existing_rows_become_holds_before_it_is_required():
    sql = _render("upgrade")

    assert re.findall(r"add column (\w+)", sql) == ["state"]
    add = sql.index("add column state text")
    update = sql.index("update acs_item_events set state = 'hold'")
    required = sql.index("alter column state set not null")
    assert add < update < required  # never NOT NULL before the existing rows have a value


def test_upgrade_relaxes_exactly_the_four_hold_only_columns_and_no_other():
    sql = _render("upgrade")

    relaxed = re.findall(r"alter column (\w+) drop not null", sql)

    assert relaxed == list(HOLD_ONLY)  # ruleset_id was already nullable; event_key, item_key, event_time stay required
    assert "alter column item_key drop not null" not in sql and "alter column event_key drop not null" not in sql


def test_upgrade_enforces_the_closed_state_and_the_shape_of_each_state_in_the_database():
    sql = _render("upgrade")

    assert "check (state in ('hold', 'non_hold_101', 'other_code10'))" in sql
    assert re.search(r"state = 'hold' and destination is not null and is_ill is not null and is_branch_services is not null "
                     r"and is_collection_services is not null", sql)
    assert re.search(r"state in \('non_hold_101', 'other_code10'\) and destination is null and is_ill is null "
                     r"and is_branch_services is null and is_collection_services is null and ruleset_id is null", sql)


def test_the_states_in_the_database_are_exactly_the_apis_closed_enum():
    sql = _render("upgrade")
    match = re.search(r"check \(state in \(([^)]*)\)\)", sql)

    assert match and set(re.findall(r"'([^']*)'", match.group(1))) == set(models.ACS_ITEM_STATES)


def test_upgrade_replaces_the_item_index_with_the_latest_record_per_item_shape():
    sql = _render("upgrade")

    assert "drop index acs_hold_events_scope_item_idx" in sql
    assert "create index acs_item_events_scope_item_time_idx on acs_item_events (customer_id, branch_id, item_key, event_time)" in sql
    assert "concurrently" not in sql  # new, unused tables: plain DDL


@pytest.mark.parametrize("word", FORBIDDEN_WORDS)
def test_the_amendment_adds_no_raw_message_code_patron_barcode_title_or_free_text_column_anywhere(word):
    assert word not in _render("upgrade")


def test_the_identity_index_and_the_column_types_are_not_touched():
    sql = _render("upgrade")

    assert "unique" not in sql  # the dedup index is only renamed, never rebuilt or changed
    assert not re.search(r"\btimestamp\b", sql) and "alter column event_time" not in sql
    assert "alter column item_key" not in sql and "alter column key_id" not in sql


# --- downgrade -----------------------------------------------------------------------------------------------------------------

def test_the_downgrade_refuses_before_any_change_if_a_non_hold_row_exists():
    sql = _render("downgrade")

    guard = sql.index("raise exception")
    assert "state <> 'hold'" in sql and "cannot downgrade" in sql
    for first_ddl in ("drop index", "drop constraint", "drop column", "rename to"):
        assert guard < sql.index(first_ddl), first_ddl  # the check comes before every structural change


def test_the_downgrade_restores_step_3_and_drops_no_table():
    sql = _render("downgrade")

    assert "drop table" not in sql
    assert "alter table acs_item_events rename to acs_hold_events" in sql
    assert "alter sequence acs_item_events_id_seq rename to acs_hold_events_id_seq" in sql
    assert "alter table acs_item_events drop column state" in sql
    assert re.findall(r"alter column (\w+) set not null", sql) == list(HOLD_ONLY)
    assert "create index acs_hold_events_scope_item_idx on acs_item_events (customer_id, branch_id, item_key)" in sql


def test_every_rename_the_upgrade_makes_the_downgrade_reverses():
    up, down = _render("upgrade"), _render("downgrade")
    renames = re.findall(r"rename constraint (\w+) to (\w+)", up) + re.findall(r"alter (?:table|index|sequence) (\w+) rename to (\w+)", up)

    assert len(renames) == 12  # the table, its sequence, 3 indexes, and 7 constraints (2 foreign keys, 5 format checks)
    for old, new in renames:
        assert f"rename constraint {new} to {old}" in down or f"{new} rename to {old}" in down, (old, new)


def test_the_docstring_documents_the_deploy_order_and_the_tiebreak():
    doc = _load().__doc__.lower()

    assert "deploy order" in doc and "sortview_v2_ingest_enabled" in doc
    assert "(event_time, id)" in doc and "tiebreak" in doc
    assert "acs_hold_events" in doc and "acs_item_events" in doc
