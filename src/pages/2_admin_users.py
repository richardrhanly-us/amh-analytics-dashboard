from __future__ import annotations

import pandas as pd
import streamlit as st

from services import auth_service
from services.access_service import get_org_access_mode
from services.app_ui_service import apply_page_chrome
from services.membership_policy import assignable_roles
from services.permission_service import can_manage_settings
from services.privacy_hardening import install_streamlit_log_scrubber
from services.sidebar_service import render_main_sidebar
from services.streamlit_access_adapter import (
    get_org_branches,
    get_user_memberships,
)
from services.streamlit_entitlement_adapter import build_entitlement_context
from services.user_admin_service import (
    add_organization_member,
    change_organization_member_role,
    list_org_users,
    list_recent_org_auth_events,
    remove_organization_member,
)

# Keep an uncaught page exception's text out of Streamlit's own server log (see services/privacy_hardening.py).
install_streamlit_log_scrubber()

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
auth_service.enforce_active_session(auth_user)
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

# This page is entirely administrative (user management), so anything
# less than full access blocks the whole page rather than partially
# rendering it -- matching the service-level enforcement in
# user_admin_service's mutating functions, which independently refuse to
# write regardless of whether this page-level gate is ever bypassed
# (e.g. direct URL navigation).
org_access_mode = get_org_access_mode(selected_org_slug)

if org_access_mode == "read_only":
    st.error(
        "This organization's account is currently suspended. Settings and user "
        "management are unavailable until it is reactivated by a platform "
        "administrator."
    )
    st.stop()

if org_access_mode == "blocked":
    st.error("This organization is no longer available. Please contact an administrator.")
    st.stop()

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

# The roles this person could give someone: an admin is not offered "owner", and someone who may not administer
# members is offered nothing -- there is no fallback list. Only what is offered: the service decides for itself,
# from the database, what the person acting may do, whatever a form sends it.
role_choices = list(assignable_roles(entitlement_context.get("role")))

if not show_admin_button or not role_choices:
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
    st.dataframe(users_df, width="stretch", hide_index=True)
else:
    st.info("No users found for this organization yet.")

st.subheader("Add user")
with st.form("add_user_form"):
    full_name = st.text_input("Full name")
    email = st.text_input("Email")
    password = st.text_input("Temporary password", type="password")
    role = st.selectbox("Role", role_choices)
    add_user_submitted = st.form_submit_button("Create or add user")

if add_user_submitted:
    result = add_organization_member(
        org_slug=selected_org_slug,
        email=email,
        password=password,
        full_name=full_name,
        role=role,
        actor_user_id=auth_user["id"],
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
        new_role = st.selectbox("New role", role_choices)
        update_role_submitted = st.form_submit_button("Update role")

    if update_role_submitted:
        result = change_organization_member_role(
            org_slug=selected_org_slug,
            user_id=selected_role_user_id,
            role=new_role,
            actor_user_id=auth_user["id"],
        )
        if result["ok"]:
            st.success(result["message"])
            st.rerun()
        else:
            st.error(result["message"])

# Takes the person out of THIS organization only. Their account, their password and any other organization they
# belong to are untouched: switching a whole account off is a platform operation and is not offered here.
st.subheader("Remove from organization")
if users:
    st.caption(
        "Removes this person's access to this organization only. Their SortView account stays active, "
        "and any other organization they belong to is not affected."
    )
    with st.form("remove_member_form"):
        selected_remove_user_id = st.selectbox(
            "User to remove",
            options=[u["user_id"] for u in users],
            format_func=lambda uid: f'{user_map[uid]["email"]} ({user_map[uid]["role"]})',
        )
        remove_confirmed = st.checkbox("Remove this user from this organization")
        remove_submitted = st.form_submit_button("Remove from organization")

    if remove_submitted and not remove_confirmed:
        st.error("Tick the box to confirm before removing a user.")
    elif remove_submitted:
        result = remove_organization_member(
            org_slug=selected_org_slug,
            user_id=selected_remove_user_id,
            actor_user_id=auth_user["id"],
        )
        if result["ok"]:
            st.success(result["message"])
            st.rerun()
        else:
            st.error(result["message"])

# Only what happened IN this organization: members added, removed and given roles. Sign-ins and password changes
# belong to the person's account, not to any one organization, and are not shown here.
st.subheader("Recent user management activity")
events = list_recent_org_auth_events(selected_org_slug, limit=25)
if events:
    events_df = pd.DataFrame(events)
    st.dataframe(events_df, width="stretch", hide_index=True)
else:
    st.info("No user management activity found yet for this organization.")
