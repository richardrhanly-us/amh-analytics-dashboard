"""Coordination layer for persistent browser login sessions.

This service connects the browser cookie mechanism in cookie_service with the
database-backed opaque sessions in session_service.

It deliberately does not authenticate passwords and does not make authorization
decisions. auth_service remains responsible for account authentication and
active-account enforcement.

The raw persistent-session token exists only in:
- the browser cookie, and
- the current Streamlit server-side session_state.

Only its SHA-256 hash is stored in PostgreSQL by session_service.
"""

from __future__ import annotations

import streamlit as st

from services import cookie_service, session_service

_PERSISTENT_SESSION_STATE_KEY = "_sortview_persistent_session"
_SUPPRESS_RESTORE_KEY = "_sortview_suppress_cookie_restore"


def restore_persistent_auth() -> dict | None:
    """Restore auth_user from a valid persistent browser session.

    Returns the current/restored auth user or None.

    A suppression flag prevents an immediately-cleared/revoked token from
    re-authenticating the user while the browser component is still
    synchronizing its state after logout or forced termination.
    """
    existing_user = st.session_state.get("auth_user")
    if existing_user is not None:
        return existing_user

    if st.session_state.get(_SUPPRESS_RESTORE_KEY):
        return None

    token = cookie_service.get_session_cookie()
    if not token:
        return None

    user = session_service.validate_session(token)

    if user is None:
        st.session_state.pop(_PERSISTENT_SESSION_STATE_KEY, None)
        cookie_service.clear_session_cookie()
        return None

    st.session_state["auth_user"] = user
    st.session_state[_PERSISTENT_SESSION_STATE_KEY] = token
    return user


def create_persistent_auth(user_id: int) -> dict:
    """Create and stage persistence after a successful normal login."""
    session = session_service.create_session(user_id)

    token = session["token"]
    expires_at = session["expires_at"]

    # An explicit successful login starts a new persistence lifecycle.
    st.session_state.pop(_SUPPRESS_RESTORE_KEY, None)
    st.session_state[_PERSISTENT_SESSION_STATE_KEY] = token

    cookie_service.set_session_cookie(
        token,
        expires_at,
    )

    return session


def clear_persistent_auth() -> bool:
    """Revoke and clear the current browser's persistent login session.

    Suppression is set before revocation/clear so a stale frontend component
    value cannot immediately restore the just-ended session.
    """
    st.session_state[_SUPPRESS_RESTORE_KEY] = True

    token = st.session_state.pop(_PERSISTENT_SESSION_STATE_KEY, None)

    revoked = False
    if token:
        revoked = session_service.revoke_session(token)

    cookie_service.clear_session_cookie()
    return revoked


def clear_all_persistent_auth_for_current_user(user_id: int) -> int:
    """Revoke all persistent sessions for the current user and clear this browser.

    Intended for forced termination of the CURRENT authenticated account, such
    as when enforce_active_session discovers that the account was deactivated.

    This must not be used when an administrator is modifying some other user's
    account because clearing the browser cookie would affect the administrator's
    own session.
    """
    st.session_state[_SUPPRESS_RESTORE_KEY] = True
    st.session_state.pop(_PERSISTENT_SESSION_STATE_KEY, None)

    revoked_count = session_service.revoke_all_sessions_for_user(user_id)
    cookie_service.clear_session_cookie()

    return revoked_count