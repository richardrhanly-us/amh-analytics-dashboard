"""Streamlit must not show an uncaught exception's contents to end users (security/privacy Step 2C).

Streamlit's default `client.showErrorDetails = "full"` renders the exception type, MESSAGE and traceback in the
browser for any uncaught error on any page. A database driver's message quotes SQL, bound values, the connection
string and the failing row, so an unwrapped query failure would put those on an end user's screen. The repository
pins `[client] showErrorDetails = "none"` in `.streamlit/config.toml`.

These tests do not just read the TOML. Each runs a fresh interpreter from the repository root, the way
`streamlit run src/app.py` is started, so Streamlit itself discovers and applies the file; a throw-away page then
raises an exception carrying SYNTHETIC canaries, and every field Streamlit would send to the browser is checked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".streamlit" / "config.toml"

CANARIES = {
    "host": "canary-db-host-9101.example.invalid",
    "password": "CANARY-DB-PASSWORD-9102",
    "sql": "canary_admin_table_9103",
    "bound": "CANARY-BOUND-VALUE-9104",
    "patron": "CANARY-PATRON-CARD-2300000009105",
    "token": "CANARY-API-TOKEN-9106",
    "row": "CANARY-FAILING-ROW-9107",
}
# Pieces of a raw backend error that must not reach the browser either.
RAW_FRAGMENTS = ("postgresql://", "SELECT ", "[SQL", "[parameters", "Authorization", "Bearer", "Failing row",
                 "RuntimeError", "Traceback", "svc_user")

FAILING_PAGE = f'''
import streamlit as st

st.title("Ordinary heading")
raise RuntimeError(
    "could not connect to postgresql://svc_user:{CANARIES["password"]}@{CANARIES["host"]}:5432/sortview; "
    "[SQL: SELECT * FROM {CANARIES["sql"]} WHERE card = %(card)s] [parameters: {{'card': '{CANARIES["patron"]}'}}]; "
    "DETAIL: Failing row contains ({CANARIES["row"]}, {CANARIES["bound"]}); Authorization: Bearer {CANARIES["token"]}"
)
'''

OK_PAGE = '''
import streamlit as st

st.title("Ordinary heading")
st.success("All good")
st.write("A normal page renders normally.")
'''

PROBE = '''
import json
import sys

from click.testing import CliRunner
from google.protobuf.json_format import MessageToDict
from streamlit import config
from streamlit.testing.v1 import AppTest
from streamlit.web import bootstrap, cli

failing_path, ok_path, cli_env = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])

# Start Streamlit the way `streamlit run` does -- its real argument/environment parsing and config loading -- with only
# the web server itself stubbed out. (--server.headless skips the first-run e-mail prompt; it is unrelated to errors.)
bootstrap.run = lambda *args, **kwargs: None
started = CliRunner().invoke(cli.main, ["run", ok_path, "--server.headless=true"], env=cli_env)
assert started.exit_code == 0, started.output


def run(path):
    at = AppTest.from_file(path, default_timeout=60)
    at.run()
    return at


failing, ok = run(failing_path), run(ok_path)
print("PROBE_JSON=" + json.dumps({
    "option": config.get_option("client.showErrorDetails"),
    "defined_in": config.get_where_defined("client.showErrorDetails"),
    # every field Streamlit would send to the browser for the failure (type, message, stack trace, ...)
    "failing_exceptions": [MessageToDict(e.proto) for e in failing.exception],
    "failing_titles": [t.value for t in failing.title],
    "ok_exceptions": [MessageToDict(e.proto) for e in ok.exception],
    "ok_titles": [t.value for t in ok.title],
    "ok_success": [s.value for s in ok.success],
}))
'''


def _probe(tmp_path: Path, **cli_env: str) -> tuple[dict, str]:
    """Start Streamlit as `streamlit run` does, from the repository root in a fresh interpreter, then run the failing and
    the normal page. No other Streamlit configuration is in play (empty home directory, no ambient STREAMLIT_*
    variables); `cli_env` is the environment `streamlit run` is given, e.g. what a hosting platform might set."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (tmp_path / "failing_page.py").write_text(FAILING_PAGE, encoding="utf-8")
    (tmp_path / "ok_page.py").write_text(OK_PAGE, encoding="utf-8")
    (tmp_path / "probe.py").write_text(PROBE, encoding="utf-8")

    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("STREAMLIT_") and k not in {"DATABASE_URL", "PYTHONPATH"}}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHONDONTWRITEBYTECODE": "1"})

    result = subprocess.run(
        [sys.executable, "-B", str(tmp_path / "probe.py"), str(tmp_path / "failing_page.py"), str(tmp_path / "ok_page.py"),
         json.dumps(cli_env)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=180, check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("PROBE_JSON=")]
    assert len(lines) == 1, f"probe failed:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
    return json.loads(lines[0].removeprefix("PROBE_JSON=")), result.stderr


def _leaks(text: str) -> list[str]:
    return [name for name, canary in CANARIES.items() if canary in text] + [f for f in RAW_FRAGMENTS if f in text]


@pytest.fixture(scope="module")
def repo_config_run(tmp_path_factory):
    return _probe(tmp_path_factory.mktemp("repo_config"))


@pytest.fixture(scope="module")
def full_override_run(tmp_path_factory):
    # What Streamlit's default does, and what an environment variable given to `streamlit run` restores.
    return _probe(tmp_path_factory.mktemp("full_override"), STREAMLIT_CLIENT_SHOW_ERROR_DETAILS="full")


# --- the configuration itself ----------------------------------------------------------------------------------------

def test_the_repository_config_is_a_valid_streamlit_setting_and_holds_no_secrets():
    from streamlit import config
    from streamlit.config import ShowErrorDetailsConfigOptions

    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))

    assert "client.showErrorDetails" in config._config_options_template  # the key exists in the installed Streamlit
    assert data["client"]["showErrorDetails"] == "none"
    assert data["client"]["showErrorDetails"] in {option.value for option in ShowErrorDetailsConfigOptions}

    def keys(node, prefix=""):
        for key, value in node.items():
            yield f"{prefix}{key}"
            if isinstance(value, dict):
                yield from keys(value, f"{prefix}{key}.")

    secret_like = ("password", "secret", "token", "key", "dsn", "url", "credential")
    assert [k for k in keys(data) if any(word in k.lower() for word in secret_like)] == []


