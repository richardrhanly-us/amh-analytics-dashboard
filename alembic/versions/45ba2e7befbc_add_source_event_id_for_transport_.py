"""add source event id for transport idempotency

Revision ID: 45ba2e7befbc
Revises: 9a39e1b9ed07
Create Date: 2026-09-10 20:03:36.551736

Continuous Ingestion Phase E. Adds transport/source idempotency alongside
the EXISTING semantic deduplication -- not a replacement for it. See the
Phase E report for the full audit; short version:

  - checkins already has a UNIQUE INDEX on (barcode, event_time), rejects
    on (barcode, event_time, error_message), acs_events on
    (event_time, message_code, barcode_key). All three are enforced via
    `ON CONFLICT ... DO NOTHING` in main.py's /upload handler and
    genuinely protect against the SAME LOGICAL AMH transaction arriving
    twice through different paths (legacy scheduled agent, new continuous
    agent, historical replay) -- this migration does not touch, weaken,
    or replace any of that.
  - source_event_id is a NEW, ORTHOGONAL protection: a deterministic hash
    of (durable local agent installation id, logical source, logical
    source generation, source-record start byte offset) -- see
    agent/event_identity.py. It recognizes "this exact physical source
    record was already durably captured," independent of what the
    parsed row's own fields happen to be. Two different protections for
    two different failure modes, deliberately layered rather than merged.

Nullable, additive, backward-compatible: the currently deployed legacy
15-minute scheduled agent (agent/SortViewAgent - What is currently
sitting on the AMH computer/) does not send source_event_id at all and
needs no code change -- every existing/legacy row simply has NULL here,
and NULL is explicitly excluded from the new partial unique index below
(a NULL never collides with another NULL, and the partial index doesn't
even contain NULL rows), so legacy uploads are completely unaffected.

Index design decision, REVISED after further review: the partial unique
index is on (customer_id, branch_id, source_event_id), NOT on
source_event_id alone. Originally scoped globally, on the reasoning that
source_event_id already encodes a specific agent installation and one
installation belongs to exactly one branch. That reasoning held for
COLLISION BY CHANCE (a uuid4 colliding with another uuid4 is not a
realistic risk at any scale SortView will reach), but missed a more
mundane failure mode: agent_id today (see agent/identity.py) is a
locally-generated file with no remote enrollment/verification. An
operator cloning a machine image, or manually copying
data/agent_identity.json between two installations as a shortcut, would
give two DIFFERENT branches the SAME agent_id. If those two branches'
generation/offset counters ever reach the same values (plausible --
e.g. two freshly (re)installed agents both starting at generation 0,
offset 0), a GLOBAL unique index would then silently DROP one branch's
entirely legitimate, unrelated event as a "duplicate" of the other's --
a real cross-tenant data-loss bug, and a worse one than the semantic-key
scoping bug this same phase's correction fixes below, because it would
be happening inside the mechanism built specifically to add safety.
Scoping by (customer_id, branch_id) closes this: every request is
already authenticated and scoped to exactly one (customer_id,
branch_id) by authenticate_agent() before any INSERT runs (see main.py),
so two different branches can never collide here even if they
accidentally share an agent_id. Within one (customer_id, branch_id),
transport dedup behaves exactly as before -- unaffected for the normal
case this feature exists for (retries/resends from one real
installation).

CHECK (source_event_id IS NULL OR source_event_id ~ '^[0-9a-f]{64}$')
is defense-in-depth: the API layer (main.py's Pydantic field pattern)
already rejects a malformed value before it reaches SQL, but this keeps
the column self-describing and safe even against a future direct-SQL
writer that skips the API. Every existing row is NULL, which trivially
satisfies the check -- no table rewrite/validation cost against existing
data beyond the normal ADD COLUMN + ADD CONSTRAINT NOT VALID / VALIDATE
sequence, which is not needed here since the column itself is brand new
and starts entirely NULL.

checkins_clean / rejects_clean (the trigger-synced "clean" copies -- see
alembic/versions/26397a3947b1's docstring for why these are live, not
dead) are deliberately NOT given this column. Verified first, not
assumed (per the standing full-schema-audit requirement): both sync
triggers (sync_checkins_to_clean, sync_rejects_to_clean) INSERT with an
explicit column list, not SELECT * -- so this ALTER TABLE is invisible
to them and requires no trigger changes. source_event_id is a
transport/ingestion concern for the raw tables, not something the
routing/dashboard-facing "clean" layer needs.

Migration-runtime note, same disclosure as 9a39e1b9ed07: this repo's CI
does not run migrations against a real Postgres instance, and no
Docker/Postgres was available in the execution environment this
migration was authored in either (no local Postgres service, no Docker).
The partial-unique-index-excludes-NULL behavior relied on here is
long-standing, well-documented core PostgreSQL semantics (not a novel or
version-sensitive feature), but running `alembic upgrade head` /
`alembic downgrade -1` against a live disposable Postgres instance
before this reaches production remains an explicit, undischarged
validation gate -- see the Phase E report's Remaining Risks section.

Uses CREATE INDEX CONCURRENTLY (autocommit_block, same pattern as
0a315b52b59f) so index creation does not lock these tables against
writes from the agent/API while running.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '45ba2e7befbc'
down_revision: str | Sequence[str] | None = '9a39e1b9ed07'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = ["checkins", "rejects", "acs_events"]

_UNIQUE_INDEXES = [
    ("checkins_source_event_id_uidx", "checkins"),
    ("rejects_source_event_id_uidx", "rejects"),
    ("acs_events_source_event_id_uidx", "acs_events"),
]

_CHECK_CONSTRAINTS = [
    ("checkins_source_event_id_format_chk", "checkins"),
    ("rejects_source_event_id_format_chk", "rejects"),
    ("acs_events_source_event_id_format_chk", "acs_events"),
]


def upgrade() -> None:
    """Upgrade schema."""
    for table in _TABLES:
        op.add_column(table, sa.Column("source_event_id", sa.Text(), nullable=True))

    for constraint_name, table in _CHECK_CONSTRAINTS:
        op.execute(
            f"""
            ALTER TABLE {table}
            ADD CONSTRAINT {constraint_name}
            CHECK (source_event_id IS NULL OR source_event_id ~ '^[0-9a-f]{{64}}$')
            """
        )

    with op.get_context().autocommit_block():
        for index_name, table in _UNIQUE_INDEXES:
            op.execute(
                f"""
                CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {index_name}
                ON {table} (customer_id, branch_id, source_event_id)
                WHERE source_event_id IS NOT NULL
                """
            )


def downgrade() -> None:
    """Downgrade schema."""
    with op.get_context().autocommit_block():
        for index_name, _table in _UNIQUE_INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")

    for constraint_name, table in _CHECK_CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint_name}")

    for table in _TABLES:
        op.drop_column(table, "source_event_id")
