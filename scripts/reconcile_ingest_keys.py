"""C6a M3a: a READ-ONLY inventory of Contract v2 ingest keys and what is known about who owns them
(docs/c6a-replacement-machine-design.md). It assigns no owner, makes no reconciliation decision and changes nothing.

    python scripts/reconcile_ingest_keys.py inventory --database-url postgresql://...
    DATABASE_URL=postgresql://... python scripts/reconcile_ingest_keys.py inventory --format json

Never point it at production without an explicit go-ahead: it writes nothing, but it reads every tenant's key registry.

HOW IT STAYS READ-ONLY. One PostgreSQL transaction, READ ONLY and REPEATABLE READ, confirmed with SHOW before anything is read
and rolled back afterwards. Every statement is a SELECT, a SHOW or a transaction-local SET. The report describes one snapshot.

HOW IT FAILS CLOSED. Row level security filters silently, so a restricted role would otherwise see an empty registry and report
a clean one. row_security = off makes PostgreSQL raise an error instead wherever a policy would have applied, and the role must
be able to read every table in full. Any refusal or error produces a fixed code and NO report: the report is built whole and
printed last, so there is never a partial one. Exit 0 is a complete report; exit 2 is none.

WHAT IT NEVER READS: an agent token's hash or description (a description can carry a hostname), an enrollment code's hash, an
installation's name, hostname or Collector version, or any event's keys or content. A cutover's free-text set_by and note ARE read,
only to test them; neither leaves this file. Exception text is never printed, because it can hold a URL or a value.

E1, THE ENROLLMENT RECORD. A redemption writes the key, its cutover row and its code's used_at in ONE transaction, so all three
carry the same instant, and the note names the code and the installation. A key is an E1 `candidate` only if every part agrees:

    the cutover row   set_by 'collector-enrollment', the exact note format, set_at equal to the key's created_at
    the code          exists, names the note's installation, used_at equal to the key's created_at, contract 2, not revoked
    the installation  exists, at the key's own branch
    a token           bound to that installation, in the key's own scope (active or not: recovery replaces tokens)

A CANDIDATE IS NOT PROOF, which is why `conclusive` is always false. set_v2_cutover.py accepts any --set-by and --note, and the
owner of a database can write any row, so equal timestamps and a well-formed note show what to review, not who owns the key.
Anything less is reported as what it is:

    none         the branch has no enrollment record, and nothing at this key's instant
    incomplete   part of the record is missing, or a timestamp matches with no enrollment record behind it
    conflicting  the parts of the record disagree
    ambiguous    the record, or the heartbeat, points at more than one key or installation

A key's `last_heartbeat_at` equal to an installation's `last_seen_at` means one transaction wrote both, so a token bound to that
installation last reported this key. It is reported as corroboration, and only ever downgrades a candidate.

SCHEMA DIFFERENCES are reported, not refused: names on ingest_key_ids are compared with what the migrations create, and each
definition is recorded so two reports can be compared. A missing table or column the inventory reads is a refusal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, TextIO

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError, OperationalError
from sqlalchemy.pool import NullPool

EXIT_OK = 0
EXIT_REFUSED = 2

REPORT_NAME = "c6a_ingest_key_inventory"
REPORT_VERSION = 1

# Copied, as issue_ingest_key.py's migration patterns are: this tool imports no application code. A real redemption in
# tests/test_reconcile_ingest_keys_postgres.py fails if collector_enrollment_service ever stops writing exactly this.
ENROLLMENT_SET_BY = "collector-enrollment"
_ENROLLMENT_NOTE = re.compile(r"initial v2 cutover \| enrollment_code_id=([0-9]{1,18}) installation_id=([0-9]{1,18})")

_REFUSALS = {
    "database_url_required": "a database URL is required (--database-url, or the DATABASE_URL environment variable)",
    "database_url_invalid": "the database URL could not be read",
    "postgresql_required": "the inventory runs on PostgreSQL only",
    "connection_failed": "could not connect to the database",
    "read_only_unavailable": "a READ ONLY, REPEATABLE READ transaction with row_security off could not be established",
    "incomplete_visibility": "this role cannot read every row of a required table, so a complete report is impossible",
    "m1_schema_unavailable": "ingest_key_ids.installation_id is missing: migration c1e7a94f2b60 (C6a M1) has not been applied",
    "schema_unsupported": "a table or column the inventory reads is missing",
    "inventory_failed": "the inventory could not be completed",
}


class InventoryRefused(Exception):
    """A fixed code from _REFUSALS and, optionally, a table name or SQLSTATE. Never a value from the database or the URL."""

    def __init__(self, code: str, **detail: str):
        super().__init__(code)
        self.code = code
        self.detail = detail


# --- the transaction ----------------------------------------------------------------------------------------------------

_SNAPSHOT_SETTINGS = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",  # must come first
    "SET LOCAL row_security = off",
    "SET LOCAL search_path = pg_catalog, public",
    "SET LOCAL statement_timeout = '120s'",
    "SET LOCAL lock_timeout = '5s'",  # never queue behind a migration, with every agent request queued behind this
)
_SNAPSHOT_EXPECTED = {"transaction_isolation": "repeatable read", "transaction_read_only": "on", "row_security": "off"}


def open_snapshot(conn) -> None:
    """Makes the connection's transaction read-only and confirms it, before anything is read."""
    for statement in _SNAPSHOT_SETTINGS:
        conn.execute(text(statement))
    for setting, expected in _SNAPSHOT_EXPECTED.items():
        if conn.execute(text(f"SHOW {setting}")).scalar() != expected:
            raise InventoryRefused("read_only_unavailable", setting=setting)


