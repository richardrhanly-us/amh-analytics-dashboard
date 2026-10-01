"""Inventories, and with explicit confirmation PERMANENTLY DELETES, one offboarded tenant's rows from the live database
(docs/data-lifecycle-offboarding.md).

THIS IS THE DATA PURGE -- the third and last lifecycle step, and the only destructive one:

    suspension   reversible; organizations.status = 'suspended'            (Super Admin)
    cutoff       permanent; status = 'cancelled' and all access revoked    (Super Admin, offboard_library)
    purge        the tenant's rows are deleted                             (THIS TOOL, the table owner, by hand)

WHO MAY RUN IT, AND FROM WHERE.
  * An authorized SortView operator, connecting as the TABLE OWNER -- and the tool enforces it: before it looks at the
    tenant at all it requires the connected role to OWN every table it reads or writes. Any other role, whatever
    privileges it has been granted, sees those tables through row level security, which would hide the tenant's rows and
    make an inventory or a "purge complete" untrue. The tool refuses to run as the application's runtime role
    (sortview_app), which has -- and must keep -- no DELETE privilege on any table.
  * From a SortView operator machine ONLY. This tool opens a direct database connection, which a city-owned machine or a
    library's Collector machine is never permitted to do. It is not part of the Collector, is not shipped in a Collector
    bundle, and must never be copied to or run on a customer's machine.

SAFE BY DEFAULT. Without --execute the tool only INVENTORIES: it runs inside a READ ONLY transaction, prints table names
and row counts, says whether a purge would be accepted, and writes nothing.

    DATABASE_URL=postgresql://... python scripts/purge_tenant_data.py --organization-id 7 --confirm-slug example-library

The destructive mode needs --execute AND the slug typed a second time AND a named operator:

    DATABASE_URL=postgresql://... python scripts/purge_tenant_data.py --organization-id 7 --confirm-slug example-library \\
        --execute --confirm-purge example-library --operator "rhanly"

IT REFUSES (and writes nothing) unless ALL of these hold:
  * the database is PostgreSQL, at this repository's Alembic head, and the connected role is the OWNER of every table
    the tool uses (see ownership_tables); DELETE privilege, superuser or BYPASSRLS do not substitute for ownership.
    Those tables are the ones in schema public, and the tool operates on exactly those: it fixes its own search path
    and verifies the resolution, so a caller's search_path cannot point it at same-named tables elsewhere;
  * the organization exists, its slug matches --confirm-slug, and it is 'cancelled';
  * the access cutoff is complete: a recorded cutoff, no active agent token, no installation that is not retired, no
    unrevoked unused enrollment code, no active ingest key;
  * the operational mapping is unambiguous: a customer that exists, mapped by exactly this one organization, and no
    branch whose operational id is not its own id;
  * no row is ambiguous about whose it is: none of the tenant's rows names another tenant's branch, and no other
    tenant's row names one of this tenant's branches;
  * NO row anywhere in a table with nullable tenant keys (acs_events and the two *_clean copies) has a NULL customer_id
    or branch_id. Such a row cannot be attributed to any tenant, so the tool cannot prove it is not this tenant's --
    and acs_events holds the most sensitive legacy data in the system. It reports the COUNT and stops; classifying or
    remediating those rows is manual work that must happen first. It never guesses an owner.

WHAT IT DELETES is exactly `data_lifecycle_policy.purge_plan()`, in that order, each statement scoped to the one tenant,
in ONE transaction. WHAT IT NEVER DELETES is listed by `retained_tables()`: lifecycle evidence, the global user accounts
and the security audit log (governed separately), and reference data.

"DELETED" MEANS DELETED FROM THE LIVE DATABASE. The same rows remain restorable from Neon's point-in-time history until
the project's retention window has passed, and indefinitely in any Neon branch created before the purge. The tool says so
every time and never reports provider history as erased: that is a separate fact an operator verifies later.

OUTPUT is table names, row counts and internal ids only -- never a row's contents, never the connection string.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import bindparam, create_engine, text

from src.services.data_lifecycle_policy import (
    EVENT_ACCESS_CUTOFF,
    EVENT_PURGE_EXECUTED,
    SurfacePolicy,
    purge_plan,
    record_tenant_lifecycle_event,
    retained_tables,
)

EXIT_OK = 0
EXIT_REFUSED = 2

RUNTIME_ROLE = "sortview_app"

_ORG_BRANCHES = "SELECT id FROM branches WHERE organization_id = :organization_id"


@dataclass(frozen=True)
class Tenant:
    organization_id: int
    slug: str
    status: str
    customer_id: int | None
    branch_ids: tuple[int, ...]

    @property
    def scope(self) -> dict[str, int | None]:
        return {"organization_id": self.organization_id, "customer_id": self.customer_id}


def _is_postgresql(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _count(conn, sql: str, params: dict | None = None) -> int:
    return int(conn.execute(text(sql), params or {}).scalar_one())


def expected_schema_revision() -> str | None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


def load_tenant(conn, organization_id: int, *, lock: bool = False) -> Tenant | None:
    sql = "SELECT id, slug, status, operational_customer_id FROM organizations WHERE id = :organization_id"
    if lock and _is_postgresql(conn):
        sql += " FOR UPDATE"
    row = conn.execute(text(sql), {"organization_id": organization_id}).mappings().first()
    if row is None:
        return None
    branch_ids = tuple(
        int(r[0]) for r in conn.execute(text(_ORG_BRANCHES + " ORDER BY id"), {"organization_id": organization_id})
    )
    customer_id = row["operational_customer_id"]
    return Tenant(
        organization_id=int(row["id"]),
        slug=str(row["slug"]),
        status=str(row["status"]),
        customer_id=None if customer_id is None else int(customer_id),
        branch_ids=branch_ids,
    )


# --- refusals ---------------------------------------------------------------------------------------------------------
# Each returns a list of reasons; an empty list means that gate is satisfied. Reasons carry counts and ids, never values.

def ownership_tables() -> tuple[str, ...]:
    """Every table this tool reads or writes to decide, count, delete, verify or record anything: the whole purge plan
    (which is also every table the cutoff, mapping, attribution and survivor checks query), the lifecycle evidence table,
    and the schema-revision table. The connected role must OWN every one of them."""
    return tuple(sorted({entry.table for entry in purge_plan()} | {"tenant_lifecycle_events", "alembic_version"}))


def table_owners(conn, tables: tuple[str, ...]) -> dict[str, tuple[str, bool]]:
    """{table: (owning role, row level security FORCED?)} for the tables that exist in schema public. Catalog only; row
    level security does not apply to it."""
    statement = text("""
        SELECT c.relname, pg_get_userbyid(c.relowner), c.relforcerowsecurity
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p')
          AND c.relname IN :tables
    """).bindparams(bindparam("tables", expanding=True))
    return {
        str(name): (str(owner), bool(forced))
        for name, owner, forced in conn.execute(statement, {"tables": list(tables)}).all()
    }


# THE OBJECTS WHOSE OWNERSHIP IS CHECKED ARE EXACTLY THE OBJECTS QUERIED AND DELETED FROM. The ownership gate looks at
# public.<table> in the catalog; every statement this tool and the policy registry issue names its tables unqualified.
# The two are tied together twice over, so the caller's search_path (a role or database default, a connection option)
# cannot make them diverge:
#   1. pin_search_path fixes the search path for the whole transaction, before any application-table statement;
#   2. unresolved_tables then PROVES, per table, that the unqualified name resolves to the very object public.<table>
#      names -- and the tool refuses if it does not, rather than trusting that the pin took effect.
PURGE_SEARCH_PATH = "public, pg_temp"


def pin_search_path(conn) -> None:
    """Transaction-local: application tables resolve in schema public only. pg_temp is named explicitly and LAST, because
    an unlisted temporary schema is otherwise searched first and a temporary table could shadow a real one. (pg_catalog
    stays implicitly first; it holds no application table.) SET LOCAL lasts exactly as long as the one transaction every
    check, count and DELETE of a run happens in."""
    conn.execute(text(f"SET LOCAL search_path = {PURGE_SEARCH_PATH}"))  # nosec B608 - a fixed constant


def unresolved_tables(conn, tables: tuple[str, ...]) -> list[str]:
    """The tables whose UNQUALIFIED name does not resolve to public.<table> under the current search path."""
    statement = text("SELECT to_regclass(:unqualified)::oid IS NOT DISTINCT FROM to_regclass(:qualified)::oid")
    return [
        table for table in tables
        if not conn.execute(statement, {"unqualified": table, "qualified": f"public.{table}"}).scalar()
    ]


def environment_problems(conn) -> list[str]:
    """PostgreSQL-only gates, checked BEFORE the tenant is even looked up: table ownership, then the schema revision.

    OWNERSHIP IS THE AUTHORITATIVE REQUIREMENT. Seven of the tables a purge touches are under row level security. A role
    that is not their owner sees only the rows RLS lets it see -- with no tenant context set, none -- so its inventory,
    its unattributable-row check, its DELETEs and its survivor check would all report zero while the rows are still
    there, and the tool would certify a purge that did not happen. DELETE privilege does not change that, and neither a
    superuser nor a BYPASSRLS attribute is accepted as a substitute: the connected role must be the owner of every
    table in ownership_tables(), or nothing further is evaluated and nothing is reported as purge-eligible."""
    problems: list[str] = []

    role = str(conn.execute(text("SELECT current_user")).scalar())
    if role == RUNTIME_ROLE:  # defense in depth; the ownership check below would refuse it as well
        problems.append(
            f"connected as the runtime role {RUNTIME_ROLE!r}; a purge is run by the table owner, never the application role"
        )
        return problems

    # Every check asks the catalog first and never attempts a statement that could fail: a failed statement would abort
    # the transaction all the later checks (and the printed report) run in.
    required = ownership_tables()
    owners = table_owners(conn, required)
    not_owned = [table for table in required if table in owners and owners[table][0] != role]
    if not_owned:
        problems.append(
            f"role {role!r} is not the owner of: {', '.join(not_owned)}. A purge requires the table-owner role, so that "
            "row level security cannot hide this tenant's rows, or unattributable rows, from the tool"
        )
        return problems

    # Ownership only helps because an owner is exempt from row level security -- unless it is FORCED, which applies the
    # policies to the owner too. No table is forced today (migration 0acba192bf69 deliberately does not); if one ever is,
    # the owner's view is filtered like anyone else's and nothing this tool counted could be trusted.
    forced = [table for table in required if table in owners and owners[table][1]]
    if forced:
        problems.append(
            f"row level security is FORCED on: {', '.join(forced)}. A forced policy applies to the table owner too, so the "
            "tool could not be sure of seeing every row"
        )
        return problems

    # Before the first statement that names an application table (the schema revision, just below): every unqualified
    # name must resolve to the public object whose ownership was just proven.
    shadowed = unresolved_tables(conn, required)
    if shadowed:
        problems.append(
            f"the search path does not resolve these tables to schema public: {', '.join(shadowed)}. The tool would be "
            "reading or deleting from different objects than the ones whose ownership it checked"
        )
        return problems

    expected = expected_schema_revision()
    actual = None
    if "alembic_version" in owners:  # owned by this role (proven above), so it is readable
        actual = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    if actual != expected:
        problems.append(f"schema revision is {actual!r}, expected this repository's head {expected!r}")
        return problems  # the plan below describes the head schema; nothing else can be trusted against another one

    missing = [table for table in required if table not in owners]
    if missing:  # at head every one exists; an absent table means ownership of it is not proven
        problems.append(f"table(s) not found, so ownership cannot be proven: {', '.join(missing)}")
        return problems

    lacking = [
        entry.table for entry in purge_plan()
        if not conn.execute(
            text("SELECT has_table_privilege(current_user, :relation, 'DELETE')"), {"relation": f"public.{entry.table}"}
        ).scalar()
    ]
    if lacking:
        problems.append(f"role {role!r} lacks DELETE on: {', '.join(lacking)}")
    return problems


def cutoff_problems(conn, tenant: Tenant) -> list[str]:
    """The access cutoff (offboard_library) must be complete before anything is deleted."""
    problems: list[str] = []
    if tenant.status != "cancelled":
        problems.append(
            f"organization status is {tenant.status!r}, not 'cancelled' -- run the access cutoff "
            "(Super Admin: Offboard Library) first; a suspended or active tenant is never purged"
        )
        return problems

    if _count(conn, "SELECT COUNT(*) FROM tenant_lifecycle_events WHERE organization_id = :organization_id "
                    "AND event_type = :event_type",
              {"organization_id": tenant.organization_id, "event_type": EVENT_ACCESS_CUTOFF}) == 0:
        problems.append("no access_cutoff lifecycle event is recorded for this organization")

    live = {
        "active agent token(s)": (
            "SELECT COUNT(*) FROM agent_tokens WHERE is_active = TRUE AND (customer_id = :customer_id OR installation_id IN "
            "(SELECT id FROM collector_installations WHERE organization_id = :organization_id))"
        ),
        "collector installation(s) not retired": (
            "SELECT COUNT(*) FROM collector_installations WHERE organization_id = :organization_id AND status <> 'retired'"
        ),
        "unused enrollment code(s) not revoked": (
            "SELECT COUNT(*) FROM collector_enrollment_codes WHERE used_at IS NULL AND revoked_at IS NULL AND "
            "installation_id IN (SELECT id FROM collector_installations WHERE organization_id = :organization_id)"
        ),
        "active ingest key(s)": (
            "SELECT COUNT(*) FROM ingest_key_ids WHERE customer_id = :customer_id AND status = 'active'"
        ),
    }
    for label, sql in live.items():
        remaining = _count(conn, sql, tenant.scope)
        if remaining:
            problems.append(f"access cutoff is incomplete: {remaining} {label} remain")
    return problems


def mapping_problems(conn, tenant: Tenant) -> list[str]:
    """The tenant's operational identity must be present and belong to it alone."""
    problems: list[str] = []
    if tenant.customer_id is None:
        problems.append(
            "the organization has no operational customer mapping, so its operational rows cannot be identified"
        )
        return problems

    if _count(conn, "SELECT COUNT(*) FROM customers WHERE id = :customer_id", tenant.scope) != 1:
        problems.append(f"operational customer {tenant.customer_id} does not exist")
    mapped = _count(conn, "SELECT COUNT(*) FROM organizations WHERE operational_customer_id = :customer_id", tenant.scope)
    if mapped != 1:
        problems.append(f"operational customer {tenant.customer_id} is mapped by {mapped} organizations, not exactly one")
    inconsistent = _count(
        conn,
        "SELECT COUNT(*) FROM branches WHERE organization_id = :organization_id "
        "AND operational_branch_id IS NOT NULL AND operational_branch_id <> id",
        tenant.scope,
    )
    if inconsistent:
        problems.append(f"{inconsistent} branch(es) have an operational id that is not their own id")
    return problems


