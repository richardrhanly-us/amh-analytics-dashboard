from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
SUPER_ADMIN_DIR = os.path.join(ROOT_DIR, "super_admin")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

if SUPER_ADMIN_DIR not in sys.path:
    sys.path.insert(0, SUPER_ADMIN_DIR)

from services.platform_admin_service import list_libraries_with_status
from super_auth import require_super_admin

st.set_page_config(
    page_title="Manage Libraries",
    page_icon="📚",
    layout="wide",
)

auth_user = require_super_admin()

st.title("Manage Libraries")
st.caption("Master list of provisioned libraries and current status.")

rows = list_libraries_with_status()

if not rows:
    st.info("No libraries have been provisioned yet.")
    st.stop()

df = pd.DataFrame(rows)

if "last_run" in df.columns:
    df["last_run"] = pd.to_datetime(df["last_run"], errors="coerce")

if "last_attempt" in df.columns:
    df["last_attempt"] = pd.to_datetime(df["last_attempt"], errors="coerce")

total_orgs = df["organization_id"].nunique()
active_orgs = df[df["organization_status"] == "active"]["organization_id"].nunique()
reporting_orgs = df[df["pipeline_status"].notna()]["organization_id"].nunique()

col1, col2, col3 = st.columns(3)

with col1:
    st.metric("Libraries", total_orgs)

with col2:
    st.metric("Active", active_orgs)

with col3:
    st.metric("Reporting", reporting_orgs)

display_df = df.copy()

display_df = display_df.rename(
    columns={
        "organization_name": "Library",
        "organization_slug": "Org Slug",
        "organization_status": "Org Status",
        "branch_name": "Primary Branch",
        "branch_slug": "Branch Slug",
        "subscription_status": "Subscription",
        "plan_name": "Plan",
        "pipeline_status": "Agent Status",
        "last_run": "Last Run",
        "last_attempt": "Last Attempt",
    }
)

display_columns = [
    "Library",
    "Org Slug",
    "Org Status",
    "Primary Branch",
    "Branch Slug",
    "Subscription",
    "Plan",
    "Agent Status",
    "Last Run",
    "Last Attempt",
]

st.dataframe(
    display_df[display_columns],
    use_container_width=True,
    hide_index=True,
)

st.subheader("Selected Library Details")

library_names = display_df["Library"].dropna().tolist()
selected_library = st.selectbox("Choose a library", library_names)

selected_row = display_df[display_df["Library"] == selected_library].iloc[0].to_dict()
st.json(selected_row)
