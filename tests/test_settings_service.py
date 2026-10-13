import pytest

from src.services import settings_service


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # load_runtime_settings is st.cache_data-wrapped (dashboard
    # performance pass); its cache is process-global, so clear it
    # before/after every test to keep these tests isolated from each
    # other and from other test modules, matching the existing
    # convention in tests/test_data_loader_refresh.py.
    settings_service.load_runtime_settings.clear()
    yield
    settings_service.load_runtime_settings.clear()


def _fake_effective_settings(org_slug, branch_slug=None):
    return {
        "organization": {"id": 1, "name": f"Org {org_slug}", "slug": org_slug},
        "branch": {"id": 1, "name": branch_slug or "Main", "slug": branch_slug or "main", "is_primary": True},
        "subscription": None,
        "settings": {},
        "entitlements": {},
    }


def test_cache_hits_on_repeated_call_same_args(monkeypatch, tmp_path):
    calls = []

    def counting(org_slug, branch_slug=None):
        calls.append((org_slug, branch_slug))
        return _fake_effective_settings(org_slug, branch_slug)

    monkeypatch.setattr(settings_service, "get_effective_settings", counting)
    settings_file = tmp_path / "branch_settings.json"

    for _ in range(5):
        settings_service.load_runtime_settings(settings_file, org_slug="acme", branch_slug="main")

    assert len(calls) == 1


def test_cache_is_scoped_per_org_and_branch(monkeypatch, tmp_path):
    # A different (org_slug, branch_slug) pair must never share a cache
    # entry -- one tenant's settings can never leak into another
    # tenant's or another branch's result.
    calls = []

    def counting(org_slug, branch_slug=None):
        calls.append((org_slug, branch_slug))
        return _fake_effective_settings(org_slug, branch_slug)

    monkeypatch.setattr(settings_service, "get_effective_settings", counting)
    settings_file = tmp_path / "branch_settings.json"

    acme_settings = settings_service.load_runtime_settings(settings_file, org_slug="acme", branch_slug="main")
    other_settings = settings_service.load_runtime_settings(settings_file, org_slug="other-org", branch_slug="main")

    assert len(calls) == 2
    assert acme_settings["LIBRARY_NAME"] != other_settings["LIBRARY_NAME"]


# =====================================================================================================================
# FAIL CLOSED: a named organization's settings come from the database or not at all
# =====================================================================================================================
#
# load_runtime_settings used to answer ANY failure to load an organization's settings with the settings file beside the
# dashboard -- one library's own name, transit labels and hold-classification lists -- silently, and cached it under
# the failing organization's key for two minutes. It now raises SettingsUnavailableError, and reads no file.

# What a settings file holds that must never reach another organization. Synthetic: no test prints a real value.
FILE_CANARIES = ("CANARY-FILE-LIBRARY-NAME", "CANARY-FILE-HOME", "CANARY-FILE-DESTINATION", "CANARY-FILE-STAFF-NAME",
                 "DACANARY-FILE-PATTERN")
FILE_DOCUMENT = {
    "library": {"library_name": FILE_CANARIES[0], "branch_name": "Main Branch"},
    "transit": {"home_branch_label": FILE_CANARIES[1],
                "destinations": [{"key": "canary", "label": FILE_CANARIES[2], "enabled": True}]},
    "internal_routing": {"branch_services_names": [FILE_CANARIES[3]], "branch_services_da_patterns": [FILE_CANARIES[4]]},
}
SECRET_ERROR_TEXT = "CANARY-DB-ERROR-TEXT postgresql://svc:CANARY-DB-PASSWORD@db.invalid/sortview"


@pytest.fixture
def canary_file(tmp_path):
    import json

    path = tmp_path / "branch_settings.json"
    path.write_text(json.dumps(FILE_DOCUMENT), encoding="utf-8")
    return path


@pytest.fixture
def file_reads(monkeypatch):
    """Every attempt to read a settings file, recorded. The real readers still run, so a read would really happen."""
    reads: list[str] = []
    real_from_file, real_branch = settings_service.load_app_settings_from_file, settings_service.load_branch_settings

    def from_file(settings_file):
        reads.append("load_app_settings_from_file")
        return real_from_file(settings_file)

    def branch(settings_file):
        reads.append("load_branch_settings")
        return real_branch(settings_file)

    monkeypatch.setattr(settings_service, "load_app_settings_from_file", from_file)
    monkeypatch.setattr(settings_service, "load_branch_settings", branch)
    return reads


def _raises(error):
    def effective(org_slug, branch_slug=None):
        raise error
    return effective


