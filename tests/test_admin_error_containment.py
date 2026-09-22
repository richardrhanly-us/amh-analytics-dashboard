"""Admin / settings error containment (security/privacy Step 2B).

Before this step an administrator saw, and the application kept, the raw text of whatever backend exception a
failed operation raised: `f"...: {type(e).__name__}: {e}"`. A database driver's message quotes the SQL, the bound
values, the connection string and the failing row, so those reached the Streamlit page, `st.session_state`, and
`settings_error` (which is cached process-wide by st.cache_data).

What this step guarantees, for the admin settings page, the admin users path, the Super Admin Provision Library
and Manage Libraries pages, and `load_runtime_settings`:

  * the page shows a fixed, support-safe message -- never the exception's text;
  * nothing persisted (session state, `settings_error`) holds exception text;
  * the failure is still logged, as a safe summary (type, SQLSTATE, code location -- never the message);
  * the operations still work when nothing fails.

Every value is a SYNTHETIC canary. The pages are Streamlit scripts, so they run for real under `AppTest` with their
dependencies patched.
"""

from __future__ import annotations

import ast
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy.exc import DataError
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PAGE = ROOT / "src" / "pages" / "1_admin_settings.py"
USERS_PAGE = ROOT / "src" / "pages" / "2_admin_users.py"
PROVISION_PAGE = ROOT / "super_admin" / "pages" / "Provision_Library.py"
MANAGE_PAGE = ROOT / "super_admin" / "pages" / "Manage_Libraries.py"
SETTINGS_SERVICE = ROOT / "src" / "services" / "settings_service.py"
USER_ADMIN_SERVICE = ROOT / "src" / "services" / "user_admin_service.py"

# --- the synthetic backend failure ---------------------------------------------------------------------------------

CANARIES = {
    "host": "canary-db-host-8101.example.invalid",
    "password": "CANARY-DB-PASSWORD-8102",
    "sql": "canary_admin_table_8103",
    "bound": "CANARY-BOUND-VALUE-8104",
    "patron": "CANARY-PATRON-CARD-2300000008105",
    "token": "CANARY-API-TOKEN-8106",
    "row": "CANARY-FAILING-ROW-8107",
}
# Fragments of a raw backend error that must never be on an administrator's screen or in their session.
RAW_ERROR_FRAGMENTS = (
    "[SQL", "UPDATE ", "postgresql://", "DataError", "_DriverError", "Traceback", "invalid input syntax",
    "Failing row", "Authorization", "Bearer", "sqlalchemy", "22P02", "ValueError", "RuntimeError",
)


class _DriverError(Exception):
    pgcode = "22P02"


def backend_error() -> DataError:
    """What psycopg2/SQLAlchemy really produce: SQL, bound values, host, credentials, a token, the failing row."""
    c = CANARIES
    driver = _DriverError(
        f'invalid input syntax for type json: "{c["bound"]}" DETAIL: Failing row contains ({c["row"]}, {c["patron"]}). '
        f'connection to server at "{c["host"]}" failed; postgresql://svc_user:{c["password"]}@{c["host"]}:5432/sortview; '
        f'Authorization: Bearer {c["token"]}'
    )
    return DataError(
        f"UPDATE {c['sql']} SET settings_json = %(v)s WHERE api_token = '{c['token']}'", {"v": c["bound"]}, driver
    )


def _raise_backend_error(*_args, **_kwargs):
    raise backend_error()


def test_control_the_synthetic_backend_error_really_carries_every_canary():
    # Proves the assertions below can fail: this is the text the pages used to put on screen and keep.
    old_style_message = f"{type(backend_error()).__name__}: {backend_error()}"

    assert [name for name, canary in CANARIES.items() if canary not in old_style_message] == []
    assert [fragment for fragment in ("[SQL", "UPDATE ", "postgresql://", "DataError") if fragment not in old_style_message] == []


# --- helpers -------------------------------------------------------------------------------------------------------

def _rendered(at: AppTest) -> str:
    """All text the page shows: element values, labels, bodies, dataframe contents, JSON, code."""
    parts: list[str] = []

    def walk(block):
        for element in block:
            for attribute in ("value", "label", "body", "code"):
                value = getattr(element, attribute, None)
                if value is not None and not callable(value):
                    parts.append(value.to_csv() if isinstance(value, pd.DataFrame) else str(value))
            children = getattr(element, "children", None)
            if children:
                walk(children.values() if isinstance(children, dict) else children)

    walk(at.main)
    walk(at.sidebar)
    return "\n".join(parts)


def _session_state_dump(at: AppTest) -> str:
    return json.dumps({key: at.session_state[key] for key in at.session_state.filtered_state}, default=str)


def _leaks(text: str) -> list[str]:
    return [n for n, c in CANARIES.items() if c in text] + [f for f in RAW_ERROR_FRAGMENTS if f in text]


