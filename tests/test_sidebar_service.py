from __future__ import annotations

from streamlit.testing.v1 import AppTest


def test_sidebar_logout_clears_persistent_auth_and_session_state():
    script = """
import sys
sys.path.insert(0, "src")

import streamlit as st
from services import auth_service, persistent_auth_service, sidebar_service


def fake_log_auth_event(**kwargs):
    st.session_state["_logout_audit_called"] = True


def fake_clear_persistent_auth():
    st.session_state["_persistent_clear_called"] = True


auth_service.log_auth_event = fake_log_auth_event
persistent_auth_service.clear_persistent_auth = fake_clear_persistent_auth


auth_user = st.session_state.get("auth_user")

if auth_user is not None:
    sidebar_service.render_main_sidebar(
        auth_user=auth_user,
        entitlement_context={"role": "admin"},
        org_options={"Acme": "acme"},
        selected_org_slug=st.session_state["selected_org_slug"],
        branch_options={"Main": "main"},
        selected_branch_slug=st.session_state["selected_branch_slug"],
        show_admin_button=False,
    )
"""

    at = AppTest.from_string(script)

    at.session_state["auth_user"] = {
        "id": 1,
        "email": "someone@example.invalid",
    }
    at.session_state["selected_org_slug"] = "acme"
    at.session_state["selected_branch_slug"] = "main"

    at.run()

    logout_buttons = [b for b in at.button if b.label == "Log out"]
    assert len(logout_buttons) == 1

    logout_buttons[0].click()
    at.run()

    assert at.session_state["_persistent_clear_called"] is True
    assert at.session_state["_logout_audit_called"] is True
    assert at.session_state["auth_user"] is None
    assert "selected_org_slug" not in at.session_state
    assert "selected_branch_slug" not in at.session_state