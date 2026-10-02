"""The data-lifecycle policy registry, the runtime-role privilege baseline, the tenant_lifecycle_events migration and the
lifecycle document -- everything that can be checked without a database.

These are the "is the policy still coherent" tests: every table has exactly one category, the purge order respects the
foreign keys, the runtime role is never given DELETE, the two registries describe the same tables, and the document
does not promise more than the product does. tests/test_tenant_lifecycle_postgres.py checks the same registries against
a really migrated PostgreSQL schema.
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

from scripts import runtime_role_privileges as privileges
from src.services import data_lifecycle_policy as policy

ROOT = Path(__file__).resolve().parent.parent
REVISION = "a7c4e19d5b02"
PREVIOUS_HEAD = "0d1dcae29e32"

# Every foreign key in the migrated schema whose parent a purge deletes: (child, parent). From the migrations.
_FOREIGN_KEYS = [
    ("checkins", "customers"), ("checkins", "branches"), ("rejects", "customers"), ("rejects", "branches"),
    ("checkin_events", "customers"), ("checkin_events", "branches"), ("reject_events", "customers"),
    ("reject_events", "branches"), ("acs_item_events", "customers"), ("acs_item_events", "branches"),
    ("ingest_key_ids", "customers"), ("ingest_key_ids", "branches"), ("v2_cutovers", "customers"),
    ("v2_cutovers", "branches"), ("organizations", "customers"), ("memberships", "organizations"),
    ("subscriptions", "organizations"), ("organization_settings", "organizations"), ("branch_settings", "branches"),
    ("collector_installations", "organizations"), ("collector_installations", "branches"),
    ("collector_enrollment_codes", "collector_installations"), ("agent_tokens", "collector_installations"),
]
# Related without a foreign key (the schema does not enforce these, so the plan has to).
_LOGICAL_PARENTS = [
    ("branches", "organizations"), ("pipeline_status", "customers"), ("acs_events", "customers"),
    ("checkins_clean", "checkins"), ("rejects_clean", "rejects"), ("agent_tokens", "customers"),
]


# ======================================================================================================================
# the policy registry
# ======================================================================================================================

def test_every_table_is_classified_exactly_once_with_a_known_category():
    tables = [entry.table for entry in policy.DATABASE_SURFACES]

    assert len(tables) == len(set(tables))
    assert {entry.category for entry in policy.DATABASE_SURFACES} == set(policy.DATABASE_CATEGORIES)
    for entry in policy.DATABASE_SURFACES:
        assert entry.category in policy.DATABASE_CATEGORIES and entry.data_class and entry.at_cutoff


def test_only_revoke_and_purge_surfaces_are_ever_deleted():
    for entry in policy.DATABASE_SURFACES:
        deletable = entry.category in (policy.REVOKE, policy.PURGE)
        assert (entry.purge_where is not None) == deletable, entry.table
        assert (entry.purge_order is not None) == deletable, entry.table


def test_every_delete_predicate_is_scoped_to_the_tenant_by_a_bound_identifier():
    for entry in policy.purge_plan():
        assert re.search(r":(organization_id|customer_id)\b", entry.purge_where), entry.table
        assert not re.search(r"\b(1\s*=\s*1|true)\b", entry.purge_where, re.IGNORECASE), entry.table
        assert ";" not in entry.purge_where


def test_the_purge_order_is_strict_and_deletes_children_before_the_rows_they_reference():
    order = {entry.table: entry.purge_order for entry in policy.purge_plan()}

    assert len(set(order.values())) == len(order)  # no ties: the order is fully determined
    assert [entry.purge_order for entry in policy.purge_plan()] == sorted(order.values())
    for child, parent in _FOREIGN_KEYS + _LOGICAL_PARENTS:
        assert order[child] < order[parent], f"{child} must be deleted before {parent}"
    assert max(order, key=order.get) == "customers"


def test_lifecycle_evidence_is_retained_and_user_and_audit_data_are_governed_separately():
    assert policy.surface("tenant_lifecycle_events").category == policy.RETAIN
    for table in ("app_users", "auth_sessions", "password_reset_tokens", "auth_audit_log"):
        entry = policy.surface(table)
        assert entry.category == policy.SEPARATELY_GOVERNED and entry.purge_where is None
    assert {"tenant_lifecycle_events", "app_users", "auth_audit_log"} <= set(policy.retained_tables())
    # A purge removes the LINK between a user and the tenant, and only that.
    assert policy.surface("memberships").purge_where == "organization_id = :organization_id"


def test_the_global_reference_allowlist_is_explicit():
    assert {e.table for e in policy.DATABASE_SURFACES if e.category == policy.GLOBAL_REFERENCE} == {
        "plans", "feature_entitlements", "bin_routing_map", "alembic_version",
    }


def test_the_cutoff_leaves_subscriptions_alone_and_names_the_billing_caveat():
    entry = policy.surface("subscriptions")

    assert "unchanged" in entry.at_cutoff and entry.category == policy.PURGE and "billing" in entry.note


def test_the_access_artifacts_are_the_revoke_category():
    assert {e.table for e in policy.DATABASE_SURFACES if e.category == policy.REVOKE} == {
        "organizations", "agent_tokens", "collector_installations", "collector_enrollment_codes", "ingest_key_ids",
    }
    assert "customer_id = :customer_id OR installation_id IN" in policy.surface("agent_tokens").purge_where  # legacy too


def test_provider_and_local_machine_surfaces_are_listed_and_never_claimed_as_ours_to_delete():
    categories = {surface.category for surface in policy.EXTERNAL_SURFACES}
    names = " ".join(surface.name for surface in policy.EXTERNAL_SURFACES)

    assert categories == {policy.PROVIDER_AGING, policy.LOCAL_MACHINE}
    for expected in ("Neon point-in-time history", "Neon branches", "Sentry", "DataRoot", "runtime backups",
                     "SORTVIEW_API_TOKEN", "SortViewAgent"):
        assert expected in names
    assert "vendor-owned" in policy.OUT_OF_SCOPE and "no SortView tool may delete them" in policy.OUT_OF_SCOPE


# ======================================================================================================================
# the runtime-role privilege baseline
# ======================================================================================================================

def test_the_runtime_role_is_never_given_delete_or_truncate_on_anything():
    for relation, granted in privileges.BASELINE.items():
        assert not granted & {"DELETE", "TRUNCATE"}, relation
        assert granted <= {"SELECT", "INSERT", "UPDATE"}, relation
    sql = privileges.provisioning_sql().upper()
    grants = [line for line in sql.splitlines() if line.startswith("GRANT")]
    assert grants and not any("DELETE" in line or "TRUNCATE" in line or " ALL " in line for line in grants)


def test_the_baseline_and_the_lifecycle_policy_describe_the_same_tables():
    assert set(privileges.BASELINE) == {e.table for e in policy.DATABASE_SURFACES} | set(policy.DERIVED_VIEWS)


def test_the_operational_event_tables_are_insert_only_for_the_runtime_role_and_under_rls():
    for table in ("checkins", "rejects", "acs_events", "checkin_events", "reject_events", "acs_item_events"):
        assert privileges.BASELINE[table] == {"SELECT", "INSERT"}, table
        assert table in privileges.RLS_TABLES
    assert privileges.BASELINE["ingest_key_ids"] == {"SELECT", "INSERT", "UPDATE"}
    assert "ingest_key_ids" in privileges.RLS_TABLES


def test_the_runtime_role_can_append_lifecycle_evidence_and_do_nothing_else_with_it():
    assert privileges.BASELINE["tenant_lifecycle_events"] == {"INSERT"}  # not even SELECT


def test_the_runtime_role_has_no_access_to_the_clean_copies_or_the_schema_bookkeeping():
    for relation in ("checkins_clean", "rejects_clean", "checkins_routed", "bin_routing_map", "alembic_version"):
        assert privileges.BASELINE[relation] == frozenset(), relation


def test_the_cutoff_needs_nothing_the_baseline_does_not_already_grant():
    # offboard_library UPDATEs exactly these, and INSERTs its evidence.
    for table in ("organizations", "agent_tokens", "collector_installations", "collector_enrollment_codes",
                  "ingest_key_ids", "auth_sessions"):
        assert "UPDATE" in privileges.BASELINE[table], table
    assert "INSERT" in privileges.BASELINE["tenant_lifecycle_events"]


def test_sequence_usage_covers_exactly_the_tables_the_role_may_insert_into():
    assert "tenant_lifecycle_events_id_seq" in privileges.SEQUENCE_USAGE
    assert "pipeline_status_id_seq" not in privileges.SEQUENCE_USAGE  # composite key, no sequence
    for unused in ("plans_id_seq", "feature_entitlements_id_seq", "v2_cutovers_id_seq"):
        assert unused not in privileges.SEQUENCE_USAGE
    sql = privileges.provisioning_sql()
    assert sql.count("GRANT USAGE ON SEQUENCE") == len(privileges.SEQUENCE_USAGE)
    assert "SELECT ON SEQUENCE" not in sql.upper()


def test_the_role_attributes_are_the_non_privileged_ones():
    assert privileges.ROLE_ATTRIBUTES == {
        "rolcanlogin": True, "rolinherit": True, "rolsuper": False, "rolcreatedb": False, "rolcreaterole": False,
        "rolreplication": False, "rolbypassrls": False,
    }


def test_provisioning_sql_is_printed_and_never_executed(monkeypatch):
    monkeypatch.setattr(privileges, "create_engine", lambda *_a, **_k: pytest.fail("must not connect"))
    out = io.StringIO()

    code = privileges.main(["provisioning-sql"], out=out)

    sql = out.getvalue()
    assert code == 0 and sql.startswith("-- Runtime role privilege baseline for sortview_app")
    assert "GRANT INSERT ON TABLE public.tenant_lifecycle_events TO sortview_app;" in sql
    assert "GRANT SELECT, INSERT ON TABLE public.checkins TO sortview_app;" in sql
    assert "-- checkins_clean: no access" in sql
    assert "CREATE ROLE" in sql and "PASSWORD '<set out of band>'" in sql and "\nCREATE ROLE" not in sql  # a comment only


def test_verify_refuses_without_a_database_and_against_a_non_postgresql_one(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out = io.StringIO()
    assert privileges.main(["verify"], out=out) == 2 and "REFUSED" in out.getvalue()

    out = io.StringIO()
    assert privileges.main(["verify", "--database-url", f"sqlite:///{tmp_path / 'x.db'}"], out=out) == 2
    assert "PostgreSQL only" in out.getvalue()


def test_privileges_are_not_managed_by_an_ordinary_migration():
    # A migration grants only what its OWN new table needs. No migration revokes or grants across the schema.
    for path in (ROOT / "alembic" / "versions").glob("*.py"):
        source = path.read_text(encoding="utf-8").upper()
        assert "ON ALL TABLES" not in source and "ALTER DEFAULT PRIVILEGES" not in source, path.name
        assert not re.search(r"GRANT[^\n]*\bDELETE\b", source), path.name


# ======================================================================================================================
# migration a7c4e19d5b02 (rendered offline; no database is touched)
# ======================================================================================================================

def _script_directory():
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _render(function_name: str, monkeypatch, role_exists: bool) -> str:
    revision = _script_directory().get_revision(REVISION)
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", revision.path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_role_exists", lambda bind, role_name: role_exists)

    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        getattr(module, function_name)()
    return " ".join(buffer.getvalue().lower().split())


def test_the_migration_extends_the_previous_head_and_the_history_stays_linear():
    script = _script_directory()

    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    assert len(script.get_heads()) == 1  # later migrations extend the chain; this one just has to stay in it
    assert REVISION in {r.revision for r in script.walk_revisions()}


def test_the_table_stores_historical_scalars_with_no_foreign_key(monkeypatch):
    sql = _render("upgrade", monkeypatch, role_exists=False)

    assert "create table tenant_lifecycle_events" in sql
    for column in ("organization_id bigint not null", "organization_slug text not null", "operational_customer_id integer,",
                   "actor_user_id bigint,", "actor_label text not null", "occurred_at timestamptz not null default now()",
                   "details jsonb not null"):
        assert column in sql, column
    assert "references" not in sql and "foreign key" not in sql and "on delete" not in sql


def test_the_event_type_is_a_closed_list_and_details_must_be_an_object(monkeypatch):
    sql = _render("upgrade", monkeypatch, role_exists=False)

    assert "event_type in ('access_cutoff', 'access_cutoff_reverted', 'purge_executed')" in sql
    assert "jsonb_typeof(details) = 'object'" in sql and "btrim(actor_label) <> ''" in sql
    for event_type in policy.LIFECYCLE_EVENT_TYPES:
        assert f"'{event_type}'" in sql


def test_the_table_is_append_only_for_every_role(monkeypatch):
    sql = _render("upgrade", monkeypatch, role_exists=False)

    assert "before update or delete on tenant_lifecycle_events for each row" in sql
    assert "raise exception 'tenant_lifecycle_events is append-only" in sql


def test_the_runtime_role_gets_insert_only_and_only_when_it_exists(monkeypatch):
    granted = _render("upgrade", monkeypatch, role_exists=True)
    assert "grant insert on table public.tenant_lifecycle_events to sortview_app" in granted
    assert "grant usage on sequence public.tenant_lifecycle_events_id_seq to sortview_app" in granted
    assert granted.count("grant ") == 2
    for forbidden in ("grant select", "grant update", "grant delete", "grant all", "truncate"):
        assert forbidden not in granted, forbidden

    assert "grant" not in _render("upgrade", monkeypatch, role_exists=False)


def test_the_migration_touches_no_existing_table_or_row(monkeypatch):
    sql = _render("upgrade", monkeypatch, role_exists=True)

    for forbidden in ("alter table", "drop ", "insert into", "update tenant", "delete from", "create policy"):
        assert forbidden not in sql, forbidden
    assert sql.count("create table") == 1


def test_the_downgrade_removes_exactly_what_the_upgrade_added(monkeypatch):
    sql = _render("downgrade", monkeypatch, role_exists=True)

    assert sql.index("revoke insert on table public.tenant_lifecycle_events") < sql.index("drop table")
    assert "drop trigger if exists trg_tenant_lifecycle_events_append_only" in sql
    assert "drop function if exists tenant_lifecycle_events_append_only()" in sql
    assert sql.count("drop table") == 1 and "drop table if exists tenant_lifecycle_events" in sql


# ======================================================================================================================
# the document: what it must say, and what it must not claim
# ======================================================================================================================

@pytest.fixture(scope="module")
def document() -> str:
    return (ROOT / "docs" / "data-lifecycle-offboarding.md").read_text(encoding="utf-8")


def _flat(text_: str) -> str:
    return " ".join(text_.split())


def test_the_document_keeps_suspension_offboarding_and_purge_apart(document):
    flat = _flat(document)

    for heading in ("## three separate things", "## suspension", "## offboarding: the access cutoff",
                    "## the database purge"):
        assert heading in document
    assert "a tenant is purged only after it has been offboarded" in flat
    assert "Suspension (`set_library_active_status`) never reaches offboarding" in flat


def test_the_document_does_not_present_a_purge_as_immediate_physical_erasure(document):
    flat = _flat(document)

    assert "LIVE DATABASE PURGE COMPLETE" in document and "PROVIDER HISTORY AGED OUT: NOT VERIFIED" in document
    assert "does not, and cannot, physically erase the data immediately" in flat
    assert "remain restorable until the project's history-retention window has passed" in flat
    assert "holds the tenant's data until that branch is deleted by hand" in flat
    assert "restores the tenant too" in flat
    for overclaim in ("permanently erased", "irrecoverable", "unrecoverable", "immediately erased", "securely wiped"):
        assert overclaim not in flat.lower()


def test_the_document_states_the_backup_and_provider_position_without_inventing_periods(document):
    flat = _flat(document)

    assert "There is no off-platform backup." in flat
    assert "Not documented in this repository" in flat  # Sentry's retention: stated as unknown, not guessed
    assert "No retention period is invented here." in flat
    assert "do not assume it" in flat  # the Neon window is read at the time, not hardcoded as a promise


def test_the_document_says_a_tenant_purge_is_not_deletion_of_a_persons_account_or_audit_history(document):
    flat = _flat(document)

    assert "It does **not** delete any person's global SortView account" in flat
    assert "must not be described as deleting a person's account or their audit history" in flat


def test_the_document_covers_unattributable_rows_local_cleanup_and_the_vendor_logs(document):
    flat = _flat(document)

    assert "cannot be attributed to any tenant" in flat and "never infers an owner" in flat
    assert "A plain uninstall **preserves** `<DataRoot>`" in flat and "-PurgeData" in flat
    assert "<InstallRoot>.backup-<timestamp>" in flat and "SORTVIEW_API_TOKEN" in flat and "C:\\SortViewAgent" in flat
    assert "Tech Logic source logs are vendor-owned" in flat and "No SortView tool deletes them" in flat


def test_the_document_says_who_may_run_the_purge_and_from_where(document):
    flat = _flat(document)

    assert "a city-owned machine and a library's Collector machine are never permitted" in flat
    assert "refuses to run as the application's runtime role, `sortview_app`" in flat


def test_the_document_lists_every_table_and_every_purge_step_the_registry_has(document):
    for entry in policy.DATABASE_SURFACES:
        assert f"`{entry.table}`" in document, entry.table
    purge_section = document[document.index("**What is deleted,**"):document.index("**What is kept.**")]
    positions = [purge_section.index(f"`{entry.table}`") for entry in policy.purge_plan()]
    assert positions == sorted(positions)  # the document's order is the registry's order


def test_the_document_has_a_verification_checklist_and_an_evidence_list(document):
    assert "## verification checklist" in document and "## evidence to retain" in document
    assert document.count("- [ ]") >= 12


def test_the_evidence_table_is_described_as_append_only_and_protected_never_as_tamper_proof(document):
    flat = _flat(document)
    migration = _flat((ROOT / "alembic" / "versions" / f"{REVISION}_add_tenant_lifecycle_events.py").read_text(encoding="utf-8"))

    assert "is append-only and protected, not tamper-proof" in flat
    assert "not cryptographic immutability" in flat and "the table owner can still drop the trigger or the table" in flat
    assert "not cryptographically immutable or tamper-proof" in migration
    # Wherever the strong words appear, they are being denied, not claimed.
    for source in (flat, migration, _flat((ROOT / "scripts" / "purge_tenant_data.py").read_text(encoding="utf-8")),
                   _flat((ROOT / "src" / "services" / "data_lifecycle_policy.py").read_text(encoding="utf-8"))):
        for sentence in re.split(r"(?<=[.:;])\s", source):
            if re.search(r"immutab|tamper-?proof", sentence, re.IGNORECASE):
                assert re.search(r"\bnot\b", sentence), sentence


def test_the_document_states_the_migration_first_deployment_order(document):
    flat = _flat(document)
    section = flat[flat.index("## deployment order"):flat.index("## verification checklist")]

    steps = [section.index(step) for step in (
        "Apply migration `a7c4e19d5b02`", "Verify the table, the trigger and the grants", "Deploy the application code")]
    assert steps == sorted(steps)
    assert "Migration first is the supported procedure." in section
    assert "offboarding would fail when it tries to insert its evidence row into a table that does not exist yet" in section
    assert "it rolls back, the library is left exactly as it was" in section
    assert "runtime_role_privileges.py verify" in section and "trg_tenant_lifecycle_events_append_only" in section
    migration = _flat((ROOT / "alembic" / "versions" / f"{REVISION}_add_tenant_lifecycle_events.py").read_text(encoding="utf-8"))
    assert "DEPLOY ORDER: run this migration BEFORE deploying the application code" in migration


def test_the_document_says_what_the_evidence_table_retains_including_the_operators_identity(document):
    flat = _flat(document)

    assert "`actor_label` is the Super Admin operator's sign-in name, which is their staff e-mail address" in flat
    assert "This is platform and operator audit data, not patron or item data." in flat
    assert "survives the purge of the tenant it describes" in flat
    assert "`details` must never contain a patron identifier, a barcode, a title, a token" in flat
    assert "stores counts, internal row ids, status names and a schema revision" not in flat  # the old, incomplete claim


def test_the_document_says_the_purge_enforces_table_ownership(document):
    flat = _flat(document)

    assert "The owner requirement is enforced, not assumed." in flat
    assert "a role that is not the owner is refused before any count is printed" in flat
    assert "neither superuser nor BYPASSRLS is accepted in place of ownership" in flat


def test_the_document_records_both_deployments_as_verified(document):
    flat = _flat(document)

    assert "verified on 2026-10-01 for both production deployments separately: the API backend and the Super Admin app" in flat
    for name in ("docs/data-lifecycle-offboarding.md", "scripts/runtime_role_privileges.py",
                 f"alembic/versions/{REVISION}_add_tenant_lifecycle_events.py"):
        text_ = (ROOT / name).read_text(encoding="utf-8").lower()
        for stale in ("provisional", "unverified", "not yet verified", "is not recorded in this repository"):
            assert stale not in text_, (name, stale)


def test_the_backup_and_admin_guides_point_at_the_lifecycle_document():
    for name in ("backup-restore.md", "collector-v1-admin-guide.md"):
        assert "docs/data-lifecycle-offboarding.md" in (ROOT / "docs" / name).read_text(encoding="utf-8"), name
