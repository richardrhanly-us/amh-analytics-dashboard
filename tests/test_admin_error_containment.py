"""Admin / settings error containment (security/privacy Step 2B).

Before this step an administrator saw, and the application kept, the raw text of whatever backend exception a
failed operation raised: `f"...: {type(e).__name__}: {e}"`. A database driver's message quotes the SQL, the bound
values, the connection string and the failing row, so those reached the Streamlit page, `st.session_state`, and
`settings_error` (which is cached process-wide by st.cache_data).

What this step guarantees, for the admin settings page, the admin users path and `load_runtime_settings` (the
retired Super Admin Streamlit pages were covered here too until A2 item 8 removed them):

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
from pathlib import Path

import pandas as pd
import pytest
from membership_db import MembershipDatabase, record_audit
from sqlalchemy.exc import DataError
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PAGE = ROOT / "src" / "pages" / "1_admin_settings.py"
USERS_PAGE = ROOT / "src" / "pages" / "2_admin_users.py"
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
    import services.permission_service as permission
    import services.sidebar_service as sidebar
    import services.streamlit_access_adapter as access_adapter
    import services.streamlit_entitlement_adapter as entitlement_adapter
    from services import auth_service

    monkeypatch.setattr(access_adapter, "get_user_memberships",
                        lambda user_id: [{"organization_slug": "acme", "organization_name": "Acme"}])
    monkeypatch.setattr(access_adapter, "get_org_branches",
                        lambda org_slug: [{"branch_slug": "main", "branch_name": "Main", "is_primary": True}])
    # Organization lifecycle policy: both admin pages now call
    # access_service.get_org_access_mode() right after selecting the org,
    # and block the whole page unless it returns "full". This test's
    # synthetic org is unaffected by lifecycle status.
    monkeypatch.setattr(access, "get_org_access_mode", lambda org_slug: "full")
    monkeypatch.setattr(entitlement_adapter, "build_entitlement_context", lambda user_id, org_slug: {})
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

USER_CREATE_FAILED_MESSAGE = ("The user could not be created. Please check the details and try again. "
                              "If this keeps happening, contact SortView support.")


def _patch_users_page(monkeypatch, *, create_user):
    """The page over a real (in-memory) database in which the signed-in admin owns the organization: the service
    reads who is acting from it, so nothing about the service itself is replaced but the account creation."""
    import services.streamlit_entitlement_adapter as entitlement_adapter
    import services.user_admin_service as user_admin
    from services import auth_service

    _patch_admin_preamble(monkeypatch)
    # The page offers roles by the signed-in person's own role, and offers nothing to someone without one.
    monkeypatch.setattr(entitlement_adapter, "build_entitlement_context", lambda user_id, org_slug: {"role": "owner"})
    db = MembershipDatabase()
    db.organization(1, "acme")
    db.user(1, "admin@example.invalid")
    db.member(1, 1, "owner")
    monkeypatch.setattr(user_admin, "list_recent_org_auth_events", lambda org_slug, limit=25: [])
    monkeypatch.setattr(user_admin, "get_engine", lambda: db.engine)
    monkeypatch.setattr(auth_service, "log_auth_event_with_connection", record_audit)
    monkeypatch.setattr(auth_service, "create_user_with_connection", create_user)
    return db


def _submit_new_user(at: AppTest) -> None:
    _text_input(at, "Full name").input("Pat Example")
    _text_input(at, "Email").input("pat@example.invalid")
    _text_input(at, "Temporary password").input("Temp-Password-1")
    _button(at, "Create or add user").click()
    at.run()


def test_users_page_does_not_relay_an_arbitrary_value_error_from_user_creation(monkeypatch, caplog):
    # Before: `except ValueError as e: message = str(e)` -- whatever a lower layer's ValueError said reached the page.
    _patch_users_page(monkeypatch, create_user=lambda _conn, **_kwargs: (_ for _ in ()).throw(ValueError(str(backend_error()))))

    with caplog.at_level(logging.DEBUG, logger="sortview.user_admin"):
        at = _run(USERS_PAGE, session=ADMIN_SESSION)
        _submit_new_user(at)

    assert not at.exception
    assert [e.value for e in at.error] == [USER_CREATE_FAILED_MESSAGE]
    assert _leaks(_rendered(at)) == [] and _leaks(_session_state_dump(at)) == []
    assert "User creation refused | error_type=builtins.ValueError" in caplog.text
    assert [n for n, c in CANARIES.items() if c in caplog.text] == []
    assert "Traceback" not in caplog.text and all(r.exc_info is None for r in caplog.records)


def test_users_page_adds_an_existing_account_without_saying_that_it_existed(monkeypatch):
    # R8C: an address that already has an account -- in some other organization -- is added like any other, and the
    # page says the same thing it says for a brand-new one. No account is created and nothing about it is changed.
    def must_not_create(_conn, **_kwargs):
        raise AssertionError("an existing account must not be created again")

    db = _patch_users_page(monkeypatch, create_user=must_not_create)
    db.user(7, "pat@example.invalid")

    at = _run(USERS_PAGE, session=ADMIN_SESSION)
    _submit_new_user(at)

    assert not at.exception and not at.error
    assert db.active_role(1, 7) == "owner"  # the form's first choice, for an owner
    assert "already exists" not in _rendered(at)


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

    def create_user(_conn, **kwargs):
        created.append(kwargs)
        return {"id": 9, "email": kwargs["email"]}

    db = _patch_users_page(monkeypatch, create_user=create_user)

    at = _run(USERS_PAGE, session=ADMIN_SESSION)
    _submit_new_user(at)

    assert not at.error and not at.exception
    assert [(c["email"], c["full_name"]) for c in created] == [("pat@example.invalid", "Pat Example")]
    assert db.active_role(1, 9) == "owner"
    assert [(e["event_type"], e["metadata"]["org_slug"]) for e in db.audit()] == [("membership_added", "acme")]


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
    """A failed database load used to be answered with the settings file (and a `settings_error` key). It now FAILS
    CLOSED: SettingsUnavailableError, whose whole text is the same stable code, and the file is never used."""
    monkeypatch.setattr(settings_service, "get_effective_settings", _raise_backend_error)

    with caplog.at_level(logging.DEBUG, logger="sortview.settings"), pytest.raises(settings_service.SettingsUnavailableError) as excinfo:
        settings_service.load_runtime_settings(_settings_file(tmp_path), org_slug="acme", branch_slug="main")

    error = excinfo.value
    assert str(error) == error.code == "database_unavailable" == settings_service.SETTINGS_ERROR_DATABASE_UNAVAILABLE
    assert error.__cause__ is None and error.__context__ is None  # the driver's exception does not ride along
    assert _leaks(repr(error) + str(error)) == [] and "Fallback Library" not in repr(error)
    # the failure is diagnosable, as a safe summary
    assert ("Database settings load failed; no settings are available | error_type=sqlalchemy.exc.DataError "
            "sqlstate=22P02") in caplog.text
    assert [n for n, c in CANARIES.items() if c in caplog.text] == [] and "Traceback" not in caplog.text
    assert "Fallback Library" not in caplog.text


def test_a_failed_settings_load_caches_nothing_so_no_exception_text_or_file_value_is_kept(settings_service, monkeypatch, tmp_path):
    calls = []

    def failing(org_slug, branch_slug=None):
        calls.append(1)
        raise backend_error()

    monkeypatch.setattr(settings_service, "get_effective_settings", failing)
    settings_file = _settings_file(tmp_path)

    for _ in range(2):
        with pytest.raises(settings_service.SettingsUnavailableError):
            settings_service.load_runtime_settings(settings_file, org_slug="acme", branch_slug="main")

    assert len(calls) == 2  # nothing was cached process-wide for the failure: the second call asked the database again
    monkeypatch.setattr(settings_service, "get_effective_settings", _effective)
    recovered = settings_service.load_runtime_settings(settings_file, org_slug="acme", branch_slug="main")
    assert recovered["source"] == "database" and recovered["LIBRARY_NAME"] == "Acme"
    assert _leaks(repr(recovered)) == [] and "Fallback Library" not in repr(recovered)


def test_settings_have_no_error_key_when_the_database_load_succeeds(settings_service, monkeypatch, tmp_path):
    monkeypatch.setattr(settings_service, "get_effective_settings", _effective)

    result = settings_service.load_runtime_settings(_settings_file(tmp_path), org_slug="acme", branch_slug="main")

    assert result["source"] == "database" and "settings_error" not in result
    assert result["LIBRARY_NAME"] == "Acme"


# =====================================================================================================================
# 6. Structural guard: no handler in these modules can put exception text anywhere
# =====================================================================================================================

GUARDED_FILES = (SETTINGS_PAGE, USERS_PAGE, SETTINGS_SERVICE, USER_ADMIN_SERVICE)


# Exceptions that carry no text of their own: only a fixed, stable code that names one of a module's own messages.
# Reading that one attribute in their own handler is as safe as reading EnrollmentError's `.reason`.
FIXED_CODE_ATTRIBUTES = {"EnrollmentError": "reason", "_Refused": "code", "MembershipPolicyError": "code"}


def _unsafe_exception_uses(source: str) -> list[str]:
    """Every place an `except ... as NAME` variable (or logger.exception / exc_info / traceback) is used other than
    handing it to log_safe_exception -- the only sanctioned use -- or reading the fixed code of one of the
    exceptions in FIXED_CODE_ATTRIBUTES (EnrollmentError's `.reason`, a membership refusal's `.code`)."""
    tree = ast.parse(source)
    findings: list[str] = []

    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name):
        allowed: set[int] = set()
        for node in ast.walk(handler):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "log_safe_exception" and node.args:
                allowed.add(id(node.args[-1]))
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.attr == FIXED_CODE_ATTRIBUTES.get(getattr(handler.type, "id", ""))):
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
    # A fixed code is readable only on the exceptions known to carry one, and only that attribute of them.
    "try:\n    x()\nexcept ValueError as e:\n    return refused(e.code)",
    "try:\n    x()\nexcept _Refused as e:\n    return refused(str(e))",
    "try:\n    x()\nexcept MembershipPolicyError as e:\n    return refused(e.args)",
], ids=["fstring", "type-and-message", "str-return", "state", "logger-exception", "exc-info", "traceback", "mixed",
        "code-of-any-exception", "refusal-text", "policy-error-args"])
def test_control_the_structural_guard_flags_each_unsafe_pattern(source):
    assert _unsafe_exception_uses(source) != []


def test_control_the_structural_guard_allows_the_safe_pattern():
    safe = ("try:\n    x()\nexcept Exception as exc:\n    log_safe_exception(logger, 'failed', exc)\n    st.error(MESSAGE)\n"
            "try:\n    y()\nexcept EnrollmentError as e:\n    st.error(f'refused: {e.reason}')\n"
            "try:\n    z()\nexcept MembershipPolicyError as e:\n    return refused(e.code)\n"
            "try:\n    z()\nexcept _Refused as e:\n    return refused(e.code)")
    assert _unsafe_exception_uses(safe) == []