# The columns read below, and nothing else, so a schema that lacks one is refused in one place.
_REQUIRED_COLUMNS = {
    "ingest_key_ids": ("id", "key_id", "customer_id", "branch_id", "status", "created_at", "retired_at", "last_heartbeat_at",
                       "health_status", "installation_id"),
    "agent_tokens": ("id", "customer_id", "branch_id", "is_active", "installation_id", "created_at", "last_used_at"),
    "collector_installations": ("id", "organization_id", "branch_id", "status", "required_contract", "last_seen_at"),
    "collector_enrollment_codes": ("id", "installation_id", "used_at", "revoked_at", "required_contract"),
    "v2_cutovers": ("id", "customer_id", "branch_id", "cutover_at", "set_by", "set_at", "note"),
    "organizations": ("id", "slug", "status", "operational_customer_id"),
    "branches": ("id", "organization_id", "status", "operational_branch_id"),
    "checkin_events": ("customer_id", "branch_id", "key_id", "received_at"),
    "reject_events": ("customer_id", "branch_id", "key_id", "received_at"),
    "acs_item_events": ("customer_id", "branch_id", "key_id", "received_at"),
}
_EVENT_TABLES = ("checkin_events", "reject_events", "acs_item_events")

_COLUMNS_SQL = """
    SELECT c.relname AS table_name, a.attname AS column_name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    WHERE n.nspname = 'public' AND c.relname = ANY(:tables)
"""


def require_schema_and_visibility(conn) -> None:
    """Refuses unless every table and column read below exists and this role sees every row of each table."""
    present = {(row["table_name"], row["column_name"]) for row in _rows(conn, _COLUMNS_SQL, tables=list(_REQUIRED_COLUMNS))}
    if ("ingest_key_ids", "installation_id") not in present:
        raise InventoryRefused("m1_schema_unavailable")
    for table, columns in _REQUIRED_COLUMNS.items():
        for column in columns:
            if (table, column) not in present:
                raise InventoryRefused("schema_unsupported", table=table, column=column)
    for table in _REQUIRED_COLUMNS:
        try:
            # Under row_security = off a policy that would apply raises 42501 at rewrite, whatever the LIMIT.
            conn.execute(text(f"SELECT * FROM public.{table} LIMIT 0"))  # nosec B608 - a constant above
        except DBAPIError as exc:
            if _sqlstate(exc) == "42501":
                raise InventoryRefused("incomplete_visibility", table=table) from None
            raise


# --- what is read -------------------------------------------------------------------------------------------------------

_KEYS_SQL = """
    SELECT id, key_id, customer_id, branch_id, status, created_at, retired_at, last_heartbeat_at, health_status, installation_id
    FROM ingest_key_ids
    ORDER BY id
"""
_TOKENS_SQL = """
    SELECT id, customer_id, branch_id, is_active, installation_id, created_at, last_used_at
    FROM agent_tokens
    ORDER BY id
"""
_INSTALLATIONS_SQL = """
    SELECT id, organization_id, branch_id, status, required_contract, last_seen_at
    FROM collector_installations
    ORDER BY id
"""
_CODES_SQL = """
    SELECT id, installation_id, used_at, revoked_at, required_contract
    FROM collector_enrollment_codes
    ORDER BY id
"""
_CUTOVERS_SQL = """
    SELECT id, customer_id, branch_id, cutover_at, set_by, set_at, note
    FROM v2_cutovers
    ORDER BY set_at, id
"""
_ORGANIZATIONS_SQL = "SELECT id, slug, status, operational_customer_id FROM organizations ORDER BY id"
_BRANCHES_SQL = "SELECT id, organization_id, status, operational_branch_id FROM branches ORDER BY id"
_EVENTS_SQL = """
    SELECT customer_id, branch_id, key_id, COUNT(*) AS events, MAX(received_at) AS last_received_at
    FROM {table}
    GROUP BY customer_id, branch_id, key_id
"""
_DATABASE_SQL = """
    SELECT now() AS generated_at, current_database() AS name, current_user AS role,
           current_setting('server_version') AS server_version, r.rolsuper AS superuser, r.rolbypassrls AS bypass_rls
    FROM pg_roles r
    WHERE r.rolname = current_user
"""
_ALEMBIC_READABLE_SQL = """
    SELECT to_regclass('public.alembic_version') IS NOT NULL
       AND has_table_privilege(current_user, 'public.alembic_version', 'SELECT')
"""

