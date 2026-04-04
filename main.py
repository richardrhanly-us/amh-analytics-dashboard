from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, text
import os
import traceback

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

        inserted_checkins = 0
        inserted_rejects = 0

        with engine.begin() as conn:
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
                barcode_value = row.get("barcode")
                if barcode_value == "":
                    barcode_value = None

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

        return {
            "status": "success",
            "checkins_received": len(checkins),
            "rejects_received": len(rejects),
            "checkins_inserted": inserted_checkins,
            "rejects_inserted": inserted_rejects
        }

    except Exception as e:
        print("UPLOAD ERROR:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