def test_streamlit_discovers_and_applies_the_repository_config_from_the_repository_root(repo_config_run):
    probe, _stderr = repo_config_run

    assert probe["option"] == "none"
    assert Path(probe["defined_in"]) == CONFIG  # applied from the tracked file, not a default or another config


# --- behavior ---------------------------------------------------------------------------------------------------------

def test_an_uncaught_exception_shows_the_user_no_message_type_traceback_or_secret(repo_config_run):
    probe, _stderr = repo_config_run

    (exception,) = probe["failing_exceptions"]  # exactly one error element
    assert _leaks(json.dumps(exception)) == []  # nothing Streamlit would send to the browser carries a canary
    assert "type" not in exception and "stackTrace" not in exception  # not even the exception class or a traceback
    assert "This app has encountered an error" in exception["message"]  # ...but the user is told an error occurred
    assert "redacted" in exception["message"]


def test_a_normal_page_is_unaffected(repo_config_run):
    probe, _stderr = repo_config_run

    assert probe["ok_exceptions"] == []
    assert probe["ok_titles"] == ["Ordinary heading"] and probe["ok_success"] == ["All good"]
    assert probe["failing_titles"] == ["Ordinary heading"]  # what rendered before the failure still renders


# --- controls: these prove the assertions above can fail -------------------------------------------------------------

def test_control_with_the_setting_overridden_to_full_the_same_exception_shows_every_canary(full_override_run):
    # Also documents the precedence: an environment variable (or --client.showErrorDetails flag) given to
    # `streamlit run` outranks the repository file, so a host or a developer can turn the details back on.
    probe, _stderr = full_override_run

    assert probe["option"] == "full"
    assert probe["defined_in"] == "command-line argument or environment variable"
    (exception,) = probe["failing_exceptions"]
    assert [name for name, canary in CANARIES.items() if canary not in json.dumps(exception)] == []
    assert [f for f in ("postgresql://", "[SQL", "Bearer") if f not in json.dumps(exception)] == []
