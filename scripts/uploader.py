from sqlalchemy import create_engine, text

from config import load_config
from logger_config import get_logger

logger = get_logger("uploader")

config = load_config()

CUSTOMER_ID = config["customer_id"]
BRANCH_ID = config["branch_id"]
DATABASE_URL = config["database_url"]

if not DATABASE_URL:
    raise ValueError("database_url is missing from agent_config.json")

engine = create_engine(DATABASE_URL)


def normalize_checkins_for_db(df):
    df = df.copy()
    df = df[df["datetime"].notna()].copy()

    db_df = df[
        [
            "datetime",
            "title",
            "barcode",
            "collection_code",
            "call_number",
            "shelf_code",
            "destination",
            "bin",
            "is_problem",
            "message",
            "flag_1",
            "flag_2",
            "flag_3",
        ]
    ].copy()

    db_df = db_df.rename(columns={"datetime": "event_time"})
    db_df["customer_id"] = CUSTOMER_ID
    db_df["branch_id"] = BRANCH_ID
    db_df["source_file"] = "Checkins.txt"

    db_df = db_df[
        [
            "customer_id",
            "branch_id",
            "event_time",
            "title",
            "barcode",
            "collection_code",
            "call_number",
            "shelf_code",
            "destination",
            "bin",
            "is_problem",
            "message",
            "flag_1",
            "flag_2",
            "flag_3",
            "source_file",
        ]
    ]

    return db_df


def normalize_rejects_for_db(df):
    df = df.copy()
    df = df[df["datetime"].notna()].copy()

    db_df = df[
        [
            "datetime",
            "barcode",
            "error_message",
        ]
    ].copy()

    db_df = db_df.rename(columns={"datetime": "event_time"})
    db_df["customer_id"] = CUSTOMER_ID
    db_df["branch_id"] = BRANCH_ID
    db_df["source_file"] = "Rejects.txt"

    db_df = db_df[
        [
            "customer_id",
            "branch_id",
            "event_time",
            "barcode",
            "error_message",
            "source_file",
        ]
    ]

    return db_df


def delete_existing_window(conn, table_name, min_time, max_time):
    query = text(f"""
        DELETE FROM {table_name}
        WHERE customer_id = :customer_id
          AND branch_id = :branch_id
          AND event_time >= :min_time
          AND event_time <= :max_time
    """)

    conn.execute(
        query,
        {
            "customer_id": CUSTOMER_ID,
            "branch_id": BRANCH_ID,
            "min_time": min_time,
            "max_time": max_time,
        },
    )


def upload_checkins_and_rejects(checkins_df, rejects_df):
    checkins_db = normalize_checkins_for_db(checkins_df)
    rejects_db = normalize_rejects_for_db(rejects_df)

    logger.info("Checkins rows ready for Neon: %s", len(checkins_db))
    logger.info("Rejects rows ready for Neon: %s", len(rejects_db))

    if len(checkins_db) == 0 and len(rejects_db) == 0:
        logger.warning("No valid rows found for upload")
        return {
            "uploaded_checkins": 0,
            "uploaded_rejects": 0,
        }

    with engine.begin() as conn:
        customer_exists = conn.execute(
            text("SELECT 1 FROM customers WHERE id = :customer_id"),
            {"customer_id": CUSTOMER_ID},
        ).fetchone()

        branch_exists = conn.execute(
            text("SELECT 1 FROM branches WHERE id = :branch_id"),
            {"branch_id": BRANCH_ID},
        ).fetchone()

        if not customer_exists:
            raise ValueError(f"customer_id {CUSTOMER_ID} does not exist")

        if not branch_exists:
            raise ValueError(f"branch_id {BRANCH_ID} does not exist")

        if len(checkins_db) > 0:
            min_checkins_time = checkins_db["event_time"].min()
            max_checkins_time = checkins_db["event_time"].max()

            logger.info(
                "Deleting existing checkins in Neon window %s to %s",
                min_checkins_time,
                max_checkins_time,
            )

            delete_existing_window(
                conn,
                "checkins",
                min_checkins_time,
                max_checkins_time,
            )

        if len(rejects_db) > 0:
            min_rejects_time = rejects_db["event_time"].min()
            max_rejects_time = rejects_db["event_time"].max()

            logger.info(
                "Deleting existing rejects in Neon window %s to %s",
                min_rejects_time,
                max_rejects_time,
            )

            delete_existing_window(
                conn,
                "rejects",
                min_rejects_time,
                max_rejects_time,
            )

    if len(checkins_db) > 0:
        checkins_db.to_sql(
            "checkins",
            engine,
            if_exists="append",
            index=False,
            chunksize=1000,
            method="multi",
        )

    if len(rejects_db) > 0:
        rejects_db.to_sql(
            "rejects",
            engine,
            if_exists="append",
            index=False,
            chunksize=1000,
            method="multi",
        )

    logger.info(
        "Neon upload complete | checkins=%s rejects=%s",
        len(checkins_db),
        len(rejects_db),
    )

    return {
        "uploaded_checkins": int(len(checkins_db)),
        "uploaded_rejects": int(len(rejects_db)),
    }