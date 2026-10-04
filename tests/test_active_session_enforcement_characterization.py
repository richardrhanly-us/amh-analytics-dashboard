"""Characterization tests for active-session enforcement (Block 2a).

These pin what auth_service.enforce_active_session does TODAY, before its
Streamlit-facing part is extracted into an adapter: the two termination
branches and their exact messages, the order of the checks, what is cleaned
out of session state, which persistent-session calls are made, and that each
of the four Streamlit entry points that call it stops before anything
protected runs. No production behavior is asserted that the code does not
already have.

Three layers, from narrowest seam to widest:

1. enforce_active_session with its direct collaborators replaced by recording
   fakes (auth_service.is_user_active / log_auth_event and the three
   persistent_auth_service functions it calls) -- pins ordering and messages.
2. enforce_active_session with the REAL persistent_auth_service, faking only
   session_service -- pins the persistent-token rules (no token, wrong user,
   rejected token) and the real session-state / cookie cleanup.
3. The real entry scripts (src/app.py and the three src/pages/ scripts).

Modules are imported the "flat" way (services.auth_service), matching how
Streamlit loads them with src/ as the script root -- src.services.auth_service
is a separate module object and patching it would not reach the script under
test (see the same note in tests/test_auth_service.py). The persistent-auth
module is always reached by its own import rather than through an attribute of
auth_service, so these tests do not depend on auth_service importing it.

Every patch here is made by a helper called from the test that needs it; there
is deliberately no autouse fixture (see tests/conftest.py on autouse fixtures
that depend on `monkeypatch` and st.cache_data).
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
SETTINGS_PAGE = SRC / "pages" / "1_admin_settings.py"
USERS_PAGE = SRC / "pages" / "2_admin_users.py"
SUPER_ADMIN_PAGE = SRC / "pages" / "Super_Admin_Home.py"

DEACTIVATED_MESSAGE = "Your account has been deactivated. Please contact an administrator."
INVALID_SESSION_MESSAGE = "Your session is no longer valid. Please log in again."

USER = {"id": 1, "email": "user@example.invalid", "full_name": "Test User"}
TOKEN = "synthetic-session-token"


def _guarded_script():
    import streamlit as st

    from services import auth_service

    auth_service.enforce_active_session(st.session_state["auth_user"])
    st.write("PROTECTED_CONTENT_RENDERED")


def _run_guarded(*, token: str | None = None) -> AppTest:
    at = AppTest.from_function(_guarded_script, default_timeout=60)
    at.session_state["auth_user"] = dict(USER)
    at.session_state["selected_org_slug"] = "acme"
    at.session_state["selected_branch_slug"] = "main"
    if token is not None:
        from services import persistent_auth_service

        at.session_state[persistent_auth_service._PERSISTENT_SESSION_STATE_KEY] = token
    at.run()
    return at


def _protected_content_rendered(at: AppTest) -> bool:
    return "PROTECTED_CONTENT_RENDERED" in [m.value for m in at.markdown]


def _assert_session_terminated(at: AppTest, message: str) -> None:
    assert not at.exception, [e.value for e in at.exception]
    assert [e.value for e in at.error] == [message]
    assert not _protected_content_rendered(at)
    # auth_user is kept as a key and set to None; the selections are removed.
    assert "auth_user" in at.session_state
    assert at.session_state["auth_user"] is None
    assert "selected_org_slug" not in at.session_state
    assert "selected_branch_slug" not in at.session_state


def _assert_session_untouched(at: AppTest) -> None:
    assert at.session_state["auth_user"] == USER
    assert at.session_state["selected_org_slug"] == "acme"
    assert at.session_state["selected_branch_slug"] == "main"


# =====================================================================================================================
# 1. enforce_active_session against recording fakes of its direct collaborators
# =====================================================================================================================

def _spy_on_collaborators(
    monkeypatch,
    *,
    active: bool = True,
    token_valid: bool = True,
    raise_on_log: bool = False,
    raise_on_active_check: bool = False,
) -> list[tuple]:
    """Replaces everything enforce_active_session calls and returns the list
    those fakes append to, in call order."""
    from services import auth_service, persistent_auth_service

    events: list[tuple] = []

    def fake_is_user_active(user_id):
        events.append(("is_user_active", user_id))
        if raise_on_active_check:
            raise RuntimeError("synthetic database failure")
        return active

    def fake_log_auth_event(**kwargs):
        events.append(("log_auth_event", kwargs))
        if raise_on_log:
            raise RuntimeError("synthetic audit failure")

    def fake_current_persistent_auth_is_valid(user_id):
        events.append(("current_persistent_auth_is_valid", user_id))
        return token_valid

    def fake_clear_persistent_auth():
        events.append(("clear_persistent_auth",))
        return True

    def fake_clear_all_persistent_auth_for_current_user(user_id):
        events.append(("clear_all_persistent_auth_for_current_user", user_id))
        return 1

    monkeypatch.setattr(auth_service, "is_user_active", fake_is_user_active)
    monkeypatch.setattr(auth_service, "log_auth_event", fake_log_auth_event)
    monkeypatch.setattr(
        persistent_auth_service, "current_persistent_auth_is_valid", fake_current_persistent_auth_is_valid
    )
    monkeypatch.setattr(persistent_auth_service, "clear_persistent_auth", fake_clear_persistent_auth)
    monkeypatch.setattr(
        persistent_auth_service,
        "clear_all_persistent_auth_for_current_user",
        fake_clear_all_persistent_auth_for_current_user,
    )
    return events


INACTIVE_AUDIT_EVENT = {
    "event_type": "session_terminated_inactive",
    "is_success": True,
    "user_id": 1,
    "email": "user@example.invalid",
    "message": "Session terminated: account is no longer active.",
}


def test_active_user_with_valid_session_checks_account_then_token_and_changes_nothing(monkeypatch):
    events = _spy_on_collaborators(monkeypatch)

    at = _run_guarded()

    assert not at.exception, [e.value for e in at.exception]
    assert events == [("is_user_active", 1), ("current_persistent_auth_is_valid", 1)]
    assert [e.value for e in at.error] == []
    assert _protected_content_rendered(at)
    _assert_session_untouched(at)


def test_inactive_account_is_audited_then_revoked_and_never_reaches_the_token_check(monkeypatch):
    # token_valid=False as well: an inactive account must take the
    # "deactivated" branch, not the "session no longer valid" one.
    events = _spy_on_collaborators(monkeypatch, active=False, token_valid=False)

    at = _run_guarded()

    assert events == [
        ("is_user_active", 1),
        ("log_auth_event", INACTIVE_AUDIT_EVENT),
        ("clear_all_persistent_auth_for_current_user", 1),
    ]
    _assert_session_terminated(at, DEACTIVATED_MESSAGE)


def test_inactive_account_is_still_revoked_and_terminated_when_the_audit_write_raises(monkeypatch):
    events = _spy_on_collaborators(monkeypatch, active=False, raise_on_log=True)

    at = _run_guarded()

    assert events == [
        ("is_user_active", 1),
        ("log_auth_event", INACTIVE_AUDIT_EVENT),
        ("clear_all_persistent_auth_for_current_user", 1),
    ]
    _assert_session_terminated(at, DEACTIVATED_MESSAGE)


def test_invalid_session_clears_this_browsers_persistent_auth_and_terminates_without_an_audit_event(monkeypatch):
    events = _spy_on_collaborators(monkeypatch, active=True, token_valid=False)

    at = _run_guarded()

    # Only the current browser's session is cleared (not every session for
    # the user), and enforce_active_session itself writes no audit event here.
    assert events == [
        ("is_user_active", 1),
        ("current_persistent_auth_is_valid", 1),
        ("clear_persistent_auth",),
    ]
    _assert_session_terminated(at, INVALID_SESSION_MESSAGE)


def test_a_failing_account_check_propagates_and_leaves_the_session_as_it_was(monkeypatch):
    # A database failure is not treated as "inactive" or as "valid": it is
    # raised, nothing protected runs, and no cleanup or revocation happens.
    events = _spy_on_collaborators(monkeypatch, raise_on_active_check=True)

    at = _run_guarded()

    assert [type(e) for e in at.exception] != []
    assert events == [("is_user_active", 1)]
    assert [e.value for e in at.error] == []
    assert not _protected_content_rendered(at)
    _assert_session_untouched(at)


# =====================================================================================================================
# 2. enforce_active_session with the real persistent_auth_service (only session_service is faked)
# =====================================================================================================================

def _spy_on_session_service(monkeypatch, *, active: bool = True, validated_user: dict | None = None) -> list[tuple]:
    from services import auth_service, session_service

    events: list[tuple] = []

    def fake_validate_session(raw_token, **_kwargs):
        events.append(("validate_session", raw_token))
        return validated_user

    def fake_revoke_session(raw_token, **_kwargs):
        events.append(("revoke_session", raw_token))
        return True

    def fake_revoke_all_sessions_for_user(user_id, **_kwargs):
        events.append(("revoke_all_sessions_for_user", user_id))
        return 1

    monkeypatch.setattr(auth_service, "is_user_active", lambda user_id: active)
    monkeypatch.setattr(auth_service, "log_auth_event", lambda **_kwargs: None)
    monkeypatch.setattr(session_service, "validate_session", fake_validate_session)
    monkeypatch.setattr(session_service, "revoke_session", fake_revoke_session)
    monkeypatch.setattr(session_service, "revoke_all_sessions_for_user", fake_revoke_all_sessions_for_user)
    return events


def _assert_persistent_auth_cleared(at: AppTest) -> None:
    from services import persistent_auth_service

    assert persistent_auth_service._PERSISTENT_SESSION_STATE_KEY not in at.session_state
    assert at.session_state[persistent_auth_service._SUPPRESS_RESTORE_KEY] is True
    pending = at.session_state[persistent_auth_service.cookie_service._PENDING_KEY]
    assert pending["action"] == "clear"


def test_session_without_a_persistent_token_passes_without_validating_anything(monkeypatch):
    events = _spy_on_session_service(monkeypatch)

    at = _run_guarded()

    assert not at.exception, [e.value for e in at.exception]
    assert events == []
    assert _protected_content_rendered(at)
    _assert_session_untouched(at)


def test_persistent_token_that_validates_to_the_same_user_passes_and_is_kept(monkeypatch):
    from services import persistent_auth_service

    events = _spy_on_session_service(monkeypatch, validated_user=dict(USER))

    at = _run_guarded(token=TOKEN)

    assert not at.exception, [e.value for e in at.exception]
    assert events == [("validate_session", TOKEN)]
    assert _protected_content_rendered(at)
    _assert_session_untouched(at)
    assert at.session_state[persistent_auth_service._PERSISTENT_SESSION_STATE_KEY] == TOKEN
    assert persistent_auth_service._SUPPRESS_RESTORE_KEY not in at.session_state


@pytest.mark.parametrize(
    "validated_user",
    [
        pytest.param({"id": 2, "email": "other@example.invalid", "full_name": "Other User"}, id="token-of-another-user"),
        pytest.param(None, id="expired-revoked-or-unknown-token"),
    ],
)
def test_persistent_token_that_does_not_validate_to_this_user_terminates_the_session(monkeypatch, validated_user):
    events = _spy_on_session_service(monkeypatch, validated_user=validated_user)

    at = _run_guarded(token=TOKEN)

    # Only this browser's token is revoked; the user's other sessions are not.
    assert events == [("validate_session", TOKEN), ("revoke_session", TOKEN)]
    _assert_session_terminated(at, INVALID_SESSION_MESSAGE)
    _assert_persistent_auth_cleared(at)


def test_inactive_account_revokes_every_session_without_validating_the_current_token(monkeypatch):
    events = _spy_on_session_service(monkeypatch, active=False, validated_user=dict(USER))

    at = _run_guarded(token=TOKEN)

    assert events == [("revoke_all_sessions_for_user", 1)]
    _assert_session_terminated(at, DEACTIVATED_MESSAGE)
    _assert_persistent_auth_cleared(at)


# =====================================================================================================================
# 3. The real entry scripts stop before anything protected runs
# =====================================================================================================================

FAILURES = {
    "inactive": {"active": False, "token_valid": True, "message": DEACTIVATED_MESSAGE},
    "invalid_session": {"active": True, "token_valid": False, "message": INVALID_SESSION_MESSAGE},
}


def _record_anything_past_the_guard(monkeypatch) -> list[str]:
    """Each admin page's first step after enforce_active_session is one of
    these lookups. They record and then fail loudly, so a page that got past
    the guard is caught even if it would otherwise render nothing."""
    import services.access_service as access
    import services.platform_admin_service as platform_admin
    import services.streamlit_entitlement_adapter as entitlement_adapter

    reached: list[str] = []

    def recorder(name):
        def _reached(*_args, **_kwargs):
            reached.append(name)
            raise AssertionError(f"{name} must not be reached when session enforcement fails")

        return _reached

    for module, name in (
        (access, "get_user_memberships"),
        (access, "get_org_access_mode"),
        (access, "get_org_branches"),
        (entitlement_adapter, "build_entitlement_context"),
        (platform_admin, "is_platform_admin"),
    ):
        monkeypatch.setattr(module, name, recorder(name))
    return reached


@pytest.mark.parametrize("failure", sorted(FAILURES))
@pytest.mark.parametrize(
    "page",
    [
        pytest.param(SETTINGS_PAGE, id="admin_settings"),
        pytest.param(USERS_PAGE, id="admin_users"),
        pytest.param(SUPER_ADMIN_PAGE, id="super_admin_home"),
    ],
)
def test_page_stops_before_protected_content_when_enforcement_fails(monkeypatch, page, failure):
    scenario = FAILURES[failure]
    _spy_on_collaborators(monkeypatch, active=scenario["active"], token_valid=scenario["token_valid"])
    reached = _record_anything_past_the_guard(monkeypatch)

    at = AppTest.from_file(str(page), default_timeout=60)
    at.session_state["auth_user"] = dict(USER)
    at.session_state["selected_org_slug"] = "acme"
    at.session_state["selected_branch_slug"] = "main"
    at.run()

    assert not at.exception, [e.value for e in at.exception]
    assert reached == []
    assert [e.value for e in at.error] == [scenario["message"]]
    assert [t.value for t in at.title] == []
    assert at.session_state["auth_user"] is None
    assert "selected_org_slug" not in at.session_state
    assert "selected_branch_slug" not in at.session_state


# src/app.py is the multi-page app's MAIN script and is run in a subprocess,
# following the convention in tests/test_required_field_indicators.py (running
# it through AppTest in-process contaminates Streamlit's multi-page state for
# later tests). Both failure scenarios share one subprocess.
_APP_PROBE = """
import json
import sys

