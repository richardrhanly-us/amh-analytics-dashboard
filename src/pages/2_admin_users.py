from __future__ import annotations

import pandas as pd
import streamlit as st

from services.app_ui_service import apply_page_chrome
from services.access_service import get_user_memberships, get_org_branches
from services.entitlement_service import build_entitlement_context
from services.permission_service import can_manage_settings
from services.sidebar_service import render_main_sidebar
from services.user_admin_service import (
    list_org_users,
    create_or_add_org_user,
    update_org_user_role,
    set_user_active,
    list_recent_org_auth_events,
)

st.set_page_config(
    page_title="Admin Users",
    page_icon="👥",
    layout="wide",
)

apply_page_chrome()

if "auth_user" not in st.session_state or st.session_state["auth_user"] is None:
    st.error("Please log in from the main app first.")
    st.stop()

auth_user = st.session_state["auth_user"]
user_memberships = get_user_memberships(auth_user["id"])

if not user_memberships:
    st.error("Your account does not have access to any organizations.")
    st.stop()

allowed_org_slugs = [m["organization_slug"] for m in user_memberships]

if (
    "selected_org_slug" not in st.session_state
    or st.session_state["selected_org_slug"] not in allowed_org_slugs
):
    st.session_state["selected_org_slug"] = allowed_org_slugs[0]

selected_org_slug = st.session_state["selected_org_slug"]

org_options = {
    m["organization_name"]: m["organization_slug"]
    for m in user_memberships
}

branch_rows = get_org_branches(selected_org_slug)

if not branch_rows:
    st.error("No active branches were found for this organization.")
    st.stop()

allowed_branch_slugs = [b["branch_slug"] for b in branch_rows]

if (
    "selected_branch_slug" not in st.session_state
    or st.session_state["selected_branch_slug"] not in allowed_branch_slugs
):
    primary_branch = next((b for b in branch_rows if b["is_primary"]), None)
    st.session_state["selected_branch_slug"] = (
        primary_branch["branch_slug"] if primary_branch else allowed_branch_slugs[0]
    )

selected_branch_slug = st.session_state["selected_branch_slug"]

branch_options = {
    b["branch_name"]: b["branch_slug"]
    for b in branch_rows
}

entitlement_context = build_entitlement_context(
    user_id=auth_user["id"],
    org_slug=selected_org_slug,
)

show_admin_button = can_manage_settings(entitlement_context)

if not show_admin_button:
    st.error("You do not have permission to manage users.")
    st.stop()

render_main_sidebar(
    auth_user=auth_user,
    entitlement_context=entitlement_context,
    org_options=org_options,
    selected_org_slug=selected_org_slug,
    branch_options=branch_options,
    selected_branch_slug=selected_branch_slug,
    show_admin_button=show_admin_button,
)

selected_org_slug = st.session_state["selected_org_slug"]
selected_branch_slug = st.session_state["selected_branch_slug"]

st.title("Admin / Users")

selected_org_name = next(
    name for name, slug in org_options.items()
    if slug == selected_org_slug
)
st.caption(f"Organization: {selected_org_name}")

users = list_org_users(selected_org_slug)
user_map = {u["user_id"]: u for u in users}

st.subheader("Organization users")
if users:
    users_df = pd.DataFrame(users)
    st.dataframe(users_df, use_container_width=True, hide_index=True)
else:
    st.info("No users found for this organization yet.")

st.subheader("Add user")
with st.form("add_user_form"):
    full_name = st.text_input("Full name")
    email = st.text_input("Email")
    password = st.text_input("Temporary password", type="password")
    role = st.selectbox("Role", ["owner", "admin", "manager", "viewer"])
    add_user_submitted = st.form_submit_button("Create or add user")

if add_user_submitted:
    result = create_or_add_org_user(
        org_slug=selected_org_slug,
        email=email,
        password=password,
        full_name=full_name,
        role=role,
    )
    if result["ok"]:
        st.success(result["message"])
        st.rerun()
    else:
        st.error(result["message"])

st.subheader("Update role")
if users:
    with st.form("update_role_form"):
        selected_role_user_id = st.selectbox(
            "User",
            options=[u["user_id"] for u in users],
            format_func=lambda uid: f'{user_map[uid]["email"]} ({user_map[uid]["role"]})',
        )
        new_role = st.selectbox("New role", ["owner", "admin", "manager", "viewer"])
        update_role_submitted = st.form_submit_button("Update role")

    if update_role_submitted:
        result = update_org_user_role(
            org_slug=selected_org_slug,
            user_id=selected_role_user_id,
            role=new_role,
        )
        if result["ok"]:
            st.success(result["message"])
            st.rerun()
        else:
            st.error(result["message"])

st.subheader("Activate / deactivate user")
if users:
    with st.form("activate_deactivate_form"):
        selected_status_user_id = st.selectbox(
            "User to update",
            options=[u["user_id"] for u in users],
            format_func=lambda uid: f'{user_map[uid]["email"]} (active={user_map[uid]["is_active"]})',
        )
        desired_status = st.selectbox("Set active status", [True, False])
        update_status_submitted = st.form_submit_button("Update status")

    if update_status_submitted:
        result = set_user_active(
            user_id=selected_status_user_id,
            is_active=desired_status,
        )
        if result["ok"]:
            st.success(result["message"])
            st.rerun()
        else:
            st.error(result["message"])

st.subheader("Recent auth activity")
events = list_recent_org_auth_events(selected_org_slug, limit=25)
if events:
    events_df = pd.DataFrame(events)
    st.dataframe(events_df, use_container_width=True, hide_index=True)
else:
    st.info("No auth activity found yet for this organization.")
