"""Provisions a new row in the `agent_tokens` table (see
alembic/versions/26397a3947b1_baseline_current_schema.py for the schema)
for a single agent installation -- e.g. the canonical continuous agent
being brought up for a controlled production cutover, kept as a SEPARATE
token from whatever the legacy agent already uses for the same
customer_id/branch_id, so either can be revoked/rotated independently
(set is_active = FALSE) without touching the other.

SCOPE VALIDATION: --customer-id/--branch-id are the OPERATIONAL pair --
organizations.operational_customer_id and branches.operational_branch_id, NOT
the SaaS organizations.id / branches.id. With --execute the script reads the
database first and refuses to insert unless the pair is a valid, fully mapped
tenant: the customers row exists, the branch exists, exactly one organization
maps to that customer, the branch's operational_branch_id is set to its own
id, the branch belongs to that same organization, and the organization
(active/trial) and branch (active) are in a usable status. Nothing is printed
or written if validation fails -- in particular no raw token is shown for a
token that was never stored. A dry run never touches a database, so it cannot
validate; it says so.

SAFE BY DEFAULT: prints the token (shown exactly once -- it is never
stored anywhere in plaintext, only its SHA-256 hash is written to the
database, the same scheme main.py's authenticate_agent already looks up
against) and the exact INSERT statement, but does NOT write to any
database unless --execute is passed together with a database URL. Never
prints the raw token more than once, and never logs it anywhere else.

Usage (dry run -- print what would happen, write nothing):

    python scripts/create_agent_token.py --customer-id 1 --branch-id 1 \\
        --description "NBPL canonical agent (production cutover)"

Usage (actually insert the row):

    DATABASE_URL=postgresql://... python scripts/create_agent_token.py \\
        --customer-id 1 --branch-id 1 \\
        --description "NBPL canonical agent (production cutover)" --execute

    python scripts/create_agent_token.py --customer-id 1 --branch-id 1 \\
        --description "..." --execute "postgresql://..."

After a successful --execute run, immediately hand the printed token to
whoever is configuring the agent (e.g. via
agent/deploy/set-sortview-api-token.ps1) -- it cannot be recovered from
the database afterward, only re-issued as a new token.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets

from sqlalchemy import create_engine, text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision a new agent_tokens row for one agent installation."
    )
    parser.add_argument("--customer-id", type=int, required=True)
    parser.add_argument("--branch-id", type=int, required=True)
    parser.add_argument(
        "--description", required=True,
        help='Human-readable label, e.g. "NBPL canonical agent (production cutover)".',
    )
    parser.add_argument(
        "--execute",
        nargs="?",
        const="__use_env__",
        default=None,
        metavar="DATABASE_URL",
        help=(
            "Actually write the row. Without this flag, only prints the token and the "
            "SQL that WOULD run. Pass a bare --execute to use the DATABASE_URL env var, "
            "or --execute postgresql://... to specify it directly."
        ),
    )
    return parser.parse_args()


def generate_token() -> tuple[str, str]:
    """Returns (raw_token, sha256_hex_digest) -- same hashing scheme as
    main.py's authenticate_agent (`encode(digest(:token, 'sha256'), 'hex')`
    in Postgres, hashlib.sha256(...).hexdigest() here -- identical
    result)."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    return raw_token, token_hash


# Statuses under which a tenant may be issued a new token. organizations.status
# is constrained to active/trial/suspended/cancelled; branches.status to
# active/inactive. readiness_service requires branch status 'active'.
_ALLOWED_ORGANIZATION_STATUSES = ("active", "trial")
_ALLOWED_BRANCH_STATUS = "active"


class ScopeError(Exception):
    """The (customer_id, branch_id) pair is not a valid provisioned tenant."""


