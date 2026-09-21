"""SortView pins `client.showErrorDetails = "none"` in code, whatever the host sets (security hardening after the hosted canary).

The hosted canary check showed Streamlit Community Cloud forcing the legacy `client.showErrorDetails = false` at startup,
over `.streamlit/config.toml` (Cloud documents this). In Streamlit 1.63.0 `false` means "stacktrace": the message is
redacted, but the exception TYPE and the traceback (server file paths, source lines) still reach the browser. An environment
variable or a `--client.showErrorDetails` flag can do the same. `install_streamlit_log_scrubber()` -- the first call in every
entry script -- therefore calls `enforce_streamlit_error_details()`, which sets "none" through Streamlit's supported
`st.set_option`. It is unconditional; there is no opt-out.

  1. unit tests: every starting value ends at "none"; idempotent; re-applied on every install call; never raises or logs a value;
  2. simulated Community Cloud: a fresh interpreter started the way `streamlit run` is, with `false` supplied by an environment
     variable, then by a CLI flag. A control page WITHOUT the enforcement shows the type and traceback (proving the assertions
     can fail); the same page WITH it shows the browser only the generic message.

What this cannot prove: that Community Cloud lets a script's `st.set_option` override its startup value. Only the hosted canary
(docs/production-verification-runbook.md) can show that.

Every value is a SYNTHETIC canary.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
import streamlit
from streamlit import config

from services import privacy_hardening as hardening

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
CONFIG = ROOT / ".streamlit" / "config.toml"

OPTION = "client.showErrorDetails"
FLAG_OR_ENV = "command-line argument or environment variable"  # what Streamlit reports for a value from a flag or a variable
SET_IN_CODE = "<user defined>"  # what Streamlit reports for a value set by a script (st.set_option)


# =====================================================================================================================
# 1. The helper
# =====================================================================================================================

STARTING_VALUES = ["full", "true", "false", "False", "stacktrace", "type", "none", True, False]


@pytest.fixture
def fresh_startup(monkeypatch):
    monkeypatch.setattr(hardening, "_error_details_startup", None)


@pytest.mark.parametrize("start", STARTING_VALUES, ids=repr)
def test_every_starting_value_ends_as_none(start, fresh_startup):
    config.set_option(OPTION, start, FLAG_OR_ENV)

    assert hardening.enforce_streamlit_error_details() is True

    assert config.get_option(OPTION) == "none"
    assert streamlit.get_option(OPTION) == "none"  # the public API reads the same value


def test_it_is_idempotent(fresh_startup):
    config.set_option(OPTION, "false", FLAG_OR_ENV)

    results = [hardening.enforce_streamlit_error_details() for _ in range(3)]

    assert results == [True, True, True]
    assert config.get_option(OPTION) == "none"
    assert hardening.streamlit_error_details_startup() == ("false", FLAG_OR_ENV)  # the first call's view is kept


def test_it_records_what_the_platform_set_and_does_not_overwrite_that_record(fresh_startup):
    config.set_option(OPTION, "false", FLAG_OR_ENV)
    assert hardening.streamlit_error_details_startup() is None  # nothing enforced yet

    hardening.enforce_streamlit_error_details()
    assert hardening.streamlit_error_details_startup() == ("false", FLAG_OR_ENV)
    assert config.get_where_defined(OPTION) == SET_IN_CODE

    config.set_option(OPTION, "full", FLAG_OR_ENV)  # something sets it back later; the startup record must not change
    hardening.enforce_streamlit_error_details()
    assert hardening.streamlit_error_details_startup() == ("false", FLAG_OR_ENV)
    assert config.get_option(OPTION) == "none"


def test_installing_the_scrubber_enforces_it(fresh_startup):
    config.set_option(OPTION, "false", FLAG_OR_ENV)

    hardening.install_streamlit_log_scrubber()

    assert config.get_option(OPTION) == "none"


def test_a_later_install_call_enforces_it_again_even_though_the_scrubber_is_already_installed(fresh_startup):
    hardening.install_streamlit_log_scrubber()
    factory = logging.getLogRecordFactory()
    assert hardening.is_streamlit_log_scrubber_installed()

    config.set_option(OPTION, "false", FLAG_OR_ENV)  # e.g. something re-applied the platform's value
    hardening.install_streamlit_log_scrubber()

    assert config.get_option(OPTION) == "none"  # the early-out for "already installed" must not skip the enforcement
    assert logging.getLogRecordFactory() is factory  # ...and the log scrubber was not wrapped a second time


SECRET = "CANARY-EXCEPTION-TEXT-9501 postgresql://svc:CANARY-PASSWORD-9502@canary-host-9503.example.invalid/db"


def _boom(*_args, **_kwargs):
    raise RuntimeError(SECRET)


def test_it_does_not_raise_or_log_anything_but_a_fixed_line_when_the_option_cannot_be_set(fresh_startup, monkeypatch, caplog):
    config.set_option(OPTION, "false", FLAG_OR_ENV)
    monkeypatch.setattr(streamlit, "set_option", _boom)

    with caplog.at_level(logging.DEBUG):
        assert hardening.enforce_streamlit_error_details() is False
        hardening.install_streamlit_log_scrubber()  # must not raise either: a failure here must not stop the page

    assert hardening.is_streamlit_log_scrubber_installed()  # the log scrubbing still installed
    assert "Could not enforce Streamlit client.showErrorDetails=none" in caplog.text
    assert "CANARY" not in caplog.text and "postgresql" not in caplog.text and "RuntimeError" not in caplog.text
    assert "false" not in caplog.text  # no configuration value either
    assert all(record.exc_info is None for record in caplog.records)


def test_it_reports_failure_when_reading_the_option_fails_too(fresh_startup, monkeypatch, caplog):
    monkeypatch.setattr(config, "get_option", _boom)

    with caplog.at_level(logging.DEBUG):
        assert hardening.enforce_streamlit_error_details() is False

    assert "CANARY" not in caplog.text and "RuntimeError" not in caplog.text


def test_it_reports_failure_when_the_value_does_not_take_effect(fresh_startup, monkeypatch, caplog):
    config.set_option(OPTION, "false", FLAG_OR_ENV)
    monkeypatch.setattr(streamlit, "set_option", lambda *a, **k: None)  # silently ignored

    with caplog.at_level(logging.DEBUG):
        assert hardening.enforce_streamlit_error_details() is False

    assert config.get_option(OPTION) == "false"
    assert "Could not enforce Streamlit client.showErrorDetails=none" in caplog.text  # a silent failure is still reported


def test_it_logs_nothing_when_it_works(fresh_startup, caplog):
    config.set_option(OPTION, "false", FLAG_OR_ENV)

    with caplog.at_level(logging.DEBUG, logger=hardening.__name__):
        assert hardening.enforce_streamlit_error_details() is True

    assert [r for r in caplog.records if r.name == hardening.__name__] == []


def test_the_repository_config_still_pins_none():
    # The code enforcement is IN ADDITION to the repository setting, never instead of it.
    import tomllib

    assert tomllib.loads(CONFIG.read_text(encoding="utf-8"))["client"]["showErrorDetails"] == "none"


def test_there_is_no_way_to_opt_out():
    import ast

    tree = ast.parse((SRC / "services" / "privacy_hardening.py").read_text(encoding="utf-8"))
    (function,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "enforce_streamlit_error_details"]
    code = function.body[1:]  # everything after the docstring
    names = {n.id for stmt in code for n in ast.walk(stmt) if isinstance(n, ast.Name)}
    attributes = {n.attr for stmt in code for n in ast.walk(stmt) if isinstance(n, ast.Attribute)}

    assert not names & {"os", "sys"} and not attributes & {"environ", "getenv", "argv"}  # nothing the environment can switch off


# =====================================================================================================================
# 2. Simulated Community Cloud: `false` supplied at startup, exactly as `streamlit run` receives it
# =====================================================================================================================

CANARIES = {
    "host": "canary-db-host-9601.example.invalid",
    "password": "CANARY-DB-PASSWORD-9602",
    "sql": "canary_admin_table_9603",
    "bound": "CANARY-BOUND-VALUE-9604",
    "patron": "CANARY-PATRON-CARD-2300000009605",
    "token": "CANARY-API-TOKEN-9606",
    "row": "CANARY-FAILING-ROW-9607",
}
RAW_FRAGMENTS = ("postgresql://", "SELECT ", "[SQL", "[parameters", "Authorization", "Bearer", "Failing row",
                 "RuntimeError", "Traceback", "svc_user", "raise RuntimeError")

FAILING_BODY = f'''
import streamlit as st

st.title("Ordinary heading")
raise RuntimeError(
    "could not connect to postgresql://svc_user:{CANARIES["password"]}@{CANARIES["host"]}:5432/sortview; "
    "[SQL: SELECT * FROM {CANARIES["sql"]} WHERE card = %(card)s] [parameters: {{'card': '{CANARIES["patron"]}'}}]; "
    "DETAIL: Failing row contains ({CANARIES["row"]}, {CANARIES["bound"]}); Authorization: Bearer {CANARIES["token"]}"
)
'''
OK_BODY = '''
import streamlit as st

st.title("Ordinary heading")
st.success("All good")
'''
# What every real entry script does first (tests/test_streamlit_log_scrubbing.py guards that).
ENFORCING_HEADER = (
    f"import sys\nsys.path.insert(0, {str(SRC)!r})\n"
    "from services.privacy_hardening import install_streamlit_log_scrubber\n\ninstall_streamlit_log_scrubber()\n"
)

PROBE = '''
import json
import os
import sys

from click.testing import CliRunner
from google.protobuf.json_format import MessageToDict
from streamlit import config
from streamlit.testing.v1 import AppTest
from streamlit.web import bootstrap, cli

failing_path, ok_path = sys.argv[1], sys.argv[2]
cli_env, cli_args = json.loads(sys.argv[3]), json.loads(sys.argv[4])

# Start Streamlit as `streamlit run` does -- real argument, environment and config loading -- with only the server stubbed.
bootstrap.run = lambda *args, **kwargs: None
started = CliRunner().invoke(cli.main, ["run", ok_path, "--server.headless=true", *cli_args], env=cli_env)
assert started.exit_code == 0, started.output
before = (config.get_option("client.showErrorDetails"), config.get_where_defined("client.showErrorDetails"))


def run(path):
    at = AppTest.from_file(path, default_timeout=60)
    at.run()
    return at


failing = run(failing_path)
first_exceptions = [MessageToDict(e.proto) for e in failing.exception]
failing.run()  # a second script run in the same process (AppTest.run returns the same object): the setting must still hold
again_exceptions = [MessageToDict(e.proto) for e in failing.exception]
ok = run(ok_path)
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
from services.privacy_hardening import streamlit_error_details_startup

print("PROBE_JSON=" + json.dumps({
    "before": list(before),
    "after": [config.get_option("client.showErrorDetails"), config.get_where_defined("client.showErrorDetails")],
    "startup": streamlit_error_details_startup(),
    "failing_exceptions": first_exceptions,
    "failing_again_exceptions": again_exceptions,
    "failing_titles": [t.value for t in failing.title],
    "ok_exceptions": [MessageToDict(e.proto) for e in ok.exception],
    "ok_titles": [t.value for t in ok.title],
    "ok_success": [s.value for s in ok.success],
}))
'''


def _probe(tmp_path: Path, *, enforcing: bool, cli_args: tuple[str, ...] = (), **cli_env: str) -> dict:
    """Start Streamlit from the repository root in a fresh interpreter with `cli_env` / `cli_args` as the host's startup
    settings, then run a failing page and an ordinary page. `enforcing` says whether the pages start with SortView's
    install call (as every entry script does) or are bare pages (the control)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    header = ENFORCING_HEADER if enforcing else ""
    (tmp_path / "failing_page.py").write_text(header + FAILING_BODY, encoding="utf-8")
    (tmp_path / "ok_page.py").write_text(header + OK_BODY, encoding="utf-8")
    (tmp_path / "probe.py").write_text(PROBE, encoding="utf-8")

    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("STREAMLIT_") and k not in {"DATABASE_URL", "PYTHONPATH"}}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(
        [sys.executable, "-B", str(tmp_path / "probe.py"), str(tmp_path / "failing_page.py"), str(tmp_path / "ok_page.py"),
         json.dumps(cli_env), json.dumps(list(cli_args))],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=240, check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("PROBE_JSON=")]
    assert len(lines) == 1, f"probe failed:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
    return json.loads(lines[0].removeprefix("PROBE_JSON="))