def _assert_contained(at: AppTest, caplog, *, log_message: str, expected_errors=None, expected_warnings=None):
    """Rendered page, session state and log: none holds the exception's text; the log holds a safe summary."""
    assert not at.exception, [e.value for e in at.exception]
    rendered, state = _rendered(at), _session_state_dump(at)
    assert _leaks(rendered) == [] and _leaks(state) == []
    assert len(rendered) > 50  # the page really rendered (this is not an empty-string pass)
    if expected_errors is not None:
        assert [e.value for e in at.error] == expected_errors
    if expected_warnings is not None:
        assert [w.value for w in at.warning] == expected_warnings
    # diagnostics survive, as a safe summary: which operation, the error class, its SQLSTATE -- no message, no values
    assert f"{log_message} | error_type=sqlalchemy.exc.DataError sqlstate=22P02" in caplog.text
    assert [n for n, c in CANARIES.items() if c in caplog.text] == []
    assert "Traceback" not in caplog.text and all(r.exc_info is None for r in caplog.records)


def _run(path: Path, *, session: dict | None = None, secrets: dict | None = None) -> AppTest:
    at = AppTest.from_file(str(path), default_timeout=60)
    for key, value in (session or {}).items():
        at.session_state[key] = value
    for key, value in (secrets or {}).items():
        at.secrets[key] = value
    at.run()
    return at


def _button(at: AppTest, label: str | None = None, key: str | None = None):
    matches = [b for b in at.button if (label is not None and b.label == label) or (key is not None and b.key == key)]
    assert len(matches) == 1, (label, key, [b.label for b in at.button])
    return matches[0]


def _text_input(at: AppTest, label: str, nth: int = 0):
    return [t for t in at.text_input if t.label == label][nth]


# --- a fake database engine (records what a page writes; can fail like a real one) ---------------------------------

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
        self._engine.executed.append((sql, dict(params or {})))
        if "FROM organizations o" in sql:
            return _Result({"organization_id": 1, "organization_name": "Acme", "org_settings_row_id": None,
                            "org_settings_json": {}})
        if "FROM branches b" in sql:
            return _Result({"branch_id": 2, "branch_name": "Main", "branch_settings_row_id": None,
                            "branch_settings_json": {}})
        return _Result(None)


class _Engine:
    def __init__(self, *, fail: bool = False):
        self.executed: list[tuple[str, dict]] = []
        self._fail = fail

    def connect(self):
        if self._fail:
            raise backend_error()
        return _Conn(self)

    begin = connect


# --- the shared page preamble (memberships, branches, entitlements, sidebar) -----------------------------------------

def _patch_admin_preamble(monkeypatch):
    import services.access_service as access
    import services.entitlement_service as entitlement
    import services.permission_service as permission
    import services.sidebar_service as sidebar
    from services import auth_service

    monkeypatch.setattr(access, "get_user_memberships",
                        lambda user_id: [{"organization_slug": "acme", "organization_name": "Acme"}])
    monkeypatch.setattr(access, "get_org_branches",
                        lambda org_slug: [{"branch_slug": "main", "branch_name": "Main", "is_primary": True}])
    monkeypatch.setattr(entitlement, "build_entitlement_context", lambda user_id, org_slug: {})
    monkeypatch.setattr(permission, "can_manage_settings", lambda context: True)
    monkeypatch.setattr(sidebar, "render_main_sidebar", lambda **_kwargs: None)
    # PRE-PILOT active-session guard: both admin pages now call
    # auth_service.enforce_active_session() right after reading auth_user
    # from session state, which queries auth_service's own get_engine()
    # (a separate name binding from database.get_engine, so patching that
    # alone doesn't reach it). This test's synthetic admin user is active.
    monkeypatch.setattr(auth_service, "is_user_active", lambda user_id: True)


ADMIN_SESSION = {"auth_user": {"id": 1, "email": "admin@example.invalid"}}


def _effective(org_slug, branch_slug=None):
    return {
        "organization": {"id": 1, "name": "Acme", "slug": org_slug},
        "branch": {"id": 2, "name": "Main", "slug": branch_slug or "main", "is_primary": True},
        "subscription": None,
        "settings": {},
        "entitlements": {},
    }


# =====================================================================================================================
# 1. Admin Settings page (src/pages/1_admin_settings.py)
# =====================================================================================================================

SETTINGS_LOAD_MESSAGE = ("Settings could not be loaded right now. Please try again in a few minutes. "
                         "If this keeps happening, contact SortView support.")
SETTINGS_SAVE_MESSAGE = ("Settings could not be saved. Please try again in a few minutes. "
                         "If this keeps happening, contact SortView support.")
SETTINGS_PREVIEW_MESSAGE = "The settings preview could not be loaded right now. Please try again in a few minutes."


def _patch_settings_page(monkeypatch, *, effective=_effective, engine: _Engine | None = None):
    import database
    import services.tenant_service as tenant

    _patch_admin_preamble(monkeypatch)
    monkeypatch.setattr(tenant, "get_effective_settings", effective)
    monkeypatch.setattr(database, "get_engine", lambda: engine if engine is not None else _Engine())


