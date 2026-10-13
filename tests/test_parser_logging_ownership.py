"""The canonical Tech Logic parsers EMIT log records; they do not CONFIGURE logging (agent/parser/__init__.py).

Found in the Windows rehearsal of Collector 1.0.14. The three parser modules used to call agent/logger_config.py's
get_logger() at import, which creates `logs/` under the CURRENT DIRECTORY and opens a file there. So merely importing
them -- which the Collector's preflight does, to prove its runtime imports -- left

    logs/parser.acs.log    logs/parser.checkins.log    logs/parser.rejects.log

wherever the process had been started: inside the release bundle while `setup.ps1` ran its first preflight (after which
the bundle failed its own manifest verification, so setup could neither be re-run nor RESUMED from that folder), and
inside the install root on every preflight and scheduled run (where the installed-manifest check reports any file the
release did not put there).

What is held here:

  * importing the parsers, or anything of the Collector that imports them, touches no file -- proved in a FRESH
    interpreter started in an EMPTY directory, because logger state that an earlier import left behind in this process
    would hide the bug;
  * the exact operation that failed -- preflight's runtime-imports check -- leaves that directory empty;
  * the parser loggers carry no handler of their own, and still emit: an application that wants the summaries gets them;
  * Contract v2 stays silent even when an application HAS attached handlers (the disable in collector/v2_transform.py);
  * the legacy mirror still gets its parser log files -- from its own modules, deliberately, not from the library;
  * a Collector install no longer contains agent/logger_config.py, and nothing it runs imports it;
  * a real release bundle is still exactly its manifest after the import chain has run from inside it, by the real
    verifier -- which is unchanged and still refuses a single unexpected file.

A FROZEN bundle cannot be built here (it needs PyInstaller and the isolated build environment), so the last point is
proved on a SOURCE bundle, which ships the same parser and Collector modules as files. The frozen executable run from a
frozen bundle is a rehearsal step: see docs/releases/collector-1.0.14.md.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from collector_v2_support import (
    BARCODE_CI,
    BARCODE_HOLD,
    BARCODE_REJ,
    acs_line,
    checkin_line,
    item_message,
    make_context,
    minutes_ago,
    reject_line,
)

from collector import __version__ as RELEASE_VERSION
from collector import build_release, deploy_manifest

REPO_ROOT = Path(__file__).resolve().parent.parent
PARSER_LOGGERS = ("parser.acs", "parser.checkins", "parser.rejects")
PARSER_MODULES = ("agent.parser.acs", "agent.parser.checkins", "agent.parser.rejects")
LEGACY_WRAPPERS = {"agent.parse_acs": "parser.acs", "agent.parse_checkins": "parser.checkins", "agent.parse_rejects": "parser.rejects"}
BASE = minutes_ago(90)
CANARY_DESTINATION = "CANARY-UNMAPPED-DEST"


def _dispatcher_targets() -> list[str]:
    """Every module the frozen executable can dispatch to, read from the dispatcher itself (so a new subcommand is covered)."""
    source = (REPO_ROOT / "collector" / "freeze" / "dispatcher.py").read_text(encoding="utf-8")
    targets = sorted(set(re.findall(r"^\s+from (collector\.\w+) import main as sub_main", source, re.MULTILINE)))
    assert {"collector.run", "collector.preflight", "collector.bootstrap_state"} <= set(targets)
    return targets


#: Everything a Collector process can import that reaches -- or could come to reach -- the parsers.
IMPORT_CHAINS = sorted({
    *PARSER_MODULES, "agent.parser", "collector.parsers", "collector.run", "collector.v2_transform", "collector.v2_run",
    "agent.runtime.collector",  # the continuous agent imports the same parsers
    *_dispatcher_targets(),
})


def _run_in_empty_directory(code: str, cwd: Path, *args: str, pythonpath: Path = REPO_ROOT) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Runs `code` in a FRESH interpreter whose working directory is `cwd`, and returns what it left there.

    PYTHONDONTWRITEBYTECODE: Python's own __pycache__ is not what is being measured, and (for the bundle test) must not
    be written into the bundle either."""
    cwd.mkdir(parents=True, exist_ok=True)
    before = {path.relative_to(cwd).as_posix() for path in cwd.rglob("*")}
    env = {**os.environ, "PYTHONPATH": str(pythonpath), "PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": "sqlite:///x"}
    completed = subprocess.run([sys.executable, "-c", code, *args], cwd=cwd, env=env, capture_output=True, text=True, check=False)
    created = sorted({path.relative_to(cwd).as_posix() for path in cwd.rglob("*")} - before)
    return completed, created