def _leaks(text: str) -> list[str]:
    return [name for name, canary in CANARIES.items() if canary in text] + [f for f in RAW_FRAGMENTS if f in text]


@pytest.fixture(scope="module")
def control_env_false(tmp_path_factory):
    return _probe(tmp_path_factory.mktemp("control_false"), enforcing=False, STREAMLIT_CLIENT_SHOW_ERROR_DETAILS="false")


@pytest.fixture(scope="module")
def enforced_env_false(tmp_path_factory):
    return _probe(tmp_path_factory.mktemp("env_false"), enforcing=True, STREAMLIT_CLIENT_SHOW_ERROR_DETAILS="false")


@pytest.fixture(scope="module")
def enforced_flag_false(tmp_path_factory):
    return _probe(tmp_path_factory.mktemp("flag_false"), enforcing=True, cli_args=("--client.showErrorDetails=false",))


@pytest.fixture(scope="module")
def enforced_env_full(tmp_path_factory):
    return _probe(tmp_path_factory.mktemp("env_full"), enforcing=True, STREAMLIT_CLIENT_SHOW_ERROR_DETAILS="full")


@pytest.fixture(scope="module")
def enforced_repo_config_only(tmp_path_factory):
    return _probe(tmp_path_factory.mktemp("repo_only"), enforcing=True)