def _stored(settings):
    def effective(org_slug, branch_slug=None):
        return {**_fake_effective_settings(org_slug, branch_slug), "settings": settings}
    return effective


def _assert_failed_closed(excinfo, file_reads, caplog=None):
    """The one error, saying one fixed thing, carrying nothing -- and no file was read."""
    error = excinfo.value
    assert type(error) is settings_service.SettingsUnavailableError and isinstance(error, RuntimeError)
    assert str(error) == error.code == "database_unavailable" == settings_service.SETTINGS_ERROR_DATABASE_UNAVAILABLE
    assert error.args == ("database_unavailable",)
    # The original exception does not ride along: not as the cause, and not as the context.
    assert error.__cause__ is None and error.__context__ is None
    assert file_reads == []
    everything = repr(error) + str(error) + (caplog.text if caplog is not None else "")
    assert not [canary for canary in FILE_CANARIES if canary in everything]
    assert "CANARY" not in everything and "acme" not in repr(error) and "main" not in repr(error)


@pytest.mark.parametrize("error", [
    RuntimeError(SECRET_ERROR_TEXT),                         # the database, a query, anything the loader raises
    TimeoutError(SECRET_ERROR_TEXT),
    ConnectionError(SECRET_ERROR_TEXT),
    RuntimeError("Organization not found: acme"),            # what get_effective_settings raises for a missing organization
    RuntimeError("Branch not found for organization: acme"),  # ... and for a missing branch
    KeyError("settings"),
    ValueError(SECRET_ERROR_TEXT),
], ids=["database error", "timeout", "connection lost", "organization not found", "branch not found", "key error", "value error"])
def test_any_failure_to_load_a_named_organizations_settings_raises_and_reads_no_file(monkeypatch, canary_file, file_reads, caplog, error):
    import logging

    monkeypatch.setattr(settings_service, "get_effective_settings", _raises(error))

    with caplog.at_level(logging.DEBUG, logger="sortview.settings"), pytest.raises(settings_service.SettingsUnavailableError) as excinfo:
        settings_service.load_runtime_settings(canary_file, org_slug="acme", branch_slug="main")

    _assert_failed_closed(excinfo, file_reads, caplog)
    # Still diagnosable: one safe summary -- the error's type and where, never its text.
    assert f"Database settings load failed; no settings are available | error_type=builtins.{type(error).__name__}" in caplog.text
    assert "using the settings file" not in caplog.text and "Traceback" not in caplog.text


@pytest.mark.parametrize("settings", [
    {"transit": None},
    {"transit": ["not", "a", "block"]},
    {"transit": {"destinations": ["a destination that is not an object"]}},
    {"transit": {"destinations": [None]}},
    {"transit": {"destinations": 7}},
], ids=["transit is null", "transit is a list", "a destination that is text", "a destination that is null", "destinations is a number"])
def test_stored_settings_that_cannot_be_read_raise_and_are_never_replaced_by_the_file(monkeypatch, canary_file, file_reads, settings):
    """The persistent trigger: an organization whose own stored block is malformed used to be shown the file's
    settings on every load. It is not repaired here, and it is not papered over either."""
    monkeypatch.setattr(settings_service, "get_effective_settings", _stored(settings))

    with pytest.raises(settings_service.SettingsUnavailableError) as excinfo:
        settings_service.load_runtime_settings(canary_file, org_slug="acme", branch_slug="main")

    _assert_failed_closed(excinfo, file_reads)


def test_the_repositorys_own_settings_file_never_reaches_another_organization(monkeypatch, file_reads, caplog):
    """The real file the dashboard is started with (src/branch_settings.json), for an organization whose database
    load fails. Its values are read HERE, for the comparison only, and are never put in an assertion message."""
    import json
    import logging
    from pathlib import Path

    real_file = Path(settings_service.__file__).resolve().parent.parent / "branch_settings.json"
    document = json.loads(real_file.read_text(encoding="utf-8"))
    values = {document["library"]["library_name"], document["transit"]["home_branch_label"]}
    values |= {destination["label"] for destination in document["transit"]["destinations"]}
    values |= {entry for entries in document["internal_routing"].values() for entry in entries}
    assert len(values) > 10  # the file really holds what this test is about
    monkeypatch.setattr(settings_service, "get_effective_settings", _raises(RuntimeError("db unavailable")))

    with caplog.at_level(logging.DEBUG, logger="sortview.settings"), pytest.raises(settings_service.SettingsUnavailableError) as excinfo:
        settings_service.load_runtime_settings(real_file, org_slug="some-other-library", branch_slug="main")

    observable = repr(excinfo.value) + str(excinfo.value) + caplog.text
    assert sum(1 for value in values if value in observable) == 0  # a count: no value is echoed on failure
    assert file_reads == []