def test_settings_page_shows_a_fixed_message_when_settings_cannot_be_loaded(monkeypatch, caplog):
    _patch_settings_page(monkeypatch, effective=_raise_backend_error)

    with caplog.at_level(logging.DEBUG, logger="sortview.admin_settings"):
        at = _run(SETTINGS_PAGE, session=ADMIN_SESSION)

    assert not at.exception
    assert [e.value for e in at.error] == [SETTINGS_LOAD_MESSAGE]
    assert _leaks(_rendered(at)) == [] and _leaks(_session_state_dump(at)) == []
    assert not at.text_input  # st.stop(): no settings form was rendered from a failed load
    assert "Admin settings load failed | error_type=sqlalchemy.exc.DataError sqlstate=22P02" in caplog.text
    assert [n for n, c in CANARIES.items() if c in caplog.text] == []
    assert "Traceback" not in caplog.text and all(r.exc_info is None for r in caplog.records)


def test_settings_page_shows_a_fixed_message_when_saving_fails(monkeypatch, caplog):
    _patch_settings_page(monkeypatch, engine=_Engine(fail=True))

    with caplog.at_level(logging.DEBUG, logger="sortview.admin_settings"):
        at = _run(SETTINGS_PAGE, session=ADMIN_SESSION)
        assert not at.error  # the page loads normally; only the save fails
        _button(at, "Save Settings").click()
        at.run()

    _assert_contained(at, caplog, log_message="Admin settings save failed", expected_errors=[SETTINGS_SAVE_MESSAGE])
    assert _text_input(at, "Library name")  # the form is still on screen, so the administrator can retry


def test_settings_page_shows_a_fixed_message_when_the_preview_cannot_be_loaded(monkeypatch, caplog):
    calls = []

    def effective_then_fail(org_slug, branch_slug=None):
        calls.append(1)
        if len(calls) == 1:  # the page's own load succeeds; the "Current DB Preview" load fails
            return _effective(org_slug, branch_slug)
        raise backend_error()

    _patch_settings_page(monkeypatch, effective=effective_then_fail)

    with caplog.at_level(logging.DEBUG, logger="sortview.admin_settings"):
        at = _run(SETTINGS_PAGE, session=ADMIN_SESSION)

    _assert_contained(at, caplog, log_message="Admin settings preview failed", expected_errors=[SETTINGS_PREVIEW_MESSAGE])
    assert _text_input(at, "Library name")  # the rest of the page is unaffected


def test_settings_page_still_saves_settings_when_nothing_fails(monkeypatch):
    engine = _Engine()
    _patch_settings_page(monkeypatch, engine=engine)

    at = _run(SETTINGS_PAGE, session=ADMIN_SESSION)
    assert not at.error and not at.exception
    _text_input(at, "Library name").input("Acme Public Library")
    _button(at, "Save Settings").click()
    at.run()

    assert not at.error and not at.exception
    inserts = [(sql, params) for sql, params in engine.executed if sql.startswith("INSERT INTO")]
    assert [sql.split()[2] for sql, _ in inserts] == ["organization_settings", "branch_settings"]
    assert json.loads(inserts[0][1]["settings_json"])["library_name"] == "Acme Public Library"
    assert json.loads(inserts[1][1]["settings_json"])["branch_name"] == "Main"
    assert _leaks(_rendered(at)) == []


# =====================================================================================================================
# 2. Admin Users page + user_admin_service
# =====================================================================================================================

DUPLICATE_USER_MESSAGE = "A user with that email already exists."
USER_CREATE_FAILED_MESSAGE = ("The user could not be created. Please check the details and try again. "
                              "If this keeps happening, contact SortView support.")


def _patch_users_page(monkeypatch, *, create_user):
    import services.user_admin_service as user_admin
    from services import auth_service

    _patch_admin_preamble(monkeypatch)
    monkeypatch.setattr(user_admin, "list_org_users", lambda org_slug: [])
    monkeypatch.setattr(user_admin, "list_recent_org_auth_events", lambda org_slug, limit=25: [])
    monkeypatch.setattr(user_admin, "_get_org_row", lambda org_slug: {"id": 1, "slug": org_slug, "name": "Acme"})
    monkeypatch.setattr(user_admin, "get_engine", lambda: _Engine())
    monkeypatch.setattr(auth_service, "get_user_by_email", lambda email: None)
    monkeypatch.setattr(auth_service, "create_user", create_user)


def _submit_new_user(at: AppTest) -> None:
    _text_input(at, "Full name").input("Pat Example")
    _text_input(at, "Email").input("pat@example.invalid")
    _text_input(at, "Temporary password").input("Temp-Password-1")
    _button(at, "Create or add user").click()
    at.run()


