"""Required-field indicators on real forms (WCAG 3.3.2 Labels or Instructions).

Streamlit widgets have no programmatic `required` HTML attribute to set, so
"required" here means: the field's label carries a trailing " *" and the
form shows a "* Required" caption near it -- a visible, consistent
convention rather than an invented/unsupported ARIA attribute.

Every field marked this way was confirmed, by reading the actual submit
handler in services/auth_service.py, to make that handler return a failure
when the field is empty (login: unknown-user lookup fails; reset/change
password: explicit `len(new_password) < 8` and mismatch checks; current
password: `check_password_hash` on an empty string never matches). Fields
were deliberately left UNMARKED where the code does not enforce them:
- src/pages/1_admin_settings.py's main settings form saves every field
  unconditionally (the one partial exception, an empty destination Key/
  Label, silently drops that row rather than rejecting the form).
- src/pages/2_admin_users.py's "Add user" form does not reject an empty
  email or temporary password anywhere in the call chain down to
  auth_service.create_user (full_name is optional by its own `= ""`
  default). This is a pre-existing validation gap, not something this
  change set alters -- fixing it would be a change to business logic,
  out of scope here.

This file does not re-verify that gap's absence of validation (see
tests/test_user_admin_service.py / tests/test_auth_service.py for the
service-level behavior); it only proves the UI marks exactly the fields
that ARE enforced, leaves the rest alone, and that submitting each marked
form still reaches the same service call with the same arguments as
before.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
APP = SRC / "app.py"


def _captions(at: AppTest) -> list[str]:
    return [c.value for c in at.caption]


def _labels(at: AppTest, widget: str) -> list[str]:
    return [w.label for w in getattr(at, widget)]


def _field(at: AppTest, label: str):
    (match,) = [t for t in at.text_input if t.label == label]
    return match


def _button(at: AppTest, label: str):
    (match,) = [b for b in at.button if b.label == label]
    return match


# --- src/app.py: login form + forgot-password form --------------------------
#
# src/app.py is the real multi-page app's MAIN script (a src/pages/ folder
# sits next to it). Driving it through AppTest.from_file in the same
# process as any later AppTest.from_string call was found, empirically, to
# leave Streamlit's own multi-page-app state contaminated: a subsequent
# AppTest.from_string script then fails with an unrelated
# "StreamlitAPIException: The title of the page cannot be empty..." raised
# from deep inside streamlit's navigation internals, not from anything in
# this codebase. tests/test_streamlit_startup.py and
# tests/test_real_apps_redaction.py already avoid this by running src/app.py
# in a subprocess; this file follows the same established convention for
# the same reason, doing both checks (labels + submit behavior) in one
# subprocess run to keep it to a single ~1s subprocess per test session.

_APP_PROBE = """
import json
import sys
sys.path.insert(0, {src!r})

from streamlit.testing.v1 import AppTest
from services import auth_service, persistent_auth_service

# AppTest has no browser-side JavaScript. These tests exercise the login and
# forgot-password forms, not persistent-cookie restoration.
persistent_auth_service.restore_persistent_auth_state = lambda: (True, None)

login_calls = []
def fake_authenticate_user(email, password):
    login_calls.append({{"email": email, "password": password}})
    return {{"ok": False, "code": "invalid_credentials", "message": "Invalid email or password."}}
auth_service.authenticate_user = fake_authenticate_user

reset_calls = []
def fake_request_password_reset(email):
    reset_calls.append(email)
    return {{"ok": True, "message": "If that email exists, a reset link has been sent."}}
auth_service.request_password_reset = fake_request_password_reset

result = {{}}

at = AppTest.from_file({app!r}, default_timeout=120)
at.run()
result["login_exception"] = [e.value for e in at.exception]
result["login_labels"] = [t.label for t in at.text_input]
result["login_captions"] = [c.value for c in at.caption]

(email,) = [t for t in at.text_input if t.label == "Email *"]
(password,) = [t for t in at.text_input if t.label == "Password *"]
email.input("someone@example.invalid")
password.input("a-password")
(submit,) = [b for b in at.button if b.label == "Log In"]
submit.click()
at.run()
result["login_submit_exception"] = [e.value for e in at.exception]
result["login_submit_errors"] = [e.value for e in at.error]
result["login_calls"] = login_calls

(forgot,) = [b for b in at.button if b.label == "Forgot password?"]
forgot.click()
at.run()
result["forgot_labels"] = [t.label for t in at.text_input]
result["forgot_captions"] = [c.value for c in at.caption]

