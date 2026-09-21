"""evolve acs_hold_events into acs_item_events

Revision ID: e5a2c7b93d14
Revises: d3f1a8c95b27
Create Date: 2026-09-22 09:00:00.000000

Privacy Contract v2, server-contract amendment (docs/contract-v2-design.md, section 9).

WHY. The dashboard decides whether an item is a hold by its LATEST ACS item record, and it does so differently in two places:
Overview looks only at `101` records; Live Today looks at every code-10 record. A later non-hold record therefore retracts an
earlier hold. Step 3's hold-only table cannot represent that (the cloud would never learn of the later record), so the table
becomes a stream of ACS item events with a closed, derived `state`:

    hold          a 101 record that is hold-positive (101YNY)          -- carries destination, the three flags, ruleset_id
    non_hold_101  a 101 record that is not a hold                      -- carries ONLY event_key, event_time, item_key, state
    other_code10  any other code-10 record                             -- carries ONLY event_key, event_time, item_key, state

The raw message code is never stored, and message-64 patron records are never events.

WHAT THIS DOES (a new revision; Step 3's revision d3f1a8c95b27 is untouched, so no merged history is rewritten):

  * renames the table acs_hold_events -> acs_item_events, and renames its sequence, primary key, foreign keys, constraints and
    indexes to match, so nothing is left carrying the old name;
  * adds `state` (existing rows are all holds, so they become 'hold'), then makes it NOT NULL;
  * makes destination, is_ill, is_branch_services and is_collection_services NULLable -- only a hold has them;
  * adds CHECKs that state is one of the three values and that the row's SHAPE fits its state: a hold has all four fields, a
    non-hold has NONE of them (and no ruleset_id). A writer that skips the API therefore cannot store a placeholder;
  * replaces the (customer_id, branch_id, item_key) index with (customer_id, branch_id, item_key, event_time), the shape
    "latest record per item" reads.

Nothing else changes: not one v1 table, trigger, view or index is touched, and the other v2 tables are unaffected. The
dedup identity is still UNIQUE (customer_id, branch_id, key_id, event_key).

ORDERING. For items with the same event_time the dashboard needs a deterministic tiebreak. The stored `id` (assigned in the
order the events were inserted, which is the order the collector sent them, which is source-file order) is that tiebreak:
"latest" means the greatest (event_time, id).

DEPLOY ORDER. This amendment is safe only while v2 ingest is OFF (SORTVIEW_V2_INGEST_ENABLED unset), which is true until the
collector cutover: the API code of this amendment and this migration must be deployed together, migration first, because the
previous API code writes the old table name.

DOWNGRADE refuses to run if any non-hold row exists (it would have to destroy retraction data), and otherwise restores the
Step 3 shape and names exactly. Plain (non-CONCURRENT) DDL: the tables are new, and v2 is not enabled.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5a2c7b93d14'
down_revision: str | Sequence[str] | None = 'd3f1a8c95b27'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (old name, new name, kind) for every object that carries the table's name.
_INDEX_RENAMES = (
    ("acs_hold_events_pkey", "acs_item_events_pkey"),
    ("acs_hold_events_event_identity_uidx", "acs_item_events_event_identity_uidx"),
    ("acs_hold_events_scope_time_idx", "acs_item_events_scope_time_idx"),
)
_CONSTRAINT_RENAMES = (
    ("acs_hold_events_customer_id_fkey", "acs_item_events_customer_id_fkey"),
    ("acs_hold_events_branch_id_fkey", "acs_item_events_branch_id_fkey"),
    ("acs_hold_events_key_id_format_chk", "acs_item_events_key_id_format_chk"),
    ("acs_hold_events_event_key_format_chk", "acs_item_events_event_key_format_chk"),
    ("acs_hold_events_item_key_format_chk", "acs_item_events_item_key_format_chk"),
    ("acs_hold_events_destination_format_chk", "acs_item_events_destination_format_chk"),
    ("acs_hold_events_ruleset_id_format_chk", "acs_item_events_ruleset_id_format_chk"),
)


def upgrade() -> None:
    """Upgrade schema."""

    op.execute("ALTER TABLE acs_hold_events RENAME TO acs_item_events")
    op.execute("ALTER SEQUENCE acs_hold_events_id_seq RENAME TO acs_item_events_id_seq")
    for old, new in _INDEX_RENAMES:
        op.execute(f"ALTER INDEX {old} RENAME TO {new}")
    for old, new in _CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE acs_item_events RENAME CONSTRAINT {old} TO {new}")

    # Existing rows (none, while v2 is off) are all holds.
    op.execute("ALTER TABLE acs_item_events ADD COLUMN state TEXT")
    op.execute("UPDATE acs_item_events SET state = 'hold'")
    op.execute("ALTER TABLE acs_item_events ALTER COLUMN state SET NOT NULL")

    # Only a hold has these; a non-hold stores NULL, never a placeholder.
    for column in ("destination", "is_ill", "is_branch_services", "is_collection_services"):
        op.execute(f"ALTER TABLE acs_item_events ALTER COLUMN {column} DROP NOT NULL")

    op.execute("""
        ALTER TABLE acs_item_events
        ADD CONSTRAINT acs_item_events_state_chk CHECK (state IN ('hold', 'non_hold_101', 'other_code10'))
    """)
    op.execute("""
        ALTER TABLE acs_item_events
        ADD CONSTRAINT acs_item_events_state_shape_chk CHECK (
            (state = 'hold'
                AND destination IS NOT NULL AND is_ill IS NOT NULL
                AND is_branch_services IS NOT NULL AND is_collection_services IS NOT NULL)
            OR
            (state IN ('non_hold_101', 'other_code10')
                AND destination IS NULL AND is_ill IS NULL
                AND is_branch_services IS NULL AND is_collection_services IS NULL AND ruleset_id IS NULL)
        )
    """)

    op.execute("DROP INDEX acs_hold_events_scope_item_idx")
    op.execute("""
        CREATE INDEX acs_item_events_scope_item_time_idx
        ON acs_item_events (customer_id, branch_id, item_key, event_time)
    """)


def downgrade() -> None:
    """Downgrade schema. Refuses if a non-hold row exists (it would destroy retraction data); otherwise restores Step 3."""

    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM acs_item_events WHERE state <> 'hold') THEN
                RAISE EXCEPTION 'cannot downgrade: acs_item_events holds non-hold rows that the Step 3 shape cannot store';
            END IF;
        END
        $$
    """)

    op.execute("DROP INDEX acs_item_events_scope_item_time_idx")
    op.execute("CREATE INDEX acs_hold_events_scope_item_idx ON acs_item_events (customer_id, branch_id, item_key)")

    op.execute("ALTER TABLE acs_item_events DROP CONSTRAINT acs_item_events_state_shape_chk")
    op.execute("ALTER TABLE acs_item_events DROP CONSTRAINT acs_item_events_state_chk")
    for column in ("destination", "is_ill", "is_branch_services", "is_collection_services"):
        op.execute(f"ALTER TABLE acs_item_events ALTER COLUMN {column} SET NOT NULL")
    op.execute("ALTER TABLE acs_item_events DROP COLUMN state")

    for old, new in reversed(_CONSTRAINT_RENAMES):
        op.execute(f"ALTER TABLE acs_item_events RENAME CONSTRAINT {new} TO {old}")
    for old, new in reversed(_INDEX_RENAMES):
        op.execute(f"ALTER INDEX {new} RENAME TO {old}")
    op.execute("ALTER SEQUENCE acs_item_events_id_seq RENAME TO acs_hold_events_id_seq")
    op.execute("ALTER TABLE acs_item_events RENAME TO acs_hold_events")
