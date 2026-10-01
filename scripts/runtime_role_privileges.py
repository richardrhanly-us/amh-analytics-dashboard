"""The runtime database role's intended privileges, as data -- and two ways to use them.

WHY THIS EXISTS. The production application connects as `sortview_app`, a non-owning role. That was verified on 2026-10-01
for the API backend and, separately, for the Super Admin deployment (every app in this repository takes its role from
its own DATABASE_URL setting). Its grants were originally applied by hand from a script that was never committed, so the single most
important authorization fact about production -- what the application role can and cannot do -- lived only in the
production database. This file records that model so it can be reviewed, reproduced and CHECKED, instead of trusted.

OBSERVED, NOT ASSUMED. Every entry in BASELINE is the role's EFFECTIVE privilege as observed in the production inventory
of 2026-10-01 (schema revision 0d1dcae29e32), with one exception: tenant_lifecycle_events, which did not exist yet and is
granted by migration a7c4e19d5b02. The same inventory confirmed: no role membership; BYPASSRLS false; no DELETE and no
TRUNCATE on any relation; no column-level grants; schema public USAGE true and CREATE false; no default privileges for
future objects; and SELECT/UPDATE false on every sequence (USAGE only, on the insert-path sequences).

    verify              READ-ONLY. Compares the role's EFFECTIVE privileges in a database with BASELINE below and exits 1
                        on any difference -- a missing grant, an extra grant, a relation this file does not know.
    provisioning-sql    Prints the GRANT statements that give a role exactly this baseline in a FRESH environment. It
                        connects to nothing and executes nothing: the output is reviewed and applied by the table owner.

    DATABASE_URL=postgresql://... python scripts/runtime_role_privileges.py verify
    python scripts/runtime_role_privileges.py provisioning-sql > review-me.sql

RUN `verify` FROM A SORTVIEW OPERATOR MACHINE ONLY -- it opens a direct database connection, which a city-owned machine or
a Collector machine is never permitted to do. Any role that can connect can run it: has_*_privilege() answers for another
role without needing that role's password, and it reads system catalogs only, never a row of application data.

THIS FILE CHANGES NO PRIVILEGE ANYWHERE. Grants are deliberately NOT managed by an ordinary Alembic migration: a migration
that rewrote the role's grants on every environment would turn a reviewed security boundary into a side effect of
`alembic upgrade`. A migration grants only what its own new table needs (0d1dcae29e32, a7c4e19d5b02); the whole picture
is here.

THE RULE THE BASELINE ENCODES: the runtime role never holds DELETE or TRUNCATE on anything. Revocation is always an UPDATE
(is_active, revoked_at, status); removing a tenant's rows is done by the table owner (scripts/purge_tenant_data.py).

"effective" matters: verify asks has_table_privilege(), which includes anything granted to PUBLIC or inherited through a
role membership, not just rows naming the role in information_schema.role_table_grants.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import TextIO

from sqlalchemy import create_engine, text

EXIT_OK = 0
EXIT_DIFFERENT = 1
EXIT_REFUSED = 2

RUNTIME_ROLE = "sortview_app"

SEL, INS, UPD = "SELECT", "INSERT", "UPDATE"
CHECKED_PRIVILEGES = (SEL, INS, UPD, "DELETE", "TRUNCATE")
NEVER_GRANTED = ("DELETE", "TRUNCATE")

# Every table and view in schema public, and exactly what the runtime role may do with it. An empty set means NO access
# at all, which is as deliberate as a grant. All of it is the observed production baseline (see OBSERVED, NOT ASSUMED
# above); the trailing notes say what a grant is for or where it comes from, not how it was derived.
BASELINE: dict[str, frozenset[str]] = {
    # --- account structure -------------------------------------------------------------------------------------------
    "organizations": frozenset({SEL, INS, UPD}),
    "customers": frozenset({SEL, INS}),
    "branches": frozenset({SEL, INS, UPD}),
    "memberships": frozenset({SEL, INS, UPD}),
    "subscriptions": frozenset({SEL, INS}),
    "organization_settings": frozenset({SEL, INS, UPD}),
    "branch_settings": frozenset({SEL, INS, UPD}),
    "plans": frozenset({SEL}),
    "feature_entitlements": frozenset({SEL}),
    # --- users, sessions, security audit --------------------------------------------------------------------------------
    "app_users": frozenset({SEL, INS, UPD}),
    "auth_sessions": frozenset({SEL, INS, UPD}),              # migration 0d1dcae29e32
    "password_reset_tokens": frozenset({SEL, INS, UPD}),
    "auth_audit_log": frozenset({SEL, INS}),
    # --- collector access artifacts -----------------------------------------------------------------------------------
    "agent_tokens": frozenset({SEL, INS, UPD}),               # INSERT: enrollment redemption issues a token
    "collector_installations": frozenset({SEL, INS, UPD}),
    "collector_enrollment_codes": frozenset({SEL, INS, UPD}),
    # --- operational data: insert-only for the application, under row level security -----------------------------
    "checkins": frozenset({SEL, INS}),
    "rejects": frozenset({SEL, INS}),
    "acs_events": frozenset({SEL, INS}),
    "checkin_events": frozenset({SEL, INS}),
    "reject_events": frozenset({SEL, INS}),
    "acs_item_events": frozenset({SEL, INS}),
    "ingest_key_ids": frozenset({SEL, INS, UPD}),
    "pipeline_status": frozenset({SEL, INS, UPD}),            # the heartbeat upsert
    "v2_cutovers": frozenset({SEL}),                      # written only by an operator, as the owner
    # --- lifecycle evidence: the application may append and nothing else, not even read. Granted by migration
    # a7c4e19d5b02 (INSERT, plus USAGE on the id sequence); required because the Super Admin app, which records a
    # cutoff, connects as this role (verified 2026-10-01). It is the one entry not yet observable in production: the table does not exist
    # there until that migration is applied, so `verify` reports it as missing until then.
    "tenant_lifecycle_events": frozenset({INS}),
    # --- no access at all -----------------------------------------------------------------------------------------------
    "checkins_clean": frozenset(),                      # written by a SECURITY DEFINER trigger (67d06f4ccd24)
    "rejects_clean": frozenset(),
    "checkins_routed": frozenset(),                     # a view over checkins_clean
    "bin_routing_map": frozenset(),
    "alembic_version": frozenset(),
}

# Row level security must be ENABLED (not forced: the owner keeps bypassing it for migrations and the purge) on these.
RLS_TABLES = frozenset({
    "checkins", "rejects", "acs_events", "checkin_events", "reject_events", "acs_item_events", "ingest_key_ids",
})

# Tables whose primary key is not a `<table>_id_seq` serial, so an INSERT grant needs no sequence.
_NO_SEQUENCE = frozenset({"pipeline_status"})

# USAGE (for nextval) on the id sequence of every table the role may INSERT into; never SELECT or UPDATE on a sequence.
SEQUENCE_USAGE: frozenset[str] = frozenset(
    f"{table}_id_seq" for table, privileges in BASELINE.items() if INS in privileges and table not in _NO_SEQUENCE
)

ROLE_ATTRIBUTES = {
    "rolcanlogin": True,
    "rolinherit": True,
    "rolsuper": False,
    "rolcreatedb": False,
    "rolcreaterole": False,
    "rolreplication": False,
    "rolbypassrls": False,
}


# --- verify -----------------------------------------------------------------------------------------------------------

def differences(conn, role: str = RUNTIME_ROLE) -> list[str]:
    """Every way `role`'s effective privileges in this database differ from the baseline. Read-only; catalogs only."""
    found: list[str] = []
    bind = {"role": role}

    attributes = conn.execute(
        text(f"SELECT {', '.join(ROLE_ATTRIBUTES)} FROM pg_roles WHERE rolname = :role"), bind  # nosec B608 - fixed names
    ).mappings().first()
    if attributes is None:
        return [f"role {role!r} does not exist"]
    for attribute, expected in ROLE_ATTRIBUTES.items():
        if bool(attributes[attribute]) != expected:
            found.append(f"role attribute {attribute} is {attributes[attribute]}, expected {expected}")

    memberships = conn.execute(text("""
        SELECT parent.rolname
        FROM pg_auth_members m
        JOIN pg_roles parent ON parent.oid = m.roleid
        JOIN pg_roles member ON member.oid = m.member
        WHERE member.rolname = :role
        ORDER BY 1
    """), bind).scalars().all()
    if memberships:
        found.append(f"role is a member of {', '.join(memberships)}; expected no role membership")

    schema = conn.execute(text("""
        SELECT has_schema_privilege(:role, 'public', 'USAGE') AS usage,
               has_schema_privilege(:role, 'public', 'CREATE') AS create
    """), bind).mappings().one()
    if not schema["usage"]:
        found.append("schema public: USAGE is missing")
    if schema["create"]:
        found.append("schema public: CREATE is granted; expected none")

    relations = conn.execute(text("""
        SELECT c.relname,
               c.relrowsecurity AS rls,
               has_table_privilege(:role, c.oid, 'SELECT')   AS "SELECT",
               has_table_privilege(:role, c.oid, 'INSERT')   AS "INSERT",
               has_table_privilege(:role, c.oid, 'UPDATE')   AS "UPDATE",
               has_table_privilege(:role, c.oid, 'DELETE')   AS "DELETE",
               has_table_privilege(:role, c.oid, 'TRUNCATE') AS "TRUNCATE"
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v')
        ORDER BY c.relname
    """), bind).mappings().all()
    seen = set()
    for relation in relations:
        name = relation["relname"]
        seen.add(name)
        if name not in BASELINE:
            found.append(f"{name}: not in the baseline (a new relation must be classified before it ships)")
            continue
        actual = {privilege for privilege in CHECKED_PRIVILEGES if relation[privilege]}
        expected = BASELINE[name]
        for privilege in sorted(actual - expected):
            found.append(f"{name}: {privilege} is granted; the baseline does not allow it")
        for privilege in sorted(expected - actual):
            found.append(f"{name}: {privilege} is missing")
        if name in RLS_TABLES and not relation["rls"]:
            found.append(f"{name}: row level security is not enabled")
    for name in sorted(set(BASELINE) - seen):
        found.append(f"{name}: in the baseline but not in the database (is the schema at head?)")

    sequences = conn.execute(text("""
        SELECT c.relname,
               has_sequence_privilege(:role, c.oid, 'USAGE')  AS usage,
               has_sequence_privilege(:role, c.oid, 'SELECT') AS sel,
               has_sequence_privilege(:role, c.oid, 'UPDATE') AS upd
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'S'
        ORDER BY c.relname
    """), bind).mappings().all()
    for sequence in sequences:
        name, wanted = sequence["relname"], sequence["relname"] in SEQUENCE_USAGE
        if wanted and not sequence["usage"]:
            found.append(f"sequence {name}: USAGE is missing")
        if not wanted and sequence["usage"]:
            found.append(f"sequence {name}: USAGE is granted; the baseline does not allow it")
        if sequence["sel"] or sequence["upd"]:
            found.append(f"sequence {name}: SELECT/UPDATE is granted; expected USAGE at most")
    for name in sorted(SEQUENCE_USAGE - {sequence["relname"] for sequence in sequences}):
        found.append(f"sequence {name}: in the baseline but not in the database")

    column_grants = conn.execute(text("""
        SELECT c.relname || '.' || a.attname
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND a.attacl IS NOT NULL
        ORDER BY 1
    """)).scalars().all()
    if column_grants:
        found.append(f"column-level grants exist on {', '.join(column_grants)}; expected none")

    default_grants = conn.execute(text("""
        SELECT COUNT(*)
        FROM pg_default_acl d
        CROSS JOIN LATERAL aclexplode(d.defaclacl) AS acl
        JOIN pg_roles grantee ON grantee.oid = acl.grantee
        WHERE grantee.rolname = :role
    """), bind).scalar_one()
    if default_grants:
        found.append(f"{default_grants} default privilege(s) grant future objects to the role; expected none")

    return found


