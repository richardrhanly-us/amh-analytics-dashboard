import json
from pathlib import Path

import pandas as pd
import streamlit as st

CHECKINS_FILE = "data/processed/checkins_clean.csv"
REJECTS_FILE = "data/processed/rejects_clean.csv"
STATUS_FILE = "data/processed/pipeline_status.json"


@st.cache_data
def load_checkins_df(path=CHECKINS_FILE):
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df


@st.cache_data
def load_rejects_df(path=REJECTS_FILE):
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df


@st.cache_data
def load_pipeline_status(path=STATUS_FILE):
    file_path = Path(path)

    if not file_path.exists():
        return {}

    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)