# =====================================================================================================================
# 1. importing is free of filesystem side effects
# =====================================================================================================================

@pytest.mark.parametrize("module", IMPORT_CHAINS)
def test_importing_it_in_an_empty_directory_creates_nothing(tmp_path, module):
    completed, created = _run_in_empty_directory("import importlib, sys; importlib.import_module(sys.argv[1])", tmp_path / "cwd", module)

    assert completed.returncode == 0, completed.stderr
    assert created == [], f"importing {module} created {created} in the working directory"


def test_importing_and_reloading_the_three_parsers_creates_no_logs_folder_or_parser_log(tmp_path):
    code = (
        "import importlib, sys\n"
        "for name in sys.argv[1:]:\n"
        "    module = importlib.import_module(name)\n"
        "    importlib.reload(module)\n"
        "    importlib.reload(module)\n"
    )
    cwd = tmp_path / "cwd"

    completed, created = _run_in_empty_directory(code, cwd, *PARSER_MODULES)

    assert completed.returncode == 0, completed.stderr
    assert created == []
    for unwanted in ("logs", "logs/parser.acs.log", "logs/parser.checkins.log", "logs/parser.rejects.log"):
        assert not (cwd / unwanted).exists(), unwanted


def test_the_preflight_runtime_imports_check_leaves_the_working_directory_empty(tmp_path):
    """THE operation that put three files into the release bundle: `preflight` proving the Collector's modules import."""
    code = (
        "from collector import preflight\n"
        "result = preflight._check_runtime_imports()\n"
        "print('PASSED' if result.passed else 'FAILED: ' + result.detail)\n"
    )

    completed, created = _run_in_empty_directory(code, tmp_path / "cwd")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "PASSED"  # it really imported collector.run (and so the parsers)
    assert created == []


