"""add removed_at to memberships

Revision ID: e3a7b1c9d4f2
Revises: 16b41d730e15
Create Date: 2026-10-07 12:00:00.000000

Lets a person be removed from ONE organization without deleting anything and without touching their account. PURELY
ADDITIVE: one NULLable column on `memberships`. No row is rewritten, and no existing column, constraint, index or grant
is touched.

    removed_at   TIMESTAMPTZ   NULL      the membership is ACTIVE: it gives its user that role in that organization
                               not NULL  the membership was REMOVED at that instant: it gives no access at all

WHY A COLUMN AND NOT A DELETE. The runtime role never holds DELETE on anything (scripts/runtime_role_privileges.py:
"Revocation is always an UPDATE"), so the only way an organization's administrator could take someone's access away was
to switch off the person's whole account (app_users.is_active) -- which also locked them out of every other organization
they belong to. With this column, removal is an UPDATE of the one membership row, and app_users.is_active goes back to
meaning only what it says: the ACCOUNT is usable.

NO BACKFILL. Every existing membership has removed_at NULL and is exactly as active as it was.

UNIQUE (organization_id, user_id) STAYS. A person has at most one membership row per organization, removed or not.
Adding a removed person back is an UPDATE of that row -- removed_at back to NULL, role set to the role now being given --
never a second row. The role left on a removed row is history, and gives nobody anything.

NO INDEX. Every query that reads a membership already finds it through the (organization_id, user_id) unique index or by
user; removed_at is then one more condition on a handful of rows.

NO GRANT, NO POLICY. The runtime role already holds table-level SELECT, INSERT and UPDATE on memberships, which cover a
column added later. It still cannot delete a membership. scripts/runtime_role_privileges.py needs no change.

DEPLOY ORDER. Apply this migration BEFORE deploying the application that reads the column: every query that decides
access now requires `removed_at IS NULL` and fails against a table without it. The migration itself is safe to run under
the previous application version, which names its columns and never selects `*` from memberships.

Plain (non-CONCURRENT) DDL: adding a NULLable column with no default is a catalog-only change.

DOWNGRADE drops the column -- AND WITH IT THE FACT THAT ANYONE WAS REMOVED. Every membership that had been removed
becomes an active membership again, with the role it last held: people who were deliberately taken out of an organization
get their access back. Before downgrading a database in which removals have happened, list them
(SELECT organization_id, user_id, role, removed_at FROM memberships WHERE removed_at IS NOT NULL) and decide what should
become of each; the previous application version can only take access away by deactivating the whole account.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3a7b1c9d4f2"
down_revision: str | Sequence[str] | None = "16b41d730e15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE memberships ADD COLUMN removed_at TIMESTAMPTZ")


def downgrade() -> None:
    """Downgrade schema. Removed memberships become active again: see the module docstring."""
    op.execute("ALTER TABLE memberships DROP COLUMN removed_at")
