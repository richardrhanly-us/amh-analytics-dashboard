"""baseline current schema

Revision ID: 26397a3947b1
Revises:
Create Date: 2026-04-26 20:32:42.956337

This revision was originally stamped empty (`alembic stamp head`) to adopt
Alembic on top of a database that already existed. Production's
alembic_version already points at this revision, so upgrade()/downgrade()
below will never run there -- filling them in here is safe for production
and makes it possible to build a fresh database (e.g. for local dev or
tests) with `alembic upgrade head` alone, instead of also needing
init_db.py and src/sql/001_multi_tenant_foundation.sql.

Content was reconstructed by introspecting the live production schema
(information_schema.columns, pg_indexes, pg_constraint) on 2026-07-27.

Corrected on 2026-08-04: an initial pass of this migration modeled
checkins_routed as a plain table and omitted two triggers entirely.
Neither checkins_clean nor rejects_clean nor checkins_routed are dead --
checkins/rejects each have an AFTER INSERT trigger (sync_checkins_to_clean,
sync_rejects_to_clean) that copies every new row into checkins_clean /
rejects_clean in real time, and checkins_routed is a VIEW joining
checkins_clean with bin_routing_map to add routing labels and a
Transit/Internal grouping. None of this is referenced by any Python code
in this repo -- it's a live, database-level system invisible to a
code-only audit. pipeline_status's *_history_rows columns are still
unconfirmed as unused; left as-is pending that check.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '26397a3947b1'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # --- reference / plan tables -----------------------------------------

    op.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            id BIGSERIAL PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # --- multi-tenant core --------------------------------------------------

    op.execute("""
        CREATE TABLE IF NOT EXISTS organizations (
            id BIGSERIAL PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            operational_customer_id INTEGER,
            CHECK (status IN ('active', 'trial', 'suspended', 'cancelled'))
        )
    """)

    # Legacy/operational customer table. Predates the organizations layer;
    # checkins/rejects still carry a direct FK to it. Not referenced by any
    # Python code in this repo -- confirm before dropping.
    op.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            id SERIAL PRIMARY KEY,
            organization_id BIGINT NOT NULL,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            is_primary BOOLEAN NOT NULL DEFAULT FALSE,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            operational_branch_id INTEGER,
            onboarding_status TEXT NOT NULL DEFAULT 'pending',
            onboarding_message TEXT,
            onboarding_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (organization_id, slug),
            CHECK (status IN ('active', 'inactive'))
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS app_users (
            id BIGSERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL DEFAULT '',
            password_hash TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            failed_login_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TIMESTAMPTZ,
            last_login_at TIMESTAMPTZ,
            last_failed_login_at TIMESTAMPTZ,
            last_password_changed_at TIMESTAMPTZ,
            is_platform_admin BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)

    # Case-insensitive uniqueness, matching auth_service.get_user_by_email's
    # `lower(email)` lookup.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS app_users_email_lower_uidx
        ON app_users (lower(email))
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS memberships (
            id BIGSERIAL PRIMARY KEY,
            organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (organization_id, user_id),
            CHECK (role IN ('owner', 'admin', 'manager', 'viewer'))
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id BIGSERIAL PRIMARY KEY,
            organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            plan_id BIGINT NOT NULL REFERENCES plans(id),
            status TEXT NOT NULL DEFAULT 'trial',
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ends_at TIMESTAMPTZ,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (status IN ('trial', 'active', 'past_due', 'cancelled'))
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS feature_entitlements (
            id BIGSERIAL PRIMARY KEY,
            plan_id BIGINT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
            feature_key TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            limit_value INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (plan_id, feature_key)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS organization_settings (
            id BIGSERIAL PRIMARY KEY,
            organization_id BIGINT NOT NULL UNIQUE REFERENCES organizations(id) ON DELETE CASCADE,
            settings_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS branch_settings (
            id BIGSERIAL PRIMARY KEY,
            branch_id BIGINT NOT NULL UNIQUE REFERENCES branches(id) ON DELETE CASCADE,
            settings_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # --- auth ----------------------------------------------------------------

    op.execute("""
        CREATE TABLE IF NOT EXISTS auth_audit_log (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT,
            email TEXT,
            event_type TEXT NOT NULL,
            is_success BOOLEAN NOT NULL,
            message TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS auth_audit_log_created_at_idx
        ON auth_audit_log (created_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS auth_audit_log_email_idx
        ON auth_audit_log (lower(email))
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS auth_audit_log_event_type_idx
        ON auth_audit_log (event_type)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS auth_audit_log_user_id_idx
        ON auth_audit_log (user_id)
    """)

    # --- agent auth ------------------------------------------------------------

    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_tokens (
            id BIGSERIAL PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            customer_id INTEGER NOT NULL,
            branch_id INTEGER NOT NULL,
            description TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP NULL
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS agent_tokens_scope_idx
        ON agent_tokens (customer_id, branch_id, is_active)
    """)

    # --- operational data -------------------------------------------------------

    op.execute("""
        CREATE TABLE IF NOT EXISTS acs_events (
            id BIGSERIAL PRIMARY KEY,
            customer_id INTEGER,
            branch_id INTEGER,
            event_time TIMESTAMP,
            message_code TEXT,
            barcode TEXT,
            barcode_key TEXT,
            title TEXT,
            patron_id TEXT,
            destination TEXT,
            raw_message TEXT,
            source_file TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS acs_events_unique_idx
        ON acs_events (event_time, message_code, barcode_key)
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS checkins (
            id BIGSERIAL PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            branch_id INTEGER NOT NULL REFERENCES branches(id),
            event_time TIMESTAMP NOT NULL,
            title TEXT,
            barcode TEXT,
            collection_code TEXT,
            call_number TEXT,
            shelf_code TEXT,
            destination TEXT,
            bin TEXT,
            is_problem BOOLEAN,
            message TEXT,
            flag_1 TEXT,
            flag_2 TEXT,
            flag_3 TEXT,
            source_file TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS checkins_unique_event
        ON checkins (barcode, event_time)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_checkins_branch ON checkins (branch_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_checkins_event_time ON checkins (event_time)
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS rejects (
            id BIGSERIAL PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            branch_id INTEGER NOT NULL REFERENCES branches(id),
            event_time TIMESTAMP NOT NULL,
            barcode TEXT,
            error_message TEXT,
            source_file TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS rejects_unique_event
        ON rejects (barcode, event_time, error_message)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_rejects_branch ON rejects (branch_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_rejects_event_time ON rejects (event_time)
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_status (
            customer_id INTEGER NOT NULL,
            branch_id INTEGER NOT NULL,
            last_attempt TIMESTAMP NULL,
            last_run TIMESTAMP NULL,
            status TEXT NULL,
            checkins_rows INTEGER NULL,
            rejects_rows INTEGER NULL,
            acs_rows INTEGER NULL,
            uploaded_checkins_rows INTEGER NULL,
            uploaded_rejects_rows INTEGER NULL,
            uploaded_acs_rows INTEGER NULL,
            checkins_history_rows INTEGER NULL,
            rejects_history_rows INTEGER NULL,
            acs_history_rows INTEGER NULL,
            checkins_bad_datetime_rows INTEGER NULL,
            rejects_bad_datetime_rows INTEGER NULL,
            acs_bad_datetime_rows INTEGER NULL,
            transit_items INTEGER NULL,
            problem_items INTEGER NULL,
            destination_breakdown JSONB NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (customer_id, branch_id)
        )
    """)

    # --- bin routing / "clean" sync system --------------------------------------
    # Not referenced by any Python code in this repo, but very much live:
    # checkins/rejects each have an AFTER INSERT trigger (created below) that
    # copies every new row into checkins_clean / rejects_clean in real time,
    # and checkins_routed is a view built on top of checkins_clean that adds
    # routing labels from bin_routing_map. Confirmed with production DDL on
    # 2026-08-04.

    op.execute("""
        CREATE TABLE IF NOT EXISTS bin_routing_map (
            bin TEXT PRIMARY KEY,
            routing_label TEXT NOT NULL,
            routing_group TEXT NOT NULL,
            notes TEXT
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS checkins_clean (
            id BIGINT,
            customer_id INTEGER,
            branch_id INTEGER,
            event_time TIMESTAMP,
            title TEXT,
            barcode TEXT,
            collection_code TEXT,
            call_number TEXT,
            shelf_code TEXT,
            destination TEXT,
            destination_raw TEXT,
            destination_clean TEXT,
            bin TEXT,
            is_problem BOOLEAN,
            message TEXT,
            flag_1 TEXT,
            flag_2 TEXT,
            flag_3 TEXT,
            source_file TEXT,
            created_at TIMESTAMP,
            ingested_at TIMESTAMPTZ,
            UNIQUE (id)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS rejects_clean (
            id BIGINT,
            customer_id INTEGER,
            branch_id INTEGER,
            event_time TIMESTAMP,
            barcode TEXT,
            error_message TEXT,
            source_file TEXT,
            created_at TIMESTAMP,
            ingested_at TIMESTAMPTZ,
            UNIQUE (id)
        )
    """)

    # checkins_routed is a VIEW, not a table -- joins checkins_clean with
    # bin_routing_map to add routing labels and a Transit/Internal grouping.
    op.execute("""
        CREATE OR REPLACE VIEW checkins_routed AS
        SELECT
            c.id,
            c.customer_id,
            c.branch_id,
            c.event_time,
            c.title,
            c.barcode,
            c.collection_code,
            c.call_number,
            c.shelf_code,
            c.destination,
            c.destination_raw,
            c.destination_clean,
            c.bin,
            c.is_problem,
            c.message,
            c.flag_1,
            c.flag_2,
            c.flag_3,
            c.source_file,
            c.created_at,
            c.ingested_at,
            m.routing_label,
            m.routing_group,
            CASE
                WHEN c.destination_clean = ANY (ARRAY['Westside'::text, 'Library Express'::text])
                THEN 'Transit'::text
                ELSE 'Internal'::text
            END AS destination_group
        FROM checkins_clean c
        LEFT JOIN bin_routing_map m ON c.bin = m.bin
    """)

    # Trigger functions that keep checkins_clean / rejects_clean in sync with
    # every new checkins / rejects row. destination_clean normalizes the raw
    # destination text into the labels the dashboard displays.
    op.execute("""
        CREATE OR REPLACE FUNCTION sync_checkins_to_clean()
         RETURNS trigger
         LANGUAGE plpgsql
        AS $function$
        BEGIN
            INSERT INTO checkins_clean (
                id,
                customer_id,
                branch_id,
                event_time,
                title,
                barcode,
                collection_code,
                call_number,
                shelf_code,
                destination,
                destination_raw,
                destination_clean,
                bin,
                is_problem,
                message,
                flag_1,
                flag_2,
                flag_3,
                source_file,
                created_at,
                ingested_at
            )
            VALUES (
                NEW.id,
                NEW.customer_id,
                NEW.branch_id,
                NEW.event_time,
                NEW.title,
                NEW.barcode,
                NEW.collection_code,
                NEW.call_number,
                NEW.shelf_code,
                NEW.destination,
                NEW.destination,
                CASE
                    WHEN UPPER(TRIM(COALESCE(NEW.destination, ''))) IN ('1', 'LOCAL', 'MAIN') THEN 'Main'
                    WHEN UPPER(TRIM(COALESCE(NEW.destination, ''))) LIKE '%WESTSIDE%' THEN 'Westside'
                    WHEN UPPER(TRIM(COALESCE(NEW.destination, ''))) LIKE '%LIBRARY EXPRESS%' THEN 'Library Express'
                    WHEN UPPER(TRIM(COALESCE(NEW.destination, ''))) LIKE '%NO AGENCY DESTINATION%' THEN 'No Agency Destination'
                    ELSE COALESCE(NEW.destination, '')
                END,
                NEW.bin,
                NEW.is_problem,
                NEW.message,
                NEW.flag_1,
                NEW.flag_2,
                NEW.flag_3,
                NEW.source_file,
                NEW.created_at,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (id) DO NOTHING;

            RETURN NEW;
        END;
        $function$
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION sync_rejects_to_clean()
         RETURNS trigger
         LANGUAGE plpgsql
        AS $function$
        BEGIN
            INSERT INTO rejects_clean (
                id,
                customer_id,
                branch_id,
                event_time,
                barcode,
                error_message,
                source_file,
                created_at,
                ingested_at
            )
            VALUES (
                NEW.id,
                NEW.customer_id,
                NEW.branch_id,
                NEW.event_time,
                NEW.barcode,
                NEW.error_message,
                NEW.source_file,
                NEW.created_at,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (id) DO NOTHING;

            RETURN NEW;
        END;
        $function$
    """)

    op.execute("DROP TRIGGER IF EXISTS trg_sync_checkins_to_clean ON checkins")
    op.execute("""
        CREATE TRIGGER trg_sync_checkins_to_clean
        AFTER INSERT ON checkins
        FOR EACH ROW
        EXECUTE FUNCTION sync_checkins_to_clean()
    """)

    op.execute("DROP TRIGGER IF EXISTS trg_sync_rejects_to_clean ON rejects")
    op.execute("""
        CREATE TRIGGER trg_sync_rejects_to_clean
        AFTER INSERT ON rejects
        FOR EACH ROW
        EXECUTE FUNCTION sync_rejects_to_clean()
    """)

    # --- seed data ---------------------------------------------------------------

    op.execute("""
        INSERT INTO plans (code, name, description)
        VALUES
            ('starter', 'Starter', 'Single branch dashboard for smaller libraries'),
            ('pro', 'Pro', 'Multi-branch analytics with exports and alerts'),
            ('enterprise', 'Enterprise', 'System-level analytics and advanced diagnostics')
        ON CONFLICT (code) DO NOTHING
    """)

    op.execute("""
        INSERT INTO feature_entitlements (plan_id, feature_key, enabled, limit_value)
        SELECT p.id, x.feature_key, x.enabled, x.limit_value
        FROM plans p
        JOIN (
            VALUES
                ('starter', 'max_branches', TRUE, 1),
                ('starter', 'exports', FALSE, NULL),
                ('starter', 'alerts', FALSE, NULL),
                ('starter', 'advanced_reports', FALSE, NULL),
                ('starter', 'history_days', TRUE, 90),

                ('pro', 'max_branches', TRUE, 10),
                ('pro', 'exports', TRUE, NULL),
                ('pro', 'alerts', TRUE, NULL),
                ('pro', 'advanced_reports', TRUE, NULL),
                ('pro', 'history_days', TRUE, 730),

                ('enterprise', 'max_branches', TRUE, 999),
                ('enterprise', 'exports', TRUE, NULL),
                ('enterprise', 'alerts', TRUE, NULL),
                ('enterprise', 'advanced_reports', TRUE, NULL),
                ('enterprise', 'history_days', TRUE, 3650)
        ) AS x(plan_code, feature_key, enabled, limit_value)
            ON x.plan_code = p.code
        ON CONFLICT (plan_id, feature_key) DO NOTHING
    """)


def downgrade() -> None:
    """Downgrade schema."""

    op.execute("DROP TRIGGER IF EXISTS trg_sync_checkins_to_clean ON checkins")
    op.execute("DROP TRIGGER IF EXISTS trg_sync_rejects_to_clean ON rejects")
    op.execute("DROP FUNCTION IF EXISTS sync_checkins_to_clean()")
    op.execute("DROP FUNCTION IF EXISTS sync_rejects_to_clean()")
    op.execute("DROP VIEW IF EXISTS checkins_routed CASCADE")

    for table in (
        "rejects_clean",
        "checkins_clean",
        "bin_routing_map",
        "pipeline_status",
        "rejects",
        "checkins",
        "acs_events",
        "agent_tokens",
        "auth_audit_log",
        "branch_settings",
        "organization_settings",
        "feature_entitlements",
        "subscriptions",
        "memberships",
        "app_users",
        "branches",
        "customers",
        "organizations",
        "plans",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
