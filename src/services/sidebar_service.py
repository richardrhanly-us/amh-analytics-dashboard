from __future__ import annotations

import streamlit as st

from services import auth_service


def render_main_sidebar(
    auth_user: dict,
    entitlement_context: dict,
    org_options: dict[str, str],
    selected_org_slug: str,
    branch_options: dict[str, str],
    selected_branch_slug: str,
    show_admin_button: bool,
) -> None:
    with st.sidebar:
        st.caption(auth_user["email"])
        st.caption(f'Role: {entitlement_context.get("role", "unknown")}')

        subscription = entitlement_context.get("subscription")
        if subscription:
            st.caption(f'Plan: {subscription.get("plan_name", "Unknown")}')

        if len(org_options) > 1:
            new_org_names = list(org_options.keys())
            new_org_name = st.selectbox(
                "Organization",
                options=new_org_names,
                index=list(org_options.values()).index(selected_org_slug),
            )
            new_org_slug = org_options[new_org_name]

            if new_org_slug != st.session_state["selected_org_slug"]:
                st.session_state["selected_org_slug"] = new_org_slug
                st.session_state.pop("selected_branch_slug", None)
                st.rerun()

        if len(branch_options) > 1:
            new_branch_names = list(branch_options.keys())
            new_branch_name = st.selectbox(
                "Branch",
                options=new_branch_names,
                index=list(branch_options.values()).index(selected_branch_slug),
            )
            new_branch_slug = branch_options[new_branch_name]

            if new_branch_slug != st.session_state["selected_branch_slug"]:
                st.session_state["selected_branch_slug"] = new_branch_slug
                st.rerun()

        if show_admin_button:
            st.markdown("---")
            st.caption("Admin")
            st.page_link("app.py", label="Dashboard", icon="🏠")
            st.page_link("pages/1_admin_settings.py", label="Admin settings", icon="⚙️")
            st.page_link("pages/2_admin_users.py", label="Admin users", icon="👥")

        with st.expander("Change password"):
            with st.form("change_password_form"):
                current_password = st.text_input("Current password", type="password")
                new_password = st.text_input("New password", type="password")
                confirm_password = st.text_input("Confirm new password", type="password")
                change_password_submitted = st.form_submit_button("Update password")

            if change_password_submitted:
                result = auth_service.change_password(
                    user_id=auth_user["id"],
                    current_password=current_password,
                    new_password=new_password,
                    confirm_password=confirm_password,
                )

                if result["ok"]:
                    st.success(result["message"])
                else:
                    st.error(result["message"])

        if st.button("Log out"):
            auth_service.log_auth_event(
                event_type="logout",
                is_success=True,
                user_id=auth_user["id"],
                email=auth_user["email"],
                message="User logged out.",
                metadata={
                    "selected_org_slug": st.session_state.get("selected_org_slug"),
                    "selected_branch_slug": st.session_state.get("selected_branch_slug"),
                },
            )
            st.session_state["auth_user"] = None
            st.session_state.pop("selected_org_slug", None)
            st.session_state.pop("selected_branch_slug", None)
            st.rerun()
