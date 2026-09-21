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
  * the control: with the redaction setting overridden to `full` the SAME exception carries the canary host, so the
    assertions above could fail.

What it cannot prove: what a hosting platform does with the output, whether the deployed app is started from the
repository root, or whether the platform overrides `client.showErrorDetails`. See docs/production-verification-runbook.md.

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

script, cli_env = sys.argv[1], json.loads(sys.argv[2])
sys.path.insert(0, os.path.dirname(script))  # what `streamlit run` does: the script's own folder is importable

# Start Streamlit as `streamlit run <script>` does (real argument, environment and config loading); only the server is stubbed.
bootstrap.run = lambda *args, **kwargs: None
started = CliRunner().invoke(cli.main, ["run", script, "--server.headless=true"], env=cli_env)
assert started.exit_code == 0, started.output
streamlit_logger.update_formatter()  # CliRunner swaps stderr while it runs; rebuild the handlers on the real streams

at = AppTest.from_file(script, default_timeout=120)
at.run()
assert not at.exception, "the login page itself must render"
(email,) = [t for t in at.text_input if t.label == "Email"]
(password,) = [t for t in at.text_input if t.label == "Password"]
email.input("nobody@example.invalid")
password.input("not-a-real-password")
(submit,) = [b for b in at.button if b.label == "Log In"]
submit.click()
at.run()
print("PROBE_JSON=" + json.dumps({"exceptions": [MessageToDict(e.proto) for e in at.exception]}))
'''


class Run:
    def __init__(self, exceptions: list[dict], stdout: str, stderr: str):
        self.exceptions, self.stdout, self.stderr = exceptions, stdout, stderr

    @property
    def output(self) -> str:
        return self.stdout + "\n" + self.stderr


def _run(tmp_path: Path, script: str, **cli_env: str) -> Run:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (tmp_path / "probe.py").write_text(PROBE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("STREAMLIT_") and k != "PYTHONPATH"}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": DATABASE_URL})
    result = subprocess.run(
        [sys.executable, "-B", str(tmp_path / "probe.py"), str(ROOT / script), json.dumps(cli_env)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("PROBE_JSON=")]
    assert len(lines) == 1, f"probe failed:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
    stdout = "\n".join(line for line in result.stdout.splitlines() if not line.startswith("PROBE_JSON="))
    return Run(json.loads(lines[0].removeprefix("PROBE_JSON="))["exceptions"], stdout, result.stderr)


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
    return _run(tmp_path_factory.mktemp("control"), "src/app.py", STREAMLIT_CLIENT_SHOW_ERROR_DETAILS="full")


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
