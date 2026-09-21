"""add contract v2 ingest tables

Revision ID: d3f1a8c95b27
Revises: b4e91d7a3c58
Create Date: 2026-09-21 16:00:00.000000

Privacy Contract v2, server side (docs/contract-v2-design.md). PURELY ADDITIVE: four new tables and their indexes. No v1
table (checkins, rejects, acs_events, their *_clean copies, pipeline_status ...), trigger, view, index or constraint is
created, altered or dropped, so the running v1 API and the deployed v1 collectors are unaffected by this migration.

    ingest_key_ids    server-issued key registry, one row per (customer_id, branch_id, key_id). It holds NO key material:
                      the HMAC secret is generated locally by the collector and never leaves it. It records a non-secret
                      algorithm identifier, a lifecycle state (active / retired) and the latest v2 heartbeat snapshot.
    checkin_events    v2 check-in events.
    reject_events     v2 reject events.
    acs_hold_events   v2 ACS hold events.

The three event tables PHYSICALLY LACK every prohibited legacy column: barcode, title, patron_id, raw_message, any free-text
message, and source_event_id. Nothing in them can hold a patron, a raw barcode or free text, whatever a caller sends.

Time is TIMESTAMPTZ throughout (v2 accepts offset-aware timestamps only).

Deduplication is a UNIQUE index per event table on (customer_id, branch_id, key_id, event_key). The API turns a conflicting
duplicate (same identity, different content) into a 409 rather than ignoring it; the index is what makes the identity
unique and an identical resend a no-op.

CHECK constraints mirror the API's formats (UUIDv4 key ids, 64-hex HMAC keys, lower-case slugs), so a writer that skips the
API still cannot store a raw label or a free-text value. `error_class` is checked against a slug pattern here and against the
closed enum in the API, so adding a class later needs no migration. The patterns below are duplicated from
src/services/ingest_v2_models.py on purpose (a migration is a fixed historical record and must not import app code);
tests/test_ingest_v2_migration.py fails if they ever drift apart.

The event tables deliberately have no foreign key to ingest_key_ids: the key is validated once per request in the API, and
the hot insert path stays free of an extra per-row check. They do reference customers/branches, like v1's checkins.

Downgrade drops the four tables. That is safe while they are empty (the pre-cutover rollback) and DESTROYS v2 data once a
collector has uploaded any.

Plain (non-CONCURRENT) DDL is used: every table is new and empty, so there is nothing to lock or rewrite.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd3f1a8c95b27'
down_revision: str | Sequence[str] | None = 'b4e91d7a3c58'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID4 = "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
HMAC_HEX = "^[0-9a-f]{64}$"
DESTINATION = "^[a-z][a-z0-9_]{0,31}$"
BIN = "^[a-z0-9][a-z0-9_]{0,15}$"
ERROR_CLASS = "^[a-z][a-z_]{0,31}$"

_TABLES = ("checkin_events", "reject_events", "acs_hold_events", "ingest_key_ids")


def upgrade() -> None:
    """Upgrade schema."""

    op.execute(f"""
        CREATE TABLE ingest_key_ids (
            id BIGSERIAL PRIMARY KEY,
            key_id TEXT NOT NULL,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            branch_id INTEGER NOT NULL REFERENCES branches(id),
            algorithm TEXT NOT NULL DEFAULT 'hmac-sha256-v1',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            retired_at TIMESTAMPTZ,
            last_heartbeat_at TIMESTAMPTZ,
            health_status TEXT,
            last_error_class TEXT,
            pending_outbox_count INTEGER,
            quarantined_count INTEGER,
            oldest_pending_event_at TIMESTAMPTZ,
            last_success_at TIMESTAMPTZ,
            watcher_last_active_at TIMESTAMPTZ,
            CONSTRAINT ingest_key_ids_key_id_format_chk CHECK (key_id ~ '{UUID4}'),
            CONSTRAINT ingest_key_ids_algorithm_chk CHECK (algorithm IN ('hmac-sha256-v1')),
            CONSTRAINT ingest_key_ids_status_chk CHECK (status IN ('active', 'retired')),
            CONSTRAINT ingest_key_ids_lifecycle_chk CHECK (
                (status = 'active' AND retired_at IS NULL) OR (status = 'retired' AND retired_at IS NOT NULL)
            ),
            CONSTRAINT ingest_key_ids_health_status_chk CHECK (
                health_status IS NULL OR health_status IN ('healthy', 'degraded', 'error')
            ),
            CONSTRAINT ingest_key_ids_last_error_class_chk CHECK (
                last_error_class IS NULL OR last_error_class IN (
                    'retryable_infra', 'auth_failure', 'permanent_rejection', 'source_unavailable',
                    'configuration_error', 'other'
                )
            ),
            CONSTRAINT ingest_key_ids_pending_outbox_count_chk CHECK (
                pending_outbox_count IS NULL OR (pending_outbox_count >= 0 AND pending_outbox_count <= 10000000)
            ),
            CONSTRAINT ingest_key_ids_quarantined_count_chk CHECK (
                quarantined_count IS NULL OR (quarantined_count >= 0 AND quarantined_count <= 10000000)
            )
        )
    """)
    op.execute("CREATE UNIQUE INDEX ingest_key_ids_key_id_uidx ON ingest_key_ids (key_id)")
    op.execute("CREATE INDEX ingest_key_ids_scope_idx ON ingest_key_ids (customer_id, branch_id, status)")

    op.execute(f"""
        CREATE TABLE checkin_events (
            id BIGSERIAL PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            branch_id INTEGER NOT NULL REFERENCES branches(id),
            key_id TEXT NOT NULL,
            event_key TEXT NOT NULL,
            event_time TIMESTAMPTZ NOT NULL,
            item_key TEXT,
            destination TEXT NOT NULL,
            bin TEXT NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT checkin_events_key_id_format_chk CHECK (key_id ~ '{UUID4}'),
            CONSTRAINT checkin_events_event_key_format_chk CHECK (event_key ~ '{HMAC_HEX}'),
            CONSTRAINT checkin_events_item_key_format_chk CHECK (item_key IS NULL OR item_key ~ '{HMAC_HEX}'),
            CONSTRAINT checkin_events_destination_format_chk CHECK (destination ~ '{DESTINATION}'),
            CONSTRAINT checkin_events_bin_format_chk CHECK (bin ~ '{BIN}')
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX checkin_events_event_identity_uidx
        ON checkin_events (customer_id, branch_id, key_id, event_key)
    """)
    op.execute("CREATE INDEX checkin_events_scope_time_idx ON checkin_events (customer_id, branch_id, event_time)")
    op.execute("""
        CREATE INDEX checkin_events_scope_item_idx
        ON checkin_events (customer_id, branch_id, item_key) WHERE item_key IS NOT NULL
    """)

    op.execute(f"""
        CREATE TABLE reject_events (
            id BIGSERIAL PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            branch_id INTEGER NOT NULL REFERENCES branches(id),
            key_id TEXT NOT NULL,
            event_key TEXT NOT NULL,
            event_time TIMESTAMPTZ NOT NULL,
            error_class TEXT NOT NULL,
            item_key TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT reject_events_key_id_format_chk CHECK (key_id ~ '{UUID4}'),
            CONSTRAINT reject_events_event_key_format_chk CHECK (event_key ~ '{HMAC_HEX}'),
            CONSTRAINT reject_events_item_key_format_chk CHECK (item_key IS NULL OR item_key ~ '{HMAC_HEX}'),
            CONSTRAINT reject_events_error_class_format_chk CHECK (error_class ~ '{ERROR_CLASS}')
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX reject_events_event_identity_uidx
        ON reject_events (customer_id, branch_id, key_id, event_key)
    """)
    op.execute("CREATE INDEX reject_events_scope_time_idx ON reject_events (customer_id, branch_id, event_time)")
    op.execute("""
        CREATE INDEX reject_events_scope_item_idx
        ON reject_events (customer_id, branch_id, item_key) WHERE item_key IS NOT NULL
    """)

    op.execute(f"""
        CREATE TABLE acs_hold_events (
            id BIGSERIAL PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            branch_id INTEGER NOT NULL REFERENCES branches(id),
            key_id TEXT NOT NULL,
            event_key TEXT NOT NULL,
            event_time TIMESTAMPTZ NOT NULL,
            item_key TEXT NOT NULL,
            destination TEXT NOT NULL,
            is_ill BOOLEAN NOT NULL,
            is_branch_services BOOLEAN NOT NULL,
            is_collection_services BOOLEAN NOT NULL,
            ruleset_id TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT acs_hold_events_key_id_format_chk CHECK (key_id ~ '{UUID4}'),
            CONSTRAINT acs_hold_events_event_key_format_chk CHECK (event_key ~ '{HMAC_HEX}'),
            CONSTRAINT acs_hold_events_item_key_format_chk CHECK (item_key ~ '{HMAC_HEX}'),
            CONSTRAINT acs_hold_events_destination_format_chk CHECK (destination ~ '{DESTINATION}'),
            CONSTRAINT acs_hold_events_ruleset_id_format_chk CHECK (ruleset_id IS NULL OR ruleset_id ~ '{UUID4}')
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX acs_hold_events_event_identity_uidx
        ON acs_hold_events (customer_id, branch_id, key_id, event_key)
    """)
    op.execute("CREATE INDEX acs_hold_events_scope_time_idx ON acs_hold_events (customer_id, branch_id, event_time)")
    op.execute("""
        CREATE INDEX acs_hold_events_scope_item_idx
        ON acs_hold_events (customer_id, branch_id, item_key)
    """)


def downgrade() -> None:
    """Downgrade schema. Drops only the four v2 tables (and, with them, their indexes and constraints)."""
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
