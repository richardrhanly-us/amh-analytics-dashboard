from __future__ import annotations

import pandas as pd
import streamlit as st

from services.app_ui_service import apply_page_chrome
from services.platform_admin_service import is_platform_admin
from services.tenant_service import get_organization_by_slug

st.set_page_config(
    page_title="Super Admin",
    page_icon="🛠️",
    layout="wide",
)

apply_page_chrome()

if "auth_user" not in st.session_state or st.session_state["auth_user"] is None:
    st.error("Please log in from the main app first.")
    st.stop()

auth_user = st.session_state["auth_user"]

if not is_platform_admin(auth_user["id"]):
    st.error("You do not have platform admin access.")
    st.stop()

st.caption("SortView Platform")
st.title("Super Admin")
st.caption("Platform-only administration pages.")

st.success("Platform admin access confirmed.")

st.markdown("### Available Actions")
st.write("- Open Provision Library to create a new tenant and generate agent config.")