# --- provisioning SQL ---------------------------------------------------------------------------------------------------

def provisioning_sql(role: str = RUNTIME_ROLE) -> str:
    """The statements that give `role` exactly the baseline. For a FRESH environment, reviewed and run by the table
    owner AFTER `alembic upgrade head`. It creates no role and sets no password."""
    lines = [
        f"-- Runtime role privilege baseline for {role} (scripts/runtime_role_privileges.py).",
        "-- Review before running. Run as the table owner, after `alembic upgrade head`.",
        f"-- The role must already exist: CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE",
        "--   NOREPLICATION NOBYPASSRLS INHERIT PASSWORD '<set out of band>';",
        "BEGIN;",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role};",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role};",
        f"REVOKE CREATE ON SCHEMA public FROM {role};",
        f"GRANT USAGE ON SCHEMA public TO {role};",
    ]
    for relation in sorted(BASELINE):
        privileges = [privilege for privilege in (SEL, INS, UPD) if privilege in BASELINE[relation]]
        if privileges:
            lines.append(f"GRANT {', '.join(privileges)} ON TABLE public.{relation} TO {role};")
        else:
            lines.append(f"-- {relation}: no access")
    for sequence in sorted(SEQUENCE_USAGE):
        lines.append(f"GRANT USAGE ON SEQUENCE public.{sequence} TO {role};")
    lines.append("COMMIT;")
    lines.append("-- Then confirm: python scripts/runtime_role_privileges.py verify")
    return "\n".join(lines) + "\n"


