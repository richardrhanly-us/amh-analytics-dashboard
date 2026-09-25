"""Browser-side persistence for the opaque session token from session_service.

This module knows NOTHING about users, passwords, or auth_sessions -- it only
ever handles a string token and a datetime. The token is set via a tiny,
first-party Streamlit CCv2 component (registered once, below); it is read
back via st.context.cookies, which needs no component at all since it
reflects the Cookie header Streamlit already received on the page's initial
HTTP request.

WHY NOT A THIRD-PARTY COOKIE PACKAGE. extra-streamlit-components (the
better-maintained of the two evaluated) has an open, upstream-acknowledged
bug (github.com/Mohamed-512/Extra-Streamlit-Components issue #77: "cookie
set / delete is not sync operation") describing exactly the set/rerun race
this module is designed to avoid -- see render_cookie_writer()'s docstring.
streamlit-cookies-controller has had no release since 2024-04-10 and its
documented API has no Secure/SameSite/Path/expiry parameters at all.
Writing ~25 lines of first-party CCv2 avoids both problems and adds zero
new requirements.txt entries.

RACE-CONDITION DESIGN. The writer component is mounted from
render_cookie_writer(), which must be called once per script run, from a
render that does NOT ALSO call st.rerun(). set_session_cookie()/
clear_session_cookie() only stage a pending operation in st.session_state;
the actual document.cookie write happens on the NEXT natural render (e.g.
the rerun a login/logout action already triggers), never in the same
script run as the st.rerun() call that produced it. document.cookie itself
is synchronous once the JS executes -- the only real hazard is the
component never getting a chance to render before a forced rerun tears
down the DOM, which this call sequencing avoids by construction. No
acknowledgment/confirmation trigger is used: nothing here needs to know
WHEN the browser ran the JS, only that it eventually will before the next
real user interaction, which the sequencing above already guarantees.

*** STEP 4 REQUIREMENT -- NOT YET IMPLEMENTED, DOCUMENTED HERE SO IT ISN'T
LOST ***
st.context.cookies reflects the Cookie header from the CURRENT Streamlit
session's initial HTTP request, and does not update for the rest of that
session's lifetime -- not even after this module's own JS deletes the
cookie. Concretely: a logout that clears the cookie and calls st.rerun()
will still see the OLD cookie value in st.context.cookies on that very next
rerun, because it's still the same underlying session/connection. If a
future cookie-restoration helper (Step 4, in app.py) blindly trusted
st.context.cookies after logout, a user could log out, and the immediately
following rerun could read the stale cookie and silently re-authenticate
them.

The Step 4 logout/forced-revocation flow MUST therefore set a session_state
flag, e.g.:

    st.session_state["_sortview_suppress_cookie_restore"] = True

before clearing auth_user and calling st.rerun(). Whatever future function
restores a session from the cookie MUST check and honor this flag, refusing
to restore while it is set. A genuine hard browser refresh starts a brand
new Streamlit session with a fresh st.context.cookies read (correctly
reflecting the now-deleted cookie), at which point the flag is no longer
needed and can be treated as expired/irrelevant for that new session.
This module does not implement any part of this yet -- see Step 4.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import streamlit as st

_PENDING_KEY = "_sortview_pending_cookie_op"

_COOKIE_STEM = "sortview_session"


def _cookie_secure() -> bool:
    return os.getenv("SORTVIEW_COOKIE_SECURE", "true").strip().lower() == "true"


def _cookie_name() -> str:
    # __Host- is a browser-ENFORCED prefix: a cookie with this name is only
    # ever accepted if it was set with Secure, Path=/, and no Domain
    # attribute -- exactly what this module always sends. __Host- cookies
    # cannot be set over plain HTTP, so local/non-HTTPS dev uses the plain
    # name instead (set SORTVIEW_COOKIE_SECURE=false for that case).
    return f"__Host-{_COOKIE_STEM}" if _cookie_secure() else _COOKIE_STEM


# --- the component (registered once, at import time) ----------------------

_COOKIE_WRITER = st.components.v2.component(
    "sortview_cookie_writer",
    html="<div id='sortview-cookie-writer'></div>",
    js="""
export default function (component) {
  const { data } = component
  if (!data || !data.action) return

  const attrs = ["Path=/"]
  if (data.secure) attrs.push("Secure")
  attrs.push("SameSite=Lax")

  if (data.action === "set") {
    attrs.push(`Max-Age=${data.max_age_seconds}`)
    document.cookie = `${data.name}=${encodeURIComponent(data.value)}; ${attrs.join("; ")}`
  } else if (data.action === "clear") {
    attrs.push("Max-Age=0")
    document.cookie = `${data.name}=; ${attrs.join("; ")}`
  }
}
""",
)


# --- read (no component involved -- see module docstring) -----------------

def get_session_cookie() -> str | None:
    try:
        return st.context.cookies.get(_cookie_name())
    except Exception:
        return None


# --- stage a write/clear (actual DOM write happens in render_cookie_writer) -

def set_session_cookie(
    token: str,
    expires_at: datetime,
    *,
    now: datetime | None = None,
) -> None:
    current_time = now or datetime.now(UTC)
    max_age_seconds = max(0, int((expires_at - current_time).total_seconds()))
    st.session_state[_PENDING_KEY] = {
        "action": "set",
        "name": _cookie_name(),
        "value": token,
        "max_age_seconds": max_age_seconds,
        "secure": _cookie_secure(),
    }


def clear_session_cookie() -> None:
    st.session_state[_PENDING_KEY] = {
        "action": "clear",
        "name": _cookie_name(),
        "secure": _cookie_secure(),
    }


def render_cookie_writer() -> None:
    """Call once per script run, from a render that does NOT also call
    st.rerun() -- see the module docstring's RACE-CONDITION DESIGN note.
    A no-op when nothing is pending; pops the pending op immediately after
    mounting so it is written exactly once, never re-mounted on a later
    rerun."""
    pending: dict[str, Any] | None = st.session_state.pop(_PENDING_KEY, None)
    if pending is None:
        return
    _COOKIE_WRITER(key="sortview_cookie_writer", data=pending)
