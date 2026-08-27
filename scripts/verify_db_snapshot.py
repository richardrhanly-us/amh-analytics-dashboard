"""Prints a quick summary of a SortView database's state.

Used to compare "before" and "after" during a backup/restore drill (or
any time you want a fast sanity check of what's actually in a database):
schema version, row counts per table, and the most recent event per
activity table. Not a pass/fail gate -- the human comparing two runs'
output is the actual verification.

Usage:
    DATABASE_URL=postgresql://... python scripts/verify_db_snapshot.py
    python scripts/verify_db_snapshot.py "postgresql://..."
"""

from __future__ import annotations

import os
import sys

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


def get_database_url() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "Usage: DATABASE_URL=postgresql://... python scripts/verify_db_snapshot.py\n"
            "   or: python scripts/verify_db_snapshot.py postgresql://..."
        )
    return database_url


def main() -> None:
    engine = create_engine(get_database_url(), connect_args={"sslmode": "require"})

    with engine.connect() as conn:
        try:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except Exception:
            version = "(alembic_version table missing or empty)"
        print(f"alembic_version: {version}\n")

        # table is always one of the hardcoded TABLES/LATEST_EVENT_TABLES
        # constants above, never external input.
        print("row counts:")
        for table in TABLES:
            try:
                count = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar()  # nosec B608
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


if __name__ == "__main__":
    main()