(email2,) = [t for t in at.text_input if t.label == "Email *"]
email2.input("someone@example.invalid")
(send,) = [b for b in at.button if b.label == "Send Reset Link"]
send.click()
at.run()
result["reset_request_exception"] = [e.value for e in at.exception]
result["reset_calls"] = reset_calls

print("RESULT_JSON=" + json.dumps(result))
""".strip()


@pytest.fixture(scope="module")
def app_probe_result() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "PYTHONPATH"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    code = _APP_PROBE.format(src=str(SRC), app=str(APP))
    completed = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(code)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    (line,) = [line for line in completed.stdout.splitlines() if line.startswith("RESULT_JSON=")]
    return json.loads(line[len("RESULT_JSON="):])


def test_login_form_marks_email_and_password_as_required(app_probe_result):
    assert app_probe_result["login_exception"] == []
    assert "Email *" in app_probe_result["login_labels"]
    assert "Password *" in app_probe_result["login_labels"]
    assert "* Required" in app_probe_result["login_captions"]


def test_login_form_does_not_use_the_old_unmarked_labels(app_probe_result):
    # Regression guard against silently reverting the label change.
    assert "Email" not in app_probe_result["login_labels"]
    assert "Password" not in app_probe_result["login_labels"]


def test_login_submit_still_authenticates_with_the_entered_credentials(app_probe_result):
    assert app_probe_result["login_calls"] == [{"email": "someone@example.invalid", "password": "a-password"}]
    assert app_probe_result["login_submit_exception"] == []
    assert app_probe_result["login_submit_errors"] == ["Invalid email or password."]


def test_forgot_password_form_marks_email_as_required(app_probe_result):
    assert "Email *" in app_probe_result["forgot_labels"]
    assert "* Required" in app_probe_result["forgot_captions"]


def test_forgot_password_submit_still_calls_request_password_reset_with_the_entered_email(app_probe_result):
    assert app_probe_result["reset_calls"] == ["someone@example.invalid"]
    assert app_probe_result["reset_request_exception"] == []


# --- src/app.py: reset-password form (reached only via ?reset_token=...) ---
#
# This Streamlit/AppTest version (streamlit==1.52.0) exposes no way to set
# st.query_params before a page's first run, so the reset-token-gated
# branch can't be driven end-to-end through AppTest the way the other two
# forms above are. The exact source block is verified directly instead --
# the same three lines that were hand-edited for this change.


def test_reset_password_form_marks_both_password_fields_as_required():
    source = APP.read_text(encoding="utf-8")
    with_form, _, rest = source.partition('with st.form("reset_password_form"):')
    assert with_form != source, "reset_password_form not found in src/app.py"
    form_block = rest.split("st.stop()", 1)[0]

    assert '"New password *"' in form_block
    assert '"Confirm new password *"' in form_block
    assert 'st.caption("* Required")' in form_block


# --- src/services/sidebar_service.py: change-password form -----------------

_SIDEBAR_SCRIPT = """
import sys
sys.path.insert(0, {src!r})
import streamlit as st
from services import sidebar_service

calls = []
sidebar_service.auth_service.change_password = lambda **kwargs: (
    calls.append(kwargs) or {{"ok": True, "message": "Password updated."}}
)

