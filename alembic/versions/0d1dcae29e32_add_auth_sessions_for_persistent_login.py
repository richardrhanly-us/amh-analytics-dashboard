"""add auth_sessions for persistent login

Revision ID: 0d1dcae29e32
Revises: f2a91c7d4e83
Create Date: 2026-09-25 14:31:34.500119

Adds auth_sessions to support persistent (refresh-survives) login for the
Streamlit dashboard, alongside the existing session-only st.session_state
auth. Preserves the existing custom auth system entirely -- this is a new,
separate table, not a change to app_users, password_reset_tokens, or
auth_audit_log.

Design (see second-opinion architecture review for full rationale):
    - Only SHA-256(token) is ever stored (token_hash); the raw opaque
      session token lives in a browser cookie only, never in this table
      or in any log.
    - A session is revoked by setting revoked_at, never by deleting the
      row (auditability; matches password_reset_tokens' used_at pattern).
    - Multiple concurrent sessions per user are allowed by design (no
      unique constraint on user_id) -- a new login never revokes a prior
      session on another device.
    - No RLS: this table has no customer_id/branch_id and is not part of
      the tenant-scoped operational domain 0acba192bf69 covers -- a
      session belongs to a user, who may belong to multiple orgs/branches
      via memberships, so there is no single tenant to scope a policy
      against.

Runtime-role grant. sortview_app is the shared runtime credential for both
the Streamlit dashboard and the FastAPI backend (verified against both
services' live DATABASE_URL). Tables in this repo are owned by the schema
owner, not by sortview_app, so a newly created table is invisible to the
running application until explicitly granted -- see 67d06f4ccd24's
discovery that an already-deployed grant script had under-provisioned
checkins_clean/rejects_clean, which would have made POST /upload fail
outright. This migration grants SELECT, INSERT, UPDATE on auth_sessions
(never DELETE -- revocation is UPDATE revoked_at, matching every other
table in this repo having no DELETE grant) and USAGE on its BIGSERIAL
sequence (nextval() needs USAGE only, not SELECT -- see
tests/test_rls_phase1_postgres.py's own runtime-role setup, which grants
USAGE-only on every table's _id_seq). Guarded by the same pg_roles
existence check as 67d06f4ccd24's _role_exists, so this migration is safe
to run against a fresh database where sortview_app does not exist yet.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0d1dcae29e32"
down_revision: str | Sequence[str] | None = "f2a91c7d4e83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNTIME_ROLE = "sortview_app"


def _role_exists(bind, role_name: str) -> bool:
    row = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
        {"name": role_name},
    ).first()
    return row is not None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("app_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
    )

    op.create_index(
        "ix_auth_sessions_user_id",
        "auth_sessions",
        ["user_id"],
    )

    bind = op.get_bind()
    if _role_exists(bind, _RUNTIME_ROLE):
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON TABLE public.auth_sessions TO {_RUNTIME_ROLE}"
        )
        op.execute(
            f"GRANT USAGE ON SEQUENCE public.auth_sessions_id_seq TO {_RUNTIME_ROLE}"
        )


def downgrade() -> None:
    """Downgrade schema. Revokes sortview_app's grants first (if the role
    exists), then drops the index, then the table -- symmetric with
    upgrade()'s order and matching 67d06f4ccd24's downgrade style of being
    fully explicit rather than relying on DROP TABLE's automatic privilege
    cleanup. The explicit REVOKEs are not load-bearing (PostgreSQL revokes
    all privileges on an object automatically when it's dropped), but they
    keep this migration's upgrade/downgrade symmetric and auditable."""
    bind = op.get_bind()
    if _role_exists(bind, _RUNTIME_ROLE):
        op.execute(
            f"REVOKE SELECT, INSERT, UPDATE ON TABLE public.auth_sessions FROM {_RUNTIME_ROLE}"
        )
        op.execute(
            f"REVOKE USAGE ON SEQUENCE public.auth_sessions_id_seq FROM {_RUNTIME_ROLE}"
        )

    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
