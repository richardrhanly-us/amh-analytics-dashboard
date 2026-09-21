"""scripts/redaction_canary/app.py -- the verification harness for the live-deployment redaction checks.

The harness is deployed as its OWN non-production Streamlit app to prove, on the real hosting platform, that an uncaught
exception reaches neither the browser nor the hosting log (docs/production-verification-runbook.md). These tests prove the
harness itself: inert unless enabled, isolated from the production apps, and -- when enabled -- behaving under the repository
config and the log scrubber exactly as the runbook's pass criteria say, for a page error, a database-style error and an
error raised inside a button callback -- including under a simulated Streamlit Community Cloud, which forces
`client.showErrorDetails = false` at startup: the panel must show BOTH that startup value and the "none" SortView then enforces.

Every value is a SYNTHETIC canary.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "scripts" / "redaction_canary" / "app.py"
CONFIG = ROOT / ".streamlit" / "config.toml"

CANARIES = (
    "CANARY-DB-PASSWORD-LIVE-0001", "canary-db-host-live.example.invalid", "CANARY-API-TOKEN-LIVE-0002",
    "CANARY-PATRON-CARD-2300000000003", "canary.patron.0004@example.invalid", "CANARY-BOUND-VALUE-LIVE-0005",
    "canary_table_live_0006", "canary_svc_user", "postgresql://", "Failing row", "Bearer ",
)
TRIGGERS = {
    "Raise a database-style error": ("sqlalchemy.exc.DataError", "raise_database_error", "sqlstate=22P02"),
    "Raise a plain error": ("builtins.RuntimeError", "raise_plain_error", ""),
    "Raise an error inside a button callback": ("builtins.RuntimeError", "raise_in_callback", ""),
}

PROBE = '''
import json
import os
import sys

from click.testing import CliRunner
from google.protobuf.json_format import MessageToDict
from streamlit import logger as streamlit_logger
from streamlit.testing.v1 import AppTest
from streamlit.web import bootstrap, cli

script, labels, cli_env = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])
enforcement_off = sys.argv[4] == "off"
bootstrap.run = lambda *args, **kwargs: None
started = CliRunner().invoke(cli.main, ["run", script, "--server.headless=true"], env=cli_env)
assert started.exit_code == 0, started.output
streamlit_logger.update_formatter()
if enforcement_off:  # CONTROL ONLY: what the canary would do without SortView's `client.showErrorDetails` pin
    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    from services import privacy_hardening

    privacy_hardening.enforce_streamlit_error_details = lambda: True

at = AppTest.from_file(script, default_timeout=120)
at.run()
result = {
    "initial_exceptions": len(at.exception),
    "info": [i.value for i in at.info],
    "buttons": [b.label for b in at.button],
    "diagnostics": [json.loads(j.value) for j in at.json],
    "after": {},
}
for label in labels:
    (button,) = [b for b in at.button if b.label == label]
    button.click()
    at.run()
    result["after"][label] = [MessageToDict(e.proto) for e in at.exception]
print("PROBE_JSON=" + json.dumps(result))
'''


class Run:
    def __init__(self, result: dict, stdout: str, stderr: str):
        self.result, self.stdout, self.stderr = result, stdout, stderr

    @property
    def output(self) -> str:
        return self.stdout + "\n" + self.stderr


def _run(tmp_path: Path, *, enabled: bool, labels=(), enforcement: bool = True, **cli_env: str) -> Run:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (tmp_path / "probe.py").write_text(PROBE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("STREAMLIT_") and k not in {"PYTHONPATH", "SORTVIEW_REDACTION_CANARY_ENABLED"}}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHONDONTWRITEBYTECODE": "1"})
    if enabled:
        env["SORTVIEW_REDACTION_CANARY_ENABLED"] = "true"
    result = subprocess.run(
        [sys.executable, "-B", str(tmp_path / "probe.py"), str(APP), json.dumps(list(labels)), json.dumps(cli_env),
         "on" if enforcement else "off"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("PROBE_JSON=")]
    assert len(lines) == 1, f"probe failed:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
    stdout = "\n".join(line for line in result.stdout.splitlines() if not line.startswith("PROBE_JSON="))
    return Run(json.loads(lines[0].removeprefix("PROBE_JSON=")), stdout, result.stderr)


def _leaks(text: str) -> list[str]:
    return [c for c in CANARIES if c in text]


@pytest.fixture(scope="module")
def disabled_run(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("disabled"), enabled=False)


@pytest.fixture(scope="module")
def enabled_run(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("enabled"), enabled=True, labels=list(TRIGGERS))


@pytest.fixture(scope="module")
def unredacted_run(tmp_path_factory):
    # CONTROL: the override to `full` with SortView's enforcement switched off inside the probe.
    return _run(tmp_path_factory.mktemp("unredacted"), enabled=True, labels=["Raise a plain error"], enforcement=False,
                STREAMLIT_CLIENT_SHOW_ERROR_DETAILS="full")


@pytest.fixture(scope="module")
def cloud_false_run(tmp_path_factory):
    # What Streamlit Community Cloud does: `client.showErrorDetails=false` handed to `streamlit run`, over the repository file.
    return _run(tmp_path_factory.mktemp("cloud_false"), enabled=True, labels=list(TRIGGERS),
                STREAMLIT_CLIENT_SHOW_ERROR_DETAILS="false")


STARTUP = "client.showErrorDetails at startup (before enforcement)"
STARTUP_WHERE = "client.showErrorDetails at startup defined in"
EFFECTIVE = "client.showErrorDetails effective (after enforcement)"
EFFECTIVE_WHERE = "client.showErrorDetails effective defined in"


# --- the harness is inert and isolated ----------------------------------------------------------------------------------

def test_it_is_inert_unless_explicitly_enabled(disabled_run):
    result = disabled_run.result

    assert result["initial_exceptions"] == 0 and result["buttons"] == [] and result["diagnostics"] == []
    assert any("Disabled" in text for text in result["info"])
    assert _leaks(disabled_run.output) == []


def test_it_is_not_part_of_any_deployed_app():
    for base in ("src", "super_admin"):
        for path in (ROOT / base).rglob("*.py"):
            assert "redaction_canary" not in path.read_text(encoding="utf-8"), path
    assert "redaction_canary" not in (ROOT / "main.py").read_text(encoding="utf-8")
    served = [p for base in ("src", "super_admin") for p in (ROOT / base / "pages").glob("*canary*")]
    assert served == []  # never a page a production app would serve


def test_it_installs_the_log_scrubber_before_anything_else_like_every_entry_script():
    source = APP.read_text(encoding="utf-8")

    assert source.index("install_streamlit_log_scrubber()") < source.index("st.set_page_config(")
    imported = [alias.name for node in ast.parse(source).body
                if isinstance(node, ast.ImportFrom) and node.module == "services.privacy_hardening" for alias in node.names]
    assert "install_streamlit_log_scrubber" in imported  # however the import is formatted


def test_every_canary_the_tests_look_for_really_is_in_the_exception_text():
    source = APP.read_text(encoding="utf-8")

    for canary in [c for c in CANARIES if c not in ("postgresql://", "Failing row", "Bearer ", "canary_svc_user")]:
        assert canary in source, canary
    assert "postgresql://canary_svc_user:" in source and "Failing row contains" in source and "Authorization: Bearer" in source


# --- enabled: the runbook's pass criteria ---------------------------------------------------------------------------------

def test_the_effective_configuration_shows_the_repository_setting_and_the_scrubber(enabled_run):
    (diagnostics,) = enabled_run.result["diagnostics"]

    assert enabled_run.result["initial_exceptions"] == 0
    assert diagnostics[STARTUP] == "none"
    assert Path(diagnostics[STARTUP_WHERE]) == CONFIG  # at startup: from the tracked file, not an override
    assert diagnostics[EFFECTIVE] == "none" and diagnostics[EFFECTIVE_WHERE] == "<user defined>"  # then pinned in code
    assert diagnostics["repository_config_file_in_working_directory"] is True
    assert Path(diagnostics["working_directory"]) == ROOT
    assert diagnostics["log_scrubber_installed"] is True and diagnostics["logger.enableRich"] is False


@pytest.mark.parametrize("label", list(TRIGGERS))
def test_each_trigger_shows_the_browser_only_the_generic_error(enabled_run, label):
    (exception,) = enabled_run.result["after"][label]

    assert "This app has encountered an error" in exception["message"]
    assert "type" not in exception and "stackTrace" not in exception  # no exception type, no traceback
    assert _leaks(json.dumps(exception)) == [] and "RuntimeError" not in json.dumps(exception)


def test_the_process_output_holds_no_canary_for_any_trigger(enabled_run):
    assert _leaks(enabled_run.output) == []
    assert "Traceback (most recent call last)" not in enabled_run.output
    assert enabled_run.output.count("Uncaught app execution") == len(TRIGGERS)  # every trigger was logged, none dropped


@pytest.mark.parametrize("label", list(TRIGGERS))
def test_each_trigger_is_logged_as_type_and_location_only(enabled_run, label):
    error_type, function, extra = TRIGGERS[label]

    lines = [line for line in enabled_run.stderr.splitlines() if "Uncaught app execution" in line and function in line]
    assert len(lines) == 1, (function, enabled_run.stderr[-800:])
    assert f"Uncaught app execution | error_type={error_type}" in lines[0]
    assert extra in lines[0] and "app.py" in lines[0]


def test_the_diagnostics_show_the_platform_value_and_the_enforced_value_under_cloud_false(cloud_false_run):
    # The panel is how an operator sees what the platform supplied, so it must not hide it behind the enforced value: the
    # hosted check reads "startup = false" followed by "effective = none".
    (diagnostics,) = cloud_false_run.result["diagnostics"]

    assert diagnostics[STARTUP] == "false"
    assert diagnostics[STARTUP_WHERE] == "command-line argument or environment variable"
    assert diagnostics[EFFECTIVE] == "none"
    assert diagnostics[EFFECTIVE_WHERE] == "<user defined>"


@pytest.mark.parametrize("label", list(TRIGGERS))
def test_under_cloud_false_each_trigger_still_shows_the_browser_only_the_generic_error(cloud_false_run, label):
    (exception,) = cloud_false_run.result["after"][label]

    assert "This app has encountered an error" in exception["message"]
    assert "type" not in exception and "stackTrace" not in exception  # `false` alone would have shown both
    assert _leaks(json.dumps(exception)) == [] and "RuntimeError" not in json.dumps(exception)


def test_under_cloud_false_the_process_output_is_still_scrubbed(cloud_false_run):
    assert _leaks(cloud_false_run.output) == []
    assert cloud_false_run.output.count("Uncaught app execution") == len(TRIGGERS)


def test_control_with_redaction_overridden_the_browser_shows_every_canary(unredacted_run):
    # (SortView's enforcement is switched off inside the probe: with it on, even `full` from the host ends as `none`.)
    (exception,) = unredacted_run.result["after"]["Raise a plain error"]

    assert [c for c in ("CANARY-DB-PASSWORD-LIVE-0001", "CANARY-API-TOKEN-LIVE-0002", "CANARY-PATRON-CARD-2300000000003",
                        "canary-db-host-live.example.invalid", "postgresql://") if c not in json.dumps(exception)] == []