def test_users_page_does_not_relay_an_arbitrary_value_error_from_user_creation(monkeypatch, caplog):
    # Before: `except ValueError as e: message = str(e)` -- whatever a lower layer's ValueError said reached the page.
    _patch_users_page(monkeypatch, create_user=lambda **_kwargs: (_ for _ in ()).throw(ValueError(str(backend_error()))))

    with caplog.at_level(logging.DEBUG, logger="sortview.user_admin"):
        at = _run(USERS_PAGE, session=ADMIN_SESSION)
        _submit_new_user(at)

    assert not at.exception
    assert [e.value for e in at.error] == [USER_CREATE_FAILED_MESSAGE]
    assert _leaks(_rendered(at)) == [] and _leaks(_session_state_dump(at)) == []
    assert "User creation refused | error_type=builtins.ValueError" in caplog.text
    assert [n for n, c in CANARIES.items() if c in caplog.text] == []
    assert "Traceback" not in caplog.text and all(r.exc_info is None for r in caplog.records)


def test_users_page_still_tells_the_admin_a_duplicate_email_exists(monkeypatch):
    from services import auth_service

    def duplicate(**_kwargs):
        raise auth_service.UserAlreadyExistsError()

    _patch_users_page(monkeypatch, create_user=duplicate)

    at = _run(USERS_PAGE, session=ADMIN_SESSION)
    _submit_new_user(at)

    assert not at.exception
    assert [e.value for e in at.error] == [DUPLICATE_USER_MESSAGE]


def test_the_real_create_user_still_raises_a_value_error_for_a_duplicate_email(monkeypatch):
    from services import auth_service

    monkeypatch.setattr(auth_service, "get_user_by_email", lambda email: {"id": 1, "email": email})
    monkeypatch.setattr(auth_service, "log_auth_event", lambda **_kwargs: None)

    with pytest.raises(auth_service.UserAlreadyExistsError, match="A user with that email already exists."):
        auth_service.create_user(email="Pat@Example.invalid", password="Temp-Password-1")
    with pytest.raises(ValueError, match="A user with that email already exists."):  # existing handlers keep working
        auth_service.create_user(email="Pat@Example.invalid", password="Temp-Password-1")


def test_users_page_still_creates_a_user_when_nothing_fails(monkeypatch):
    created = []

    def create_user(**kwargs):
        created.append(kwargs)
        return {"id": 9, "email": kwargs["email"]}

    from services import auth_service

    _patch_users_page(monkeypatch, create_user=create_user)
    monkeypatch.setattr(auth_service, "log_auth_event", lambda **_kwargs: None)

    at = _run(USERS_PAGE, session=ADMIN_SESSION)
    _submit_new_user(at)

    assert not at.error and not at.exception
    assert [(c["email"], c["full_name"]) for c in created] == [("pat@example.invalid", "Pat Example")]


# =====================================================================================================================
# 3. Super Admin -- Provision Library
# =====================================================================================================================

PROVISION_FAILED_MESSAGE = ("Library provisioning could not be completed. Check Manage Libraries in case the library "
                            "already exists, then try again. If this keeps happening, contact SortView support.")
OPERATIONAL_INCOMPLETE_WARNING = ("SaaS organization and branch were created, but operational provisioning is INCOMPLETE. "
                                  "Retry below or from Manage Libraries. If it keeps failing, contact SortView support.")
INSTALLATION_RECORD_WARNING = ("Library provisioned, but the installation record could not be created. Add it from "
                               "Manage Libraries to get the Installation ID the on-site installer needs.")
OPERATIONAL_STAGE_FAILED_CODE = "operational_identity_assignment_failed"

SUPER_ADMIN = {"id": 7, "email": "root@example.invalid"}
SUPER_ADMIN_SESSION = {"auth_user": SUPER_ADMIN}


def _patch_super_admin_auth(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "super_admin"))
    import super_auth

    monkeypatch.setattr(super_auth, "require_super_admin", lambda: SUPER_ADMIN)


def _patch_provisioning(monkeypatch, *, create_org=None, create_installation=None, assign=None):
    import services.tenant_service as tenant

    _patch_super_admin_auth(monkeypatch)
    calls = {"create_org": [], "create_installation": [], "assign": []}

    def default_create_org(**kwargs):
        calls["create_org"].append(kwargs)
        return {"organization": {"id": 10, "slug": kwargs["org_slug"]}, "branch": {"id": 20, "slug": kwargs["branch_slug"]},
                "plan": {"code": kwargs["plan_code"]}, "subscription": {"status": "trial"}}

    def default_create_installation(**kwargs):
        calls["create_installation"].append(kwargs)
        return {"id": 5, "name": kwargs["name"]}

    def default_assign(**kwargs):
        calls["assign"].append(kwargs)
        return {"operational_customer_id": 50, "operational_branch_id": 2}

    monkeypatch.setattr(tenant, "create_organization_with_primary_branch", create_org or default_create_org)
    monkeypatch.setattr(tenant, "create_collector_installation", create_installation or default_create_installation)
    monkeypatch.setattr(tenant, "assign_operational_identity", assign or default_assign)
    return calls


def _provision(at: AppTest) -> None:
    _text_input(at, "Organization name").input("Acme Library")
    _button(at, "Provision Library").click()
    at.run()


