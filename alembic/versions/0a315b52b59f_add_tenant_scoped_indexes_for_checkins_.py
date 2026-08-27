"""add tenant scoped indexes for checkins rejects acs_events

Revision ID: 0a315b52b59f
Revises: 26397a3947b1
Create Date: 2026-08-04 21:45:29.092280

checkins/rejects/acs_events have never had an index covering
customer_id, only a plain index on branch_id and one on event_time
separately. Every dashboard query filters on
(customer_id, branch_id) and orders by event_time
(see data_loader.py's _scoped_query), so those queries have had no
index that actually matches their WHERE + ORDER BY shape -- Postgres
falls back to the branch_id index (or a sequential scan) plus a
separate sort. This gets worse as history grows.

Adds a single composite index per table on
(customer_id, branch_id, event_time), which covers the WHERE clause
and satisfies the ORDER BY without a separate sort step. Purely
additive -- existing indexes are left in place.

Uses CREATE INDEX CONCURRENTLY (via autocommit_block) so index
creation does not lock the tables against writes from the agent/API
while running.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0a315b52b59f'
down_revision: str | Sequence[str] | None = '26397a3947b1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEXES = [
    ("idx_checkins_customer_branch_event_time", "checkins"),
    ("idx_rejects_customer_branch_event_time", "rejects"),
    ("idx_acs_events_customer_branch_event_time", "acs_events"),
]


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        for index_name, table_name in INDEXES:
            op.execute(
                f"""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name}
                ON {table_name} (customer_id, branch_id, event_time)
                """
            )


def downgrade() -> None:
    """Downgrade schema."""
    with op.get_context().autocommit_block():
        for index_name, table_name in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")
