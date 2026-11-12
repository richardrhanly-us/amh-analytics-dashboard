"""add the replacement link and its authorization to Collector installations

Revision ID: b8e2d4f61a07
Revises: c1e7a94f2b60
Create Date: 2026-11-12 12:00:00.000000

C6a R1 (docs/c6a-replacement-machine-design.md, Amendment A, sections 14.2 and 14.2a): an installation created to replace
another names it, with who authorized the replacement and when. The installation row is the durable authorization record;
auth_audit_log records the lifecycle events. Nothing redeems a replacement yet (R2), and no existing row is changed.

    collector_installations.replaces_installation_id             BIGINT, NULL = not a replacement
    collector_installations.replacement_authorized_by_user_id    BIGINT, the authorizing platform administrator
    collector_installations.replacement_authorized_at            TIMESTAMPTZ, when the replacement was authorized
    fk_collector_installations_replaces_same_branch              (replaces_installation_id, branch_id)
                                                                 -> collector_installations (id, branch_id)
    ck_collector_installations_replacement_all_or_none           the three columns are set together or not at all
    ck_collector_installations_replaces_not_self                 an installation never replaces itself
    trg_collector_installations_replacement_write_once           the three columns never change once written

WHY THE FOREIGN KEY IS COMPOSITE. It reuses uq_collector_installations_id_branch_id (c1e7a94f2b60), so a replacement can
only replace an installation at its own branch -- and so, as a branch belongs to one organization, of its own tenant. A
NULL replaces_installation_id skips the check (MATCH SIMPLE): every existing row stays NULL and unchanged. NO ON DELETE
ACTION: a purge deletes a tenant's installations in one statement, and a NO ACTION key is checked at the end of the
statement, so the self-reference never blocks it.

NO FOREIGN KEY ON THE AUTHORIZING USER. replacement_authorized_by_user_id is a historical value, as
tenant_lifecycle_events.actor_user_id is: deleting a user (no application path does) must neither break the all-or-none
CHECK, as ON DELETE SET NULL would, nor erase who authorized the replacement.

WRITE-ONCE, ENFORCED BY THE DATABASE. The runtime role has UPDATE on this table (heartbeats, status changes, renames), so
code convention alone would not make the link immutable. A BEFORE UPDATE OF trigger on the three columns raises when any of
them would change, for every role, the table owner included. It fires only for an UPDATE that names one of them, so a
heartbeat or a status change costs nothing more. An UPDATE that names one and leaves it unchanged is allowed.
The general create path (services.tenant_service.insert_collector_installation) never names these columns; only
replacement authorization (services.admin_installation_service.authorize_replacement) writes them, in its INSERT.

NO INDEX. A branch's replacements are found through the existing collector_installations branch_id index, and a branch
has a handful of installations.

NO GRANT, NO POLICY. The runtime role's table-level grants cover added columns (as for c1e7a94f2b60), and a trigger
function needs no grant to fire. scripts/runtime_role_privileges.py needs no change.

LOCKS. ALTER TABLE takes ACCESS EXCLUSIVE on collector_installations, which every agent request reads, until commit. The
table is tiny and no row is rewritten (nullable columns with no default; the constraints validate over few rows), so the
lock is held for moments once granted. lock_timeout makes a migration queued behind a long transaction fail and roll back
instead of making every agent request wait behind it; it can simply be run again.

DEPLOY ORDER.
    1. apply this migration;
    2. deploy the API, whose installation administration reads the new columns.
Step 2 must never come first: issuing an enrollment code and changing an installation's status would fail. The migration
is safe against the previous API version, which never names these columns.

DOWNGRADE drops the trigger, its function, the constraints and the three columns, and with them every replacement link and
its authorization. Roll the API back first. Downgrading after a replacement was authorized loses that record from the
installation rows; the auth_audit_log events remain.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e2d4f61a07"
down_revision: str | Sequence[str] | None = "c1e7a94f2b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_LOCK_TIMEOUT)
    op.execute("""
        ALTER TABLE collector_installations
            ADD COLUMN replaces_installation_id BIGINT,
            ADD COLUMN replacement_authorized_by_user_id BIGINT,
            ADD COLUMN replacement_authorized_at TIMESTAMPTZ
    """)
    op.execute("""
        ALTER TABLE collector_installations
        ADD CONSTRAINT fk_collector_installations_replaces_same_branch
        FOREIGN KEY (replaces_installation_id, branch_id) REFERENCES collector_installations (id, branch_id)
    """)
    op.execute("""
        ALTER TABLE collector_installations
        ADD CONSTRAINT ck_collector_installations_replacement_all_or_none CHECK (
            (replaces_installation_id IS NULL
             AND replacement_authorized_by_user_id IS NULL
             AND replacement_authorized_at IS NULL)
            OR
            (replaces_installation_id IS NOT NULL
             AND replacement_authorized_by_user_id IS NOT NULL
             AND replacement_authorized_at IS NOT NULL)
        )
    """)
    op.execute("""
        ALTER TABLE collector_installations
        ADD CONSTRAINT ck_collector_installations_replaces_not_self CHECK (replaces_installation_id <> id)
    """)
    op.execute("""
        CREATE FUNCTION collector_installations_replacement_write_once()
         RETURNS trigger
         LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF NEW.replaces_installation_id IS DISTINCT FROM OLD.replaces_installation_id
               OR NEW.replacement_authorized_by_user_id IS DISTINCT FROM OLD.replacement_authorized_by_user_id
               OR NEW.replacement_authorized_at IS DISTINCT FROM OLD.replacement_authorized_at THEN
                RAISE EXCEPTION 'collector_installations replacement columns are write-once';
            END IF;
            RETURN NEW;
        END;
        $function$
    """)
    op.execute("""
        CREATE TRIGGER trg_collector_installations_replacement_write_once
        BEFORE UPDATE OF replaces_installation_id, replacement_authorized_by_user_id, replacement_authorized_at
        ON collector_installations
        FOR EACH ROW
        EXECUTE FUNCTION collector_installations_replacement_write_once()
    """)


def downgrade() -> None:
    """Downgrade schema. Removes exactly what upgrade added; no other column, row or constraint is touched."""
    op.execute(_LOCK_TIMEOUT)
    op.execute("DROP TRIGGER IF EXISTS trg_collector_installations_replacement_write_once ON collector_installations")
    op.execute("DROP FUNCTION IF EXISTS collector_installations_replacement_write_once()")
    op.execute("ALTER TABLE collector_installations DROP CONSTRAINT ck_collector_installations_replaces_not_self")
    op.execute("ALTER TABLE collector_installations DROP CONSTRAINT ck_collector_installations_replacement_all_or_none")
    op.execute("ALTER TABLE collector_installations DROP CONSTRAINT fk_collector_installations_replaces_same_branch")
    op.execute("""
        ALTER TABLE collector_installations
            DROP COLUMN replacement_authorized_at,
            DROP COLUMN replacement_authorized_by_user_id,
            DROP COLUMN replaces_installation_id
    """)
