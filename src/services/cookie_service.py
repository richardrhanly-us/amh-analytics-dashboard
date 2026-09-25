"""Browser-side persistence for the opaque session token from session_service.

This module knows NOTHING about users, passwords, or auth_sessions -- it only
ever handles a string token and a datetime. The token is written and read via
small first-party Streamlit CCv2 components registered once below.

The reader intentionally uses document.cookie rather than st.context.cookies.
Production-equivalent testing on Streamlit Community Cloud proved that the
browser accepted the custom __Host- cookie while st.context.cookies did not
expose it, including from a newly opened browser tab. Reading through the
component therefore uses the browser's actual cookie state.

WHY NOT A THIRD-PARTY COOKIE PACKAGE. extra-streamlit-components (the
better-maintained of the two evaluated) has an open, upstream-acknowledged
bug (github.com/Mohamed-512/Extra-Streamlit-Components issue #77: "cookie
set / delete is not sync operation") describing exactly the set/rerun race
this module is designed to avoid -- see render_cookie_writer()'s docstring.
streamlit-cookies-controller has had no release since 2024-04-10 and its
documented API has no Secure/SameSite/Path/expiry parameters at all.
Writing a small first-party CCv2 implementation avoids both problems and adds
zero new requirements.txt entries.

RACE-CONDITION DESIGN. The writer component is mounted from
render_cookie_writer(), which must be called once per script run, from a
render that does NOT ALSO call st.rerun(). set_session_cookie()/
clear_session_cookie() only stage a pending operation in st.session_state;
the actual document.cookie write happens on the NEXT natural render (e.g.
the rerun a login/logout action already triggers), never in the same
script run as the st.rerun() call that produced it. document.cookie itself
is synchronous once the JS executes -- the only real hazard is the
component never getting a chance to render before a forced rerun tears
down the DOM, which this call sequencing avoids by construction.

The reader uses Component v2 state. JavaScript reads document.cookie and
reports the current cookie value to Python with setStateValue(). If the
browser value differs from the component's stored state, Streamlit reruns
and the ComponentResult returned to Python reflects the new value.

*** STEP 4 REQUIREMENT -- NOT YET IMPLEMENTED, DOCUMENTED HERE SO IT ISN'T
LOST ***

Component state is persistent across Streamlit reruns. After logout or forced
revocation, Python may therefore briefly still have the reader's previous
token value until the frontend observes the cleared browser cookie and sends
the new None state back.

The Step 4 logout/forced-revocation flow MUST therefore set a session_state
flag, e.g.:

    st.session_state["_sortview_suppress_cookie_restore"] = True

before clearing auth_user and rerunning. The future restoration helper MUST
honor this flag and refuse restoration while logout/revocation is being
completed. The reader will subsequently synchronize its state from the actual
browser cookie.

This module does not implement any part of that restoration flow yet -- see
Step 4.
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


# --- components (registered once, at import time) --------------------------

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

_COOKIE_READER = st.components.v2.component(
    "sortview_cookie_reader",
    html="<div id='sortview-cookie-reader'></div>",
    js="""
export default function (component) {
  const { data, setStateValue } = component
  if (!data || !data.name) return

  const prefix = `${data.name}=`
  const match = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))

  if (!match) {
    setStateValue("value", null)
    return
  }

  const encoded = match.slice(prefix.length)

  try {
    setStateValue("value", decodeURIComponent(encoded))
  } catch {
    setStateValue("value", null)
  }
}
""",
)


# --- read ------------------------------------------------------------------

def get_session_cookie() -> str | None:
    """Read the current session token directly from the browser cookie.

    On the first mount the Python-side default is None. If JavaScript finds a
    cookie whose value differs from that state, setStateValue() causes
    Streamlit to rerun and the returned ComponentResult then contains the
    browser value.
    """
    try:
        result = _COOKIE_READER(
            key="sortview_cookie_reader",
            data={"name": _cookie_name()},
            default={"value": None},
            on_value_change=lambda: None,
        )
        return result.value
    except Exception:
        return None


# --- stage a write/clear ---------------------------------------------------

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
    """Mount a staged browser-cookie write exactly once.

    Call once per script run from a render that does NOT also call st.rerun().
    See the module docstring's RACE-CONDITION DESIGN note.

    This is a no-op when nothing is pending. The pending operation is popped
    immediately before mounting so it cannot be mounted again on a later
    rerun.
    """
    pending: dict[str, Any] | None = st.session_state.pop(_PENDING_KEY, None)

    if pending is None:
        return

    _COOKIE_WRITER(
        key="sortview_cookie_writer",
        data=pending,
    )