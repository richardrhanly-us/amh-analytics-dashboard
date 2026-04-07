from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, text
import os
import traceback
import json

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set")

engine = create_engine(DATABASE_URL)


@app.get("/")
def root():
    return {"status": "SortView API running"}


@app.post("/upload")
def upload(data: dict):
    try:
        checkins = data.get("checkins", [])
        rejects = data.get("rejects", [])
        acs = data.get("acs", [])

        inserted_checkins = 0
        inserted_rejects = 0
        inserted_acs = 0

        with engine.begin() as conn:
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

            for row in checkins:
                result = conn.execute(text("""
                    INSERT INTO checkins (
                        customer_id, branch_id, event_time, title, barcode,
                        collection_code, call_number, shelf_code,
                        destination, bin, is_problem, message,
                        flag_1, flag_2, flag_3, source_file
                    )
                    VALUES (
                        :customer_id, :branch_id, :event_time, :title, :barcode,
                        :collection_code, :call_number, :shelf_code,
                        :destination, :bin, :is_problem, :message,
                        :flag_1, :flag_2, :flag_3, :source_file
                    )
                    ON CONFLICT (barcode, event_time) DO NOTHING
                """), row)

                inserted_checkins += result.rowcount

            for row in rejects:
                barcode_value = row.get("barcode") or ""

                reject_row = {
                    "customer_id": row.get("customer_id"),
                    "branch_id": row.get("branch_id"),
                    "event_time": row.get("event_time"),
                    "barcode": barcode_value,
                    "error_message": row.get("message"),
                    "source_file": row.get("source_file"),
                }

                result = conn.execute(text("""
                    INSERT INTO rejects (
                        customer_id, branch_id, event_time,
                        barcode, error_message, source_file
                    )
                    VALUES (
                        :customer_id, :branch_id, :event_time,
                        :barcode, :error_message, :source_file
                    )
                    ON CONFLICT (barcode, event_time, error_message) DO NOTHING
                """), reject_row)

                inserted_rejects += result.rowcount

            for row in acs:
                acs_row = {
                    "customer_id": row.get("customer_id"),
                    "branch_id": row.get("branch_id"),
                    "event_time": row.get("event_time"),
                    "message_code": row.get("message_code"),
                    "barcode": row.get("barcode"),
                    "barcode_key": row.get("barcode") or "",
                    "title": row.get("title"),
                    "patron_id": row.get("patron_id"),
                    "destination": row.get("destination"),
                    "raw_message": row.get("raw_message"),
                    "source_file": row.get("source_file"),
                }

                result = conn.execute(text("""
                    INSERT INTO acs_events (
                        customer_id, branch_id, event_time,
                        message_code, barcode, barcode_key, title,
                        patron_id, destination, raw_message, source_file
                    )
                    VALUES (
                        :customer_id, :branch_id, :event_time,
                        :message_code, :barcode, :barcode_key, :title,
                        :patron_id, :destination, :raw_message, :source_file
                    )
                    ON CONFLICT DO NOTHING
                """), acs_row)

                inserted_acs += result.rowcount

        return {
            "status": "success",
            "checkins_received": len(checkins),
            "rejects_received": len(rejects),
            "acs_received": len(acs),
            "checkins_inserted": inserted_checkins,
            "rejects_inserted": inserted_rejects,
            "acs_inserted": inserted_acs
        }

    except Exception as e:
        print("UPLOAD ERROR:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/upload-pipeline-status")