def attribution_problems(conn, tenant: Tenant) -> list[str]:
    """No row may be unattributable, and no row may be claimed by two tenants at once."""
    problems: list[str] = []
    for entry in purge_plan():
        # A NULL tenant key cannot be attributed to ANY tenant, so it is counted across the whole table, not just "this
        # tenant's part" of it: nothing proves such a row is not this tenant's.
        if entry.nullable_tenant_keys:
            unattributable = _count(
                conn,
                f"SELECT COUNT(*) FROM {entry.table} WHERE "  # nosec B608 - table/column names from the policy registry
                + " OR ".join(f"{column} IS NULL" for column in entry.nullable_tenant_keys),
            )
            if unattributable:
                both = _count(
                    conn,
                    f"SELECT COUNT(*) FROM {entry.table} WHERE "  # nosec B608
                    + " AND ".join(f"{column} IS NULL" for column in entry.nullable_tenant_keys),
                )
                problems.append(
                    f"{entry.table}: {unattributable} row(s) have a NULL tenant key ({both} with every key NULL, "
                    f"{unattributable - both} partially keyed) and cannot be attributed to a tenant; they must be "
                    "classified or remediated by hand before a complete purge of any tenant can be certified"
                )

        if entry.paired_keys and tenant.customer_id is not None:
            conflicting = _count(
                conn,
                f"SELECT COUNT(*) FROM {entry.table} WHERE "  # nosec B608
                f"(customer_id = :customer_id AND branch_id IS NOT NULL AND branch_id NOT IN ({_ORG_BRANCHES})) "
                f"OR (branch_id IN ({_ORG_BRANCHES}) AND (customer_id IS NULL OR customer_id <> :customer_id))",
                tenant.scope,
            )
            if conflicting:
                problems.append(
                    f"{entry.table}: {conflicting} row(s) pair this tenant's customer with another tenant's branch, or "
                    "this tenant's branch with another customer"
                )
    return problems