_SCHEMA_TABLES = ("ingest_key_ids", "agent_tokens", "collector_installations", "collector_enrollment_codes", "v2_cutovers",
                  *_EVENT_TABLES)
_INDEXES_SQL = """
    SELECT tablename AS table_name, indexname AS name, indexdef AS definition
    FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = ANY(:tables)
    ORDER BY tablename, indexname
"""
_CONSTRAINTS_SQL = """
    SELECT c.relname AS table_name, con.conname AS name, con.contype::text AS type, pg_get_constraintdef(con.oid) AS definition
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = ANY(:tables)
    ORDER BY c.relname, con.conname
"""
_TRIGGERS_SQL = """
    SELECT c.relname AS table_name, t.tgname AS name, t.tgenabled::text AS enabled, pg_get_triggerdef(t.oid) AS definition
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT t.tgisinternal AND n.nspname = 'public' AND c.relname = ANY(:tables)
    ORDER BY c.relname, t.tgname
"""
_POLICIES_SQL = """
    SELECT tablename AS table_name, policyname AS name, cmd AS command, permissive, roles::text[] AS roles,
           qual AS using_expression, with_check AS check_expression
    FROM pg_policies
    WHERE schemaname = 'public' AND tablename = ANY(:tables)
    ORDER BY tablename, policyname
"""
_RLS_SQL = """
    SELECT c.relname AS table_name, c.relrowsecurity AS enabled, c.relforcerowsecurity AS forced
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = ANY(:tables)
    ORDER BY c.relname
"""