def test_provision_page_shows_a_fixed_message_when_provisioning_fails(monkeypatch, caplog):
    calls = _patch_provisioning(monkeypatch, create_org=_raise_backend_error)

    with caplog.at_level(logging.DEBUG, logger="sortview.super_admin.provision"):
        at = _run(PROVISION_PAGE, session=SUPER_ADMIN_SESSION)
        _provision(at)

    _assert_contained(at, caplog, log_message="Library provisioning failed", expected_errors=[PROVISION_FAILED_MESSAGE])
    assert calls["create_installation"] == [] and calls["assign"] == []  # nothing further was attempted
    assert at.session_state["provision_result"] is None


def test_provision_page_keeps_only_a_code_when_the_operational_stage_fails(monkeypatch, caplog):
    _patch_provisioning(monkeypatch, assign=_raise_backend_error)

    with caplog.at_level(logging.DEBUG, logger="sortview.super_admin.provision"):
        at = _run(PROVISION_PAGE, session=SUPER_ADMIN_SESSION)
        _provision(at)

    _assert_contained(at, caplog, log_message="Operational identity stage failed")
    assert OPERATIONAL_INCOMPLETE_WARNING in [w.value for w in at.warning]
    # what is stored in session state (and rendered by st.json) is a stable code, not exception text
    stored = at.session_state["provision_result"]
    assert stored["operational_error"] == OPERATIONAL_STAGE_FAILED_CODE
    assert stored["operational_identity"] is None and stored["agent_config"] is None
    assert OPERATIONAL_STAGE_FAILED_CODE in _rendered(at)  # ...and it is what st.json shows


def test_provision_page_keeps_only_a_code_when_the_retry_also_fails(monkeypatch, caplog):
    _patch_provisioning(monkeypatch, assign=_raise_backend_error)

    with caplog.at_level(logging.DEBUG, logger="sortview.super_admin.provision"):
        at = _run(PROVISION_PAGE, session=SUPER_ADMIN_SESSION)
        _provision(at)
        _button(at, "Retry operational identity assignment").click()
        at.run()

    _assert_contained(at, caplog, log_message="Operational identity stage failed")
    assert at.session_state["provision_result"]["operational_error"] == OPERATIONAL_STAGE_FAILED_CODE


def test_provision_page_reports_a_failed_installation_record_without_the_exception(monkeypatch, caplog):
    _patch_provisioning(monkeypatch, create_installation=_raise_backend_error)

    with caplog.at_level(logging.DEBUG, logger="sortview.super_admin.provision"):
        at = _run(PROVISION_PAGE, session=SUPER_ADMIN_SESSION)
        _provision(at)

    _assert_contained(at, caplog, log_message="Collector installation record creation failed",
                      expected_warnings=[INSTALLATION_RECORD_WARNING])
    assert [s.value for s in at.success] == ["Library provisioned."]  # the committed library is still reported


def test_provision_page_still_provisions_when_nothing_fails(monkeypatch):
    calls = _patch_provisioning(monkeypatch)

    at = _run(PROVISION_PAGE, session=SUPER_ADMIN_SESSION)
    _provision(at)

    assert not at.error and not at.warning and not at.exception
    assert [s.value for s in at.success] == ["Library provisioned."]
    stored = at.session_state["provision_result"]
    assert stored["operational_error"] is None
    assert stored["operational_identity"] == {"operational_customer_id": 50, "operational_branch_id": 2}
    assert stored["collector_installation"] == {"id": 5, "name": "Main AMH Sorter"}
    assert stored["agent_config"]["customer_id"] == 50 and stored["agent_config"]["branch_id"] == 2
    assert [c["org_slug"] for c in calls["create_org"]] == ["acme-library"]


def test_provision_page_retry_succeeds_and_clears_the_code(monkeypatch):
    outcomes = iter([backend_error(), None])

    def assign(**_kwargs):
        error = next(outcomes)
        if error is not None:
            raise error
        return {"operational_customer_id": 50, "operational_branch_id": 2}

    _patch_provisioning(monkeypatch, assign=assign)

    at = _run(PROVISION_PAGE, session=SUPER_ADMIN_SESSION)
    _provision(at)
    assert at.session_state["provision_result"]["operational_error"] == OPERATIONAL_STAGE_FAILED_CODE
    _button(at, "Retry operational identity assignment").click()
    at.run()

    assert not at.exception
    stored = at.session_state["provision_result"]
    assert stored["operational_error"] is None and stored["operational_identity"]["operational_customer_id"] == 50


# =====================================================================================================================
# 4. Super Admin -- Manage Libraries
# =====================================================================================================================

_HINT = "If this keeps happening, contact SortView support."
ASSIGN_MESSAGE = f"The operational identity could not be assigned. Assignment is safe to run again. {_HINT}"
SUSPEND_MESSAGE = f"The library could not be suspended. Please try again. {_HINT}"
REACTIVATE_MESSAGE = f"The library could not be reactivated. Please try again. {_HINT}"
UPDATE_MESSAGE = f"The installation could not be updated. Check the values and try again. {_HINT}"
ADD_MESSAGE = f"The installation could not be added. Check the values and try again. {_HINT}"
ENROLLMENT_MESSAGE = f"An enrollment code could not be generated. Please try again. {_HINT}"
NAME_REQUIRED_MESSAGE = "Installation name is required."


