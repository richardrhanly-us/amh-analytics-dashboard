"""secure clean-table sync triggers (SECURITY DEFINER) and revoke sortview_app's direct grants

Revision ID: 67d06f4ccd24
Revises: e5a2c7b93d14
Create Date: 2026-09-22 12:00:00.000000

PRE-GA hardening, discovered while writing the RLS phase 1 tests (see
0acba192bf69, which now revises this migration instead of e5a2c7b93d14
directly). Independent of RLS: this bug exists whether or not RLS is ever
enabled.

THE BUG. sync_checkins_to_clean()/sync_rejects_to_clean() (both created in
26397a3947b1, both SECURITY INVOKER -- the PostgreSQL default, confirmed
empirically against a real database as part of this migration's own
authorship: prosecdef=false, proconfig=NULL for both, before this
migration runs) execute their `INSERT INTO checkins_clean/rejects_clean
... ON CONFLICT (id) DO NOTHING` AS THE CALLING ROLE, not as the function's
owner. PostgreSQL requires SELECT privilege on the target table for ANY
ON CONFLICT clause, DO NOTHING included -- not just DO UPDATE -- because
conflict detection reads the existing row via the arbiter index, even
though DO NOTHING never returns that data to the caller. Confirmed
empirically: a role granted INSERT only succeeds on a plain INSERT into
checkins_clean, but fails with "permission denied for table checkins_clean"
on the identical row via ON CONFLICT (id) DO NOTHING.

The already-deployed production sortview_app grant script gave
checkins_clean/rejects_clean INSERT only (no SELECT). That is insufficient:
POST /upload would fail outright -- independent of RLS -- the moment
sortview_app becomes the runtime credential.

WHY NOT JUST GRANT SELECT. checkins_clean/rejects_clean are outside the RLS
tranche (0acba192bf69 deliberately excludes them -- their customer_id/
branch_id are nullable, unlike checkins/rejects, and nothing in the
application reads them, confirmed by grep across src/, main.py, super_admin/:
zero references anywhere). Granting sortview_app plain SELECT on an
RLS-unprotected table carrying the same patron-checkout-adjacent columns as
checkins/rejects (title, barcode, message, flag_1/2/3, source_file) would
open exactly the unscoped cross-tenant read path the RLS tranche exists to
close, just on a sibling table nobody remembered to include.

THE FIX. SECURITY DEFINER on both trigger functions, with search_path
pinned explicitly (required hardening for any SECURITY DEFINER function
that references objects unqualified, as both of these do -- without a
pinned search_path this would be the textbook search-path-hijack privilege
escalation vector). Both functions are already owned by the migration/
schema-owner role, so no ownership change is needed. Once this lands, the
trigger's INSERT INTO checkins_clean/rejects_clean executes with the
OWNER's privileges regardless of which role fired the triggering INSERT on
checkins/rejects -- sortview_app needs, and after this migration has, NO
direct privilege of any kind (not SELECT, not INSERT) on either clean
table. Its existing INSERT grant (from the already-deployed production
grant script) is explicitly REVOKEd, not left as merely unused.

ROLE MAY NOT EXIST YET. sortview_app is a cluster-level role created and
managed outside Alembic (see the runtime-role-separation design). In
production it already exists, so the REVOKE applies immediately. In a
fresh disposable/test database migrated from scratch, no such role exists
yet at migration time -- the REVOKE (and the downgrade's re-GRANT) are
therefore guarded by a catalog check (pg_roles), matching this project's
existing pattern for conditional, idempotent migration DDL (see
c53c1b536c71's _drop_if_invalid).

DOWNGRADE restores the exact pre-migration state, inspected directly
against a real database rather than assumed: SECURITY INVOKER (the
explicit opposite of SECURITY DEFINER, not merely "unset"), search_path
fully unset via RESET (not SET ... TO DEFAULT, which would instead pin an
explicit default value as a per-function override -- RESET removes the
function's config entry entirely, restoring proconfig to NULL exactly as
it was), and sortview_app's INSERT grant restored on both tables if the
role exists.

Plain DDL: ALTER FUNCTION/REVOKE/GRANT are metadata-only, no table rewrite,
no data touched, safe outside a maintenance window.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '67d06f4ccd24'
down_revision: str | Sequence[str] | None = 'e5a2c7b93d14'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNTIME_ROLE = "sortview_app"
_CLEAN_TABLES = ("checkins_clean", "rejects_clean")
_FUNCTIONS = ("sync_checkins_to_clean", "sync_rejects_to_clean")


def _role_exists(bind, role_name: str) -> bool:
    row = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
        {"name": role_name},
    ).first()
    return row is not None


def upgrade() -> None:
    """Upgrade schema."""
    for function_name in _FUNCTIONS:
        op.execute(f"""
            ALTER FUNCTION public.{function_name}()
            SECURITY DEFINER
            SET search_path = public, pg_temp
        """)

    bind = op.get_bind()
    if _role_exists(bind, _RUNTIME_ROLE):
        for table in _CLEAN_TABLES:
            op.execute(f"REVOKE INSERT ON TABLE public.{table} FROM {_RUNTIME_ROLE}")


def downgrade() -> None:
    """Downgrade schema. Restores the exact pre-migration state: SECURITY
    INVOKER, no per-function search_path override, and sortview_app's
    INSERT grant back on both clean tables (if the role exists)."""
    bind = op.get_bind()
    if _role_exists(bind, _RUNTIME_ROLE):
        for table in _CLEAN_TABLES:
            op.execute(f"GRANT INSERT ON TABLE public.{table} TO {_RUNTIME_ROLE}")

    for function_name in _FUNCTIONS:
        op.execute(f"ALTER FUNCTION public.{function_name}() SECURITY INVOKER")
        op.execute(f"ALTER FUNCTION public.{function_name}() RESET search_path")
