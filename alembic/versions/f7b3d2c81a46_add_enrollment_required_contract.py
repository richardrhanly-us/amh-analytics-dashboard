"""add required_contract to collector installations and enrollment codes

Revision ID: f7b3d2c81a46
Revises: e3a7b1c9d4f2
Create Date: 2026-10-11 00:00:00.000000

Onboarding Stage A1: the SERVER decides which ingest contract an enrollment permits, so a new external customer can be
enrolled on Contract v2 only and an installer that cannot do v2 is refused at enrollment -- not later, at /upload.

    collector_installations.required_contract     SMALLINT NOT NULL DEFAULT 1, CHECK (required_contract IN (1, 2))
        The contract this installation must be enrolled on. 1 is the legacy contract (v1); 2 is Contract v2.

    collector_enrollment_codes.required_contract  SMALLINT NOT NULL DEFAULT 1, CHECK (required_contract IN (1, 2))
        A SNAPSHOT of the installation's value, taken when the code is generated and never changed afterwards: what a
        code permits cannot move under it, and the row records what it permitted.

EVERY EXISTING ROW BECOMES 1, AND SO DOES EVERY NEW ONE THAT DOES NOT SAY OTHERWISE. Nothing about an existing
installation, an existing code or an existing enrollment changes: contract 1 is exactly today's behavior. An
installation is contract 2 only when it is created so, explicitly. Making contract 2 the normal choice for a new
installation is a later stage (it needs an installer that can enroll on it); this migration deliberately does not.

ONE GRANT. A successful contract-2 enrollment records the branch's initial v2 cutover in the same transaction, and the
API connects as the runtime role, which until now could only read v2_cutovers:

    GRANT INSERT ON TABLE public.v2_cutovers TO sortview_app
    GRANT USAGE ON SEQUENCE public.v2_cutovers_id_seq TO sortview_app

NO UPDATE AND NO DELETE: the table stays append-only for the application, as it is for an operator. v2_cutovers has no
row level security (see f2a91c7d4e83), so this INSERT is scoped by the code that performs it
(services.collector_enrollment_service: only the enrolling tenant's own branch, and only when the branch has no cutover
row at all). Database-level scoping of that insert is future hardening, not part of this migration. The grant is
guarded by the same pg_roles existence check as 0d1dcae29e32 and a7c4e19d5b02, so the migration runs unchanged against
a fresh database where sortview_app does not exist yet.

DEPLOY ORDER: run this migration BEFORE deploying the API code that uses it. That code reads required_contract in every
enrollment redemption and every code generation; deployed first, enrollment would error until the migration ran. The
migration itself is additive (two columns with a constant default, one grant) and safe to run against the previous API
version, which neither reads the columns nor inserts a cutover.

DOWNGRADE revokes the grant and drops the two columns with their constraints. Cutover rows an enrollment already
recorded are ordinary v2_cutovers rows and are left exactly as they are; what is lost is which installations and codes
required contract 2.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7b3d2c81a46"
down_revision: str | Sequence[str] | None = "e3a7b1c9d4f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNTIME_ROLE = "sortview_app"
_TABLES = ("collector_installations", "collector_enrollment_codes")


def _role_exists(bind, role_name: str) -> bool:
    row = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
        {"name": role_name},
    ).first()
    return row is not None


def upgrade() -> None:
    """Upgrade schema."""
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN required_contract SMALLINT NOT NULL DEFAULT 1")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_required_contract_chk CHECK (required_contract IN (1, 2))"
        )

    bind = op.get_bind()
    if _role_exists(bind, _RUNTIME_ROLE):
        op.execute(f"GRANT INSERT ON TABLE public.v2_cutovers TO {_RUNTIME_ROLE}")
        op.execute(f"GRANT USAGE ON SEQUENCE public.v2_cutovers_id_seq TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    """Downgrade schema. Revokes the runtime role's INSERT on v2_cutovers (if the role exists), then drops the two
    columns and their constraints. No v2_cutovers row is touched."""
    bind = op.get_bind()
    if _role_exists(bind, _RUNTIME_ROLE):
        op.execute(f"REVOKE INSERT ON TABLE public.v2_cutovers FROM {_RUNTIME_ROLE}")
        op.execute(f"REVOKE USAGE ON SEQUENCE public.v2_cutovers_id_seq FROM {_RUNTIME_ROLE}")

    for table in reversed(_TABLES):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {table}_required_contract_chk")
        op.execute(f"ALTER TABLE {table} DROP COLUMN required_contract")
