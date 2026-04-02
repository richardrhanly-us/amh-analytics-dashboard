import json
from pathlib import Path

import pandas as pd
import streamlit as st

CHECKINS_FILE = "data/processed/checkins_clean.csv"
REJECTS_FILE = "data/processed/rejects_clean.csv"
STATUS_FILE = "data/processed/pipeline_status.json"


def get_file_mtime(path):
    file_path = Path(path)
    if file_path.exists():
        return file_path.stat().st_mtime
    return 0


@st.cache_data
def load_checkins_df(path=CHECKINS_FILE, mtime=None):
    df = pd.read_csv(path, low_memory=False)

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    elif "checkin_datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["checkin_datetime"], errors="coerce")

    if "bin" not in df.columns and "sort_bin" in df.columns:
        df["bin"] = df["sort_bin"]

    return df


@st.cache_data
def load_rejects_df(path=REJECTS_FILE, mtime=None):
    df = pd.read_csv(path, low_memory=False)
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df


@st.cache_data
def load_pipeline_status(path=STATUS_FILE, mtime=None):
    file_path = Path(path)

    if not file_path.exists():
        return {}

    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)
