"""accept the unknown_alert state for Contract v2 ACS item events

Revision ID: 5813f1df32a0
Revises: b8e2d4f61a07
Create Date: 2026-11-18 12:00:00.000000

Hold-count accuracy, decision D4 (docs/hold-count-accuracy.md, section 5.2): a 101 alert record of an alert type the
collector does not recognise is stored as `unknown_alert` -- neither a confirmed hold nor a confirmed non-hold. It has
the non-hold SHAPE (no destination, no classification flag, no ruleset): it carries nothing new, and nothing about the
raw record. Only a collector from 1.0.16 sends it; the server must accept it first, so this runs before any such
collector is deployed.

    acs_item_events_state_chk         state IN ('hold', 'non_hold_101', 'other_code10', 'unknown_alert')
    acs_item_events_state_shape_chk   unknown_alert takes the non-hold shape

Both constraints are only WIDENED: every existing row satisfies the new ones, and no row, column or index changes.
Re-adding a CHECK scans the table under its lock; the table is pseudonymised event rows, so this is brief, and a
5-second lock timeout makes it fail (and roll back whole) rather than wait behind ingest.

Downgrade refuses while any unknown_alert row exists (narrowing the constraints would reject it), as e5a2c7b93d14's
downgrade refuses non-hold rows; otherwise it restores the previous two constraints exactly.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "5813f1df32a0"
down_revision: str | Sequence[str] | None = "b8e2d4f61a07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATE_CHECK = "ALTER TABLE acs_item_events ADD CONSTRAINT acs_item_events_state_chk CHECK (state IN ({states}))"
_SHAPE_CHECK = """
    ALTER TABLE acs_item_events
    ADD CONSTRAINT acs_item_events_state_shape_chk CHECK (
        (state = 'hold'
            AND destination IS NOT NULL AND is_ill IS NOT NULL
            AND is_branch_services IS NOT NULL AND is_collection_services IS NOT NULL)
        OR
        (state IN ({non_hold})
            AND destination IS NULL AND is_ill IS NULL
            AND is_branch_services IS NULL AND is_collection_services IS NULL AND ruleset_id IS NULL)
    )
"""


def _replace_constraints(states: str, non_hold: str) -> None:
    # As b8e2d4f61a07: never queue behind ingest for the table lock; fail fast and retry instead.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE acs_item_events DROP CONSTRAINT acs_item_events_state_shape_chk")
    op.execute("ALTER TABLE acs_item_events DROP CONSTRAINT acs_item_events_state_chk")
    op.execute(_STATE_CHECK.format(states=states))
    op.execute(_SHAPE_CHECK.format(non_hold=non_hold))


def upgrade() -> None:
    """Upgrade schema."""

    _replace_constraints("'hold', 'non_hold_101', 'other_code10', 'unknown_alert'",
                         "'non_hold_101', 'other_code10', 'unknown_alert'")


def downgrade() -> None:
    """Downgrade schema. Refuses if an unknown_alert row exists; otherwise restores e5a2c7b93d14's constraints."""

    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM acs_item_events WHERE state = 'unknown_alert') THEN
                RAISE EXCEPTION 'cannot downgrade: acs_item_events holds unknown_alert rows that the previous constraints reject';
            END IF;
        END
        $$
    """)
    _replace_constraints("'hold', 'non_hold_101', 'other_code10'", "'non_hold_101', 'other_code10'")