src, app = sys.argv[1], sys.argv[2]
sys.path.insert(0, src)

from streamlit.testing.v1 import AppTest

import services.access_service as access
import services.auth_service as auth_service
import services.persistent_auth_service as persistent_auth_service
import services.streamlit_entitlement_adapter as entitlement_adapter

USER = {"id": 1, "email": "user@example.invalid", "full_name": "Test User"}
SCENARIOS = {
    "inactive": {"active": False, "token_valid": True},
    "invalid_session": {"active": True, "token_valid": False},
}

results = {}

for name, scenario in SCENARIOS.items():
    events = []
    reached = []

    def is_user_active(user_id, scenario=scenario, events=events):
        events.append("is_user_active")
        return scenario["active"]

    def log_auth_event(events=events, **kwargs):
        events.append("log_auth_event:" + kwargs["event_type"])

    def current_persistent_auth_is_valid(user_id, scenario=scenario, events=events):
        events.append("current_persistent_auth_is_valid")
        return scenario["token_valid"]

    def clear_persistent_auth(events=events):
        events.append("clear_persistent_auth")
        return True

    def clear_all_persistent_auth_for_current_user(user_id, events=events):
        events.append("clear_all_persistent_auth_for_current_user")
        return 1

    def recorder(label, reached=reached):
        def _reached(*_args, **_kwargs):
            reached.append(label)
            raise AssertionError(label + " must not be reached when session enforcement fails")
        return _reached

    auth_service.is_user_active = is_user_active
    auth_service.log_auth_event = log_auth_event
    persistent_auth_service.current_persistent_auth_is_valid = current_persistent_auth_is_valid
    persistent_auth_service.clear_persistent_auth = clear_persistent_auth
    persistent_auth_service.clear_all_persistent_auth_for_current_user = clear_all_persistent_auth_for_current_user
    access.get_user_memberships = recorder("get_user_memberships")
    access.get_org_access_mode = recorder("get_org_access_mode")
    access.get_org_branches = recorder("get_org_branches")
    access.user_can_access_org = recorder("user_can_access_org")
    entitlement_adapter.build_entitlement_context = recorder("build_entitlement_context")

    at = AppTest.from_file(app, default_timeout=120)
    at.session_state["auth_user"] = dict(USER)
    at.session_state["selected_org_slug"] = "acme"
    at.session_state["selected_branch_slug"] = "main"
    at.run()

    results[name] = {
        "exceptions": [str(e.value) for e in at.exception],
        "errors": [e.value for e in at.error],
        "titles": [t.value for t in at.title],
        "events": events,
        "reached": reached,
        "auth_user": at.session_state["auth_user"],
        "org_selected": "selected_org_slug" in at.session_state,
        "branch_selected": "selected_branch_slug" in at.session_state,
    }

print("RESULT_JSON=" + json.dumps(results))
""".strip()


@pytest.fixture(scope="module")
def app_probe_result() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "PYTHONPATH"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(_APP_PROBE), str(SRC), str(APP)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )

    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT_JSON=")]
    assert len(lines) == 1, f"app probe produced no result:\n{completed.stdout}\n{completed.stderr[-2000:]}"
    return json.loads(lines[0][len("RESULT_JSON="):])


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_main_app_stops_before_protected_content_when_enforcement_fails(app_probe_result, failure):
    result = app_probe_result[failure]

    assert result["exceptions"] == []
    assert result["reached"] == []
    assert result["errors"] == [FAILURES[failure]["message"]]
    assert result["titles"] == []
    assert result["auth_user"] is None
    assert result["org_selected"] is False
    assert result["branch_selected"] is False


def test_main_app_runs_the_same_check_order_as_the_guard_itself(app_probe_result):
    assert app_probe_result["inactive"]["events"] == [
        "is_user_active",
        "log_auth_event:session_terminated_inactive",
        "clear_all_persistent_auth_for_current_user",
    ]
    assert app_probe_result["invalid_session"]["events"] == [
        "is_user_active",
        "current_persistent_auth_is_valid",
        "clear_persistent_auth",
    ]
