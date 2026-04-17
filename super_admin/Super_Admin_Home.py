from __future__ import annotations

import os
import sys

import streamlit as st

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from services.app_ui_service import apply_page_chrome
from services.platform_admin_service import is_platform_admin

st.set_page_config(
    page_title="SortView Super Admin",
    page_icon="🛠️",
    layout="wide",
)

apply_page_chrome()

if "auth_user" not in st.session_state or st.session_state["auth_user"] is None:
    st.error("Please log in first.")
    st.stop()

auth_user = st.session_state["auth_user"]

if not is_platform_admin(auth_user["id"]):
    st.error("You do not have platform admin access.")
    st.stop()

st.title("SortView Super Admin")
st.caption("Platform administration only.")

st.success("Platform admin access confirmed.")
st.write("Use the Provision Library page in the sidebar.")
