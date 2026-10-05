"""add server-stamped report times to pipeline_status

Revision ID: 16b41d730e15
Revises: c8d5f2a47e91
Create Date: 2026-10-05 12:00:00.000000

Two server-stamped instants for the legacy pipeline status row, so that "when did this branch last report, and which of
its two signals was reported last" has an answer that does not depend on any collector's clock. PURELY ADDITIVE: two
NULLable columns on `pipeline_status`. No row is rewritten, no existing column, constraint, index, policy, trigger or
grant is touched, and no other table is involved.

    status_reported_at          TIMESTAMPTZ   The server's clock when it last accepted a report carrying the per-run
                                              `status` (a scheduled run's result, a run start, an install probe).
    health_status_reported_at   TIMESTAMPTZ   The server's clock when it last accepted a report carrying the continuous
                                              agent's `health_status`.

WHY TWO. One `pipeline_status` row merges two independent signal families -- the per-run fields and the heartbeat fields
(9a39e1b9ed07) -- written by different programs that each update only their own columns. A single "last report" time
could not say whose report it was, so it could not say which of `status` and `health_status` is the current one.

WHY NOT THE COLUMNS THAT ALREADY EXIST. `last_attempt`, `last_run` and `updated_at` are TIMESTAMP WITHOUT TIME ZONE.
The first two are whatever wall-clock reading the writer sent, and the writers do not agree on a clock (the scheduled
Collector sends UTC, the legacy agent local time); `updated_at` is the server's clock in whatever time zone the writing
session had. None of them is an instant. Those three columns are NOT changed by this migration: the dashboard and the
operator health check keep reading them exactly as they do today.

NO BACKFILL, NO DEFAULT. Both columns are NULL for every existing row, and stay NULL until the server stamps them. No
existing value is trustworthy enough to manufacture an instant from, and a wrong instant is worse than none: NULL means
"no report has been stamped", which is true. There is deliberately no server default either -- a default would stamp a
row once, when it is inserted, and then never move, which would read as a collector that reported once and stopped.

THIS MIGRATION STAMPS NOTHING. Nothing writes these columns until the API that sets them is deployed; they are never
part of a collector's or an agent's payload, and no request field corresponds to them.

NO INDEX. The row is read by its primary key, (customer_id, branch_id): one row per branch.

NO GRANT, NO POLICY. The runtime role already holds table-level SELECT, INSERT and UPDATE on pipeline_status, which cover
columns added later (there are no column-level grants), so scripts/runtime_role_privileges.py needs no change.
pipeline_status is outside row level security (0acba192bf69 leaves it out on purpose) and this migration does not change
that: a read of it is scoped only by its own customer_id and branch_id filter.

DEPLOY ORDER.
    1. apply this migration;
    2. only then deploy the API that stamps these columns.
Step 2 must never come first: an API that names a column the database does not have fails every pipeline status report
it receives. The other direction is safe -- this migration can run against the previous API version, which reads and
writes named columns only and simply leaves the new ones NULL.

Plain (non-CONCURRENT) DDL: adding NULLable columns with no default is a catalog-only change, and the table holds one row
per branch.

Downgrade drops the two columns, and with them whatever instants were stored. Roll the API back first: once it stamps
these columns, its pipeline status upsert names them.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "16b41d730e15"
down_revision: str | Sequence[str] | None = "c8d5f2a47e91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE pipeline_status ADD COLUMN status_reported_at TIMESTAMPTZ")
    op.execute("ALTER TABLE pipeline_status ADD COLUMN health_status_reported_at TIMESTAMPTZ")


def downgrade() -> None:
    """Downgrade schema. Drops the two columns (and the instants stored in them); nothing else."""
    for column in ("health_status_reported_at", "status_reported_at"):
        op.execute(f"ALTER TABLE pipeline_status DROP COLUMN IF EXISTS {column}")
