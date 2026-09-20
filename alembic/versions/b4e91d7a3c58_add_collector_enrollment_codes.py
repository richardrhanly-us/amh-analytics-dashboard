"""add collector enrollment codes and agent_tokens.installation_id

Revision ID: b4e91d7a3c58
Revises: d679b80d3a64
Create Date: 2026-09-20 09:00:00.000000

Backend support for one-time Collector enrollment (SortView Collector 1.0.4
onboarding): a Super Admin generates a short-lived single-use enrollment code
for ONE explicit collector_installations row; the Collector redeems it over
HTTPS (POST /collector/enroll) for a long-lived agent token bound to that
installation.

collector_enrollment_codes
    One row per generated code. Only the SHA-256 hex digest of the code is
    stored (code_hash, UNIQUE) -- never the raw code. A code is redeemable only
    while used_at IS NULL AND revoked_at IS NULL AND expires_at is in the
    future. Generating a new code for an installation revokes that
    installation's earlier unused codes. installation_id cascades on delete: a
    code has no meaning without its installation.
    created_by_user_id -> app_users(id) records which Super Admin issued the
    code (app_users is the existing user table; ON DELETE SET NULL keeps the
    audit row if the user is ever removed). It is nullable.

agent_tokens.installation_id
    Nullable BIGINT FK to collector_installations(id). NULL means "a legacy
    token" and stays fully valid exactly as before: every existing token keeps
    NULL and nothing about how it authenticates changes. A non-NULL value marks
    a token issued by enrollment for that installation; the API then refuses to
    let that token claim a DIFFERENT installation in a heartbeat. ON DELETE
    CASCADE (not SET NULL): SET NULL would silently turn an enrolled credential
    into a legacy-shaped one, so a token dies with its installation instead.

No existing row is modified, and no token is created, changed or revoked.

DEPLOY ORDER: run this migration BEFORE deploying the API code that uses it.
That code reads agent_tokens.installation_id in the token lookup that EVERY
authenticated request (/upload, heartbeats) goes through -- to fail closed for a
token whose bound installation is inactive/retired -- and writes
collector_enrollment_codes from POST /collector/enroll. Deploying the API first
would make authentication error until the migration ran. The migration itself is
purely additive (a new table, a nullable column) and safe to run against the
previous API version.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e91d7a3c58"
down_revision: str | Sequence[str] | None = "d679b80d3a64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "collector_enrollment_codes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "installation_id",
            sa.BigInteger(),
            sa.ForeignKey("collector_installations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "created_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("app_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("code_hash", name="uq_collector_enrollment_codes_code_hash"),
    )

    op.create_index(
        "ix_collector_enrollment_codes_installation_id",
        "collector_enrollment_codes",
        ["installation_id"],
    )

    # The rows that can still be redeemed: what "revoke this installation's
    # previous unused codes" scans.
    op.create_index(
        "ix_collector_enrollment_codes_unused",
        "collector_enrollment_codes",
        ["installation_id"],
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )

    op.add_column(
        "agent_tokens",
        sa.Column(
            "installation_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "collector_installations.id",
                ondelete="CASCADE",
                name="fk_agent_tokens_installation_id",
            ),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_agent_tokens_installation_id",
        "agent_tokens",
        ["installation_id"],
        postgresql_where=sa.text("installation_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_agent_tokens_installation_id", table_name="agent_tokens")
    op.drop_column("agent_tokens", "installation_id")

    op.drop_index("ix_collector_enrollment_codes_unused", table_name="collector_enrollment_codes")
    op.drop_index(
        "ix_collector_enrollment_codes_installation_id", table_name="collector_enrollment_codes"
    )
    op.drop_table("collector_enrollment_codes")