def _library_row(**overrides):
    row = {
        "organization_id": 10, "organization_name": "Acme Library", "organization_slug": "acme", "organization_status": "active",
        "branch_id": 20, "branch_name": "Main", "branch_slug": "main", "branch_status": "active",
        "subscription_status": "active", "plan_name": "Trial", "pipeline_status": "ok",
        "last_run": "2026-01-01 00:00:00", "last_attempt": "2026-01-01 00:00:00",
        "operational_customer_id": 50, "operational_branch_id": 2,
    }
    row.update(overrides)
    return row


def _installation(**overrides):
    row = {"id": 5, "name": "Main AMH Sorter", "organization_id": 10, "branch_id": 20, "branch_name": "Main",
           "hostname": "amh-host", "collector_version": "1.0.4", "status": "active", "installed_at": None, "last_seen_at": None}
    row.update(overrides)
    return row


def _patch_manage(monkeypatch, *, row=None, set_status=None, assign=None, update=None, create=None, enroll=None):
    import services.collector_enrollment_service as enrollment
    import services.platform_admin_service as platform
    import services.tenant_service as tenant

    _patch_super_admin_auth(monkeypatch)
    calls = {"set_status": [], "assign": [], "update": [], "create": [], "enroll": []}

    def recording(name, outcome=None, result=None):
        def call(**kwargs):
            calls[name].append(kwargs)
            if outcome is not None:
                raise outcome()
            return result
        return call

    monkeypatch.setattr(platform, "list_libraries_with_status", lambda: [row or _library_row()])
    monkeypatch.setattr(platform, "set_library_active_status", set_status or recording("set_status"))
    monkeypatch.setattr(tenant, "list_collector_installations_for_organization", lambda organization_id: [_installation()])
    monkeypatch.setattr(tenant, "assign_operational_identity",
                        assign or recording("assign", result={"operational_customer_id": 50, "operational_branch_id": 2}))
    monkeypatch.setattr(tenant, "update_collector_installation", update or recording("update"))
    monkeypatch.setattr(tenant, "create_collector_installation", create or recording("create", result={"id": 6}))
    monkeypatch.setattr(enrollment, "generate_enrollment_code_for_installation", enroll or recording("enroll"))
    return calls


def _failing(name):
    def call(**_kwargs):
        raise backend_error()

    call.__name__ = name
    return call


def _run_manage() -> AppTest:
    return _run(MANAGE_PAGE, session=SUPER_ADMIN_SESSION, secrets={"AGENT_API_BASE_URL": "https://api.example.invalid"})


MANAGE_LOGGER = "sortview.super_admin.libraries"


def test_manage_page_shows_a_fixed_message_when_assigning_an_identity_fails(monkeypatch, caplog):
    _patch_manage(monkeypatch, row=_library_row(operational_customer_id=None, operational_branch_id=None),
                  assign=_failing("assign"))

    with caplog.at_level(logging.DEBUG, logger=MANAGE_LOGGER):
        at = _run_manage()
        _button(at, "Assign operational identity").click()
        at.run()

    _assert_contained(at, caplog, log_message="Operational identity assignment failed", expected_errors=[ASSIGN_MESSAGE])


def test_manage_page_shows_a_fixed_message_when_suspending_fails(monkeypatch, caplog):
    _patch_manage(monkeypatch, row=_library_row(organization_status="active"), set_status=_failing("set_status"))

    with caplog.at_level(logging.DEBUG, logger=MANAGE_LOGGER):
        at = _run_manage()
        _button(at, "Suspend Library").click()
        at.run()

    _assert_contained(at, caplog, log_message="Library suspend failed", expected_errors=[SUSPEND_MESSAGE])


def test_manage_page_shows_a_fixed_message_when_reactivating_fails(monkeypatch, caplog):
    _patch_manage(monkeypatch, row=_library_row(organization_status="suspended"), set_status=_failing("set_status"))

    with caplog.at_level(logging.DEBUG, logger=MANAGE_LOGGER):
        at = _run_manage()
        _button(at, "Reactivate Library").click()
        at.run()

    _assert_contained(at, caplog, log_message="Library reactivate failed", expected_errors=[REACTIVATE_MESSAGE])


def test_manage_page_shows_a_fixed_message_when_updating_an_installation_fails(monkeypatch, caplog):
    _patch_manage(monkeypatch, update=_failing("update"))

    with caplog.at_level(logging.DEBUG, logger=MANAGE_LOGGER):
        at = _run_manage()
        _button(at, "Save Installation").click()
        at.run()

    _assert_contained(at, caplog, log_message="Collector installation update failed", expected_errors=[UPDATE_MESSAGE])


