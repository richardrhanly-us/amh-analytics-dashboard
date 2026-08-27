"""Prints a quick summary of a SortView database's state.

Used to compare "before" and "after" during a backup/restore drill (or
any time you want a fast sanity check of what's actually in a database):
schema version, row counts per table, and the most recent event per
activity table.

By default this script is read-only. Pass --check-write to verify that
the database can accept a temporary write/read/rollback cycle.

Usage:

    DATABASE_URL=postgresql://... python scripts/verify_db_snapshot.py

    python scripts/verify_db_snapshot.py "postgresql://..."

    python scripts/verify_db_snapshot.py --check-write

    python scripts/verify_db_snapshot.py "postgresql://..." --check-write
"""

from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine, text

TABLES = [
    "organizations",
    "branches",
    "memberships",
    "subscriptions",
    "app_users",
    "agent_tokens",
    "customers",
    "checkins",
    "rejects",
    "acs_events",
    "pipeline_status",
]

LATEST_EVENT_TABLES = ["checkins", "rejects", "acs_events"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the current state of a SortView database."
    )

    parser.add_argument(
        "database_url",
        nargs="?",
        help="Optional PostgreSQL connection string. Defaults to DATABASE_URL.",
    )

    parser.add_argument(
        "--check-write",
        action="store_true",
        help="Verify temporary write/read/rollback behavior.",
    )

    return parser.parse_args()


def get_database_url(database_url_arg: str | None) -> str:
    if database_url_arg:
        return database_url_arg

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise SystemExit(
            "Usage: DATABASE_URL=postgresql://... python scripts/verify_db_snapshot.py\n"
            "   or: python scripts/verify_db_snapshot.py postgresql://..."
        )

    return database_url


def run_write_check(conn) -> None:
    try:
        conn.execute(text("CREATE TEMP TABLE recovery_test(id int)"))
        conn.execute(text("INSERT INTO recovery_test VALUES (1)"))

        count = conn.execute(
            text("SELECT COUNT(*) FROM recovery_test")
        ).scalar()

        if count != 1:
            raise RuntimeError(
        f"Post-restore write verification failed: expected 1 row, got {count}"
    )

        print("\nwrite check:")
        print("  PASS - temporary write/read/rollback succeeded")

    finally:
        conn.rollback()


def main() -> None:
    args = parse_args()

    engine = create_engine(
        get_database_url(args.database_url),
        connect_args={"sslmode": "require"},
    )

    with engine.connect() as conn:
        try:
            version = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar()
        except Exception:
            version = "(alembic_version table missing or empty)"

        print(f"alembic_version: {version}\n")

        print("row counts:")

        for table in TABLES:
            try:
                count = conn.execute(
                    text(f'SELECT COUNT(*) FROM "{table}"')  # nosec B608
                ).scalar()

                print(f"  {table:20s} {count}")

            except Exception as exc:
                print(f"  {table:20s} ERROR: {exc}")

        print("\nmost recent event_time:")

        for table in LATEST_EVENT_TABLES:
            try:
                latest = conn.execute(
                    text(f'SELECT MAX(event_time) FROM "{table}"')  # nosec B608
                ).scalar()

                print(f"  {table:20s} {latest}")

            except Exception as exc:
                print(f"  {table:20s} ERROR: {exc}")

        if args.check_write:
            run_write_check(conn)


if __name__ == "__main__":
    main()
