"""Standalone manual-verification probe for the Step 3 cookie mechanism.
NOT wired into app.py or any page navigation. Run directly:

    streamlit run scratch/cookie_probe.py

Uses a distinct cookie name and a harmless fixed test value -- never a real
session token -- so it can't collide with or be confused for a real login.

IMPORTANT -- READ BEFORE CLICKING ANYTHING. The browser cookie is read through
a first-party CCv2 component using document.cookie. The component reports its
value to Python with setStateValue(). If that value changes, Streamlit reruns
the script and the returned component result reflects the browser's current
cookie value.

A hard browser refresh remains useful for this manual test because it proves
the cookie actually survives a fresh page load rather than merely persisting
inside Streamlit session state.

TWO VALIDATION MODES -- this probe mirrors cookie_service's own
SORTVIEW_COOKIE_SECURE-driven naming exactly, so it exercises the SAME
secure/plain branching the real cookie_service.py uses, not a hard-coded
stand-in.

1) LOCAL FUNCTIONAL CHECK (plain HTTP, e.g. localhost)

   PowerShell:
       $env:SORTVIEW_COOKIE_SECURE="false"
       streamlit run scratch/cookie_probe.py

   Expected cookie name: sortview_session_probe
   Proves: the components mount, a SET survives a hard refresh,
   the browser reader returns it to Python, and a CLEAR survives a hard
   refresh too. Does NOT prove Secure/__Host- behavior -- a plain HTTP origin
   can't meaningfully carry a Secure cookie at all.

2) PRODUCTION-EQUIVALENT HTTPS CHECK

   Run or deploy the probe over HTTPS with SORTVIEW_COOKIE_SECURE unset (it
   defaults to "true") or explicitly set to "true".

   Expected cookie name: __Host-sortview_session_probe

   This is the decisive check for the real __Host-sortview_session
   production cookie: after SET + hard refresh, open browser DevTools ->
   Application -> Cookies and confirm ALL of:
       - cookie exists, named __Host-sortview_session_probe
       - Secure = true
       - SameSite = Lax
       - Path = /
       - no Domain attribute
       - Max-Age/expiry is approximately 14 days
   Then CLEAR + hard refresh and confirm the cookie row is gone entirely.
"""

import os

import streamlit as st

st.title("SortView cookie probe (manual test only)")

secure = (
    os.getenv("SORTVIEW_COOKIE_SECURE", "true")
    .strip()
    .lower()
    == "true"
)

PROBE_COOKIE_NAME = (
    "__Host-sortview_session_probe"
    if secure
    else "sortview_session_probe"
)
PROBE_VALUE = "sortview-cookie-test"

_WRITER = st.components.v2.component(
    "cookie_probe_writer",
    html="<div id='probe-writer'></div>",
    js="""
export default function (component) {
  const { data } = component
  if (!data || !data.action) return

  const attrs = ["Path=/", "SameSite=Lax"]
  if (data.secure) attrs.push("Secure")

  if (data.action === "set") {
    attrs.push(`Max-Age=${data.max_age_seconds}`)
    document.cookie = `${data.name}=${encodeURIComponent(data.value)}; ${attrs.join("; ")}`
  } else {
    attrs.push("Max-Age=0")
    document.cookie = `${data.name}=; ${attrs.join("; ")}`
  }
}
""",
)

_READER = st.components.v2.component(
    "cookie_probe_reader",
    html="<div id='probe-reader'></div>",
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

st.caption(
    f"SORTVIEW_COOKIE_SECURE = {secure}  |  "
    f"cookie name = `{PROBE_COOKIE_NAME}`"
)

st.info(
    "Cookie value is read directly from document.cookie through a "
    "Streamlit Component v2 reader. A hard refresh proves the cookie "
    "survives a completely fresh page load."
)

reader_result = _READER(
    key="cookie_probe_reader",
    data={"name": PROBE_COOKIE_NAME},
    default={"value": None},
    on_value_change=lambda: None,
)
current = reader_result.value

st.write(
    f"Browser cookie currently reads: `{current}`"
    if current
    else "No probe cookie is currently visible to the browser reader."
)

st.markdown("---")

st.markdown(
    "**To test SET:** click *Set test cookie* below, then do a real hard "
    "refresh of this tab (not an in-app rerun). The value above should "
    f"then read `{PROBE_VALUE}`."
)

st.markdown(
    "**To test CLEAR:** click *Clear test cookie* below, then hard refresh "
    "again. The value above should report that no probe cookie is visible."
)

col1, col2 = st.columns(2)

if col1.button("Set test cookie"):
    st.session_state["_probe_pending"] = {
        "action": "set",
        "name": PROBE_COOKIE_NAME,
        "value": PROBE_VALUE,
        "max_age_seconds": 14 * 24 * 3600,
        "secure": secure,
    }
    st.rerun()

if col2.button("Clear test cookie"):
    st.session_state["_probe_pending"] = {
        "action": "clear",
        "name": PROBE_COOKIE_NAME,
        "secure": secure,
    }
    st.rerun()

pending = st.session_state.pop("_probe_pending", None)

if pending:
    _WRITER(
        key="cookie_probe_writer",
        data=pending,
    )
    st.caption(
        "Cookie op mounted for this render. "
        "Now hard-refresh the tab to verify."
    )