# --- command line -------------------------------------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify, or print the provisioning SQL for, the runtime role's privileges.")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="read-only: compare a database's effective privileges with the baseline")
    verify.add_argument("--database-url", default=None, help="Defaults to the DATABASE_URL environment variable.")
    commands.add_parser("provisioning-sql", help="print the GRANT statements for a fresh environment (executes nothing)")
    return parser


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)

    if args.command == "provisioning-sql":
        print(provisioning_sql(), file=out, end="")
        return EXIT_OK

    url = args.database_url or os.environ.get("DATABASE_URL")
    if not url:
        print("REFUSED: needs a database URL (--database-url, or the DATABASE_URL environment variable).", file=out)
        return EXIT_REFUSED

    engine = create_engine(url, hide_parameters=True, future=True)
    try:
        with engine.connect() as conn:
            if conn.dialect.name != "postgresql":
                print("REFUSED: privilege verification runs against PostgreSQL only.", file=out)
                return EXIT_REFUSED
            conn.execute(text("SET TRANSACTION READ ONLY"))
            found = differences(conn)
            conn.rollback()
    finally:
        engine.dispose()

    if found:
        print(f"DIFFERENT: {len(found)} difference(s) between {RUNTIME_ROLE}'s effective privileges and the baseline:", file=out)
        for difference in found:
            print(f"  {difference}", file=out)
        return EXIT_DIFFERENT
    print(f"OK: {RUNTIME_ROLE}'s effective privileges match the baseline "
          f"({len(BASELINE)} relations, {len(SEQUENCE_USAGE)} sequences; no DELETE or TRUNCATE anywhere).", file=out)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
