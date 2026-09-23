"""Sets, rolls back, or shows a branch's Contract v2 mixed-era dashboard cutover (`v2_cutovers`,
alembic revision f2a91c7d4e83).

The cutover is the OPERATOR-CONTROLLED, UTC boundary the dashboard's mixed-era read model uses to decide, per branch,
which era a row belongs to: v1 rows strictly BEFORE the cutover, v2 rows AT OR AFTER it. It is never inferred from the
earliest v2 event actually observed for a branch -- only this table, and only an operator action, ever sets it.

Every `set` or `rollback` APPENDS a new row; nothing is ever updated or deleted, so the full history of a branch's
transition (who changed it, when, and why) is always reconstructable. A branch with no rows, or whose latest row has no
cutover_at, is v1-only -- the dashboard's mixed-era read model then falls back to today's unmodified all-v1 behavior.

SAFE BY DEFAULT: without --execute this touches no database; it only says what would happen. `show` never writes and
needs no --execute.

    python scripts/set_v2_cutover.py show     --customer-id 1 --branch-id 1
    python scripts/set_v2_cutover.py set      --customer-id 1 --branch-id 1 --cutover-at 2026-10-01T00:00:00+00:00 \\
                                               --set-by "rhanly" --note "NBPL pilot cutover"
    python scripts/set_v2_cutover.py rollback --customer-id 1 --branch-id 1 --set-by "rhanly" \\
                                               --note "reverting pilot, saw identity collisions in v2 dry-run"

With --execute (a bare flag uses the DATABASE_URL environment variable, or pass a URL) the change is made in ONE
transaction:

    DATABASE_URL=postgresql://... python scripts/set_v2_cutover.py set --customer-id 1 --branch-id 1 \\
        --cutover-at 2026-10-01T00:00:00+00:00 --set-by "rhanly" --execute

`--customer-id` / `--branch-id` are the OPERATIONAL pair (organizations.operational_customer_id and
branches.operational_branch_id), exactly as for scripts/issue_ingest_key.py; `set` and `rollback` both refuse a pair that
is not a mapped tenant and write nothing. `--cutover-at` must be an offset-aware ISO-8601 timestamp (a bare local time
with no offset is refused rather than silently guessed as UTC).

Never point --execute at production without an explicit go-ahead: production is changed by an operator, not by tests or
CI. This script does not enable v2 ingestion itself (SORTVIEW_V2_INGEST_ENABLED) or issue an ingest key -- see
scripts/issue_ingest_key.py for that.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine

from src.services.ingest_v2_service import get_effective_v2_cutover, record_v2_cutover

EXIT_OK = 0
EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Set, roll back, or show a branch's Contract v2 dashboard cutover.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--customer-id", type=int, required=True)
    common.add_argument("--branch-id", type=int, required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    show = commands.add_parser("show", parents=[common], help="print the tenant's current effective cutover (read-only)")
    show.add_argument("--database-url", default=None, help="Defaults to the DATABASE_URL environment variable.")

    write_common = argparse.ArgumentParser(add_help=False, parents=[common])
    write_common.add_argument("--set-by", required=True, help="Operator identifier recorded on this row.")
    write_common.add_argument("--note", default=None)
    write_common.add_argument("--execute", nargs="?", const="__use_env__", default=None, metavar="DATABASE_URL",
                              help="Actually change the database. Without it nothing is written (a dry run).")

    set_cmd = commands.add_parser("set", parents=[write_common], help="record a new v1-to-v2 cutover instant")
    set_cmd.add_argument("--cutover-at", required=True,
                         help="Offset-aware ISO-8601 UTC instant, e.g. 2026-10-01T00:00:00+00:00")

    commands.add_parser("rollback", parents=[write_common], help="revert the branch to v1-only (records cutover_at=NULL)")

    return parser


def _parse_cutover_at(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("--cutover-at must be offset-aware (e.g. end it with +00:00 or Z), not a bare local time")
    return parsed


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)

    if args.command == "show":
        url = args.database_url or os.environ.get("DATABASE_URL")
        if not url:
            print("REFUSED: needs a database URL (--database-url, or the DATABASE_URL environment variable).", file=out)
            return EXIT_REFUSED
        engine = create_engine(url, hide_parameters=True, future=True)
        try:
            with engine.connect() as conn:
                effective_cutover_at = get_effective_v2_cutover(conn, args.customer_id, args.branch_id)
        finally:
            engine.dispose()
        if effective_cutover_at is None:
            print(f"customer_id={args.customer_id} branch_id={args.branch_id}: v1-only "
                  "(no cutover on record, or the latest one is a rollback).", file=out)
        else:
            print(f"customer_id={args.customer_id} branch_id={args.branch_id}: "
                  f"v2 at/after {effective_cutover_at.isoformat()}", file=out)
        return EXIT_OK

    cutover_at: datetime | None = None
    if args.command == "set":
        try:
            cutover_at = _parse_cutover_at(args.cutover_at)
        except ValueError as exc:
            print(f"REFUSED: {exc}", file=out)
            return EXIT_REFUSED

    if args.execute is None:
        if args.command == "set":
            assert cutover_at is not None  # guaranteed by the "set" branch above, which returns early on failure
            print(f"DRY RUN: would record a v2 cutover at {cutover_at.isoformat()} for customer_id={args.customer_id} "
                  f"branch_id={args.branch_id} (set_by={args.set_by!r}). Nothing was written.", file=out)
        else:
            print(f"DRY RUN: would roll back customer_id={args.customer_id} branch_id={args.branch_id} to v1-only "
                  f"(set_by={args.set_by!r}). Nothing was written.", file=out)
        print("Re-run with --execute to make the change.", file=out)
        return EXIT_OK

    url = os.environ.get("DATABASE_URL") if args.execute == "__use_env__" else args.execute
    if not url:
        print("REFUSED: --execute needs a database URL (an argument, or the DATABASE_URL environment variable).", file=out)
        return EXIT_REFUSED

    engine = create_engine(url, hide_parameters=True, future=True)
    try:
        with engine.begin() as conn:
            try:
                row_id = record_v2_cutover(
                    conn,
                    args.customer_id,
                    args.branch_id,
                    cutover_at,
                    args.set_by,
                    note=args.note,
                )
            except ValueError as exc:
                print(f"REFUSED: {exc}. Nothing was written.", file=out)
                return EXIT_REFUSED
        if args.command == "set":
            assert cutover_at is not None  # guaranteed by the "set" branch above, which returns early on failure
            print(f"RECORDED cutover row id={row_id}: customer_id={args.customer_id} branch_id={args.branch_id} "
                  f"v2 at/after {cutover_at.isoformat()}", file=out)
        else:
            print(f"RECORDED rollback row id={row_id}: customer_id={args.customer_id} branch_id={args.branch_id} "
                  "reverted to v1-only", file=out)
    finally:
        engine.dispose()

    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
