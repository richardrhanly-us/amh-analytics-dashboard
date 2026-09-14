"""Provisions a new row in the `agent_tokens` table (see
alembic/versions/26397a3947b1_baseline_current_schema.py for the schema)
for a single agent installation -- e.g. the canonical continuous agent
being brought up for a controlled production cutover, kept as a SEPARATE
token from whatever the legacy agent already uses for the same
customer_id/branch_id, so either can be revoked/rotated independently
(set is_active = FALSE) without touching the other.

SAFE BY DEFAULT: prints the token (shown exactly once -- it is never
stored anywhere in plaintext, only its SHA-256 hash is written to the
database, the same scheme main.py's authenticate_agent already looks up
against) and the exact INSERT statement, but does NOT write to any
database unless --execute is passed together with a database URL. Never
prints the raw token more than once, and never logs it anywhere else.

Usage (dry run -- print what would happen, write nothing):

    python scripts/create_agent_token.py --customer-id 100 --branch-id 5 \\
        --description "NBPL canonical agent (production cutover)"

Usage (actually insert the row):

    DATABASE_URL=postgresql://... python scripts/create_agent_token.py \\
        --customer-id 100 --branch-id 5 \\
        --description "NBPL canonical agent (production cutover)" --execute

    python scripts/create_agent_token.py --customer-id 100 --branch-id 5 \\
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


_INSERT_SQL = """
    INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active)
    VALUES (:token_hash, :customer_id, :branch_id, :description, TRUE)
    """


def main() -> None:
    args = parse_args()
    raw_token, token_hash = generate_token()
    params = {
        "token_hash": token_hash,
        "customer_id": args.customer_id,
        "branch_id": args.branch_id,
        "description": args.description,
    }

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
    print("SQL that will run (or would have run, without --execute), as a bound")
    print("statement -- shown separately from its parameters, never hand-interpolated,")
    print("so a description containing a quote can't produce broken/unsafe SQL:")
    print(f"  {_INSERT_SQL.strip()}")
    print(f"  params: {params}")
    print()

    if args.execute is None:
        print("DRY RUN -- nothing was written. Re-run with --execute to insert this row.")
        return

    database_url = args.execute if args.execute != "__use_env__" else os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "--execute requires a database URL: set DATABASE_URL or pass --execute postgresql://..."
        )

    engine = create_engine(database_url, connect_args={"sslmode": "require"})
    with engine.begin() as conn:
        conn.execute(text(_INSERT_SQL), params)

    print("WROTE the row above to the database. The token was NOT saved anywhere by this "
          "script -- copy it now.")


if __name__ == "__main__":
    main()