def test_the_runtime_imports_check_still_imports_the_parsers():
    """The check above is only a regression test while preflight still reaches the parsers through collector.run."""
    code = (
        "import sys\n"
        "from collector import preflight\n"
        "preflight._check_runtime_imports()\n"
        "print(sorted(m for m in sys.modules if m.startswith('agent.parser.')))\n"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=False,
                               env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": "sqlite:///x"})

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(sorted(PARSER_MODULES))


# =====================================================================================================================
# 2. who owns the handlers
# =====================================================================================================================

def test_after_the_collector_imports_them_the_parser_loggers_have_no_handler_and_default_settings(tmp_path):
    code = (
        "import json, logging\n"
        "import collector.run\n"
        "print(json.dumps({name: {'handlers': [type(h).__name__ for h in logging.getLogger(name).handlers],\n"
        "                         'propagate': logging.getLogger(name).propagate, 'level': logging.getLogger(name).level,\n"
        "                         'disabled': logging.getLogger(name).disabled} for name in " + repr(PARSER_LOGGERS) + "}))\n"
        "print(json.dumps({'root': len(logging.getLogger().handlers)}))\n"
    )

    completed, created = _run_in_empty_directory(code, tmp_path / "cwd")

    assert completed.returncode == 0, completed.stderr
    state, root = (json.loads(line) for line in completed.stdout.strip().splitlines())
    # No handler of their own; everything else is logging's default -- they are ordinary named loggers.
    assert state == {name: {"handlers": [], "propagate": True, "level": logging.NOTSET, "disabled": False} for name in PARSER_LOGGERS}
    # ...and the Collector gives them nowhere to go: no root handler exists, so their INFO summaries are written nowhere.
    assert root == {"root": 0}
    assert created == []


def test_the_parser_modules_use_plain_named_loggers_and_import_no_logging_helper():
    for module, name in zip(PARSER_MODULES, PARSER_LOGGERS, strict=True):
        path = REPO_ROOT / (module.replace(".", "/") + ".py")
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert f'logger = logging.getLogger("{name}")' in source, path
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {f"{'.' * node.level}{node.module or ''}" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not [entry for entry in imported if "logger_config" in entry], (path, imported)
        # Nothing in a parser module configures logging or touches the filesystem.
        for forbidden in ("get_logger", "FileHandler", "StreamHandler", "addHandler", "basicConfig", "mkdir", "open("):
            assert forbidden not in source, (path, forbidden)


def _lines() -> dict[str, list[str]]:
    return {
        "checkins": [checkin_line(BASE, BARCODE_CI, CANARY_DESTINATION)],
        "rejects": [reject_line(BASE, BARCODE_REJ)],
        "acs": [acs_line(BASE, item_message(BARCODE_HOLD, None, "Main"))],
    }


@pytest.mark.parametrize("source", ["checkins", "rejects", "acs"])
def test_the_parsers_still_emit_their_summaries_to_whatever_the_application_provides(source, caplog):
    """Handler ownership was removed -- the logging calls were not."""
    import importlib

    parser = importlib.import_module(f"agent.parser.{source}")
    with caplog.at_level(logging.INFO, logger=f"parser.{source}"):
        frame = parser.parse_lines(_lines()[source])

    assert len(frame) == 1
    records = [record for record in caplog.records if record.name == f"parser.{source}"]
    assert records and all(record.levelno == logging.INFO for record in records)
    assert any("Parsed" in record.getMessage() for record in records)


def test_an_application_handler_attached_to_a_parser_logger_receives_its_records():
    from agent.parser import checkins

    received: list[logging.LogRecord] = []

    class Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            received.append(record)

    logger = logging.getLogger("parser.checkins")
    sink, level = Sink(), logger.level
    logger.addHandler(sink)
    logger.setLevel(logging.INFO)
    try:
        checkins.parse_lines(_lines()["checkins"])
    finally:
        logger.removeHandler(sink)
        logger.setLevel(level)

    assert received and {record.name for record in received} == {"parser.checkins"}


# =====================================================================================================================
# 3. Contract v2 stays silent whatever is configured
# =====================================================================================================================

def test_a_v2_transform_emits_no_parser_record_even_to_handlers_an_application_attached(tmp_path):
    """The parsers having no handler of their own is how logging happens to be configured today. Contract v2's silence
    does not rest on that: with a handler on EVERY parser logger and on the root logger, a v2 transform still emits
    nothing -- and the same handlers do receive records from a direct (v1-style) parse, so they were really listening."""
    from agent.parser import checkins
    from collector import v2_transform

    received: list[logging.LogRecord] = []

    class Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            received.append(record)

    sink = Sink(level=logging.DEBUG)
    loggers = [logging.getLogger(name) for name in PARSER_LOGGERS] + [logging.getLogger()]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(sink)
        logger.setLevel(logging.DEBUG)
    ctx, cache = make_context(tmp_path)
    try:
        for source, lines in _lines().items():
            events, _counters = v2_transform.transform_lines(source, lines, ctx)
            assert events  # the transform really parsed something
        from_v2 = [record for record in received if record.name in PARSER_LOGGERS]
        states = [logging.getLogger(name).disabled for name in PARSER_LOGGERS]

        checkins.parse_lines(_lines()["checkins"])  # the control: outside a v2 transform the same sinks DO hear the parser
        from_v1 = [record for record in received if record.name in PARSER_LOGGERS]
    finally:
        cache.close()
        for logger, level in zip(loggers, levels, strict=True):
            logger.removeHandler(sink)
            logger.setLevel(level)

    assert from_v2 == []  # nothing at all from the parsers during the v2 transforms -- so no raw destination label either
    assert states == [False, False, False]  # and the loggers were handed back enabled
    assert from_v1, "the control parse was not heard: the sinks were not listening"
    # What v2 kept from being logged is exactly what the control shows a v1-style parse does log: the raw label.
    assert CANARY_DESTINATION in " ".join(record.getMessage() for record in from_v1)


# =====================================================================================================================
# 4. the legacy mirror keeps its parser logs -- from its own modules
# =====================================================================================================================

@pytest.mark.parametrize(("wrapper", "logger_name"), sorted(LEGACY_WRAPPERS.items()))
def test_a_legacy_mirror_module_attaches_the_legacy_handlers_to_its_parsers_logger(tmp_path, wrapper, logger_name):
    code = (
        "import importlib, json, logging, sys\n"
        "importlib.import_module(sys.argv[1])\n"
        "importlib.import_module(sys.argv[1])\n"
        "from agent.logger_config import get_logger\n"
        "get_logger(sys.argv[2])\n"  # asking again must not add a second pair
        "handlers = logging.getLogger(sys.argv[2]).handlers\n"
        "print(json.dumps([type(h).__name__ for h in handlers]))\n"
        "print(json.dumps([h.baseFilename for h in handlers if isinstance(h, logging.FileHandler)]))\n"
    )
    cwd = tmp_path / "cwd"

    completed, created = _run_in_empty_directory(code, cwd, wrapper, logger_name)

    assert completed.returncode == 0, completed.stderr
    kinds, files = (json.loads(line) for line in completed.stdout.strip().splitlines())
    assert kinds == ["FileHandler", "StreamHandler"]  # the legacy pair, exactly once
    assert [Path(name).name for name in files] == [f"{logger_name}.log"]
    assert f"logs/{logger_name}.log" in created  # deliberately: this is the legacy application's own log file
    # Only that parser's file, and the wrapper's own -- not the other two parsers'.
    assert {entry for entry in created if entry.startswith("logs/parser.")} == {f"logs/{logger_name}.log"}


def test_the_legacy_mirror_configures_parser_logging_through_the_existing_helper_and_nothing_shared_does():
    for wrapper, logger_name in LEGACY_WRAPPERS.items():
        source = (REPO_ROOT / (wrapper.replace(".", "/") + ".py")).read_text(encoding="utf-8")
        assert f'get_logger("{logger_name}")' in source, wrapper
    # The legacy entrypoint reaches all three through the wrappers it imports.
    pipeline = (REPO_ROOT / "agent" / "run_pipeline.py").read_text(encoding="utf-8")
    for wrapper in ("parse_acs", "parse_checkins", "parse_rejects"):
        assert f"from .{wrapper} import" in pipeline
    # Nothing shared with the Collector or the continuous agent configures a parser logger.
    shared = [*(REPO_ROOT / "collector").glob("*.py"), *(REPO_ROOT / "agent" / "parser").glob("*.py"),
              *(REPO_ROOT / "agent" / "runtime").glob("*.py")]
    for path in shared:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {f"{node.module or ''}.{alias.name}" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
        assert not [entry for entry in imported if "logger_config" in entry], (path, imported)  # (imports, not mentions in prose)
        for name in PARSER_LOGGERS:
            assert f'get_logger("{name}")' not in text, path


# =====================================================================================================================
# 5. what a Collector install contains
# =====================================================================================================================

def test_the_logging_helper_is_not_part_of_a_collector_install():
    assert "agent/logger_config.py" not in deploy_manifest.PARSER_RUNTIME_FILES
    assert set(deploy_manifest.PARSER_RUNTIME_FILES) == {
        "agent/__init__.py", "agent/parser/__init__.py", "agent/parser/checkins.py", "agent/parser/rejects.py", "agent/parser/acs.py",
    }


def test_the_deployed_file_list_is_the_real_import_closure_of_the_collector(tmp_path):
    """Not a claim in a docstring: every `agent` module a Collector process actually loads is a shipped file, and the
    logging helper is not among them."""
    code = (
        "import json, sys\n"
        "import collector.run, collector.preflight, collector.v2_run, collector.v2_transform\n"
        "from collector import preflight\n"
        "preflight._check_runtime_imports()\n"
        "print(json.dumps(sorted(m for m in sys.modules if m == 'agent' or m.startswith('agent.'))))\n"
    )

    completed, _created = _run_in_empty_directory(code, tmp_path / "cwd")

    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout.strip().splitlines()[-1])
    shipped = {entry[:-3].replace("/__init__", "").replace("/", ".") for entry in deploy_manifest.PARSER_RUNTIME_FILES}
    assert set(loaded) == shipped
    assert "agent.logger_config" not in loaded


# =====================================================================================================================
# 6. a release bundle is still exactly its manifest after the import chain has run inside it
# =====================================================================================================================

_BUILT_AT = "2026-01-01T00:00:00.000000Z"  # freshness: allow FRESH004 -- a fixed manifest stamp passed as built_at=, never compared with the clock

_BUNDLE_CHAIN = (
    "import sys\n"
    "from collector import preflight\n"
    "result = preflight._check_runtime_imports()\n"
    "import collector.run, collector.parsers, agent.parser.acs, agent.parser.checkins, agent.parser.rejects\n"
    "print('PASSED' if result.passed else 'FAILED: ' + result.detail)\n"
    "print(collector.run.__file__)\n"
)


@pytest.fixture
def source_bundle(tmp_path) -> Path:
    return build_release.build_release(REPO_ROOT, tmp_path / "out", RELEASE_VERSION, built_at=_BUILT_AT).bundle_dir


def _manifest_problems(bundle: Path) -> list[str]:
    """The bundle against its own MANIFEST.json, the way install.ps1 judges it: every listed file present with its hash,
    and NO file that is not listed."""
    import hashlib

    manifest = json.loads((bundle / "MANIFEST.json").read_text(encoding="utf-8"))
    listed = {entry["path"]: entry for entry in manifest["files"]}
    on_disk = {path.relative_to(bundle).as_posix(): path for path in bundle.rglob("*") if path.is_file()}
    on_disk.pop("MANIFEST.json")
    problems = [f"UNEXPECTED: {name}" for name in sorted(set(on_disk) - set(listed))]
    problems += [f"MISSING: {name}" for name in sorted(set(listed) - set(on_disk))]
    problems += [f"HASH: {name}" for name in sorted(set(listed) & set(on_disk))
                 if hashlib.sha256(on_disk[name].read_bytes()).hexdigest() != listed[name]["sha256"]]
    return problems


def test_running_the_import_chain_from_inside_a_release_bundle_adds_no_file_to_it(source_bundle):
    """The working directory IS the bundle (as it was during setup) and the code that runs IS the bundle's own."""
    assert _manifest_problems(source_bundle) == []
    folders_before = sorted(path.relative_to(source_bundle).as_posix() for path in source_bundle.rglob("*") if path.is_dir())

    completed, created = _run_in_empty_directory(_BUNDLE_CHAIN, source_bundle, pythonpath=source_bundle)

    assert completed.returncode == 0, completed.stderr
    passed, ran_from = completed.stdout.strip().splitlines()
    assert passed == "PASSED"
    assert Path(ran_from).resolve().is_relative_to(source_bundle.resolve())  # the BUNDLE's modules ran, not the repository's
    assert created == [] and not (source_bundle / "logs").exists()
    assert _manifest_problems(source_bundle) == []
    assert sorted(path.relative_to(source_bundle).as_posix() for path in source_bundle.rglob("*") if path.is_dir()) == folders_before
    # ...and that bundle, which no longer ships the logging helper, imported its parsers without it.
    assert not (source_bundle / "agent" / "logger_config.py").exists()


_POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
needs_windows_powershell = pytest.mark.skipif(
    sys.platform != "win32" or _POWERSHELL is None, reason="runs the bundle's real install.ps1 -VerifyBundleOnly in Windows PowerShell"
)


def _verify_bundle(bundle: Path) -> subprocess.CompletedProcess:
    assert _POWERSHELL is not None
    return subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(bundle / "install.ps1"), "-VerifyBundleOnly"],
        cwd=bundle, capture_output=True, text=True, timeout=300, check=False,
    )


@needs_windows_powershell
def test_the_real_verifier_accepts_the_bundle_again_after_the_import_chain_ran_inside_it_and_is_still_strict(source_bundle):
    """What failed in the rehearsal, end to end: verify, run the chain from inside the bundle, verify AGAIN. And the
    verifier itself was not loosened to make that pass: the three files this bug used to create are still refused."""
    first = _verify_bundle(source_bundle)
    assert first.returncode == 0, first.stdout + first.stderr

    completed, created = _run_in_empty_directory(_BUNDLE_CHAIN, source_bundle, pythonpath=source_bundle)
    assert completed.returncode == 0 and created == [], (completed.stderr, created)

    second = _verify_bundle(source_bundle)
    assert second.returncode == 0, second.stdout + second.stderr  # setup.ps1 can be run, or resumed, from the same folder

    (source_bundle / "logs").mkdir()
    for name in PARSER_LOGGERS:
        (source_bundle / "logs" / f"{name}.log").write_bytes(b"")
    strict = _verify_bundle(source_bundle)
    output = strict.stdout + strict.stderr
    assert strict.returncode != 0
    for name in PARSER_LOGGERS:
        assert f"{name}.log" in output, output
