"""Tests for src/services/app_ui_service.py::render_app_header's admin
settings button (WCAG 4.1.2 Name, Role, Value).

The button used to be icon-only (st.button("⚙️", help="Admin Settings")):
a bare emoji has no reliable accessible name, and Streamlit's help= is a
separate hover tooltip, never the button's own name. It now carries real
visible text ("⚙️ Admin Settings"), which IS the button's accessible name
on every platform/AT combination, with no reliance on help= or any custom
ARIA.

render_app_header calls st.columns/st.button/st.switch_page, so these
tests drive it through a real Streamlit script via AppTest rather than
monkeypatching each call individually.
"""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

_SCRIPT = """
import sys
sys.path.insert(0, {src!r})
from services.app_ui_service import render_app_header

render_app_header(
    library_name="Test Library",
    branch_name="Main",
    system_name="Test AMH",
    show_admin_button={show_admin_button},
)
""".strip()


def _run(show_admin_button: bool) -> AppTest:
    at = AppTest.from_string(_SCRIPT.format(src=str(SRC), show_admin_button=show_admin_button))
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_admin_button_has_meaningful_visible_text():
    at = _run(show_admin_button=True)
    labels = [b.label for b in at.button]
    assert "⚙️ Admin Settings" in labels


def test_admin_button_label_is_not_icon_only():
    # The prior, failing shape was a bare emoji with no other characters --
    # guard specifically against ever regressing back to that, not just
    # checking *some* label is present.
    at = _run(show_admin_button=True)
    admin_button = next(b for b in at.button if "Admin Settings" in b.label)
    assert admin_button.label.strip() != "⚙️"
    assert "Admin Settings" in admin_button.label


def test_admin_button_hidden_when_show_admin_button_is_false():
    at = _run(show_admin_button=False)
    labels = [b.label for b in at.button]
    assert "⚙️ Admin Settings" not in labels
    assert len(at.button) == 0


_SWITCH_PAGE_SCRIPT = """
import sys
sys.path.insert(0, {src!r})
import streamlit as st
from services import app_ui_service

# Capture st.switch_page's argument instead of letting the real call run --
# AppTest has no real pages/ sibling to switch to, and this project's own
# privacy hardening (services.privacy_hardening) deliberately redacts
# exception text from the page, so asserting on a raised exception's
# message is not a reliable signal here either way. Capturing the call
# directly is exact regardless of either of those.
calls = []
app_ui_service.st.switch_page = lambda target: calls.append(target)

app_ui_service.render_app_header(
    library_name="Test Library",
    branch_name="Main",
    system_name="Test AMH",
    show_admin_button=True,
)
st.session_state["switch_page_calls"] = calls
""".strip()


def test_clicking_admin_button_navigates_to_admin_settings_page():
    at = AppTest.from_string(_SWITCH_PAGE_SCRIPT.format(src=str(SRC)))
    at.run()
    assert not at.exception, [e.value for e in at.exception]

    admin_button = next(b for b in at.button if "Admin Settings" in b.label)
    admin_button.click().run()
    assert not at.exception, [e.value for e in at.exception]

    # Behavior/navigation target unchanged: still switches to exactly
    # pages/1_admin_settings.py, the same as before this button's label
    # changed from icon-only to visible text.
    assert at.session_state["switch_page_calls"] == ["pages/1_admin_settings.py"]


def test_header_still_renders_branding_content():
    # Regression guard: the header's other content (title, library/branch/
    # system line) must be unaffected by the button-label change.
    at = _run(show_admin_button=True)
    markdown_html = " ".join(m.value for m in at.markdown if m.value)
    assert "sortview-title" in markdown_html or "SORTVIEW" in markdown_html
    assert "Test Library" in markdown_html
    assert "Main" in markdown_html
    assert "Test AMH" in markdown_html
