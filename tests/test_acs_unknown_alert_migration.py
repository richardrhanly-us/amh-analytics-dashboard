"""Migration 5813f1df32a0: acs_item_events accepts the unknown_alert state (docs/hold-count-accuracy.md, D4).

Rendered offline (Alembic's SQL mode): nothing connects. The real upgrade/downgrade round trip on PostgreSQL is in
tests/test_ingest_v2_postgres.py.
"""

from __future__ import annotations

import importlib.util
import io
import os
import re

from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from src.services import ingest_v2_models as models

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVISION, PREVIOUS = "5813f1df32a0", "b8e2d4f61a07"
OLD_STATES = {"hold", "non_hold_101", "other_code10"}


def _script_directory():
    cfg = Config(os.path.join(ROOT_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _render(function_name: str) -> str:
    revision = _script_directory().get_revision(REVISION)
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", revision.path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        getattr(module, function_name)()
    return " ".join(buffer.getvalue().lower().split())


def _states(sql: str) -> set[str]:
    match = re.search(r"check \(state in \(([^)]*)\)\)", sql)
    assert match
    return set(re.findall(r"'([^']*)'", match.group(1)))


def _non_hold_shape(sql: str) -> set[str]:
    match = re.search(r"\(state in \(([^)]*)\) and destination is null", sql)
    assert match
    return set(re.findall(r"'([^']*)'", match.group(1)))


def test_it_is_the_single_head_directly_after_the_previous_head():
    script = _script_directory()

    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PREVIOUS


def test_upgrade_makes_the_database_states_exactly_the_apis_and_unknown_alert_takes_the_non_hold_shape():
    sql = _render("upgrade")

    assert _states(sql) == set(models.ACS_ITEM_STATES)
    assert _non_hold_shape(sql) == set(models.NON_HOLD_STATES)
    assert "state = 'hold' and destination is not null and is_ill is not null" in sql   # the hold shape is unchanged


def test_upgrade_only_replaces_the_two_constraints_under_a_lock_timeout():
    sql = _render("upgrade")

    assert sql.index("set local lock_timeout = '5s'") < sql.index("drop constraint")
    assert sql.count("drop constraint") == 2 and sql.count("add constraint") == 2
    for forbidden in ("drop table", "create table", "alter column", "drop column", "add column", "update ", "delete from",
                      "truncate", "create index", "drop index", "rename"):
        assert forbidden not in sql, forbidden


def test_downgrade_refuses_while_an_unknown_alert_row_exists_then_restores_the_previous_constraints():
    sql = _render("downgrade")

    refusal = sql.index("if exists (select 1 from acs_item_events where state = 'unknown_alert') then raise exception")
    assert refusal < sql.index("drop constraint")
    assert _states(sql) == OLD_STATES
    assert _non_hold_shape(sql) == OLD_STATES - {"hold"}
