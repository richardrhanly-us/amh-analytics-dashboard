"""Streamlit side of active-session enforcement.

auth_service.enforce_active_session decides WHETHER a session must end (the
account check, the audit event, and the order of the checks all stay there).
This module only carries out what that means inside a running Streamlit
script: asking persistent_auth_service about the current browser's token,
clearing persistent auth, cleaning st.session_state, showing the message, and
halting the script with st.stop().

Streamlit-specific by design. auth_service imports this module inside
enforce_active_session rather than at module scope, so auth_service itself
stays importable without Streamlit; nothing outside a Streamlit script should
import this module. It must not import auth_service.
"""

from __future__ import annotations

import streamlit as st

from services import persistent_auth_service


def _end_session(message: str) -> None:
    st.session_state["auth_user"] = None
    st.session_state.pop("selected_org_slug", None)
    st.session_state.pop("selected_branch_slug", None)

    st.error(message)
    st.stop()


def terminate_inactive_session(user_id: int) -> None:
    """Revokes every persistent session for the deactivated account, clears
    this browser, and halts the script."""
    persistent_auth_service.clear_all_persistent_auth_for_current_user(
        user_id
    )

    _end_session(
        "Your account has been deactivated. "
        "Please contact an administrator."
    )


def current_session_is_valid(user_id: int) -> bool:
    return persistent_auth_service.current_persistent_auth_is_valid(user_id)


def terminate_invalid_session() -> None:
    """Clears only the current browser's persistent session and halts the
    script."""
    persistent_auth_service.clear_persistent_auth()

    _end_session("Your session is no longer valid. Please log in again.")