def test_manage_page_shows_a_fixed_message_when_adding_an_installation_fails(monkeypatch, caplog):
    _patch_manage(monkeypatch, create=_failing("create"))

    with caplog.at_level(logging.DEBUG, logger=MANAGE_LOGGER):
        at = _run_manage()
        _button(at, "Add Installation").click()
        at.run()

    _assert_contained(at, caplog, log_message="Collector installation creation failed", expected_errors=[ADD_MESSAGE])


def test_manage_page_shows_a_fixed_message_when_enrollment_code_generation_fails(monkeypatch, caplog):
    _patch_manage(monkeypatch, enroll=_failing("enroll"))

    with caplog.at_level(logging.DEBUG, logger=MANAGE_LOGGER):
        at = _run_manage()
        _button(at, key="generate_enrollment_code_5").click()
        at.run()

    _assert_contained(at, caplog, log_message="Enrollment code generation failed", expected_errors=[ENROLLMENT_MESSAGE])


def test_manage_page_still_explains_a_refused_enrollment_code(monkeypatch):
    # EnrollmentError is a deliberate refusal with a fixed reason code (not a backend failure): unchanged.
    from services.collector_enrollment_service import EnrollmentError

    def refuse(**_kwargs):
        raise EnrollmentError("organization_status_suspended")

    _patch_manage(monkeypatch, enroll=refuse)

    at = _run_manage()
    _button(at, key="generate_enrollment_code_5").click()
    at.run()

    assert not at.exception
    assert [e.value for e in at.error] == ["Cannot generate an enrollment code: organization status suspended."]


def test_manage_page_still_asks_for_an_installation_name_instead_of_a_generic_failure(monkeypatch):
    # The one ValueError an admin can cause from these forms ("Installation name is required") used to reach them
    # through the raw exception text; it is now a fixed message and the service is not called.
    calls = _patch_manage(monkeypatch)

    at = _run_manage()
    _text_input(at, "Installation name", nth=0).input("   ")
    _button(at, "Save Installation").click()
    at.run()
    assert [e.value for e in at.error] == [NAME_REQUIRED_MESSAGE] and calls["update"] == []

    at = _run_manage()
    _text_input(at, "Installation name", nth=1).input("   ")
    _button(at, "Add Installation").click()
    at.run()
    assert [e.value for e in at.error] == [NAME_REQUIRED_MESSAGE] and calls["create"] == []


def test_manage_page_still_performs_every_operation_when_nothing_fails(monkeypatch):
    calls = _patch_manage(monkeypatch, row=_library_row(operational_customer_id=None, operational_branch_id=None))

    at = _run_manage()
    _button(at, "Assign operational identity").click()
    at.run()
    _button(at, "Suspend Library").click()
    at.run()
    _button(at, "Save Installation").click()
    at.run()
    _button(at, "Add Installation").click()
    at.run()

    assert not at.error and not at.exception
    assert calls["assign"] == [{"organization_id": 10, "branch_id": 20}]
    assert calls["set_status"] == [{"organization_id": 10, "is_active": False}]
    assert [(c["installation_id"], c["organization_id"], c["name"], c["status"]) for c in calls["update"]] == [
        (5, 10, "Main AMH Sorter", "active")]
    assert [(c["organization_id"], c["branch_id"], c["status"]) for c in calls["create"]] == [(10, 20, "provisioning")]


def test_manage_page_still_shows_a_generated_enrollment_code_once(monkeypatch):
    expires = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    _patch_manage(monkeypatch, enroll=lambda **_kwargs: {
        "installation_name": "Main AMH Sorter", "installation_id": 5, "enrollment_code": "ABCD-EFGH-JKLM",
        "expires_at": expires, "ttl_minutes": 60, "revoked_previous_count": 0})

    at = _run_manage()
    _button(at, key="generate_enrollment_code_5").click()
    at.run()

    assert not at.error and not at.exception
    assert "ABCD-EFGH-JKLM" in _rendered(at)  # the code is deliberately shown to the super admin


# =====================================================================================================================
# 5. settings_error (src/services/settings_service.py)
# =====================================================================================================================

@pytest.fixture
def settings_service():
    from services import settings_service as module

    module.load_runtime_settings.clear()
    yield module
    module.load_runtime_settings.clear()


def _settings_file(tmp_path):
    path = tmp_path / "branch_settings.json"
    path.write_text('{"library": {"library_name": "Fallback Library"}}', encoding="utf-8")
    return path


def test_settings_error_is_a_stable_code_never_exception_text(settings_service, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings_service, "get_effective_settings", _raise_backend_error)

    with caplog.at_level(logging.DEBUG, logger="sortview.settings"):
        result = settings_service.load_runtime_settings(_settings_file(tmp_path), org_slug="acme", branch_slug="main")

    assert result["source"] == "file_fallback" and result["LIBRARY_NAME"] == "Fallback Library"  # fallback unchanged
    assert result["settings_error"] == "database_unavailable" == settings_service.SETTINGS_ERROR_DATABASE_UNAVAILABLE
    assert _leaks(repr(result)) == []  # the whole dict is what st.cache_data keeps process-wide
    # the failure used to be silent; it is now diagnosable, as a safe summary
    assert ("Database settings load failed; using the settings file | error_type=sqlalchemy.exc.DataError "
            "sqlstate=22P02") in caplog.text
    assert [n for n, c in CANARIES.items() if c in caplog.text] == [] and "Traceback" not in caplog.text


