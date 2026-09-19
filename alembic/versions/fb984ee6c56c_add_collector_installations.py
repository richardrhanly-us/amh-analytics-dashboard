"""add collector installations

Revision ID: fb984ee6c56c
Revises: c53c1b536c71
Create Date: 2026-09-18 21:51:10.933087

Server-side bookkeeping for SortView Collector deployments.

organizations       = customer tenant
branches            = library branch
collector_installations = one deployed Collector instance/machine (this table)
agent_tokens        = authentication credentials (unchanged, no FK to this table)
pipeline_status     = branch/ingestion health (remains the source of health;
                      deliberately NOT duplicated here)

A branch may eventually host more than one Collector installation, so
branch_id is indexed but NOT unique. hostname is the only machine identity
recorded. No tokens, passwords, database credentials, IP addresses,
usernames or other secrets belong in this table.

last_seen_at is nullable and is not populated by the Collector or upload
API in this revision; it is set manually/server-side only.

status is TEXT + CHECK (the same pattern organizations.status and
branches.status use in the baseline) rather than a DB enum type.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fb984ee6c56c"
down_revision: str | Sequence[str] | None = "c53c1b536c71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "collector_installations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "branch_id",
            sa.Integer(),
            sa.ForeignKey("branches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("hostname", sa.Text(), nullable=True),
        sa.Column("collector_version", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'provisioning'"),
        ),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "status IN ('provisioning', 'active', 'inactive', 'retired')",
            name="ck_collector_installations_status",
        ),
    )

    op.create_index(
        "ix_collector_installations_organization_id",
        "collector_installations",
        ["organization_id"],
    )

    op.create_index(
        "ix_collector_installations_branch_id",
        "collector_installations",
        ["branch_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_collector_installations_branch_id",
        table_name="collector_installations",
    )

    op.drop_index(
        "ix_collector_installations_organization_id",
        table_name="collector_installations",
    )

    op.drop_table("collector_installations")
