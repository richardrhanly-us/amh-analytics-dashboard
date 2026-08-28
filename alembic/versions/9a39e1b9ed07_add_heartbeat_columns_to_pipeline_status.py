"""add heartbeat columns to pipeline_status

Revision ID: 9a39e1b9ed07
Revises: 96373530503a
Create Date: 2026-08-28 00:00:00.000000

Continuous Ingestion Phase 3 (Signal). Adds a separate namespace of
heartbeat-specific columns to pipeline_status, distinct from the existing
per-run columns (status, last_run, last_attempt, checkins_rows, ...).

This is deliberately additive-only and does not touch, rename, or repurpose
any existing column. The two writer types (the legacy scheduled
run_pipeline uploader, and the new continuous-agent heartbeat component)
are expected to coexist against the same (customer_id, branch_id) row
during Phase 0's parallel-validation window, each only ever populating its
own columns -- see main.py's upload_pipeline_status for the partial-update
logic that keeps them from clobbering each other.

health_status carries its own healthy/degraded/auth_failure vocabulary
rather than reusing the legacy `status` column, which already has a
different, incompatible vocabulary (started/completed/failed/...) actively
written by the legacy uploader during coexistence.

Migration-runtime note: this repo's CI does not currently run migrations
against a real Postgres instance (no Postgres service in
.github/workflows/python-tests.yml, and tests/test_main_api.py's
FakeConnection does not parse or validate SQL). This migration has been
reviewed for correctness but not integration-tested against a live
database as part of this change -- see the Phase 3 report for the
equivalent application-level test coverage that exists instead.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a39e1b9ed07"
down_revision: str | Sequence[str] | None = "96373530503a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("pipeline_status", sa.Column("health_status", sa.Text(), nullable=True))
    op.add_column("pipeline_status", sa.Column("pending_outbox_count", sa.Integer(), nullable=True))
    op.add_column("pipeline_status", sa.Column("quarantined_count", sa.Integer(), nullable=True))
    op.add_column(
        "pipeline_status", sa.Column("oldest_pending_event_at", sa.DateTime(), nullable=True)
    )
    op.add_column("pipeline_status", sa.Column("last_success_at", sa.DateTime(), nullable=True))
    op.add_column("pipeline_status", sa.Column("last_failure_category", sa.Text(), nullable=True))
    op.add_column("pipeline_status", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "pipeline_status", sa.Column("watcher_last_active_at", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pipeline_status", "watcher_last_active_at")
    op.drop_column("pipeline_status", "last_error")
    op.drop_column("pipeline_status", "last_failure_category")
    op.drop_column("pipeline_status", "last_success_at")
    op.drop_column("pipeline_status", "oldest_pending_event_at")
    op.drop_column("pipeline_status", "quarantined_count")
    op.drop_column("pipeline_status", "pending_outbox_count")
    op.drop_column("pipeline_status", "health_status")
