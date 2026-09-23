"""The REAL main app and the REAL Super Admin app, through a genuine uncaught exception (security/privacy verification).

Not a synthetic page: `src/app.py` and `super_admin/Super_Admin_Home.py` are started the way `streamlit run` starts them
(from the repository root, so `.streamlit/config.toml` is discovered by Streamlit itself), their login form is submitted
with fake credentials, and the database they are configured with does not exist. `authenticate_user` queries the
database with nothing catching the failure, so the exception is uncaught: a real one, raised by real code, whose
message carries the database HOST (SQLAlchemy/psycopg2 quote it).

What it proves about each app (the repository half of "the deployed apps redact"):

  * the browser payload holds only the generic error -- no message, no exception type, no traceback, no canary;
  * the process's own output (stdout and stderr) holds no canary and no connection-string text;
  * the log still names the failure: exception type and the SortView frames that made the call;
  * the control: with the redaction setting overridden to `full` (and SortView's in-code enforcement switched off inside the
    probe) the SAME exception carries the canary host, so the assertions above could fail;
  * under a simulated Streamlit Community Cloud -- `client.showErrorDetails=false` supplied at startup, by environment variable
    or by flag -- the browser payload still holds no exception type, no traceback and only the generic message, because every
    entry script's `install_streamlit_log_scrubber()` pins the setting to "none". The control there: with the enforcement
    switched off, `false` does show the type and traceback.

What it cannot prove: what a hosting platform does with the output, whether the deployed app is started from the
repository root, or whether Community Cloud lets the in-code enforcement win over its startup value. See docs/production-verification-runbook.md.

Every value is a SYNTHETIC canary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

HOST = "canary-db-host-9401.example.invalid"
USER = "canary_svc_user_9402"
PASSWORD = "CANARY-DB-PASSWORD-9403"
DATABASE = "canary_prod_db_9404"
DATABASE_URL = f"postgresql://{USER}:{PASSWORD}@{HOST}:5432/{DATABASE}?connect_timeout=3"
CANARIES = {"host": HOST, "user": USER, "password": PASSWORD, "database": DATABASE}
CONNECTION_TEXT = ("postgresql://", "psycopg2.OperationalError:", "Traceback (most recent call last)")

PROBE = '''
import json
import os
import sys

from click.testing import CliRunner
from google.protobuf.json_format import MessageToDict
from streamlit import logger as streamlit_logger
from streamlit.testing.v1 import AppTest
from streamlit.web import bootstrap, cli

script, cli_env, cli_args, enforcement_off = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3]), sys.argv[4] == "off"
sys.path.insert(0, os.path.dirname(script))  # what `streamlit run` does: the script's own folder is importable

# Start Streamlit as `streamlit run <script>` does (real argument, environment and config loading); only the server is stubbed.
bootstrap.run = lambda *args, **kwargs: None
started = CliRunner().invoke(cli.main, ["run", script, "--server.headless=true", *cli_args], env=cli_env)
assert started.exit_code == 0, started.output
streamlit_logger.update_formatter()  # CliRunner swaps stderr while it runs; rebuild the handlers on the real streams
if enforcement_off:  # CONTROL ONLY: what the app would do without SortView's `client.showErrorDetails` pin
    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    from services import privacy_hardening

    privacy_hardening.enforce_streamlit_error_details = lambda: True

at = AppTest.from_file(script, default_timeout=120)
at.run()
assert not at.exception, "the login page itself must render"
(email,) = [t for t in at.text_input if t.label.startswith("Email")]
(password,) = [t for t in at.text_input if t.label.startswith("Password")]
email.input("nobody@example.invalid")
password.input("not-a-real-password")
(submit,) = [b for b in at.button if b.label == "Log In"]
submit.click()
at.run()
from streamlit import config

print("PROBE_JSON=" + json.dumps({"exceptions": [MessageToDict(e.proto) for e in at.exception],
                                  "effective": config.get_option("client.showErrorDetails")}))
'''


class Run:
    def __init__(self, exceptions: list[dict], stdout: str, stderr: str, effective: str):
        self.exceptions, self.stdout, self.stderr, self.effective = exceptions, stdout, stderr, effective

    @property
    def output(self) -> str:
        return self.stdout + "\n" + self.stderr


def _run(tmp_path: Path, script: str, *, enforcement: bool = True, cli_args: tuple[str, ...] = (), **cli_env: str) -> Run:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (tmp_path / "probe.py").write_text(PROBE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("STREAMLIT_") and k != "PYTHONPATH"}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": DATABASE_URL})
    result = subprocess.run(
        [sys.executable, "-B", str(tmp_path / "probe.py"), str(ROOT / script), json.dumps(cli_env),
         json.dumps(list(cli_args)), "on" if enforcement else "off"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("PROBE_JSON=")]
    assert len(lines) == 1, f"probe failed:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
    stdout = "\n".join(line for line in result.stdout.splitlines() if not line.startswith("PROBE_JSON="))
    probe = json.loads(lines[0].removeprefix("PROBE_JSON="))
    return Run(probe["exceptions"], stdout, result.stderr, probe["effective"])


def _leaks(text: str) -> list[str]:
    return [name for name, canary in CANARIES.items() if canary in text] + [f for f in CONNECTION_TEXT if f in text]


APPS = {
    "main-app": ("src/app.py", "app.py"),
    "super-admin-app": ("super_admin/Super_Admin_Home.py", "super_auth.py"),
}


@pytest.fixture(scope="module", params=list(APPS))
def app_run(request, tmp_path_factory):
    script, own_frame = APPS[request.param]
    return _run(tmp_path_factory.mktemp(request.param), script), own_frame


@pytest.fixture(scope="module")
def unredacted_control(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("control"), "src/app.py", enforcement=False, STREAMLIT_CLIENT_SHOW_ERROR_DETAILS="full")


CLOUD_STARTUPS = {  # how a host can hand `false` to `streamlit run`: Community Cloud forces it at startup
    "env-false": {"STREAMLIT_CLIENT_SHOW_ERROR_DETAILS": "false"},
    "flag-false": {"cli_args": ("--client.showErrorDetails=false",)},
}


@pytest.fixture(scope="module", params=[(app, startup) for app in APPS for startup in CLOUD_STARTUPS],
                ids=[f"{app}-{startup}" for app in APPS for startup in CLOUD_STARTUPS])
def cloud_run(request, tmp_path_factory):
    app, startup = request.param
    script, _ = APPS[app]
    return _run(tmp_path_factory.mktemp(f"{app}-{startup}"), script, **CLOUD_STARTUPS[startup])


@pytest.fixture(scope="module", params=list(APPS))
def cloud_control(request, tmp_path_factory):
    script, _ = APPS[request.param]
    return _run(tmp_path_factory.mktemp(f"control-{request.param}"), script, enforcement=False,
                STREAMLIT_CLIENT_SHOW_ERROR_DETAILS="false")


def test_the_browser_gets_only_the_generic_error(app_run):
    run, _ = app_run

    (exception,) = run.exceptions  # exactly one error element: the failure is not swallowed
    assert "This app has encountered an error" in exception["message"]
    assert "type" not in exception and "stackTrace" not in exception
    assert _leaks(json.dumps(exception)) == []


def test_the_process_output_holds_no_canary_or_connection_text(app_run):
    run, _ = app_run

    assert _leaks(run.output) == []
    assert len(run.output.strip()) > 0  # ...and something WAS logged (this is not an empty-output pass)


def test_the_log_still_names_the_failure_and_where_in_sortview_it_happened(app_run):
    run, own_frame = app_run

    line = next(line for line in run.stderr.splitlines() if "Uncaught app execution" in line)
    assert "Uncaught app execution | error_type=sqlalchemy.exc.OperationalError" in line
    assert "cause_type=psycopg2.OperationalError" in line
    app_part = line.split(" app=")[1]  # SortView's own frames, though SQLAlchemy's are the innermost ones
    assert "authenticate_user" in app_part and own_frame in app_part


def test_control_with_redaction_overridden_the_same_exception_carries_the_canary_host(unredacted_control):
    (exception,) = unredacted_control.exceptions

    assert HOST in json.dumps(exception)  # the exception text really does contain the value; redaction is what hides it
    assert "type" in exception or "stackTrace" in exception


# --- simulated Streamlit Community Cloud: `client.showErrorDetails=false` supplied at startup ---------------------------------

def test_under_cloud_false_the_browser_still_gets_no_type_no_traceback_and_only_the_generic_message(cloud_run):
    (exception,) = cloud_run.exceptions  # exactly one error element: the failure is not swallowed

    assert cloud_run.effective == "none"  # SortView's entry script pinned it over the host's `false`
    assert "This app has encountered an error" in exception["message"] and "redacted" in exception["message"]
    assert "type" not in exception and "stackTrace" not in exception
    assert _leaks(json.dumps(exception)) == []


def test_under_cloud_false_the_log_is_unchanged(cloud_run):
    line = next(line for line in cloud_run.stderr.splitlines() if "Uncaught app execution" in line)

    assert "error_type=sqlalchemy.exc.OperationalError" in line  # the scrubbed log line is exactly as without the override
    assert _leaks(cloud_run.output) == []


def test_control_under_cloud_false_without_the_enforcement_the_type_and_traceback_reach_the_browser(cloud_control):
    (exception,) = cloud_control.exceptions

    assert cloud_control.effective == "false"
    assert "This app has encountered an error" in exception["message"]  # `false` still redacts the message...
    assert exception["type"] == "sqlalchemy.exc.OperationalError"  # ...but not the exception type
    assert exception["stackTrace"]  # ...or the traceback (file paths, source lines)
