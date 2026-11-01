"""add an optional owning installation to ingest keys

Revision ID: c1e7a94f2b60
Revises: f7b3d2c81a46
Create Date: 2026-10-31 12:00:00.000000

C6a M1 (docs/c6a-replacement-machine-design.md, Section 4): a Contract v2 ingest key may name the one installation that owns
it. Nothing writes an owner yet, so every key, existing and new, keeps today's behavior.

    ingest_key_ids.installation_id                     BIGINT, NULL = no recorded owner
    fk_ingest_key_ids_installation_branch              (installation_id, branch_id) -> collector_installations (id, branch_id)
    uq_collector_installations_id_branch_id            UNIQUE (id, branch_id), which that foreign key requires

WHY THE FOREIGN KEY IS COMPOSITE. A key on `installation_id` alone would accept any installation of any tenant, and row level
security would not stop it: its policies test the key's own customer_id and branch_id, and a foreign-key check ignores row
level security. Both branch_id columns hold branches.id (ck_branches_operational_branch_id_matches_id), so the composite key
makes an owner in another branch impossible, and a branch belongs to one organization, so that also rules out another tenant.
While a key names it, an installation cannot move to another branch either. id is the primary key, so the new UNIQUE
constraint cannot fail on existing rows and adds no rule of its own.

NO BACKFILL. A NULL installation_id skips the foreign-key check (MATCH SIMPLE), and every existing row stays NULL. Which
installation owns an existing key is an explicit reconciliation step, never inferred here.

NO ON DELETE ACTION. The default NO ACTION: deleting an installation must never delete a key, and it is refused while a key
names it. A tenant purge is unaffected, because it deletes ingest_key_ids (order 40) before collector_installations (order 52;
services.data_lifecycle_policy). A key's owner survives the key's retirement, as history.

NO OTHER INDEX. Owner checks read the key through its existing unique index on key_id, and the table holds one row per issued
key, so nothing here needs an index on the owner. A one-active-key-per-branch index is a later milestone: it needs an
inventory of the existing data first.

NO GRANT, NO POLICY. As in c8d5f2a47e91: the runtime role's table-level grants cover a column added later, and the row level
security policies are per row. scripts/runtime_role_privileges.py needs no change.

LOCKS. Every agent request reads both tables, and these statements take ACCESS EXCLUSIVE on each until the transaction commits.
The tables are tiny and no row is rewritten, so the locks are held for moments once granted. What could hurt is the wait: a
migration queued behind a long transaction would make every later agent request queue behind it. lock_timeout makes it fail
and roll back instead, leaving the schema unchanged, so it can simply be run again. SET LOCAL ends with the transaction.
Plain (non-CONCURRENT) DDL, as for every change to these tables.

DEPLOY ORDER.
    1. apply this migration;
    2. deploy the API, whose key check reads the new column on every Contract v2 request.
Step 2 must never come first: every Contract v2 request would fail. The migration is safe to run against the previous API
version, which names its columns and never reads this one.

DOWNGRADE drops the foreign key, the column and the unique constraint, and with them any owner recorded. Roll the API back
first. A later migration or running code may rely on ownership, so a downgrade with owners present is a deliberate decision.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1e7a94f2b60"
down_revision: str | Sequence[str] | None = "f7b3d2c81a46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_LOCK_TIMEOUT)
    op.execute("""
        ALTER TABLE collector_installations
        ADD CONSTRAINT uq_collector_installations_id_branch_id UNIQUE (id, branch_id)
    """)
    op.execute("ALTER TABLE ingest_key_ids ADD COLUMN installation_id BIGINT")
    op.execute("""
        ALTER TABLE ingest_key_ids
        ADD CONSTRAINT fk_ingest_key_ids_installation_branch
        FOREIGN KEY (installation_id, branch_id) REFERENCES collector_installations (id, branch_id)
    """)


def downgrade() -> None:
    """Downgrade schema. Removes exactly what upgrade added; no other column, row or constraint is touched."""
    op.execute(_LOCK_TIMEOUT)
    op.execute("ALTER TABLE ingest_key_ids DROP CONSTRAINT fk_ingest_key_ids_installation_branch")
    op.execute("ALTER TABLE ingest_key_ids DROP COLUMN installation_id")
    op.execute("ALTER TABLE collector_installations DROP CONSTRAINT uq_collector_installations_id_branch_id")
