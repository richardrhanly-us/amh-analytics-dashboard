"""Migration f7b3d2c81a46 (required_contract on collector installations and enrollment codes, and the runtime role's
INSERT on v2_cutovers), rendered as offline PostgreSQL SQL -- no database is touched -- plus what must stay true around
it: existing rows stay on the legacy contract, the grant is INSERT and nothing more, and the deployment order is written
down where an operator will read it.

What needs a real server (the columns and constraints as PostgreSQL reports them, existing rows surviving as contract 1,
the grant as the role actually holds it, and the downgrade) is in tests/test_collector_enrollment_v2_postgres.py.
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
REVISION = "f7b3d2c81a46"
PREVIOUS_HEAD = "e3a7b1c9d4f2"
TABLES = {"collector_installations", "collector_enrollment_codes"}


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


def _render(function_name: str, *, role_exists: bool) -> str:
    """The migration's SQL. Offline mode cannot ask pg_roles anything, so the answer is supplied."""
    module = _module()
    module._role_exists = lambda _bind, _name: role_exists
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        getattr(module, function_name)()
    return " ".join(buffer.getvalue().lower().split())


def _docstring() -> str:
    return " ".join((_module().__doc__ or "").split())


def test_the_migration_extends_the_one_linear_history():
    script = _script_directory()

    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    assert len(script.get_heads()) == 1  # later migrations extend the chain; this one just has to stay in it
    assert [r.revision for r in script.walk_revisions() if r.down_revision == PREVIOUS_HEAD] == [REVISION]


def test_it_adds_one_not_null_column_defaulting_to_the_legacy_contract_to_each_table():
    sql = _render("upgrade", role_exists=False)

    for table in TABLES:
        assert f"alter table {table} add column required_contract smallint not null default 1" in sql
        assert (f"alter table {table} add constraint {table}_required_contract_chk "
                "check (required_contract in (1, 2))") in sql
    assert sql.count("add column") == 2 and sql.count("add constraint") == 2
    assert set(re.findall(r"alter table (\w+)", sql)) == TABLES


def test_every_existing_row_stays_on_the_legacy_contract_and_no_row_is_written():
    sql = _render("upgrade", role_exists=True)

    # A constant default on a NOT NULL column: every existing installation and code reads as contract 1, with no
    # backfill statement and nothing else about either table changed.
    assert "default 1" in sql and "default 2" not in sql
    for forbidden in ("update ", "insert into", "delete from", "truncate", "create table", "drop ", "create index",
                      "create policy", "row level security", "create trigger", "alter column", "rename", "force row"):
        assert forbidden not in sql, forbidden
    for untouched in ("agent_tokens", "ingest_key_ids", "organizations", "branches", "organization_settings"):
        assert untouched not in sql, untouched


def test_the_one_grant_is_insert_on_v2_cutovers_and_usage_on_its_sequence():
    sql = _render("upgrade", role_exists=True)

    assert "grant insert on table public.v2_cutovers to sortview_app" in sql
    assert "grant usage on sequence public.v2_cutovers_id_seq to sortview_app" in sql
    assert sql.count("grant ") == 2
    grants = re.findall(r"grant ([a-z, ]+?) on ", sql)
    assert grants == ["insert", "usage"]
    for forbidden in ("update", "delete", "truncate", "select", "all privileges", "references", "trigger", " all on"):
        assert forbidden not in " ".join(grants), forbidden
    assert "revoke" not in sql


def test_a_database_without_the_runtime_role_gets_the_columns_and_no_grant():
    sql = _render("upgrade", role_exists=False)

    assert "grant" not in sql and "sortview_app" not in sql
    assert sql.count("add column") == 2
    assert "from pg_roles where rolname" in " ".join((ROOT / "alembic" / "versions" / f"{REVISION}_add_enrollment_required_contract.py")
                                                      .read_text(encoding="utf-8").lower().split())


def test_the_baseline_records_exactly_that_privilege_and_nothing_stronger():
    assert privileges.BASELINE["v2_cutovers"] == {"SELECT", "INSERT"}
    assert not {"UPDATE", "DELETE", "TRUNCATE"} & privileges.BASELINE["v2_cutovers"]
    assert privileges.NEVER_GRANTED == ("DELETE", "TRUNCATE")
    assert "v2_cutovers_id_seq" in privileges.SEQUENCE_USAGE
    # Stage A1 adds no row level security to it: the insert is scoped by the code that makes it.
    assert "v2_cutovers" not in privileges.RLS_TABLES
    # Nothing new is needed for the key: the role could already insert one (under row level security).
    assert privileges.BASELINE["ingest_key_ids"] == {"SELECT", "INSERT", "UPDATE"} and "ingest_key_ids" in privileges.RLS_TABLES
    # The two tables that gained a column were already writable; a column is covered by the table grant.
    assert privileges.BASELINE["collector_installations"] == {"SELECT", "INSERT", "UPDATE"}
    assert privileges.BASELINE["collector_enrollment_codes"] == {"SELECT", "INSERT", "UPDATE"}

    provisioning = privileges.provisioning_sql("sortview_app")
    assert "GRANT SELECT, INSERT ON TABLE public.v2_cutovers TO sortview_app;" in provisioning
    assert "GRANT USAGE ON SEQUENCE public.v2_cutovers_id_seq TO sortview_app;" in provisioning


def test_the_downgrade_revokes_the_grant_and_drops_exactly_what_was_added():
    sql = _render("downgrade", role_exists=True)

    assert "revoke insert on table public.v2_cutovers from sortview_app" in sql
    assert "revoke usage on sequence public.v2_cutovers_id_seq from sortview_app" in sql
    assert sql.index("revoke insert") < sql.index("drop constraint")
    for table in TABLES:
        assert f"alter table {table} drop constraint {table}_required_contract_chk" in sql
        assert f"alter table {table} drop column required_contract" in sql
    assert sql.count("drop column") == 2 and sql.count("drop constraint") == 2
    # It revokes only what it granted (the role keeps its SELECT), and no cutover row is touched.
    for forbidden in ("revoke select", "revoke all", "drop table", "delete from", "update ", "truncate", "cascade", "grant "):
        assert forbidden not in sql, forbidden
    assert "revoke" not in _render("downgrade", role_exists=False)


def test_the_migration_says_what_it_means_and_in_what_order_to_deploy():
    docstring = _docstring()

    assert "EVERY EXISTING ROW BECOMES 1, AND SO DOES EVERY NEW ONE THAT DOES NOT SAY OTHERWISE." in docstring
    assert "Making contract 2 the normal choice for a new installation is a later stage" in docstring
    assert "NO UPDATE AND NO DELETE" in docstring
    assert "v2_cutovers has no row level security" in docstring and "future hardening" in docstring
    assert "DEPLOY ORDER: run this migration BEFORE deploying the API code that uses it." in docstring
    assert "safe to run against the previous API version" in docstring
    assert "DOWNGRADE revokes the grant and drops the two columns" in docstring
    assert "Cutover rows an enrollment already recorded" in docstring


def test_the_application_reads_the_columns_the_migration_adds():
    enrollment = (ROOT / "src" / "services" / "collector_enrollment_service.py").read_text(encoding="utf-8")
    tenants = (ROOT / "src" / "services" / "tenant_service.py").read_text(encoding="utf-8")

    assert "ci.required_contract AS required_contract" in enrollment
    assert "(installation_id, code_hash, expires_at, created_by_user_id, required_contract)" in enrollment
    assert "(organization_id, branch_id, name, hostname, collector_version, status, required_contract)" in tenants