def refusals(conn, organization_id: int, confirm_slug: str, *, lock: bool = False) -> tuple[Tenant | None, list[str]]:
    """Every reason a purge of this tenant would be refused. Portable SQL only (the PostgreSQL environment gates are
    environment_problems)."""
    tenant = load_tenant(conn, organization_id, lock=lock)
    if tenant is None:
        return None, [f"organization {organization_id} does not exist"]
    if (confirm_slug or "").strip() != tenant.slug:
        # Deliberately does not print the real slug: the operator must already know which tenant this is.
        return None, [f"--confirm-slug does not match the slug of organization {organization_id}"]
    return tenant, cutoff_problems(conn, tenant) + mapping_problems(conn, tenant) + attribution_problems(conn, tenant)


# --- inventory and purge ----------------------------------------------------------------------------------------------

def _where(entry: SurfacePolicy) -> str:
    assert entry.purge_where is not None  # purge_plan() only yields entries that have one
    return entry.purge_where


def inventory(conn, tenant: Tenant) -> dict[str, int]:
    """Rows the purge would delete, per table, in purge order. Counts only."""
    return {
        entry.table: _count(conn, f"SELECT COUNT(*) FROM {entry.table} WHERE {_where(entry)}", tenant.scope)  # nosec B608
        for entry in purge_plan()
    }


