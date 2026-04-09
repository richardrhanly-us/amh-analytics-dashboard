import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

CHECKINS_FILE = "data/processed/checkins_clean.csv"
REJECTS_FILE = "data/processed/rejects_clean.csv"
STATUS_FILE = "data/processed/pipeline_status.json"
CHECKINS_HISTORY_FILE = "data/processed/checkins_history.csv"
REJECTS_HISTORY_FILE = "data/processed/rejects_history.csv"


def get_database_url():
    db_url = os.getenv("DATABASE_URL")

    if not db_url:
        try:
            db_url = st.secrets.get("DATABASE_URL")
        except Exception:
            db_url = None

    return db_url

@st.cache_resource
def get_engine():
    db_url = get_database_url()
    if not db_url:
        return None

    return create_engine(
        db_url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"sslmode": "require"},
    )


def get_file_mtime(path):
    file_path = Path(path)
    if file_path.exists():
        return file_path.stat().st_mtime
    return 0


def _read_table(query):
    engine = get_engine()

    if engine is None:
        st.error("DATABASE_URL is missing. App cannot connect to Neon.")
        return pd.DataFrame()

    try:
        return pd.read_sql(text(query), engine)
    except Exception as e:
        st.error(f"Database query failed: {e}")
        return pd.DataFrame()


def _normalize_checkins_df(df):
    if df.empty:
        return df

    if "event_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["event_time"], errors="coerce")
    elif "checkin_datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["checkin_datetime"], errors="coerce")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    if "bin" not in df.columns and "sort_bin" in df.columns:
        df["bin"] = df["sort_bin"]

    if "destination" not in df.columns:
        if "destination_raw" in df.columns:
            df["destination"] = df["destination_raw"]
        elif "destination_clean" in df.columns:
            df["destination"] = df["destination_clean"]

    return df


def _normalize_rejects_df(df):
    if df.empty:
        return df

    if "event_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["event_time"], errors="coerce")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    if "error_message" not in df.columns:
        df["error_message"] = ""

    return df


def _load_checkins_history_from_db():
    query = """
        SELECT *
        FROM checkins_routed
        ORDER BY event_time
    """
    df = _read_table(query)

    if not df.empty:
        return _normalize_checkins_df(df)

    fallback_query = """
        SELECT *
        FROM checkins_clean
        ORDER BY event_time
    """
    df = _read_table(fallback_query)
    return _normalize_checkins_df(df)


def _load_checkins_live_from_db():
    query = """
        SELECT *
        FROM checkins_routed
        WHERE event_time::date = (
            SELECT max(event_time)::date
            FROM checkins_routed
        )
        ORDER BY event_time
    """
    df = _read_table(query)

    if not df.empty:
        return _normalize_checkins_df(df)

    fallback_query = """
        SELECT *
        FROM checkins_clean
        WHERE event_time::date = (
            SELECT max(event_time)::date
            FROM checkins_clean
        )
        ORDER BY event_time
    """
    df = _read_table(fallback_query)
    return _normalize_checkins_df(df)


def _load_rejects_history_from_db():
    query = """
        SELECT *
        FROM rejects_clean
        ORDER BY event_time
    """
    df = _read_table(query)
    return _normalize_rejects_df(df)


def _load_rejects_live_from_db():
    query = """
        SELECT *
        FROM rejects_clean
        WHERE event_time::date = (
            SELECT max(event_time)::date
            FROM rejects_clean
        )
        ORDER BY event_time
    """
    df = _read_table(query)
    return _normalize_rejects_df(df)


def _load_checkins_from_csv(path):
    file_path = Path(path)

    if not file_path.exists():
        return pd.DataFrame()

    try:
        df = pd.read_csv(file_path, low_memory=False)
        return _normalize_checkins_df(df)
    except Exception:
        return pd.DataFrame()


def _load_rejects_from_csv(path):
    file_path = Path(path)

    if not file_path.exists():
        return pd.DataFrame()

    try:
        df = pd.read_csv(file_path, low_memory=False)
        return _normalize_rejects_df(df)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner=False)
def load_checkins_history_df(mtime=None, refresh_count=0):
    df = _load_checkins_history_from_db()

    if not df.empty:
        return df

    return _load_checkins_from_csv(CHECKINS_HISTORY_FILE)


@st.cache_data(ttl=60, show_spinner=False)
def load_checkins_df(path=CHECKINS_FILE, mtime=None, refresh_count=0):
    df = _load_checkins_live_from_db()

    if not df.empty:
        return df

    return _load_checkins_from_csv(path)

@st.cache_data(ttl=60, show_spinner=False)
def load_rejects_df(path=REJECTS_FILE, mtime=None, refresh_count=0):
    df = _load_rejects_live_from_db()

    if not df.empty:
        return df

    return _load_rejects_from_csv(path)


@st.cache_data(ttl=600, show_spinner=False)
def load_rejects_history_df(path=REJECTS_HISTORY_FILE, mtime=None, refresh_count=0):
    df = _load_rejects_history_from_db()

    if not df.empty:
        return df

    return _load_rejects_from_csv(path)



def _normalize_acs_df(df):
    if df.empty:
        return df

    if "event_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["event_time"], errors="coerce")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    for col in ["message_code", "barcode", "title", "patron_id", "destination", "raw_message", "source_file"]:
        if col not in df.columns:
            df[col] = None

    return df


def _load_acs_history_from_db():
    query = """
        SELECT *
        FROM acs_events
        ORDER BY event_time
    """
    df = _read_table(query)
    return _normalize_acs_df(df)


def _load_acs_live_from_db():
    query = """
        SELECT *
        FROM acs_events
        WHERE event_time::date = (
            SELECT max(event_time)::date
            FROM acs_events
        )
        ORDER BY event_time
    """
    df = _read_table(query)
    return _normalize_acs_df(df)


@st.cache_data(ttl=600, show_spinner=False)
def load_acs_history_df(mtime=None, refresh_count=0):
    return _load_acs_history_from_db()


@st.cache_data(ttl=60, show_spinner=False)
def load_acs_df(mtime=None, refresh_count=0):
    return _load_acs_live_from_db()


@st.cache_data(ttl=60, show_spinner=False)
def load_pipeline_status(path=STATUS_FILE, mtime=None, refresh_count=0):
    query = """
        SELECT
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
        FROM pipeline_status
        ORDER BY updated_at DESC
        LIMIT 1
    """
    df = _read_table(query)

    if df.empty:
        st.error("No rows returned from pipeline_status in Neon.")
        return {}

    row = df.iloc[0].to_dict()

    for key in ["last_attempt", "last_run", "updated_at"]:
        value = row.get(key)
        if pd.notna(value):
            if hasattr(value, "isoformat"):
                row[key] = value.isoformat()
            else:
                row[key] = str(value)
        else:
            row[key] = None

    destination_breakdown = row.get("destination_breakdown")

    if isinstance(destination_breakdown, str):
        try:
            row["destination_breakdown"] = json.loads(destination_breakdown)
        except Exception:
            row["destination_breakdown"] = {}
    elif destination_breakdown is None or (
        isinstance(destination_breakdown, float) and pd.isna(destination_breakdown)
    ):
        row["destination_breakdown"] = {}

    return row

    file_path = Path(path)

    if not file_path.exists():
        return {}

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