def test_the_cached_settings_dict_holds_no_exception_text(settings_service, monkeypatch, tmp_path):
    calls = []

    def failing(org_slug, branch_slug=None):
        calls.append(1)
        raise backend_error()

    monkeypatch.setattr(settings_service, "get_effective_settings", failing)
    settings_file = _settings_file(tmp_path)

    first = settings_service.load_runtime_settings(settings_file, org_slug="acme", branch_slug="main")
    second = settings_service.load_runtime_settings(settings_file, org_slug="acme", branch_slug="main")

    assert len(calls) == 1  # the second call was served from the process-wide cache
    assert first["settings_error"] == second["settings_error"] == "database_unavailable"
    assert _leaks(repr(second)) == []


def test_settings_have_no_error_key_when_the_database_load_succeeds(settings_service, monkeypatch, tmp_path):
    monkeypatch.setattr(settings_service, "get_effective_settings", _effective)

    result = settings_service.load_runtime_settings(_settings_file(tmp_path), org_slug="acme", branch_slug="main")

    assert result["source"] == "database" and "settings_error" not in result
    assert result["LIBRARY_NAME"] == "Acme"


# =====================================================================================================================
# 6. Structural guard: no handler in these modules can put exception text anywhere
# =====================================================================================================================

GUARDED_FILES = (SETTINGS_PAGE, USERS_PAGE, PROVISION_PAGE, MANAGE_PAGE, SETTINGS_SERVICE, USER_ADMIN_SERVICE)


def _unsafe_exception_uses(source: str) -> list[str]:
    """Every place an `except ... as NAME` variable (or logger.exception / exc_info / traceback) is used other than
    handing it to log_safe_exception -- the only sanctioned use -- or reading EnrollmentError's fixed `.reason`."""
    tree = ast.parse(source)
    findings: list[str] = []

    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name):
        allowed: set[int] = set()
        for node in ast.walk(handler):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "log_safe_exception" and node.args:
                allowed.add(id(node.args[-1]))
            if (isinstance(node, ast.Attribute) and node.attr == "reason" and isinstance(node.value, ast.Name)
                    and getattr(handler.type, "id", "") == "EnrollmentError"):
                allowed.add(id(node.value))
        findings += [
            f"line {n.lineno}: `{handler.name}` used outside log_safe_exception"
            for stmt in handler.body for n in ast.walk(stmt)
            if isinstance(n, ast.Name) and n.id == handler.name and id(n) not in allowed
        ]

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"exception", "format_exc", "print_exc", "format_exception"}:
            findings.append(f"line {node.lineno}: .{node.attr}")
        if isinstance(node, ast.keyword) and node.arg == "exc_info":
            findings.append(f"line {node.value.lineno}: exc_info")
        if isinstance(node, ast.Import) and any(alias.name == "traceback" for alias in node.names):
            findings.append(f"line {node.lineno}: import traceback")
    return findings


@pytest.mark.parametrize("path", GUARDED_FILES, ids=lambda p: p.name)
def test_no_handler_in_the_admin_and_settings_modules_can_expose_exception_text(path):
    assert _unsafe_exception_uses(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("source", [
    "try:\n    x()\nexcept Exception as e:\n    st.error(f'failed: {e}')",
    "try:\n    x()\nexcept Exception as e:\n    st.error(f'failed: {type(e).__name__}: {e}')",
    "try:\n    x()\nexcept ValueError as e:\n    return {'message': str(e)}",
    "try:\n    x()\nexcept Exception as e:\n    fallback['settings_error'] = repr(e)",
    "try:\n    x()\nexcept Exception:\n    logger.exception('failed')",
    "try:\n    x()\nexcept Exception as exc:\n    logger.error('failed', exc_info=True)",
    "import traceback",
    "try:\n    x()\nexcept Exception as exc:\n    log_safe_exception(logger, 'failed', exc)\n    st.error(str(exc))",
], ids=["fstring", "type-and-message", "str-return", "state", "logger-exception", "exc-info", "traceback", "mixed"])
def test_control_the_structural_guard_flags_each_unsafe_pattern(source):
    assert _unsafe_exception_uses(source) != []


def test_control_the_structural_guard_allows_the_safe_pattern():
    safe = ("try:\n    x()\nexcept Exception as exc:\n    log_safe_exception(logger, 'failed', exc)\n    st.error(MESSAGE)\n"
            "try:\n    y()\nexcept EnrollmentError as e:\n    st.error(f'refused: {e.reason}')")
    assert _unsafe_exception_uses(safe) == []