sidebar_service.render_main_sidebar(
    auth_user={{"id": 1, "email": "someone@example.invalid"}},
    entitlement_context={{"role": "admin"}},
    org_options={{"Acme": "acme"}},
    selected_org_slug="acme",
    branch_options={{"Main": "main"}},
    selected_branch_slug="main",
    # False: st.page_link(...) (the admin-button branch) requires a real
    # multi-page-app navigation context that AppTest.from_string's
    # standalone temp script doesn't have -- irrelevant to the
    # change-password form under test here anyway.
    show_admin_button=False,
)
st.session_state["change_password_calls"] = calls
""".strip()


def _run_sidebar() -> AppTest:
    at = AppTest.from_string(_SIDEBAR_SCRIPT.format(src=str(SRC)))
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_change_password_form_marks_all_three_fields_as_required():
    at = _run_sidebar()
    labels = _labels(at, "text_input")
    assert "Current password *" in labels
    assert "New password *" in labels
    assert "Confirm new password *" in labels
    assert "* Required" in _captions(at)


def test_change_password_submit_still_calls_change_password_with_the_entered_values():
    at = _run_sidebar()
    _field(at, "Current password *").input("old-password")
    _field(at, "New password *").input("new-password-123")
    _field(at, "Confirm new password *").input("new-password-123")
    _button(at, "Update password").click()
    at.run()

    assert at.session_state["change_password_calls"] == [
        {
            "user_id": 1,
            "current_password": "old-password",
            "new_password": "new-password-123",
            "confirm_password": "new-password-123",
        }
    ]
    assert not at.exception, [e.value for e in at.exception]


# --- src/pages/1_admin_settings.py: admin-password unlock field ------------


def test_admin_unlock_field_marks_password_as_required(monkeypatch):
    at = _run_admin_settings_locked(monkeypatch)

    assert "Admin password *" in _labels(at, "text_input")
    assert "* Required" in _captions(at)


def test_admin_settings_main_form_has_no_required_field_markers(monkeypatch):
    # By design/finding: the "Save Settings" handler saves every field in
    # this form unconditionally, so nothing here is TRULY required -- this
    # locks in the decision to leave it unmarked rather than an accident.
    at = _run_admin_settings_unlocked(monkeypatch)

    assert not any(label.endswith(" *") for label in _labels(at, "text_input"))
    assert "* Required" not in _captions(at)


# --- shared admin-settings-page harness (mirrors tests/test_admin_password_protection.py) ---

PAGE = SRC / "pages" / "1_admin_settings.py"
_USER = {"id": 1, "email": "admin@example.invalid"}


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Conn:
    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        if "FROM organizations o" in sql:
            return _Result({"organization_id": 1, "organization_name": "Acme", "org_settings_row_id": 11,
                            "org_settings_json": self._engine.org_settings})
        if "FROM branches b" in sql:
            return _Result({"branch_id": 2, "branch_name": "Main", "branch_settings_row_id": None,
                            "branch_settings_json": {}})
        return _Result(None)


class _AdminSettingsStub:
    def __init__(self, org_settings=None):
        self.org_settings = org_settings or {}

    def connect(self):
        return _Conn(self)

    begin = connect


def _effective(security=None):
    return {
        "organization": {"id": 1, "name": "Acme", "slug": "acme"},
        "branch": {"id": 2, "name": "Main", "slug": "main", "is_primary": True},
        "subscription": None,
        "settings": {"security": security or {}, "transit": {"home_branch_label": "Home"}},
        "entitlements": {},
    }


def _patch_admin_settings_page(monkeypatch, *, security, engine):
    import database
    import services.access_service as access
    import services.entitlement_service as entitlement
    import services.permission_service as permission
    import services.sidebar_service as sidebar
    import services.tenant_service as tenant
    from services import auth_service

    monkeypatch.setattr(access, "get_user_memberships", lambda user_id: [{"organization_slug": "acme", "organization_name": "Acme"}])
    monkeypatch.setattr(access, "get_org_branches", lambda org_slug: [{"branch_slug": "main", "branch_name": "Main", "is_primary": True}])
    monkeypatch.setattr(access, "get_org_access_mode", lambda org_slug: "full")
    monkeypatch.setattr(entitlement, "build_entitlement_context", lambda user_id, org_slug: {})
    monkeypatch.setattr(permission, "can_manage_settings", lambda context: True)
    monkeypatch.setattr(sidebar, "render_main_sidebar", lambda **_kwargs: None)
    monkeypatch.setattr(tenant, "get_effective_settings", lambda org_slug, branch_slug=None: _effective(security))
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    monkeypatch.setattr(auth_service, "is_user_active", lambda user_id: True)


def _run_admin_settings_locked(monkeypatch):
    from services import admin_lock_service as lock

    security = {"admin_enabled": True, lock.HASH_KEY: lock.hash_admin_password("Canary-Admin-Pw-1")}
    _patch_admin_settings_page(monkeypatch, security=security, engine=_AdminSettingsStub())
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.session_state["auth_user"] = _USER
    at.session_state["selected_org_slug"] = "acme"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _run_admin_settings_unlocked(monkeypatch):
    from services import admin_lock_service as lock

    security = {"admin_enabled": True, lock.HASH_KEY: lock.hash_admin_password("Canary-Admin-Pw-1")}
    _patch_admin_settings_page(monkeypatch, security=security, engine=_AdminSettingsStub())
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.session_state["auth_user"] = _USER
    at.session_state["selected_org_slug"] = "acme"
    at.session_state["admin_unlocked_scope"] = (_USER["id"], "acme")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at