def execute_purge(conn, tenant: Tenant, *, operator: str, schema_revision: str | None) -> dict[str, int]:
    """Deletes the tenant's rows in purge order inside the caller's transaction and appends the purge_executed evidence
    row. Raises (so the caller rolls back) if any tenant row survives. Returns rows deleted per table."""
    deleted: dict[str, int] = {}
    for entry in purge_plan():
        result = conn.execute(text(f"DELETE FROM {entry.table} WHERE {_where(entry)}"), tenant.scope)  # nosec B608
        deleted[entry.table] = int(result.rowcount)

    survivors = {table: count for table, count in inventory(conn, tenant).items() if count}
    if survivors:
        raise RuntimeError(f"purge left rows behind in: {', '.join(sorted(survivors))}")

    record_tenant_lifecycle_event(
        conn,
        event_type=EVENT_PURGE_EXECUTED,
        organization_id=tenant.organization_id,
        organization_slug=tenant.slug,
        operational_customer_id=tenant.customer_id,
        actor_user_id=None,
        actor_label=operator,
        details={
            "schema_revision": schema_revision,
            "rows_deleted": deleted,
            "branch_ids": list(tenant.branch_ids),
        },
    )
    return deleted


# --- output -----------------------------------------------------------------------------------------------------------

_PROVIDER_HISTORY_NOTICE = """\
PROVIDER HISTORY AGED OUT: NOT VERIFIED -- this tool cannot establish it and does not claim it.
  The rows above no longer exist in the LIVE database. They are NOT yet physically erased:
    * Neon keeps point-in-time history, from which they stay restorable until the project's retention window has
      passed since the completion time above. Read the current window from the Neon console; do not assume it.
    * Any Neon branch created before the completion time (recovery drills, pre-restore snapshots) still holds them
      until that branch is deleted by hand.
    * Error-tracking, hosting and e-mail providers age out their own records on their own schedules.
    * Files on the customer's Collector machine are removed only by the customer (uninstall with -PurgeData).
  Record "provider history aged out" separately, once the window has passed and no earlier branch remains."""


