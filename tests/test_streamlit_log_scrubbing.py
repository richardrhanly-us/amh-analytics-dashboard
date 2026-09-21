"""Streamlit's server-side log of an uncaught page exception carries no secrets (security/privacy post-containment).

`client.showErrorDetails = "none"` (Step 2C) keeps an uncaught exception's message out of the BROWSER. Streamlit still
logs the whole exception -- message, traceback, and so the SQL / bound values / connection string / failing row a database
driver puts in its message -- to the server's output ("Manage app" on Streamlit Cloud):

  * through the `streamlit.error_util` logger to stderr (what a `requirements.txt`-only install such as production
    does), or
  * with the optional `rich` package installed (dev machines, CI), as a console print to stdout that bypasses `logging`.

`install_streamlit_log_scrubber()` (called first in every Streamlit entry script) rewrites Streamlit's exception records to
a safe summary -- type, SQLSTATE, code location -- and turns the rich print off.

What these tests do NOT prove: what any hosting platform does with the output AFTER the process writes it, or that a
deployed app was started from the repository root. Those need checking on the live app (see docs/deployment.md).

Every value is a SYNTHETIC canary.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import DataError

from services import privacy_hardening as hardening

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

CANARIES = {
    "host": "canary-db-host-9201.example.invalid",
    "password": "CANARY-DB-PASSWORD-9202",
    "sql": "canary_admin_table_9203",
    "bound": "CANARY-BOUND-VALUE-9204",
    "patron": "CANARY-PATRON-CARD-2300000009205",
    "token": "CANARY-API-TOKEN-9206",
    "email": "canary.patron.9207@example.invalid",
    "row": "CANARY-FAILING-ROW-9208",
}
# Raw-error text that must not appear in a log line. (The exception CLASS name is meant to appear.)
LOG_FRAGMENTS = ("postgresql://", "SELECT ", "[SQL", "[parameters", "Authorization", "Bearer", "Failing row", "svc_user",
                 "Traceback (most recent call last)")
BROWSER_FRAGMENTS = LOG_FRAGMENTS + ("RuntimeError",)

HAS_RICH = importlib.util.find_spec("rich") is not None


def _leaks(text: str, fragments: tuple[str, ...]) -> list[str]:
    return [name for name, canary in CANARIES.items() if canary in text] + [f for f in fragments if f in text]


class _DriverError(Exception):
    pgcode = "22P02"


def backend_error() -> DataError:
    c = CANARIES
    driver = _DriverError(
        f'invalid input syntax for type json: "{c["bound"]}" DETAIL: Failing row contains ({c["row"]}, {c["patron"]}, '
        f'{c["email"]}). connection to server at "{c["host"]}" failed; postgresql://svc_user:{c["password"]}@{c["host"]}:5432/db; '
        f'Authorization: Bearer {c["token"]}'
    )
    return DataError(f"SELECT * FROM {c['sql']} WHERE card = %(card)s", {"card": c["patron"]}, driver)


# =====================================================================================================================
# 1. The scrubber, in-process
# =====================================================================================================================

@pytest.fixture
def scrubber_installed():
    from streamlit import config

    original_factory = logging.getLogRecordFactory()
    original_rich = config.get_option("logger.enableRich")
    hardening.install_streamlit_log_scrubber()
    yield
    logging.setLogRecordFactory(original_factory)
    config.set_option("logger.enableRich", original_rich)


def _captured(logger_name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger(logger_name)
    logger.handlers = [handler]
    logger.propagate = False  # as Streamlit sets on its own loggers
    logger.setLevel(logging.DEBUG)
    return logger, stream


def _raise_and_log(logger: logging.Logger, **log_kwargs) -> None:
    try:
        # As SQLAlchemy does: raised FROM the driver's own exception, whose text carries the same values.
        raise backend_error() from _DriverError(str(backend_error()))
    except Exception as exc:
        logger.error("Uncaught app execution", exc_info=exc, **log_kwargs)


def test_control_without_the_scrubber_a_streamlit_exception_record_carries_every_canary():
    logger, stream = _captured("streamlit.error_util")

    _raise_and_log(logger)

    assert [name for name, canary in CANARIES.items() if canary not in stream.getvalue()] == []
    assert "Traceback (most recent call last)" in stream.getvalue()


def test_an_uncaught_exception_record_is_reduced_to_type_sqlstate_and_location(scrubber_installed):
    logger, stream = _captured("streamlit.error_util")

    _raise_and_log(logger)
    output = stream.getvalue()

    assert _leaks(output, LOG_FRAGMENTS) == []
    assert "Uncaught app execution | error_type=sqlalchemy.exc.DataError sqlstate=22P02" in output
    assert "test_streamlit_log_scrubbing.py" in output and "_raise_and_log" in output  # where it happened is kept
    assert "cause_type=" in output  # ...and what it was raised from


# A stand-in for SQLAlchemy/psycopg2: code whose FILENAME is under site-packages, twelve frames deep.
_LIBRARY_NAMESPACE: dict = {}
exec(  # nosec B102 - test-only, fixed source
    compile("def descend(depth, error):\n    if depth == 0:\n        raise error\n    descend(depth - 1, error)\n",
            "/venv/lib/python3.11/site-packages/fakedb/engine.py", "exec"),
    _LIBRARY_NAMESPACE,
)


def _the_page_script() -> None:
    _LIBRARY_NAMESPACE["descend"](11, backend_error())  # the page's own frame sits far below the innermost frames


def test_the_summary_reaches_the_pages_own_frame_through_deep_library_frames(scrubber_installed):
    logger, stream = _captured("streamlit.error_util")

    try:
        _the_page_script()
    except Exception as exc:
        logger.error("Uncaught app execution", exc_info=exc)

    output = stream.getvalue()
    assert " app=" in output and "_the_page_script" in output.split(" app=")[1]  # named in SortView's own frames
    assert "_the_page_script" not in output.split(" app=")[0]  # ...though the innermost frames are all library code
    assert _leaks(output, LOG_FRAGMENTS) == []


def test_the_scrub_covers_logger_exception_and_any_streamlit_logger(scrubber_installed):
    for name in ("streamlit", "streamlit.runtime.scriptrunner.exec_code", "streamlit.some.future.module"):
        logger, stream = _captured(name)
        try:
            raise backend_error()
        except Exception:
            logger.exception("Something failed")  # exc_info=True form

        assert _leaks(stream.getvalue(), LOG_FRAGMENTS) == [], name
        assert "Something failed | error_type=sqlalchemy.exc.DataError" in stream.getvalue(), name


def test_records_without_an_exception_and_other_loggers_are_left_alone(scrubber_installed):
    logger, stream = _captured("streamlit.error_util")
    logger.error("plain %s message", "formatted")
    assert stream.getvalue() == "ERROR streamlit.error_util: plain formatted message\n"

    # The scrubber is scoped to Streamlit's own loggers; SortView's loggers use log_safe_exception instead.
    other, other_stream = _captured("sortview.unrelated")
    _raise_and_log(other)
    assert "Traceback (most recent call last)" in other_stream.getvalue()


def test_installing_twice_is_harmless_and_turns_the_rich_console_print_off(scrubber_installed):
    from streamlit import config

    factory = logging.getLogRecordFactory()
    hardening.install_streamlit_log_scrubber()
    assert logging.getLogRecordFactory() is factory  # not wrapped a second time

    config.set_option("logger.enableRich", True)
    hardening.install_streamlit_log_scrubber()
    assert config.get_option("logger.enableRich") is False


# =====================================================================================================================
# 2. End to end: Streamlit started as `streamlit run` starts it, a page raises, and we read the process's own output
# =====================================================================================================================

PAGE_HEAD = f'''
import sys

sys.path.insert(0, {str(SRC)!r})
from services.privacy_hardening import install_streamlit_log_scrubber

install_streamlit_log_scrubber()
'''

PAGE_BODY = f'''
import streamlit as st

st.title("Ordinary heading")
raise RuntimeError(
    "could not connect to postgresql://svc_user:{CANARIES["password"]}@{CANARIES["host"]}:5432/sortview; "
    "[SQL: SELECT * FROM {CANARIES["sql"]} WHERE card = %(card)s] [parameters: {{'card': '{CANARIES["patron"]}'}}]; "
    "DETAIL: Failing row contains ({CANARIES["row"]}, {CANARIES["bound"]}, {CANARIES["email"]}); "
    "Authorization: Bearer {CANARIES["token"]}"
)
'''

OK_PAGE = '''
import streamlit as st

st.title("Ordinary heading")
st.success("All good")
'''

PROBE = '''
import json
import sys

from click.testing import CliRunner
from google.protobuf.json_format import MessageToDict
from streamlit import config
from streamlit import logger as streamlit_logger
from streamlit.testing.v1 import AppTest
from streamlit.web import bootstrap, cli

failing_path, ok_path, rich_mode = sys.argv[1], sys.argv[2], sys.argv[3]

# Start Streamlit the way `streamlit run` does (its real argument and config loading), with only the web server stubbed.
bootstrap.run = lambda *args, **kwargs: None
started = CliRunner().invoke(cli.main, ["run", ok_path, "--server.headless=true"])
assert started.exit_code == 0, started.output
# CliRunner swaps sys.stderr while it runs, and Streamlit rebuilds its log handlers meanwhile; rebuild them on the real
# streams, as a real server's bootstrap does.
streamlit_logger.update_formatter()
if rich_mode == "on":
    config.set_option("logger.enableRich", True)
elif rich_mode == "off":
    config.set_option("logger.enableRich", False)


def run(path):
    at = AppTest.from_file(path, default_timeout=60)
    at.run()
    return at


failing, ok = run(failing_path), run(ok_path)
print("PROBE_JSON=" + json.dumps({
    "option": config.get_option("client.showErrorDetails"),
    "rich_after": config.get_option("logger.enableRich"),
    "failing_exceptions": [MessageToDict(e.proto) for e in failing.exception],
    "ok_exceptions": [MessageToDict(e.proto) for e in ok.exception],
    "ok_success": [s.value for s in ok.success],
}))
'''


class Run:
    def __init__(self, probe: dict, stdout: str, stderr: str, raise_line: int):
        self.probe, self.stdout, self.stderr, self.raise_line = probe, stdout, stderr, raise_line

    @property
    def output(self) -> str:
        return self.stdout + "\n" + self.stderr


def _run(tmp_path: Path, *, scrubbed: bool, rich_mode: str = "") -> Run:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    failing_source = (PAGE_HEAD if scrubbed else "") + PAGE_BODY
    raise_line = next(i for i, line in enumerate(failing_source.splitlines(), 1) if line.startswith("raise RuntimeError"))
    (tmp_path / "failing_page.py").write_text(failing_source, encoding="utf-8")
    (tmp_path / "ok_page.py").write_text(OK_PAGE, encoding="utf-8")
    (tmp_path / "probe.py").write_text(PROBE, encoding="utf-8")

    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("STREAMLIT_") and k not in {"DATABASE_URL", "PYTHONPATH"}}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(
        [sys.executable, "-B", str(tmp_path / "probe.py"), str(tmp_path / "failing_page.py"), str(tmp_path / "ok_page.py"), rich_mode],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=180, check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("PROBE_JSON=")]
    assert len(lines) == 1, f"probe failed:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
    stdout = "\n".join(line for line in result.stdout.splitlines() if not line.startswith("PROBE_JSON="))
    return Run(json.loads(lines[0].removeprefix("PROBE_JSON=")), stdout, result.stderr, raise_line)


@pytest.fixture(scope="module")
def scrubbed_run(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("scrubbed"), scrubbed=True)


@pytest.fixture(scope="module")
def scrubbed_run_with_rich_forced_on(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("scrubbed_rich"), scrubbed=True, rich_mode="on")


@pytest.fixture(scope="module")
def unscrubbed_logging_path(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("unscrubbed_logging"), scrubbed=False, rich_mode="off")


@pytest.fixture(scope="module")
def unscrubbed_rich_path(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("unscrubbed_rich"), scrubbed=False, rich_mode="on")


def test_an_uncaught_exception_leaves_no_canary_in_the_servers_output(scrubbed_run):
    run = scrubbed_run

    assert _leaks(run.output, LOG_FRAGMENTS) == []  # neither stdout nor stderr
    assert len(run.output.strip()) > 0  # ...and something WAS logged (this is not an empty-output pass)


def test_the_failure_is_still_diagnosable_from_the_log(scrubbed_run):
    run = scrubbed_run

    assert "Uncaught app execution | error_type=builtins.RuntimeError" in run.stderr
    assert f"at=failing_page.py:{run.raise_line}:<module>" in run.stderr  # the failing line of the page


def test_the_browser_side_redaction_is_unchanged(scrubbed_run):
    run = scrubbed_run

    assert run.probe["option"] == "none"  # Step 2C's setting still applies
    (exception,) = run.probe["failing_exceptions"]
    assert _leaks(json.dumps(exception), BROWSER_FRAGMENTS) == []
    assert "type" not in exception and "stackTrace" not in exception
    assert "This app has encountered an error" in exception["message"]  # the user is still told an error occurred


def test_a_normal_page_is_unaffected_and_logs_nothing(scrubbed_run):
    run = scrubbed_run

    assert run.probe["ok_exceptions"] == [] and run.probe["ok_success"] == ["All good"]
    assert run.probe["rich_after"] is False  # the console-print path is switched off by the installer
    assert run.output.count("Uncaught app execution") == 1  # only the deliberately failing page logged an exception


@pytest.mark.skipif(not HAS_RICH, reason="the rich console print only exists where `rich` is installed (dev, CI)")
def test_the_rich_console_print_cannot_carry_the_exception_either(scrubbed_run_with_rich_forced_on):
    run = scrubbed_run_with_rich_forced_on  # rich forced ON before the page runs; the installer must switch it off

    assert _leaks(run.output, LOG_FRAGMENTS) == []
    assert "Uncaught app execution | error_type=builtins.RuntimeError" in run.stderr


def test_control_without_the_scrubber_the_logging_path_writes_every_canary_to_stderr(unscrubbed_logging_path):
    run = unscrubbed_logging_path

    assert "Uncaught app execution" in run.stderr
    assert [name for name, canary in CANARIES.items() if canary not in run.stderr and name != "sql"] == []
    assert [f for f in ("postgresql://", "[SQL", "Bearer", "Traceback (most recent call last)") if f not in run.stderr] == []
    (exception,) = run.probe["failing_exceptions"]  # ...while the BROWSER was already clean, as Step 2C promised
    assert _leaks(json.dumps(exception), BROWSER_FRAGMENTS) == []


@pytest.mark.skipif(not HAS_RICH, reason="the rich console print only exists where `rich` is installed (dev, CI)")
def test_control_without_the_scrubber_the_rich_path_writes_every_canary_to_stdout(unscrubbed_rich_path):
    run = unscrubbed_rich_path

    assert CANARIES["password"] in run.stdout and "RuntimeError" in run.stdout
    assert "Uncaught app execution" not in run.stderr  # it bypassed `logging` entirely


# =====================================================================================================================
# 3. Every Streamlit entry script installs it, before it does anything else with Streamlit
# =====================================================================================================================

ENTRY_SCRIPTS = (
    [SRC / "app.py"]
    + sorted((SRC / "pages").glob("*.py"))
    + [ROOT / "super_admin" / "Super_Admin_Home.py"]
    + sorted((ROOT / "super_admin" / "pages").glob("*.py"))
)


def _install_problems(source: str) -> list[str]:
    """Empty when the script imports the installer from services.privacy_hardening and calls it at module level before
    any module-level statement touches `st`."""
    tree = ast.parse(source)
    imported = any(
        isinstance(node, ast.ImportFrom) and node.module == "services.privacy_hardening"
        and any(alias.name == "install_streamlit_log_scrubber" for alias in node.names)
        for node in tree.body
    )
    problems = [] if imported else ["does not import install_streamlit_log_scrubber from services.privacy_hardening"]
    installed = False
    for node in tree.body:
        is_call = (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                   and getattr(node.value.func, "id", "") == "install_streamlit_log_scrubber")
        if is_call:
            installed = True
            continue
        if not installed and any(isinstance(n, ast.Name) and n.id == "st" and not isinstance(node, (ast.Import, ast.ImportFrom))
                                 for n in ast.walk(node)):
            problems.append(f"line {node.lineno}: uses `st` before install_streamlit_log_scrubber() is called")
            break
    if not installed:
        problems.append("never calls install_streamlit_log_scrubber() at module level")
    return problems


def test_the_entry_script_list_is_complete():
    # Every script that configures a Streamlit page is an entry script -- a page opened by its own URL runs only itself.
    configuring = sorted(str(p.relative_to(ROOT)) for base in (SRC, ROOT / "super_admin") for p in base.rglob("*.py")
                         if "set_page_config" in p.read_text(encoding="utf-8"))
    assert sorted(str(p.relative_to(ROOT)) for p in ENTRY_SCRIPTS) == configuring
    assert len(ENTRY_SCRIPTS) == 7


@pytest.mark.parametrize("path", ENTRY_SCRIPTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_entry_script_installs_the_log_scrubber_first(path):
    assert _install_problems(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("source", [
    "import streamlit as st\nst.set_page_config(page_title='x')\n",
    ("import streamlit as st\nfrom services.privacy_hardening import install_streamlit_log_scrubber\n"
     "st.set_page_config(page_title='x')\ninstall_streamlit_log_scrubber()\n"),
    "import streamlit as st\nfrom services.privacy_hardening import install_streamlit_log_scrubber\nst.title('x')\n",
    "import streamlit as st\ninstall_streamlit_log_scrubber()\n",
], ids=["absent", "too-late", "never-called", "not-imported"])
def test_control_the_entry_script_guard_flags_each_way_of_getting_it_wrong(source):
    assert _install_problems(source) != []


def test_control_the_entry_script_guard_accepts_the_right_shape():
    good = ("import streamlit as st\nfrom services.privacy_hardening import install_streamlit_log_scrubber\n"
            "install_streamlit_log_scrubber()\nst.set_page_config(page_title='x')\n")
    assert _install_problems(good) == []