def test_control_streamlit_normally_shows_the_type_and_traceback_under_false(control_env_false):
    # This is the hosted canary's finding, reproduced locally: the message is redacted but the type and traceback are not.
    run = control_env_false

    assert run["after"] == ["false", FLAG_OR_ENV]  # nothing changed it: the platform's value is the effective one
    (exception,) = run["failing_exceptions"]
    assert "This app has encountered an error" in exception["message"]  # the message IS redacted under false...
    assert exception["type"] == "RuntimeError"  # ...but the exception type
    assert exception["stackTrace"] and "raise RuntimeError" in "".join(exception["stackTrace"])  # and the source context are not
    assert [name for name, canary in CANARIES.items() if canary in json.dumps(exception)] == []  # (values still don't leak)


CLOUD_CASES = {
    "env-false": ("enforced_env_false", "false"),
    "flag-false": ("enforced_flag_false", "false"),
    "env-full": ("enforced_env_full", "full"),
}


@pytest.fixture(params=list(CLOUD_CASES))
def cloud_run(request):
    fixture, startup_value = CLOUD_CASES[request.param]
    return request.getfixturevalue(fixture), startup_value


def test_the_platform_value_is_in_force_at_startup_and_recorded(cloud_run):
    run, startup_value = cloud_run

    assert run["before"] == [startup_value, FLAG_OR_ENV]  # what the host supplied, before any SortView code ran
    assert run["startup"] == [startup_value, FLAG_OR_ENV]  # ...and SortView kept a record of it before overwriting it