def test_a_failed_load_is_not_cached_and_the_next_call_reads_the_database_again(monkeypatch, canary_file, file_reads):
    outcomes = [RuntimeError("db unavailable"), None, None]
    calls = []

    def effective(org_slug, branch_slug=None):
        calls.append((org_slug, branch_slug))
        error = outcomes[len(calls) - 1]
        if error is not None:
            raise error
        return _fake_effective_settings(org_slug, branch_slug)

    monkeypatch.setattr(settings_service, "get_effective_settings", effective)

    with pytest.raises(settings_service.SettingsUnavailableError):
        settings_service.load_runtime_settings(canary_file, org_slug="acme", branch_slug="main")
    second = settings_service.load_runtime_settings(canary_file, org_slug="acme", branch_slug="main")
    third = settings_service.load_runtime_settings(canary_file, org_slug="acme", branch_slug="main")

    assert len(calls) == 2  # the failure cached nothing, so the second call tried again; the third was served from it
    assert second["source"] == third["source"] == "database" and second["LIBRARY_NAME"] == "Org acme"
    assert "settings_error" not in second and "CANARY" not in repr(second)
    assert file_reads == []


def test_repeated_failures_each_try_the_database_and_never_settle_on_a_cached_answer(monkeypatch, canary_file, file_reads):
    calls = []

    def effective(org_slug, branch_slug=None):
        calls.append(1)
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(settings_service, "get_effective_settings", effective)

    for _ in range(3):
        with pytest.raises(settings_service.SettingsUnavailableError):
            settings_service.load_runtime_settings(canary_file, org_slug="acme", branch_slug="main")

    assert len(calls) == 3 and file_reads == []


def test_a_failed_load_writes_nothing_and_opens_no_transaction(monkeypatch, canary_file, file_reads):
    """The real get_effective_settings, over an engine whose every statement fails: the failure path only ever reads."""
    import services.tenant_service as tenant

    statements: list[str] = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, statement, _params=None):
            statements.append(" ".join(str(statement).split()))
            raise RuntimeError(SECRET_ERROR_TEXT)

    class Engine:
        def connect(self):
            return Conn()

        def begin(self):
            raise AssertionError("a failed settings load must never open a write transaction")

    monkeypatch.setattr(tenant, "get_engine", lambda: Engine())

    with pytest.raises(settings_service.SettingsUnavailableError) as excinfo:
        settings_service.load_runtime_settings(canary_file, org_slug="acme", branch_slug="main")

    _assert_failed_closed(excinfo, file_reads)
    assert len(statements) == 1 and statements[0].startswith("SELECT")
    assert not [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]


# --- the file is still read when it is ASKED for ------------------------------------------------------------------------

@pytest.mark.parametrize("arguments", [
    {"org_slug": None},
    {"org_slug": ""},
    {},
    {"org_slug": "acme", "branch_slug": "main", "prefer_database": False},
    {"org_slug": None, "prefer_database": False},
], ids=["no organization", "a blank organization", "no arguments", "prefer_database=False", "neither"])
def test_explicit_file_backed_loading_still_works(monkeypatch, canary_file, arguments):
    def never(org_slug, branch_slug=None):
        raise AssertionError("the database must not be asked when the file was asked for")

    monkeypatch.setattr(settings_service, "get_effective_settings", never)

    result = settings_service.load_runtime_settings(canary_file, **arguments)

    assert result["source"] == "file" and "settings_error" not in result
    assert result["LIBRARY_NAME"] == FILE_CANARIES[0] and result["TRANSIT_HOME_LABEL"] == FILE_CANARIES[1]
    assert result["TRANSIT_LABELS"] == [FILE_CANARIES[2]]


def test_nothing_in_the_service_can_answer_a_failed_database_load_with_the_file():
    """Structural: the only call to the file loader is the explicit one, outside any exception handler -- and no
    result is ever labelled a fallback."""
    import ast
    from pathlib import Path

    source = Path(settings_service.__file__).read_text(encoding="utf-8")
    (function,) = [node for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.FunctionDef) and node.name == "load_runtime_settings"]
    file_calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name) and node.func.id == "load_app_settings_from_file"]
    in_handlers = [call for handler in ast.walk(function) if isinstance(handler, (ast.Try, ast.ExceptHandler))
                   for call in ast.walk(handler) if call in file_calls]

    assert len(file_calls) == 1 and in_handlers == []
    assert isinstance(function.body[-1], ast.Return) and function.body[-1].value is file_calls[0]  # the last statement
    assert "file_fallback" not in source and "settings_error" not in source.replace("SETTINGS_ERROR", "")