def upload_pipeline_status(data: dict):
    try:
        customer_id = data.get("customer_id")
        branch_id = data.get("branch_id")

        if customer_id is None or branch_id is None:
            raise HTTPException(status_code=400, detail="customer_id and branch_id are required")

        destination_breakdown = data.get("destination_breakdown", {})
        if destination_breakdown is None:
            destination_breakdown = {}

        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS pipeline_status (
                    customer_id INTEGER NOT NULL,
                    branch_id INTEGER NOT NULL,
                    last_attempt TIMESTAMP NULL,
                    last_run TIMESTAMP NULL,
                    status TEXT NULL,
                    checkins_rows INTEGER NULL,
                    rejects_rows INTEGER NULL,
                    uploaded_checkins_rows INTEGER NULL,
                    uploaded_rejects_rows INTEGER NULL,
                    checkins_history_rows INTEGER NULL,
                    rejects_history_rows INTEGER NULL,
                    checkins_bad_datetime_rows INTEGER NULL,
                    rejects_bad_datetime_rows INTEGER NULL,
                    transit_items INTEGER NULL,
                    problem_items INTEGER NULL,
                    destination_breakdown JSONB NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (customer_id, branch_id)
                )
            """))

            conn.execute(text("""
                INSERT INTO pipeline_status (
                    customer_id,
                    branch_id,
                    last_attempt,
                    last_run,
                    status,
                    checkins_rows,
                    rejects_rows,
                    uploaded_checkins_rows,
                    uploaded_rejects_rows,
                    checkins_history_rows,
                    rejects_history_rows,
                    checkins_bad_datetime_rows,
                    rejects_bad_datetime_rows,
                    transit_items,
                    problem_items,
                    destination_breakdown,
                    updated_at
                )
                VALUES (
                    :customer_id,
                    :branch_id,
                    :last_attempt,
                    :last_run,
                    :status,
                    :checkins_rows,
                    :rejects_rows,
                    :uploaded_checkins_rows,
                    :uploaded_rejects_rows,
                    :checkins_history_rows,
                    :rejects_history_rows,
                    :checkins_bad_datetime_rows,
                    :rejects_bad_datetime_rows,
                    :transit_items,
                    :problem_items,
                    CAST(:destination_breakdown AS JSONB),
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (customer_id, branch_id)
                DO UPDATE SET
                    last_attempt = EXCLUDED.last_attempt,
                    last_run = EXCLUDED.last_run,
                    status = EXCLUDED.status,
                    checkins_rows = EXCLUDED.checkins_rows,
                    rejects_rows = EXCLUDED.rejects_rows,
                    uploaded_checkins_rows = EXCLUDED.uploaded_checkins_rows,
                    uploaded_rejects_rows = EXCLUDED.uploaded_rejects_rows,
                    checkins_history_rows = EXCLUDED.checkins_history_rows,
                    rejects_history_rows = EXCLUDED.rejects_history_rows,
                    checkins_bad_datetime_rows = EXCLUDED.checkins_bad_datetime_rows,
                    rejects_bad_datetime_rows = EXCLUDED.rejects_bad_datetime_rows,
                    transit_items = EXCLUDED.transit_items,
                    problem_items = EXCLUDED.problem_items,
                    destination_breakdown = EXCLUDED.destination_breakdown,
                    updated_at = CURRENT_TIMESTAMP
            """), {
                "customer_id": int(customer_id),
                "branch_id": int(branch_id),
                "last_attempt": data.get("last_attempt"),
                "last_run": data.get("last_run"),
                "status": data.get("status"),
                "checkins_rows": data.get("checkins_rows"),
                "rejects_rows": data.get("rejects_rows"),
                "uploaded_checkins_rows": data.get("uploaded_checkins_rows"),
                "uploaded_rejects_rows": data.get("uploaded_rejects_rows"),
                "checkins_history_rows": data.get("checkins_history_rows"),
                "rejects_history_rows": data.get("rejects_history_rows"),
                "checkins_bad_datetime_rows": data.get("checkins_bad_datetime_rows"),
                "rejects_bad_datetime_rows": data.get("rejects_bad_datetime_rows"),
                "transit_items": data.get("transit_items"),
                "problem_items": data.get("problem_items"),
                "destination_breakdown": json.dumps(destination_breakdown),
            })

        return {
            "status": "success",
            "message": "Pipeline status uploaded"
        }

    except HTTPException:
        raise
    except Exception as e:
        print("PIPELINE STATUS UPLOAD ERROR:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
