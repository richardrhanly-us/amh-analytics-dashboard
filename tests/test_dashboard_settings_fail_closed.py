"""The dashboard FAILS CLOSED when the selected organization's settings cannot be loaded.

src/app.py used to be handed the settings file beside it (src/branch_settings.json -- one library's own name, transit
labels and hold-classification lists) whenever the database load failed for ANY organization, with nothing on screen to
say so, and drew every view from it. It now shows one fixed message and stops: no view, no report, and no other
library's value.

The real page runs under `AppTest` as a signed-in member of a synthetic organization, with the real settings service
over a database read that fails like a real driver (the synthetic backend error of
tests/test_admin_error_containment.py, which carries SQL, bound values, a connection string and a token). The settings
file in play is the repository's real one, because that is the file the page is started with.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import test_admin_error_containment as harness
from streamlit.testing.v1 import AppTest
from test_admin_error_containment import _rendered, _session_state_dump

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "src" / "app.py"
REAL_SETTINGS_FILE = ROOT / "src" / "branch_settings.json"
MESSAGE = "Settings could not be loaded right now. Please try again in a few minutes."
SESSION = {"auth_user": {"id": 1, "email": "member@example.invalid"}, "selected_org_slug": "acme", "selected_branch_slug": "main"}


def _file_values() -> set[str]:
    """Every value of the real settings file that names the library it belongs to. Compared against, never printed."""
    document = json.loads(REAL_SETTINGS_FILE.read_text(encoding="utf-8"))
    values = {document["library"]["library_name"]}
    values |= {destination["label"] for destination in document["transit"]["destinations"]}
    values |= {entry for entries in document["internal_routing"].values() for entry in entries}
    return values


@pytest.fixture
def dashboard(monkeypatch):
    """The page's dependencies up to the settings load, as a signed-in member of "acme" -- and a record of everything
    it would do AFTER that load."""
    import data_loader_diag
    import services.access_service as access
    import services.permission_service as permission
    import services.readiness_service as readiness
    import services.sidebar_service as sidebar
    import services.streamlit_access_adapter as access_adapter
    import services.streamlit_entitlement_adapter as entitlement_adapter
    from services import auth_service, settings_service

    after: list[str] = []
    file_reads: list[str] = []

    def downstream(name, result):
        def call(*_args, **_kwargs):
            after.append(name)
            return result
        return call

    monkeypatch.setattr(auth_service, "is_user_active", lambda user_id: True)
    monkeypatch.setattr(access_adapter, "get_user_memberships", lambda user_id: [
        {"organization_slug": "acme", "organization_name": "Acme Library", "customer_id": 50}])
    monkeypatch.setattr(access_adapter, "get_org_branches", lambda org_slug: [
        {"branch_slug": "main", "branch_name": "Main", "is_primary": True, "branch_id": 2}])
    monkeypatch.setattr(access, "get_org_access_mode", lambda org_slug: "full")
    monkeypatch.setattr(access, "user_can_access_org", lambda user_id, org_slug: True)
    monkeypatch.setattr(entitlement_adapter, "build_entitlement_context", lambda user_id, org_slug: {})
    for check in ("can_manage_settings", "can_export", "can_view_advanced_reports", "can_view_transits", "can_view_internal_workflow"):
        monkeypatch.setattr(permission, check, lambda context: True)
    monkeypatch.setattr(sidebar, "render_main_sidebar", lambda **_kwargs: None)

    # Everything the page does once it HAS settings. The first of them reports a problem and stops the page, so a
    # run that gets that far is recognisable -- and goes no further into the real data loaders.
    monkeypatch.setattr(data_loader_diag, "validate_tenant_schema",
                        downstream("validate_tenant_schema", [{"table": "synthetic_table", "missing": ["synthetic_column"]}]))
    monkeypatch.setattr(readiness, "get_branch_readiness", downstream("get_branch_readiness", {"is_ready": False, "message": "", "code": ""}))

    real_from_file = settings_service.load_app_settings_from_file

    def from_file(settings_file):
        file_reads.append(str(settings_file))
        return real_from_file(settings_file)

    monkeypatch.setattr(settings_service, "load_app_settings_from_file", from_file)
    settings_service.load_runtime_settings.clear()
    yield {"after": after, "file_reads": file_reads, "settings_service": settings_service}
    settings_service.load_runtime_settings.clear()


def _run() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=120)
    for key, value in SESSION.items():
        at.session_state[key] = value
    at.run()
    return at


def test_control_with_settings_loaded_the_page_goes_on_to_everything_after_them(dashboard, monkeypatch):
    """Proves the assertions below can fail: when the settings DO load, the page continues past the settings load."""
    monkeypatch.setattr(dashboard["settings_service"], "get_effective_settings", harness._effective)

    at = _run()

    assert not at.exception, [e.value for e in at.exception]
    assert dashboard["after"] == ["validate_tenant_schema"]  # reached, and it is what stopped this run
    assert [e.value for e in at.error] == ["Tenant schema validation failed."]
    assert MESSAGE not in _rendered(at) and dashboard["file_reads"] == []


def test_a_failed_settings_load_shows_one_fixed_message_and_stops_the_page(dashboard, monkeypatch, caplog):
    monkeypatch.setattr(dashboard["settings_service"], "get_effective_settings", harness._raise_backend_error)

    with caplog.at_level(logging.DEBUG, logger="sortview.settings"):
        at = _run()

    assert not at.exception, [e.value for e in at.exception]
    assert [e.value for e in at.error] == [MESSAGE]
    # st.stop(): nothing after the settings load ran -- no schema check, no readiness check, and so no view or report.
    assert dashboard["after"] == []
    assert not at.tabs and not at.dataframe and not at.metric and not at.button
    rendered, state = _rendered(at), _session_state_dump(at)
    # No raw exception text on the page or in the session ...
    assert harness._leaks(rendered) == [] and harness._leaks(state) == []
    # ... and the failure is still logged, once per attempt, as a safe summary.
    assert "Database settings load failed; no settings are available | error_type=sqlalchemy.exc.DataError sqlstate=22P02" in caplog.text
    assert [name for name, canary in harness.CANARIES.items() if canary in caplog.text] == []
    assert "Traceback" not in caplog.text


def test_no_value_of_the_repositorys_settings_file_reaches_another_organizations_page(dashboard, monkeypatch, caplog):
    values = _file_values()
    assert len(values) > 10  # the file really holds what this test is about
    monkeypatch.setattr(dashboard["settings_service"], "get_effective_settings", harness._raise_backend_error)

    with caplog.at_level(logging.DEBUG):
        at = _run()

    assert dashboard["file_reads"] == []  # the file the page is started with was never opened
    observable = _rendered(at) + _session_state_dump(at) + caplog.text
    assert sum(1 for value in values if value in observable) == 0  # a count: no value is echoed on failure


def test_the_page_recovers_on_the_next_run_once_the_database_answers(dashboard, monkeypatch):
    monkeypatch.setattr(dashboard["settings_service"], "get_effective_settings", harness._raise_backend_error)
    at = _run()
    assert [e.value for e in at.error] == [MESSAGE] and dashboard["after"] == []

    monkeypatch.setattr(dashboard["settings_service"], "get_effective_settings", harness._effective)
    at.run()

    # Nothing was cached for the failure: the same session's next run loads from the database and carries on.
    assert MESSAGE not in _rendered(at)
    assert dashboard["after"] == ["validate_tenant_schema"] and dashboard["file_reads"] == []


def test_the_page_handles_exactly_the_fail_closed_error_and_reads_the_file_nowhere_else():
    source = APP.read_text(encoding="utf-8").replace("\r\n", "\n")

    assert source.count("load_runtime_settings(") == 1 and source.count("SETTINGS_FILE") == 3  # defined, commented on, passed
    handler = source[source.index("except SettingsUnavailableError:"):]
    assert handler.startswith("except SettingsUnavailableError:\n    st.error(SETTINGS_UNAVAILABLE_MESSAGE)\n    st.stop()\n")
    assert f'SETTINGS_UNAVAILABLE_MESSAGE = "{MESSAGE}"' in source
    for gone in ("load_app_settings_from_file", "load_branch_settings", "json.load", "file_fallback", "settings_error"):
        assert gone not in source, gone
