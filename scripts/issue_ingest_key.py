"""Issues or retires a Privacy Contract v2 ingest key (`key_id`) in the `ingest_key_ids` registry
(docs/contract-v2-design.md, alembic revision d3f1a8c95b27).

A `key_id` is SERVER-ISSUED: a random UUIDv4 the collector is told to use, never one it chooses. It is not a secret and is
stored in the clear. The registry holds NO key material -- the HMAC secret behind a key_id is generated locally by the
collector and is never sent to, or stored by, the cloud. A v2 request is accepted only with an ACTIVE key_id registered to
the very tenant the request's token is scoped to; an unknown, retired or wrong-tenant key_id is rejected.

SAFE BY DEFAULT: without --execute this touches no database; it only says what would happen.

    python scripts/issue_ingest_key.py issue  --customer-id 1 --branch-id 1
    python scripts/issue_ingest_key.py retire --key-id 3f2b8c1e-....

With --execute (a bare flag uses the DATABASE_URL environment variable, or pass a URL) the change is made in ONE transaction:

    DATABASE_URL=postgresql://... python scripts/issue_ingest_key.py issue --customer-id 1 --branch-id 1 --execute

`--customer-id` / `--branch-id` are the OPERATIONAL pair (organizations.operational_customer_id and
branches.operational_branch_id), exactly as for scripts/create_agent_token.py; `issue` refuses a pair that is not a mapped
tenant and writes nothing. Retiring is one-way: issue a new key instead of reactivating an old one.

Never point --execute at production without an explicit go-ahead: production is changed by an operator, not by tests or CI.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine

from src.services.ingest_v2_models import UUID4_PATTERN
from src.services.ingest_v2_service import (
    issue_ingest_key,
    retire_ingest_key,
)

EXIT_OK = 0
EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Issue or retire a Contract v2 ingest key.")
    # `--execute` belongs to each command (it comes AFTER the command, as in the usage above), so a bare `--execute` at the
    # end of the line cannot swallow anything.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--execute", nargs="?", const="__use_env__", default=None, metavar="DATABASE_URL",
                        help="Actually change the database. Without it nothing is written (a dry run).")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue", parents=[common], help="register a new key for one tenant")
    issue.add_argument("--customer-id", type=int, required=True)
    issue.add_argument("--branch-id", type=int, required=True)
    retire = commands.add_parser("retire", parents=[common], help="retire an active key")
    retire.add_argument("--key-id", required=True)
    return parser


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)

    if args.command == "retire" and not re.fullmatch(UUID4_PATTERN, args.key_id):
        print("REFUSED: --key-id is not a key_id (a lower-case UUIDv4).", file=out)
        return EXIT_REFUSED

    if args.execute is None:
        if args.command == "issue":
            print(f"DRY RUN: would register a NEW random key_id for customer_id={args.customer_id} "
                  f"branch_id={args.branch_id} (algorithm hmac-sha256-v1, status active). Nothing was written.", file=out)
        else:
            print(f"DRY RUN: would retire key_id={args.key_id}. Nothing was written.", file=out)
        print("Re-run with --execute to make the change.", file=out)
        return EXIT_OK

    url = os.environ.get("DATABASE_URL") if args.execute == "__use_env__" else args.execute
    if not url:
        print("REFUSED: --execute needs a database URL (an argument, or the DATABASE_URL environment variable).", file=out)
        return EXIT_REFUSED

    engine = create_engine(url, hide_parameters=True, future=True)
    try:
        with engine.begin() as conn:
            if args.command == "issue":
                try:
                    key_id = issue_ingest_key(conn, args.customer_id, args.branch_id)
                except ValueError:
                    print("REFUSED: customer_id / branch_id is not a mapped operational tenant. Nothing was written.", file=out)
                    return EXIT_REFUSED
                print(f"ISSUED key_id={key_id} for customer_id={args.customer_id} branch_id={args.branch_id}", file=out)
                print("Give this key_id to the collector. It is not a secret; the HMAC secret is generated locally "
                      "and never leaves the collector.", file=out)
            else:
                if not retire_ingest_key(conn, args.key_id):
                    print("REFUSED: no ACTIVE key with that key_id. Nothing was changed.", file=out)
                    return EXIT_REFUSED
                print(f"RETIRED key_id={args.key_id}", file=out)
    finally:
        engine.dispose()
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
