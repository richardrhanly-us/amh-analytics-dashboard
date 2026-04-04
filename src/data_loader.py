import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine

CHECKINS_FILE = "data/processed/checkins_clean.csv"
REJECTS_FILE = "data/processed/rejects_clean.csv"
STATUS_FILE = "data/processed/pipeline_status.json"
CHECKINS_HISTORY_FILE = "data/processed/checkins_history.csv"


def get_database_url():
    db_url = os.getenv("DATABASE_URL")

    if not db_url:
        try:
            db_url = st.secrets["DATABASE_URL"]
        except Exception:
            db_url = None

    if not db_url:
        raise ValueError("DATABASE_URL is not set in environment or Streamlit secrets")

    return db_url


@st.cache_resource
def get_engine():
    return create_engine(get_database_url())


def get_file_mtime(path):
    file_path = Path(path)
    if file_path.exists():
        return file_path.stat().st_mtime
    return 0



def load_checkins_history_df(mtime=None):
    query = """
        SELECT *
        FROM checkins
        ORDER BY event_time
    """
    df = pd.read_sql(query, get_engine())

    if "event_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["event_time"], errors="coerce")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    return df


def load_checkins_df(path=CHECKINS_FILE, mtime=None):
    query = """
        SELECT *
        FROM checkins
        ORDER BY event_time
    """
    df = pd.read_sql(query, get_engine())

    if "event_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["event_time"], errors="coerce")
    elif "checkin_datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["checkin_datetime"], errors="coerce")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    if "bin" not in df.columns and "sort_bin" in df.columns:
        df["bin"] = df["sort_bin"]

    return df



def load_rejects_df(path=REJECTS_FILE, mtime=None):
    query = """
        SELECT *
        FROM rejects
        ORDER BY event_time
    """
    df = pd.read_sql(query, get_engine())

    if "event_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["event_time"], errors="coerce")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    return df




def load_rejects_history_df(path="data/processed/rejects_history.csv", mtime=None):
    query = """
        SELECT *
        FROM rejects
        ORDER BY event_time
    """
    df = pd.read_sql(query, get_engine())

    if "event_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["event_time"], errors="coerce")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    return df


@st.cache_data
def load_pipeline_status(path=STATUS_FILE, mtime=None):
    file_path = Path(path)

    if not file_path.exists():
        return {}

    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)