def _rows(conn, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def read_snapshot(conn) -> dict[str, Any]:
    """Everything the report is built from. Separate from building it, so the classification is plain data in, data out."""
    snapshot: dict[str, Any] = {
        "keys": _rows(conn, _KEYS_SQL),
        "tokens": _rows(conn, _TOKENS_SQL),
        "installations": _rows(conn, _INSTALLATIONS_SQL),
        "codes": _rows(conn, _CODES_SQL),
        "cutovers": _rows(conn, _CUTOVERS_SQL),
        "organizations": _rows(conn, _ORGANIZATIONS_SQL),
        "branches": _rows(conn, _BRANCHES_SQL),
        "events": [row for table in _EVENT_TABLES for row in _rows(conn, _EVENTS_SQL.format(table=table))],
    }
    database = _rows(conn, _DATABASE_SQL)[0]
    alembic = (_rows(conn, "SELECT version_num FROM alembic_version ORDER BY version_num")
               if conn.execute(text(_ALEMBIC_READABLE_SQL)).scalar() else None)
    database["alembic_revisions"] = None if alembic is None else [row["version_num"] for row in alembic]
    snapshot["database"] = database
    tables = list(_SCHEMA_TABLES)
    snapshot["schema"] = {
        "indexes": _rows(conn, _INDEXES_SQL, tables=tables),
        "constraints": _rows(conn, _CONSTRAINTS_SQL, tables=tables),
        "triggers": _rows(conn, _TRIGGERS_SQL, tables=tables),
        "policies": _rows(conn, _POLICIES_SQL, tables=tables),
        "row_level_security": _rows(conn, _RLS_SQL, tables=tables),
    }
    return snapshot


# --- schema differences ---------------------------------------------------------------------------------------------------

# What the migrations leave on the key registry. Other tables are recorded in the report without being compared here.
_EXPECTED_ON_KEYS = {
    "indexes": {"ingest_key_ids_pkey", "ingest_key_ids_key_id_uidx", "ingest_key_ids_scope_idx"},
    "constraints": {
        "ingest_key_ids_pkey", "ingest_key_ids_customer_id_fkey", "ingest_key_ids_branch_id_fkey",
        "ingest_key_ids_key_id_format_chk", "ingest_key_ids_algorithm_chk", "ingest_key_ids_status_chk",
        "ingest_key_ids_lifecycle_chk", "ingest_key_ids_health_status_chk", "ingest_key_ids_last_error_class_chk",
        "ingest_key_ids_pending_outbox_count_chk", "ingest_key_ids_quarantined_count_chk",
        "ingest_key_ids_collector_run_duration_ms_chk", "ingest_key_ids_collector_schedule_status_chk",
        "fk_ingest_key_ids_installation_branch",
    },
    "triggers": set(),
    "policies": {"tenant_isolation_select", "tenant_isolation_insert", "tenant_isolation_update"},
}
_EXPECTED_ON_EVENTS = {"tenant_isolation_select", "tenant_isolation_insert"}
_RLS_TABLES = ("ingest_key_ids", *_EVENT_TABLES)


def schema_differences(schema: dict[str, list[dict[str, Any]]], database: dict[str, Any]) -> list[str]:
    differences: list[str] = []

    def compare(table: str, kind: str, expected: set[str]) -> None:
        # PostgreSQL 18 records NOT NULL as a named constraint and earlier versions do not: recorded, never compared.
        found = {row["name"] for row in schema[kind] if row["table_name"] == table and row.get("type") != "n"}
        differences.extend(f"{table}: {kind} missing: {name}" for name in sorted(expected - found))
        differences.extend(f"{table}: {kind} unexpected: {name}" for name in sorted(found - expected))

    for kind, expected in _EXPECTED_ON_KEYS.items():
        compare("ingest_key_ids", kind, expected)
    for table in _EVENT_TABLES:
        compare(table, "policies", _EXPECTED_ON_EVENTS)

    rls = {row["table_name"]: row for row in schema["row_level_security"]}
    for table in _RLS_TABLES:
        if not rls.get(table, {}).get("enabled"):
            differences.append(f"{table}: row level security is not enabled")
        if rls.get(table, {}).get("forced"):
            differences.append(f"{table}: row level security is forced")
    differences.extend(
        f"{row['table_name']}: trigger disabled: {row['name']}" for row in schema["triggers"] if row["enabled"] == "D"
    )
    if not any(row["name"] == "uq_collector_installations_id_branch_id" for row in schema["constraints"]):
        differences.append("collector_installations: constraints missing: uq_collector_installations_id_branch_id")
    if database.get("alembic_revisions") is None:
        differences.append("alembic_version: not readable by this role")
    return differences


# --- E1, per key ------------------------------------------------------------------------------------------------------------

def _enrollment_note(cutover: dict[str, Any]) -> tuple[int, int] | None:
    """(code id, installation id) if this row has the exact form a redemption writes, else None."""
    if cutover["set_by"] != ENROLLMENT_SET_BY:
        return None
    match = _ENROLLMENT_NOTE.fullmatch(cutover["note"] or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def _heartbeat_installations(key: dict[str, Any], installations: list[dict[str, Any]]) -> list[int]:
    heartbeat = key["last_heartbeat_at"]
    if heartbeat is None:
        return []
    return sorted(int(row["id"]) for row in installations if row["last_seen_at"] == heartbeat)


def _e1(key: dict[str, Any], *, cutovers: list[dict[str, Any]], codes: dict[int, dict[str, Any]],
        installations: dict[int, dict[str, Any]], branch: dict[str, Any] | None,
        bound_token_installations: set[int], heartbeat_installations: list[int]) -> dict[str, Any]:
    """The E1 evidence for one key. Reasons are fixed names; a set_by or a note never leaves this function."""
    result: dict[str, Any] = {"status": "none", "conclusive": False, "candidate_installation_id": None, "cutover_id": None,
                              "enrollment_code_id": None, "reasons": []}
    instant = key["created_at"]
    recorded = [(cutover, _enrollment_note(cutover)) for cutover in cutovers]
    enrollment_rows = [(cutover, note) for cutover, note in recorded if note is not None]
    matching = [(cutover, note) for cutover, note in enrollment_rows if cutover["set_at"] == instant]

    if not matching:
        if any(cutover["set_at"] == instant for cutover, _note in recorded):
            result.update(status="incomplete", reasons=["cutover_instant_matches_without_enrollment_record"])
        elif enrollment_rows:
            result.update(status="incomplete", reasons=["enrollment_cutover_instant_differs"])
        else:
            result["reasons"] = ["no_enrollment_cutover"]
        return result
    if len(matching) > 1:
        result.update(status="ambiguous", reasons=["several_enrollment_cutovers_at_this_instant"])
        return result

    cutover, (code_id, installation_id) = matching[0]
    result.update(cutover_id=int(cutover["id"]), enrollment_code_id=code_id)
    conflicts: list[str] = []
    missing: list[str] = []

    code = codes.get(code_id)
    if code is None:
        missing.append("enrollment_code_missing")
    else:
        if int(code["installation_id"]) != installation_id:
            conflicts.append("code_names_another_installation")
        if code["used_at"] is None:
            missing.append("code_not_marked_used")
        elif code["used_at"] != instant:
            conflicts.append("code_used_at_differs")
        if int(code["required_contract"]) != 2:
            conflicts.append("code_is_not_contract_2")
        if code["revoked_at"] is not None:
            conflicts.append("code_revoked")

    installation = installations.get(installation_id)
    if installation is None:
        missing.append("installation_missing")
    elif branch is None or int(installation["branch_id"]) != int(branch["id"]):
        conflicts.append("installation_at_another_branch")
    if installation_id not in bound_token_installations:
        missing.append("no_token_bound_to_installation")

    if conflicts or missing:
        result.update(status="conflicting" if conflicts else "incomplete", reasons=conflicts + missing)
        return result

    result.update(status="candidate", candidate_installation_id=installation_id, reasons=["all_e1_parts_agree"])
    ambiguous: list[str] = []
    if len(enrollment_rows) > 1:
        ambiguous.append("several_enrollment_cutovers_at_this_branch")  # a redemption refuses a branch with any cutover
    if heartbeat_installations and heartbeat_installations != [installation_id]:
        ambiguous.append("heartbeat_coincides_with_another_installation")
    if ambiguous:
        result.update(status="ambiguous", candidate_installation_id=None, reasons=ambiguous)
    return result


# --- the report ---------------------------------------------------------------------------------------------------------------

def _scope(row: dict[str, Any]) -> tuple[int, int]:
    return int(row["customer_id"]), int(row["branch_id"])


def build_report(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The whole report, field by field. It holds ids, counts, statuses, timestamps and fixed reason names."""
    organizations = {int(row["id"]): row for row in snapshot["organizations"]}
    installations = {int(row["id"]): row for row in snapshot["installations"]}
    codes = {int(row["id"]): row for row in snapshot["codes"]}

    # A scope is the operational pair every key, token and cutover carries. A mapped one comes from a branch and its organization.
    mapped: dict[tuple[int, int], dict[str, Any]] = {}
    unmapped_branches = 0
    for branch in snapshot["branches"]:
        customer_id = organizations.get(int(branch["organization_id"]), {}).get("operational_customer_id")
        if customer_id is None or branch["operational_branch_id"] is None:
            unmapped_branches += 1
            continue
        mapped[(int(customer_id), int(branch["operational_branch_id"]))] = branch

    by_scope: dict[str, dict[tuple[int, int], list[dict[str, Any]]]] = {
        name: defaultdict(list) for name in ("keys", "tokens", "cutovers")
    }
    for name, scoped in by_scope.items():
        for row in snapshot[name]:
            scoped[_scope(row)].append(row)
    installations_at: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot["installations"]:
        installations_at[int(row["branch_id"])].append(row)
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot["events"]:
        events[row["key_id"]].append(row)

    scopes = sorted(set(mapped) | {scope for scoped in by_scope.values() for scope in scoped})
    known_key_ids = {row["key_id"] for row in snapshot["keys"]}
    key_reports: list[dict[str, Any]] = []
    branch_reports: list[dict[str, Any]] = []

    for scope in scopes:
        branch = mapped.get(scope)
        organization = organizations.get(int(branch["organization_id"])) if branch else None
        keys, tokens, cutovers = (by_scope[name].get(scope, []) for name in ("keys", "tokens", "cutovers"))
        here = installations_at.get(int(branch["id"]), []) if branch else []
        live = [row for row in here if row["status"] != "retired"]
        active_keys = [row for row in keys if row["status"] == "active"]
        active_tokens = [row for row in tokens if row["is_active"]]
        active_unbound = [row for row in active_tokens if row["installation_id"] is None]
        here_ids = {int(row["id"]) for row in here}
        active_bound_ids = sorted({int(row["installation_id"]) for row in active_tokens if row["installation_id"] is not None})
        bound_token_installations = {int(row["installation_id"]) for row in tokens if row["installation_id"] is not None}
        enrollment_cutovers = [row for row in cutovers if _enrollment_note(row) is not None]

        key_state = ("multiple_active" if len(active_keys) > 1 else "one_active" if active_keys
                     else "retired_only" if keys else "none_issued")
        flags = {
            "multiple_active_keys": len(active_keys) > 1,
            "no_active_key_after_retirement": key_state == "retired_only",
            "active_unbound_tokens": bool(active_unbound),
            "active_tokens_bound_to_several_installations": len(active_bound_ids) > 1,
            "bound_token_installation_outside_this_branch": bool(bound_token_installations - here_ids),
            "several_live_installations": len(live) > 1,
            "several_cutover_rows": len(cutovers) > 1,
            "several_enrollment_cutovers": len(enrollment_cutovers) > 1,
            "latest_cutover_is_a_rollback": bool(cutovers) and cutovers[-1]["cutover_at"] is None,
            "scope_not_mapped_to_a_branch": branch is None,
            "organization_cancelled": bool(organization) and organization["status"] == "cancelled",
        }
        branch_reports.append({
            "customer_id": scope[0],
            "branch_id": scope[1],
            "mapped": branch is not None,
            "organization_id": int(organization["id"]) if organization else None,
            "organization_slug": organization["slug"] if organization else None,
            "organization_status": organization["status"] if organization else None,
            "branch_status": branch["status"] if branch else None,
            "key_state": key_state,
            "active_key_ids": [row["key_id"] for row in active_keys],
            "retired_key_count": len(keys) - len(active_keys),
            "installations": [{
                "id": int(row["id"]), "status": row["status"], "required_contract": int(row["required_contract"]),
                "last_seen_at": row["last_seen_at"],
            } for row in here],
            "tokens": [{
                "id": int(row["id"]),
                "binding": "unbound" if row["installation_id"] is None else "bound",
                "installation_id": None if row["installation_id"] is None else int(row["installation_id"]),
                "is_active": bool(row["is_active"]),
                "created_at": row["created_at"],  # agent_tokens timestamps have no zone: as stored
                "last_used_at": row["last_used_at"],
            } for row in tokens],
            "cutovers": [{
                "id": int(row["id"]), "cutover_at": row["cutover_at"], "set_at": row["set_at"],
                "is_rollback": row["cutover_at"] is None, "enrollment_form": _enrollment_note(row) is not None,
            } for row in cutovers],
            "flags": sorted(name for name, raised in flags.items() if raised),
        })

        for key in keys:
            heartbeat_installations = _heartbeat_installations(key, here)
            e1 = _e1(key, cutovers=cutovers, codes=codes, installations=installations, branch=branch,
                     bound_token_installations=bound_token_installations, heartbeat_installations=heartbeat_installations)
            key_reports.append({
                "row_id": int(key["id"]),
                "key_id": key["key_id"],  # not a secret: the registry holds no key material
                "customer_id": scope[0],
                "branch_id": scope[1],
                "organization_slug": organization["slug"] if organization else None,
                "status": key["status"],
                "created_at": key["created_at"],
                "retired_at": key["retired_at"],
                "owner_state": "unknown" if key["installation_id"] is None else "recorded",
                "owner_installation_id": None if key["installation_id"] is None else int(key["installation_id"]),
                "last_heartbeat_at": key["last_heartbeat_at"],
                "health_status": key["health_status"],
                "heartbeat_coincides_with_installation_ids": heartbeat_installations,
                "events": _key_events(key, scope, events.get(key["key_id"], [])),
                "e1": e1,
                "flags": _key_flags(key, e1, installations, key_state=key_state, branch=branch, organization=organization,
                                    active_unbound=bool(active_unbound), active_bound_ids=active_bound_ids, live=live),
            })

    key_reports.sort(key=lambda row: row["row_id"])
    _mark_shared_evidence(key_reports)
    for report in key_reports:
        _add_owner_conflict(report)

    schema = snapshot["schema"]
    differences = schema_differences(schema, snapshot["database"])
    database = snapshot["database"]
    return {
        "report": REPORT_NAME,
        "report_version": REPORT_VERSION,
        "complete": True,
        "changes_made": 0,
        "notice": "An inventory only. No owner was assigned and nothing was decided. An E1 candidate is not proof of ownership.",
        "generated_at": database["generated_at"],
        "database": {
            "name": database["name"], "role": database["role"], "server_version": database["server_version"],
            "superuser": bool(database["superuser"]), "bypass_rls": bool(database["bypass_rls"]),
            "alembic_revisions": database["alembic_revisions"],
            "transaction": dict(_SNAPSHOT_EXPECTED),
        },
        "summary": _summary(key_reports, branch_reports, differences, unmapped_branches=unmapped_branches,
                            orphan_event_key_ids=len(set(events) - known_key_ids)),
        "branches": branch_reports,
        "keys": key_reports,
        "schema": {**schema, "differences": differences},
    }


def _key_events(key: dict[str, Any], scope: tuple[int, int], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and the latest arrival only."""
    own = [row for row in rows if _scope(row) == scope]
    arrivals = [row["last_received_at"] for row in own if row["last_received_at"] is not None]
    return {
        "count": sum(int(row["events"]) for row in own),
        "last_received_at": max(arrivals) if arrivals else None,
        "count_under_another_scope": sum(int(row["events"]) for row in rows if _scope(row) != scope),
    }


def _key_flags(key: dict[str, Any], e1: dict[str, Any], installations: dict[int, dict[str, Any]], *, key_state: str,
               branch: dict[str, Any] | None, organization: dict[str, Any] | None, active_unbound: bool,
               active_bound_ids: list[int], live: list[dict[str, Any]]) -> list[str]:
    active = key["status"] == "active"
    owner = None if key["installation_id"] is None else int(key["installation_id"])
    candidate = e1["candidate_installation_id"]
    reference = owner if owner is not None else candidate
    subject = None if reference is None else installations.get(reference)
    flags = {
        "scope_not_mapped_to_a_branch": branch is None,
        "organization_cancelled": bool(organization) and organization["status"] == "cancelled",
        "several_active_keys_at_branch": active and key_state == "multiple_active",
        "active_unbound_tokens_at_branch": active and active_unbound,
        "several_live_installations_at_branch": active and owner is None and len(live) > 1,
        "active_tokens_bound_to_another_installation": active and reference is not None
        and any(installation != reference for installation in active_bound_ids),
        "owner_installation_not_usable": active and owner is not None and (
            subject is None or subject["status"] not in ("provisioning", "active")),
        "candidate_installation_not_usable": active and owner is None and subject is not None
        and subject["status"] not in ("provisioning", "active"),
        "candidate_installation_not_contract_2": owner is None and subject is not None and int(subject["required_contract"]) != 2,
    }
    return sorted(name for name, raised in flags.items() if raised)


def _mark_shared_evidence(keys: list[dict[str, Any]]) -> None:
    """One redemption issues one key, so a record that fits two keys identifies neither."""
    seen = Counter(row["e1"]["cutover_id"] for row in keys if row["e1"]["cutover_id"] is not None)
    for row in keys:
        e1 = row["e1"]
        if e1["cutover_id"] is not None and seen[e1["cutover_id"]] > 1:
            e1.update(status="ambiguous", candidate_installation_id=None, reasons=["record_fits_several_keys"])


def _add_owner_conflict(report: dict[str, Any]) -> None:
    candidate, owner = report["e1"]["candidate_installation_id"], report["owner_installation_id"]
    if candidate is not None and owner is not None and candidate != owner:
        report["flags"] = sorted([*report["flags"], "owner_differs_from_e1_candidate"])


def _summary(keys: list[dict[str, Any]], branches: list[dict[str, Any]], differences: list[str], *, unmapped_branches: int,
             orphan_event_key_ids: int) -> dict[str, Any]:
    tokens = [token for branch in branches for token in branch["tokens"]]
    states = Counter(branch["key_state"] for branch in branches)
    unknown = [row for row in keys if row["owner_state"] == "unknown"]
    return {
        "keys_total": len(keys),
        "keys_active": sum(row["status"] == "active" for row in keys),
        "keys_retired": sum(row["status"] == "retired" for row in keys),
        "keys_owner_recorded": len(keys) - len(unknown),
        "keys_owner_unknown_active": sum(row["status"] == "active" for row in unknown),
        "keys_owner_unknown_retired": sum(row["status"] == "retired" for row in unknown),
        "e1_of_keys_with_unknown_owner": {
            status: sum(row["e1"]["status"] == status for row in unknown)
            for status in ("candidate", "ambiguous", "conflicting", "incomplete", "none")
        },
        "branches_total": len(branches),
        "branches_multiple_active_keys": states["multiple_active"],
        "branches_one_active_key": states["one_active"],
        "branches_no_active_key_after_retirement": states["retired_only"],
        "branches_no_key_ever_issued": states["none_issued"],
        "branches_not_mapped_to_a_scope": unmapped_branches,
        "scopes_not_mapped_to_a_branch": sum(not branch["mapped"] for branch in branches),
        "tokens_active_unbound": sum(token["is_active"] and token["binding"] == "unbound" for token in tokens),
        "tokens_active_bound": sum(token["is_active"] and token["binding"] == "bound" for token in tokens),
        "event_key_ids_not_in_registry": orphan_event_key_ids,
        "schema_differences": len(differences),
    }


# --- output ---------------------------------------------------------------------------------------------------------------------

def _json_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)  # an unexpected type is a failure, never a str() of something unknown


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, default=_json_value)


def _when(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else "-"


def render_text(report: dict[str, Any]) -> str:
    database, summary = report["database"], report["summary"]
    lines = [
        "C6a INGEST KEY INVENTORY (read-only; nothing was changed)",
        report["notice"],
        "",
        (f"database {database['name']} | role {database['role']} | PostgreSQL {database['server_version']} | "
         f"alembic {','.join(database['alembic_revisions'] or ['unreadable'])}"),
        f"snapshot {_when(report['generated_at'])} | repeatable read, read only, row_security off",
        "",
        "SUMMARY",
    ]
    for name, value in summary.items():
        if isinstance(value, dict):
            lines.append(f"  {name}: " + ", ".join(f"{inner}={count}" for inner, count in value.items()))
        else:
            lines.append(f"  {name}: {value}")

    lines += ["", "KEYS"]
    if not report["keys"]:
        lines.append("  (none)")
    for key in report["keys"]:
        owner = "unknown" if key["owner_installation_id"] is None else f"installation {key['owner_installation_id']}"
        lines.append(f"  {key['key_id']} | {key['status']} | customer {key['customer_id']} branch {key['branch_id']} | owner {owner}")
        candidate = key["e1"]["candidate_installation_id"]
        lines.append(f"    E1 {key['e1']['status']}"
                     + (f" (installation {candidate}, requires review)" if candidate is not None else "")
                     + f": {', '.join(key['e1']['reasons']) or '-'}")
        lines.append(f"    created {_when(key['created_at'])} | heartbeat {_when(key['last_heartbeat_at'])} | "
                     f"events {key['events']['count']}, latest {_when(key['events']['last_received_at'])}")
        if key["flags"]:
            lines.append(f"    flags: {', '.join(key['flags'])}")

    lines += ["", "BRANCHES WITH FLAGS"]
    flagged = [branch for branch in report["branches"] if branch["flags"]]
    if not flagged:
        lines.append("  (none)")
    for branch in flagged:
        tokens = branch["tokens"]
        lines.append(f"  customer {branch['customer_id']} branch {branch['branch_id']} ({branch['organization_slug'] or 'unmapped'}) | "
                     f"keys: {branch['key_state']} | installations: {len(branch['installations'])} | active tokens: "
                     f"{sum(t['is_active'] and t['binding'] == 'bound' for t in tokens)} bound, "
                     f"{sum(t['is_active'] and t['binding'] == 'unbound' for t in tokens)} unbound")
        lines.append(f"    flags: {', '.join(branch['flags'])}")

    schema = report["schema"]
    lines += ["", (f"SCHEMA: {len(schema['indexes'])} indexes, {len(schema['constraints'])} constraints, "
                   f"{len(schema['triggers'])} triggers, {len(schema['policies'])} policies recorded "
                   "(--format json has each definition)")]
    lines += [f"  difference: {difference}" for difference in schema["differences"]] or ["  no differences on the compared objects"]
    return "\n".join(lines)


def _refusal_document(refusal: InventoryRefused) -> dict[str, Any]:
    return {"report": REPORT_NAME, "report_version": REPORT_VERSION, "complete": False, "changes_made": 0,
            "error": {"code": refusal.code, "message": _REFUSALS[refusal.code], **refusal.detail}}


def _render_refusal(refusal: InventoryRefused, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(_refusal_document(refusal), indent=2, sort_keys=True)
    detail = "".join(f" [{name}: {value}]" for name, value in sorted(refusal.detail.items()))
    return f"REFUSED: {refusal.code}: {_REFUSALS[refusal.code]}.{detail} No report was produced and nothing was changed."


# --- the command ----------------------------------------------------------------------------------------------------------------

def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    state = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    return state if isinstance(state, str) and re.fullmatch("[0-9A-Z]{5}", state) else None


def run_inventory(database_url: str) -> dict[str, Any]:
    """The whole report, or InventoryRefused. The transaction is always rolled back."""
    try:
        url = make_url(database_url)
    except ArgumentError:
        raise InventoryRefused("database_url_invalid") from None
    if url.get_backend_name() != "postgresql":
        raise InventoryRefused("postgresql_required")

    engine = create_engine(url, hide_parameters=True, poolclass=NullPool,
                           connect_args={"connect_timeout": 10, "application_name": "sortview_key_inventory"})
    try:
        try:
            conn = engine.connect()
        except OperationalError:
            raise InventoryRefused("connection_failed") from None
        try:
            open_snapshot(conn)
            require_schema_and_visibility(conn)
            return build_report(read_snapshot(conn))
        except DBAPIError as exc:
            state = _sqlstate(exc)
            if state == "42501":
                raise InventoryRefused("incomplete_visibility") from None
            raise InventoryRefused("inventory_failed", sqlstate=state or "unknown") from None
        finally:
            conn.rollback()
            conn.close()
    finally:
        engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A read-only inventory of Contract v2 ingest keys. It changes nothing.")
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory", help="report keys, owners, tokens, installations, E1 evidence and schema")
    inventory.add_argument("--database-url", default=None, help="Defaults to the DATABASE_URL environment variable.")
    inventory.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    try:
        database_url = args.database_url or os.environ.get("DATABASE_URL")
        if not database_url:
            raise InventoryRefused("database_url_required")
        report = run_inventory(database_url)
        rendered = render_json(report) if args.format == "json" else render_text(report)
    except InventoryRefused as refusal:
        print(_render_refusal(refusal, args.format), file=out)
        return EXIT_REFUSED
    except Exception as exc:  # fails closed, and exception text can hold a URL or a value: its class name only
        print(_render_refusal(InventoryRefused("inventory_failed", exception=type(exc).__name__), args.format), file=out)
        return EXIT_REFUSED
    print(rendered, file=out)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
