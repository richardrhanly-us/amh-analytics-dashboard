"""add tenant lifecycle events

Revision ID: a7c4e19d5b02
Revises: 0d1dcae29e32
Create Date: 2026-10-01 00:00:00.000000

Government-readiness data-lifecycle/offboarding (docs/data-lifecycle-offboarding.md). PURELY ADDITIVE: one new table, its
index, one trigger function and one trigger. No existing table, row, grant, policy, trigger or view is touched.

    tenant_lifecycle_events   one row per ADMINISTRATIVE LIFECYCLE ACTION on a tenant: the permanent access cutoff
                              (platform_admin_service.offboard_library), a hand-run reversal of an accidental cutoff, and
                              the destructive data purge (scripts/purge_tenant_data.py).

WHY NOT auth_audit_log. That table is authentication-shaped (a user, an e-mail, a login outcome), has no tenant key, and is
itself subject to a future audit-retention policy. A lifecycle record must name the tenant, must be writable by an operator
process that has no authenticated app user, and MUST SURVIVE THE TENANT'S OWN PURGE.

NO FOREIGN KEYS, ON PURPOSE. organization_id, organization_slug, operational_customer_id and actor_user_id are HISTORICAL
SCALAR VALUES: the organizations / customers / app_users rows they describe may be deleted (that is what a purge does), and
the evidence that it happened has to remain. A cascading or blocking FK would either delete the evidence with the tenant or
make the purge impossible.

APPEND-ONLY, TWICE OVER. (Append-only and protected -- not cryptographically immutable or tamper-proof: see the last point.)
  * The runtime role gets exactly two privileges and nothing else: INSERT on the table, and USAGE on
    tenant_lifecycle_events_id_seq. USAGE is required because `id` is BIGSERIAL: its default calls nextval(), which needs
    USAGE on the sequence (not SELECT, not UPDATE). No SELECT is needed because the writer uses no RETURNING and nothing
    in the application reads this table. No UPDATE, DELETE, TRUNCATE, or any schema-level privilege.
    The grant is REQUIRED: recording a cutoff is done by the Super Admin app, which connects as sortview_app (verified
    on 2026-10-01 for the Super Admin deployment and, separately, for the API backend).
  * A BEFORE UPDATE OR DELETE row trigger raises for EVERY role, the table owner included, so the purge tool -- which runs as
    the owner and deletes a great deal -- structurally cannot remove or rewrite lifecycle evidence. (An owner can of course
    drop the trigger; that is a deliberate schema change, not something a DELETE statement can do by accident.)

`details` is NON-SENSITIVE STRUCTURED METADATA ONLY: row counts, internal row ids, status names, a schema revision. Never a
token or its hash, an enrollment code, a patron identifier, a barcode, a title, or raw AMH/ACS content. The writers
(src/services/data_lifecycle_policy.record_tenant_lifecycle_event) validate that before inserting; the CHECK below only
guarantees the value is a JSON object.

`event_type` is a closed list. Adding a type later is a new migration, deliberately: a new kind of lifecycle action is a
policy change worth reviewing.

DEPLOY ORDER: run this migration BEFORE deploying the application code that uses it (offboard_library and the Super Admin
"Offboard Library" control). It is additive and safe against the previous application version. Deployed the other way
round, an offboarding attempt fails on the INSERT into the missing table and its whole transaction rolls back -- closed,
but not the supported order. The operator procedure is in docs/data-lifecycle-offboarding.md ("deployment order").

The runtime-role grant is guarded by the same pg_roles existence check as 0d1dcae29e32, so this migration runs unchanged
against a fresh database where sortview_app does not exist yet.

Downgrade drops the table and DESTROYS every lifecycle record in it -- safe before any tenant has been offboarded, and not
after.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c4e19d5b02"
down_revision: str | Sequence[str] | None = "0d1dcae29e32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNTIME_ROLE = "sortview_app"


def _role_exists(bind, role_name: str) -> bool:
    row = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
        {"name": role_name},
    ).first()
    return row is not None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE TABLE tenant_lifecycle_events (
            id BIGSERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            organization_id BIGINT NOT NULL,
            organization_slug TEXT NOT NULL,
            operational_customer_id INTEGER,
            actor_user_id BIGINT,
            actor_label TEXT NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT tenant_lifecycle_events_event_type_chk CHECK (
                event_type IN ('access_cutoff', 'access_cutoff_reverted', 'purge_executed')
            ),
            CONSTRAINT tenant_lifecycle_events_actor_label_not_blank_chk CHECK (btrim(actor_label) <> ''),
            CONSTRAINT tenant_lifecycle_events_details_object_chk CHECK (jsonb_typeof(details) = 'object')
        )
    """)
    op.execute("""
        CREATE INDEX tenant_lifecycle_events_organization_idx
        ON tenant_lifecycle_events (organization_id, occurred_at DESC)
    """)

    op.execute("""
        CREATE FUNCTION tenant_lifecycle_events_append_only()
         RETURNS trigger
         LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'tenant_lifecycle_events is append-only: % is not permitted', TG_OP;
        END;
        $function$
    """)
    op.execute("""
        CREATE TRIGGER trg_tenant_lifecycle_events_append_only
        BEFORE UPDATE OR DELETE ON tenant_lifecycle_events
        FOR EACH ROW
        EXECUTE FUNCTION tenant_lifecycle_events_append_only()
    """)

    bind = op.get_bind()
    if _role_exists(bind, _RUNTIME_ROLE):
        op.execute(f"GRANT INSERT ON TABLE public.tenant_lifecycle_events TO {_RUNTIME_ROLE}")
        op.execute(f"GRANT USAGE ON SEQUENCE public.tenant_lifecycle_events_id_seq TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    """Downgrade schema. Revokes the runtime role's grants (if the role exists), then drops the trigger, its function,
    the index and the table -- every lifecycle record goes with it."""
    bind = op.get_bind()
    if _role_exists(bind, _RUNTIME_ROLE):
        op.execute(f"REVOKE INSERT ON TABLE public.tenant_lifecycle_events FROM {_RUNTIME_ROLE}")
        op.execute(f"REVOKE USAGE ON SEQUENCE public.tenant_lifecycle_events_id_seq FROM {_RUNTIME_ROLE}")

    op.execute("DROP TRIGGER IF EXISTS trg_tenant_lifecycle_events_append_only ON tenant_lifecycle_events")
    op.execute("DROP FUNCTION IF EXISTS tenant_lifecycle_events_append_only()")
    op.execute("DROP INDEX IF EXISTS tenant_lifecycle_events_organization_idx")
    op.execute("DROP TABLE IF EXISTS tenant_lifecycle_events")
