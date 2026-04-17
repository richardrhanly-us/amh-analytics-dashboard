from __future__ import annotations

import os
import sys

import streamlit as st

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import services.auth_service as auth_service
from services.platform_admin_service import is_platform_admin


def require_super_admin():
    if "auth_user" not in st.session_state:
        st.session_state["auth_user"] = None

    if st.session_state["auth_user"] is None:
        st.title("SortView Super Admin Login")

        with st.form("super_admin_login_form"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log In")

        if submitted:
            result = auth_service.authenticate_user(email=email, password=password)
            if result["ok"]:
                st.session_state["auth_user"] = result["user"]
                st.rerun()
            else:
                st.error(result["message"])

        st.stop()

    auth_user = st.session_state["auth_user"]

    if not is_platform_admin(auth_user["id"]):
        st.error("You do not have platform admin access.")

        if st.button("Log out"):
            st.session_state["auth_user"] = None
            st.rerun()

        st.stop()

    with st.sidebar:
        st.page_link(
            "Super_Admin_Home.py",
            label="Home",
            icon="🏠",
        )
        st.page_link(
            "pages/Provision_Library.py",
            label="Provision Library",
            icon="🏗️",
        )

        st.divider()
        st.caption(f"Logged in as {auth_user['email']}")

        if st.button("Log out"):
            st.session_state["auth_user"] = None
            st.rerun()

    return auth_user