def _print_tenant(tenant: Tenant, out: TextIO) -> None:
    print(f"  organization_id={tenant.organization_id} organization_slug={tenant.slug} status={tenant.status}", file=out)
    print(f"  operational_customer_id={tenant.customer_id} branch_ids={list(tenant.branch_ids)}", file=out)


def _print_counts(title: str, counts: dict[str, int], out: TextIO) -> None:
    print(title, file=out)
    for table, count in counts.items():
        print(f"  {table:28s} {count}", file=out)
    print(f"  {'TOTAL':28s} {sum(counts.values())}", file=out)


def _print_retained(out: TextIO) -> None:
    print("Never deleted by a tenant purge (see docs/data-lifecycle-offboarding.md):", file=out)
    print(f"  {', '.join(retained_tables())}", file=out)
    print("  A purge removes the tenant's memberships; it does NOT delete any person's global SortView account or the", file=out)
    print("  global security-audit history.", file=out)


def _print_refusals(problems: list[str], out: TextIO) -> None:
    for problem in problems:
        print(f"REFUSED: {problem}", file=out)


# --- command line -------------------------------------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inventory, and with explicit confirmation permanently delete, one offboarded tenant's database rows."
    )
    parser.add_argument("--organization-id", type=int, required=True, help="organizations.id (the SaaS id).")
    parser.add_argument("--confirm-slug", required=True, help="The organization's slug; must match the id.")
    parser.add_argument("--database-url", default=None, help="Defaults to the DATABASE_URL environment variable.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually delete. Without it the tool inventories in a read-only transaction.")
    parser.add_argument("--confirm-purge", default=None, metavar="SLUG",
                        help="Required with --execute: the slug again, as the destructive confirmation.")
    parser.add_argument("--operator", default=None, help="Required with --execute: who is running the purge.")
    return parser


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)

    if args.execute:
        if not (args.operator or "").strip():
            print("REFUSED: --execute needs --operator (who is running the purge). Nothing was written.", file=out)
            return EXIT_REFUSED
        if args.confirm_purge is None or args.confirm_purge != args.confirm_slug:
            print("REFUSED: --execute needs --confirm-purge with the same slug as --confirm-slug. Nothing was written.",
                  file=out)
            return EXIT_REFUSED

    url = args.database_url or os.environ.get("DATABASE_URL")
    if not url:
        print("REFUSED: needs a database URL (--database-url, or the DATABASE_URL environment variable).", file=out)
        return EXIT_REFUSED

    engine = create_engine(url, hide_parameters=True, future=True)
    try:
        with engine.connect() as conn:
            if not _is_postgresql(conn):
                print("REFUSED: the tenant purge tool runs against PostgreSQL only. Nothing was written.", file=out)
                return EXIT_REFUSED

            with conn.begin() as transaction:
                if not args.execute:
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                pin_search_path(conn)

                problems = environment_problems(conn)
                tenant = None
                if not problems:
                    tenant, problems = refusals(conn, args.organization_id, args.confirm_slug, lock=args.execute)

                database = conn.execute(text("SELECT current_database()")).scalar()
                role = conn.execute(text("SELECT current_user")).scalar()
                revision = expected_schema_revision()
                print(f"SortView tenant purge -- {'EXECUTE' if args.execute else 'INVENTORY (read-only, nothing is written)'}",
                      file=out)
                print(f"  database={database} connected_role={role} schema_revision={revision}", file=out)

                if tenant is not None:
                    _print_tenant(tenant, out)
                    _print_counts("Rows in scope, per table (purge order):", inventory(conn, tenant), out)
                    _print_retained(out)

                if problems:
                    _print_refusals(problems, out)
                    print("Nothing was written.", file=out)
                    transaction.rollback()
                    return EXIT_REFUSED
                assert tenant is not None  # no problems means the tenant was loaded and confirmed

                if not args.execute:
                    print("A purge of this tenant WOULD BE ACCEPTED. Nothing was written.", file=out)
                    print("Re-run with --execute --confirm-purge <slug> --operator <name> to delete these rows.", file=out)
                    transaction.rollback()
                    return EXIT_OK

                deleted = execute_purge(conn, tenant, operator=args.operator.strip(), schema_revision=revision)
                transaction.commit()

            completed_at = datetime.now(UTC).isoformat(timespec="seconds")
            print(file=out)
            print("EVIDENCE SUMMARY", file=out)
            print(f"  operator={args.operator.strip()} completed_at={completed_at}", file=out)
            print(f"  database={database} connected_role={role} schema_revision={revision}", file=out)
            _print_tenant(tenant, out)
            _print_counts("Rows deleted, per table:", deleted, out)
            print("A purge_executed row was appended to tenant_lifecycle_events (retained).", file=out)
            print(file=out)
            print("LIVE DATABASE PURGE COMPLETE: every row of this tenant in the tables above has been deleted from the "
                  "live database.", file=out)
            print(_PROVIDER_HISTORY_NOTICE, file=out)
            return EXIT_OK
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