def validate_token_scope(conn, customer_id: int, branch_id: int) -> dict:
    """Verify (customer_id, branch_id) is a valid, fully mapped operational
    pair belonging to ONE organization. Read-only. Raises ScopeError with a
    specific reason; returns a summary dict on success."""
    customer = conn.execute(
        text("SELECT id FROM customers WHERE id = :customer_id"),
        {"customer_id": customer_id},
    ).mappings().first()
    if customer is None:
        raise ScopeError(f"customer_id {customer_id} does not exist in customers")

    branch = conn.execute(
        text("""
            SELECT id, organization_id, status, operational_branch_id
            FROM branches
            WHERE id = :branch_id
        """),
        {"branch_id": branch_id},
    ).mappings().first()
    if branch is None:
        raise ScopeError(f"branch_id {branch_id} does not exist in branches")

    orgs = conn.execute(
        text("""
            SELECT id, slug, status
            FROM organizations
            WHERE operational_customer_id = :customer_id
        """),
        {"customer_id": customer_id},
    ).mappings().all()
    if not orgs:
        raise ScopeError(
            f"customer_id {customer_id} is not mapped to any organization "
            "(unmapped tenant); assign its operational identity first"
        )
    if len(orgs) > 1:
        raise ScopeError(
            f"customer_id {customer_id} is mapped to {len(orgs)} organizations; "
            "refusing to issue a token for an ambiguous tenant"
        )
    org = orgs[0]

    if branch["operational_branch_id"] is None:
        raise ScopeError(
            f"branch_id {branch_id} has no operational_branch_id (unmapped "
            "branch); assign its operational identity first"
        )
    if branch["operational_branch_id"] != branch["id"]:
        raise ScopeError(
            f"branch {branch_id} has inconsistent operational_branch_id "
            f"{branch['operational_branch_id']}"
        )

    if branch["organization_id"] != org["id"]:
        raise ScopeError(
            f"customer_id {customer_id} belongs to organization {org['id']} "
            f"but branch_id {branch_id} belongs to organization "
            f"{branch['organization_id']}; the pair spans different tenants"
        )

    if org["status"] not in _ALLOWED_ORGANIZATION_STATUSES:
        raise ScopeError(
            f"organization {org['id']} status is {org['status']!r}; "
            f"expected one of {', '.join(_ALLOWED_ORGANIZATION_STATUSES)}"
        )
    if branch["status"] != _ALLOWED_BRANCH_STATUS:
        raise ScopeError(
            f"branch {branch_id} status is {branch['status']!r}; "
            f"expected {_ALLOWED_BRANCH_STATUS!r}"
        )

    return {
        "organization_id": org["id"],
        "organization_slug": org["slug"],
        "customer_id": customer_id,
        "branch_id": branch_id,
    }


_INSERT_SQL = """
    INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active)
    VALUES (:token_hash, :customer_id, :branch_id, :description, TRUE)
    """


def print_token_banner(raw_token: str) -> None:
    print("=" * 78)
    print("NEW AGENT TOKEN -- shown exactly once, never recoverable afterward:")
    print()
    print(f"  {raw_token}")
    print()
    print("Hand this to whoever configures the agent's SORTVIEW_API_TOKEN")
    print("(e.g. agent/deploy/set-sortview-api-token.ps1) and then discard it from")
    print("this terminal's scrollback / any place it was displayed.")
    print("=" * 78)
    print()


def print_sql(params: dict) -> None:
    print("SQL that will run (or would have run, without --execute), as a bound")
    print("statement -- shown separately from its parameters, never hand-interpolated,")
    print("so a description containing a quote can't produce broken/unsafe SQL:")
    print(f"  {_INSERT_SQL.strip()}")
    print(f"  params: {params}")
    print()


def main() -> None:
    args = parse_args()
    raw_token, token_hash = generate_token()
    params = {
        "token_hash": token_hash,
        "customer_id": args.customer_id,
        "branch_id": args.branch_id,
        "description": args.description,
    }

    if args.execute is None:
        print_token_banner(raw_token)
        print_sql(params)
        print("DRY RUN -- nothing was written and the (customer_id, branch_id) pair was")
        print("NOT validated (a dry run has no database access). --execute validates the")
        print("pair against the database and rejects unmapped or mismatched tenants.")
        print("Re-run with --execute to insert this row.")
        return

    database_url = args.execute if args.execute != "__use_env__" else os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "--execute requires a database URL: set DATABASE_URL or pass --execute postgresql://..."
        )

    engine = create_engine(database_url, connect_args={"sslmode": "require"}, hide_parameters=True)
    with engine.begin() as conn:
        # Validate BEFORE showing any token, so a rejected pair never yields a
        # raw token that was never stored.
        try:
            scope = validate_token_scope(conn, args.customer_id, args.branch_id)
        except ScopeError as exc:
            raise SystemExit(f"REFUSED: {exc}. Nothing was written.") from exc

        print(
            f"Verified scope: organization {scope['organization_slug']} "
            f"(id {scope['organization_id']}), operational customer_id "
            f"{scope['customer_id']}, branch_id {scope['branch_id']}."
        )
        print()
        print_token_banner(raw_token)
        print_sql(params)
        conn.execute(text(_INSERT_SQL), params)

    print("WROTE the row above to the database. The token was NOT saved anywhere by this "
          "script -- copy it now.")


if __name__ == "__main__":
    main()
