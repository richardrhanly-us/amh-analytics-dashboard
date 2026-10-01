"""add collector run and schedule diagnostics to ingest_key_ids

Revision ID: c8d5f2a47e91
Revises: a7c4e19d5b02
Create Date: 2026-10-01 12:00:00.000000

Collector run / schedule diagnostics for the dashboard's Pipeline Status panel. PURELY ADDITIVE: four NULLable columns and
two CHECK constraints on `ingest_key_ids`, the table that already holds a tenant's latest Contract v2 heartbeat snapshot
(d3f1a8c95b27). No row is rewritten, no existing column, constraint, index, policy, trigger or grant is touched, and no v1
table (`pipeline_status` included) is involved.

    collector_last_run_at       TIMESTAMPTZ   Windows Task Scheduler's LastRunTime for the "SortView Collector" task:
                                              the scheduler's most recent launch of it, however that launch was started.
    collector_next_run_at       TIMESTAMPTZ   Task Scheduler's NextRunTime: the run Windows currently has scheduled. It is
                                              what Windows reported, never last run + cadence.
    collector_run_duration_ms   INTEGER       Monotonic elapsed time of the collector invocation that sent the heartbeat,
                                              from the process's start to the construction of the heartbeat (the heartbeat
                                              POST itself is the one thing not included). 0 .. 86,400,000.
    collector_schedule_status   TEXT          healthy | task_missing | task_disabled | no_next_run | query_failed

All four are NULL until a collector that reports them sends a heartbeat, and are set back to NULL by a heartbeat from an
older collector that does not (every heartbeat is a full snapshot). NULL means "not reported", and the dashboard shows it
as such.

`collector_schedule_status` is TEXT + CHECK, the same pattern as health_status and last_error_class on this table. The value
list is duplicated from src/services/ingest_v2_models.py on purpose (a migration is a fixed historical record and must not
import app code); tests/test_collector_run_diagnostics_migration.py fails if they drift apart.

NO GRANT, NO POLICY. The runtime role already holds table-level SELECT, INSERT and UPDATE on ingest_key_ids, which cover
columns added later, and the row level security policies on the table are per ROW (customer_id, branch_id), so they apply
to these columns as they do to every other. scripts/runtime_role_privileges.py needs no change.

DEPLOY ORDER.
    1. apply this migration;
    2. deploy the API and dashboard that accept, store and read the new fields;
    3. only then deploy a Collector release that sends them.
Step 3 must never come first: the previous API's heartbeat model forbids unknown fields, so a new Collector's heartbeat
would be rejected whole (422) and the tenant's v2 status would stop updating. The other direction is safe at every step --
the new API accepts an older Collector's heartbeat, and stores NULL for the fields it does not send. This migration is
safe to run against the previous API version (it reads and writes named columns only).

Plain (non-CONCURRENT) DDL: adding NULLable columns with no default is a catalog-only change, and the table holds one row
per issued key.

Downgrade drops the two constraints and the four columns, and with them whatever diagnostics were stored. Roll the
Collector and the API back first: the new API's heartbeat UPDATE names these columns.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d5f2a47e91"
down_revision: str | Sequence[str] | None = "a7c4e19d5b02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEDULE_STATUSES = ("healthy", "task_missing", "task_disabled", "no_next_run", "query_failed")
MAX_RUN_DURATION_MS = 86_400_000


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE ingest_key_ids ADD COLUMN collector_last_run_at TIMESTAMPTZ")
    op.execute("ALTER TABLE ingest_key_ids ADD COLUMN collector_next_run_at TIMESTAMPTZ")
    op.execute("ALTER TABLE ingest_key_ids ADD COLUMN collector_run_duration_ms INTEGER")
    op.execute("ALTER TABLE ingest_key_ids ADD COLUMN collector_schedule_status TEXT")

    op.execute(f"""
        ALTER TABLE ingest_key_ids
        ADD CONSTRAINT ingest_key_ids_collector_run_duration_ms_chk CHECK (
            collector_run_duration_ms IS NULL
            OR (collector_run_duration_ms >= 0 AND collector_run_duration_ms <= {MAX_RUN_DURATION_MS})
        )
    """)
    statuses = ", ".join(f"'{status}'" for status in SCHEDULE_STATUSES)
    op.execute(f"""
        ALTER TABLE ingest_key_ids
        ADD CONSTRAINT ingest_key_ids_collector_schedule_status_chk CHECK (
            collector_schedule_status IS NULL OR collector_schedule_status IN ({statuses})
        )
    """)


def downgrade() -> None:
    """Downgrade schema. Drops the two constraints and the four columns (and the diagnostics stored in them)."""
    op.execute("ALTER TABLE ingest_key_ids DROP CONSTRAINT IF EXISTS ingest_key_ids_collector_schedule_status_chk")
    op.execute("ALTER TABLE ingest_key_ids DROP CONSTRAINT IF EXISTS ingest_key_ids_collector_run_duration_ms_chk")
    for column in ("collector_schedule_status", "collector_run_duration_ms", "collector_next_run_at", "collector_last_run_at"):
        op.execute(f"ALTER TABLE ingest_key_ids DROP COLUMN IF EXISTS {column}")
