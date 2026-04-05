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
    return create_engine(db_url)


def get_file_mtime(path):
    file_path = Path(path)
    if file_path.exists():
        return file_path.stat().st_mtime
    return 0


def _read_table(query):
    engine = get_engine()

    if engine is None:
        return pd.DataFrame()

    try:
        return pd.read_sql(text(query), engine)
    except Exception:
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


@st.cache_data
def load_checkins_history_df(mtime=None):
    query = """
        SELECT *
        FROM checkins
        ORDER BY event_time
    """
    df = _read_table(query)
    return _normalize_checkins_df(df)


@st.cache_data
def load_checkins_df(path=CHECKINS_FILE, mtime=None):
    query = """
        SELECT *
        FROM checkins
        ORDER BY event_time
    """
    df = _read_table(query)
    return _normalize_checkins_df(df)


@st.cache_data
def load_rejects_df(path=REJECTS_FILE, mtime=None):
    query = """
        SELECT *
        FROM rejects
        ORDER BY event_time
    """
    df = _read_table(query)
    return _normalize_rejects_df(df)


@st.cache_data
def load_rejects_history_df(path="data/processed/rejects_history.csv", mtime=None):
    query = """
        SELECT *
        FROM rejects
        ORDER BY event_time
    """
    df = _read_table(query)
    return _normalize_rejects_df(df)


@st.cache_data
def load_pipeline_status(path=STATUS_FILE, mtime=None):
    file_path = Path(path)

    if not file_path.exists():
        return {}

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
