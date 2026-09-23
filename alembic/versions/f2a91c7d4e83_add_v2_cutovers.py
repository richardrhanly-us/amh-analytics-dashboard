"""add v2 cutovers

Revision ID: f2a91c7d4e83
Revises: 0acba192bf69
Create Date: 2026-09-23 00:00:00.000000

Government-readiness data-lifecycle audit, mixed-era read model. PURELY ADDITIVE: one new table and its index. No v1 or v2
event table, trigger, view, or constraint is created, altered or dropped, so nothing about the running v1 API, the deployed
v1 collectors, or the (still feature-gated-off) v2 ingestion path is affected by this migration.

    v2_cutovers   one row per OPERATOR ACTION that sets or changes a branch's v1-to-v2 dashboard read boundary.

This is deliberately an APPEND-ONLY audit log, not a single mutable column on collector_installations or
organization_settings/branch_settings: a mutable column would silently lose the prior value (and who changed it, and when)
every time an operator adjusted the boundary. Every set, rollback, or re-cutover is instead a new row, so the full history
of that branch's transition is always reconstructable.

The EFFECTIVE cutover for a (customer_id, branch_id) pair is the `cutover_at` value of its most recent row by `set_at`
(see src/data_loader.py::load_v2_cutover). A branch with no rows at all, or whose latest row has `cutover_at IS NULL`, is
v1-only: the dashboard's mixed-era read model must fall back to today's unmodified all-v1 behavior for that branch, so a
branch that is never piloted sees zero change from this migration.

`cutover_at IS NULL` on the latest row is the explicit ROLLBACK/reversion state (distinct from "never cut over", which is
"no rows at all"): an operator who needs to back a pilot branch back out to v1-only inserts a new row with `cutover_at`
NULL and a `note` explaining why, rather than deleting or editing the row that set the original cutover. Re-cutover after
a rollback is the same action as the first cutover: insert another new row with a new non-NULL `cutover_at`.

`cutover_at` is TIMESTAMPTZ (UTC) and is the authoritative boundary requested by the operator -- it is NEVER inferred from
the earliest v2 event actually observed for a branch, so a stray early v2 dry-run/test row can never shift where the
dashboard draws the line. The read model must apply it as: v1 rows with event_time < cutover_at, v2 rows with
event_time >= cutover_at (strictly-before / at-or-after, matching this migration's own comment above and the read model's
tests).

customer_id/branch_id reference customers(id)/branches(id) -- the same OPERATIONAL identity used by checkin_events,
reject_events, acs_item_events and acs_events/checkins/rejects, and by the RLS session variables the read model already
sets on every scoped query -- not organizations.id/the SaaS branches row id that collector_installations uses. This table
must be scoped exactly like the event tables it governs, or its boundary would not describe the same tenant a read model
query is actually scoped to.

No RLS is added here, matching collector_installations (an operations/admin record, not itself patron- or
customer-event data) rather than the operational event tables. Every reader and writer in this codebase scopes it by
bound customer_id/branch_id parameters regardless.

Downgrade drops the table. That is safe while it is unused (before any pilot sets a cutover) and DESTROYS the audit trail
of every cutover/rollback decision once an operator has recorded one -- exactly like d3f1a8c95b27's downgrade note for the
v2 event tables it governs.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a91c7d4e83"
down_revision: str | Sequence[str] | None = "0acba192bf69"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE TABLE v2_cutovers (
            id BIGSERIAL PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            branch_id INTEGER NOT NULL REFERENCES branches(id),
            cutover_at TIMESTAMPTZ,
            set_by TEXT NOT NULL,
            set_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            note TEXT,
            CONSTRAINT v2_cutovers_set_by_not_blank_chk CHECK (btrim(set_by) <> '')
        )
    """)
    op.execute("""
        CREATE INDEX v2_cutovers_scope_idx ON v2_cutovers (customer_id, branch_id, set_at DESC)
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS v2_cutovers_scope_idx")
    op.execute("DROP TABLE IF EXISTS v2_cutovers")
