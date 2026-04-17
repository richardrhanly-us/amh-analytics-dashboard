from __future__ import annotations

import os
import sys

import streamlit as st

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from super_auth import require_super_admin

st.set_page_config(
    page_title="SortView Super Admin",
    page_icon="🛠️",
    layout="wide",
)


auth_user = require_super_admin()

st.title("SortView Super Admin")
st.caption("Platform administration only.")
st.success(f"Platform admin access confirmed for {auth_user['email']}.")
st.write("Use the pages in the sidebar.")
