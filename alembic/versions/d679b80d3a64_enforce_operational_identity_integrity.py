"""enforce operational identity integrity

Revision ID: d679b80d3a64
Revises: fb984ee6c56c
Create Date: 2026-09-19 09:00:00.000000

Database-level guarantees for the SaaS <-> operational identity bridge.

IDENTITY MODEL (verified against live production data before this revision):

    organizations.id                    SaaS organization ID
    customers.id                        operational ingestion customer ID
    organizations.operational_customer_id  -> customers.id
    branches.id                         SaaS branch row AND the underlying
                                        operational branch identity
    branches.operational_branch_id      == branches.id, once provisioned
                                        (there is no operational-branches
                                        table; checkins/rejects.branch_id
                                        already have a real FK to branches.id)

Collector/API scope is the operational pair
(organizations.operational_customer_id, branches.operational_branch_id).

Until now organizations.operational_customer_id and
branches.operational_branch_id were bare nullable INTEGERs with no
constraint of any kind, so nothing stopped a bridge value from pointing at
a customer that does not exist, or two organizations from claiming the same
operational customer (which would expose one tenant's data to the other,
because the dashboard scopes every read by the operational customer_id).

WHAT THIS ADDS

1. fk_organizations_operational_customer_id
   organizations.operational_customer_id -> customers(id). NULL stays legal
   (an organization that has not been operationally provisioned yet). The
   default NO ACTION means a customers row cannot be deleted while an
   organization points at it.

2. uq_organizations_operational_customer_id
   UNIQUE INDEX on organizations(operational_customer_id) WHERE
   operational_customer_id IS NOT NULL. A partial index because many
   organizations legitimately have NULL at once; only real mappings must be
   one-to-one.

3. ck_branches_operational_branch_id_matches_id
   CHECK (operational_branch_id IS NULL OR operational_branch_id = id).

DECISION: NO separate unique index on branches.operational_branch_id.
With the CHECK above, every non-NULL operational_branch_id equals that
row's own id, and branches.id is the primary key, hence already unique.
Two branches therefore cannot share an operational_branch_id, and the value
can never dangle (it equals a row that exists by definition). A unique index
would enforce nothing the PRIMARY KEY plus the CHECK do not, while adding a
second index to maintain on every write. It would also need an FK to be
"complete", which the self-equality CHECK makes pointless.

SAFETY AGAINST EXISTING DATA: live data was checked and is compatible
(NBPL is organization 1 -> customers row 1 and branch 1 -> 1; the
clean-install test tenant has NULL in both bridge columns, which every
constraint here permits). Each statement still validates existing rows, so
if incompatible data ever exists, this migration FAILS LOUDLY and rolls
back rather than silently accepting it. The tables involved are tiny, so
plain (non-CONCURRENT) DDL is used.

No data is changed by this migration. No primary key is changed, and no
table is created or dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d679b80d3a64"
down_revision: str | Sequence[str] | None = "fb984ee6c56c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_foreign_key(
        "fk_organizations_operational_customer_id",
        "organizations",
        "customers",
        ["operational_customer_id"],
        ["id"],
    )

    op.create_index(
        "uq_organizations_operational_customer_id",
        "organizations",
        ["operational_customer_id"],
        unique=True,
        postgresql_where=sa.text("operational_customer_id IS NOT NULL"),
    )

    op.create_check_constraint(
        "ck_branches_operational_branch_id_matches_id",
        "branches",
        "operational_branch_id IS NULL OR operational_branch_id = id",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "ck_branches_operational_branch_id_matches_id",
        "branches",
        type_="check",
    )

    op.drop_index(
        "uq_organizations_operational_customer_id",
        table_name="organizations",
    )

    op.drop_constraint(
        "fk_organizations_operational_customer_id",
        "organizations",
        type_="foreignkey",
    )