def test_after_enforcement_the_effective_value_is_none_and_says_it_was_set_in_code(cloud_run):
    run, _ = cloud_run

    assert run["after"] == ["none", SET_IN_CODE]


def test_the_browser_gets_no_type_no_traceback_and_only_the_generic_message(cloud_run):
    run, _ = cloud_run

    for exceptions in (run["failing_exceptions"], run["failing_again_exceptions"]):  # the first run and a re-run
        (exception,) = exceptions
        assert "type" not in exception and "stackTrace" not in exception
        assert exception["message"].startswith("This app has encountered an error")
        assert "redacted" in exception["message"]
        assert _leaks(json.dumps(exception)) == []


def test_an_ordinary_page_still_renders(cloud_run):
    run, _ = cloud_run

    assert run["ok_exceptions"] == []
    assert run["ok_titles"] == ["Ordinary heading"] and run["ok_success"] == ["All good"]
    assert run["failing_titles"] == ["Ordinary heading"]  # what rendered before the failure still renders


def test_with_only_the_repository_config_the_result_is_the_same(enforced_repo_config_only):
    run = enforced_repo_config_only

    assert run["before"][0] == "none" and Path(run["before"][1]) == CONFIG  # the tracked file applied, as before
    assert run["after"] == ["none", SET_IN_CODE]
    (exception,) = run["failing_exceptions"]
    assert "type" not in exception and "stackTrace" not in exception
