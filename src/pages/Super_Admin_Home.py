from __future__ import annotations

import pandas as pd
import streamlit as st

from services.app_ui_service import apply_page_chrome
from services.platform_admin_service import is_platform_admin, list_platform_libraries

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
st.caption("Provision libraries and manage the platform.")

library_rows = list_platform_libraries()

top_col1, top_col2 = st.columns(2)

with top_col1:
    st.metric("Organizations", len({row["organization_id"] for row in library_rows}))

with top_col2:
    st.metric("Primary Branch Rows", len(library_rows))

st.markdown("### Libraries")

if len(library_rows) == 0:
    st.info("No libraries found.")
else:
    df = pd.DataFrame(library_rows)
    st.dataframe(df, use_container_width=True)

st.markdown("### Next Steps")
st.write("Use the Provision Library page to create a new tenant and generate its agent config.")
