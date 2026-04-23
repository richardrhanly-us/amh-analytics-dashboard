from sqlalchemy import create_engine, text
import os

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args={"sslmode": "require"},
    future=True,
)


def main():
    with engine.begin() as conn:
        conn.execute(text("""
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
        """))

        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS agent_tokens_scope_idx
            ON agent_tokens (customer_id, branch_id, is_active)
        """))

        conn.execute(text("""
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
        """))

        conn.execute(text("""
            ALTER TABLE acs_events
            ADD COLUMN IF NOT EXISTS barcode_key TEXT
        """))

        conn.execute(text("""
            UPDATE acs_events
            SET barcode_key = COALESCE(barcode, '')
            WHERE barcode_key IS NULL
        """))

        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS acs_events_unique_idx
            ON acs_events (event_time, message_code, barcode_key)
        """))

        conn.execute(text("""
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
                checkins_bad_datetime_rows INTEGER NULL,
                rejects_bad_datetime_rows INTEGER NULL,
                acs_bad_datetime_rows INTEGER NULL,
                transit_items INTEGER NULL,
                problem_items INTEGER NULL,
                destination_breakdown JSONB NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (customer_id, branch_id)
            )
        """))

        conn.execute(text("""
            ALTER TABLE pipeline_status
            ADD COLUMN IF NOT EXISTS acs_rows INTEGER NULL
        """))

        conn.execute(text("""
            ALTER TABLE pipeline_status
            ADD COLUMN IF NOT EXISTS uploaded_acs_rows INTEGER NULL
        """))

        conn.execute(text("""
            ALTER TABLE pipeline_status
            ADD COLUMN IF NOT EXISTS acs_bad_datetime_rows INTEGER NULL
        """))

    print("Database initialization complete.")


if __name__ == "__main__":
    main()
