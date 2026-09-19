"""Tests for the frozen-runtime (PyInstaller) proof -- collector/freeze/.

Two kinds of coverage, deliberately kept distinct -- same split as
tests/test_collector_build_release.py and test_collector_deploy_manifest.py:

  1. Real, executable proof: collector/freeze/dispatcher.py itself, run
     in-process against the real collector/*.py main() entry points (no
     mocking of Collector logic -- only argv/exit-code plumbing is
     exercised here, since the Collector modules' own behavior is already
     covered by their own test files).
  2. Static/textual checks on sortview_collector.spec and build_frozen.ps1
     -- this project's CI does not build PyInstaller (a ~75MB, ~25s local
     build; not something to run on every CI push). Real end-to-end proof
     of the frozen .exe was performed live on LIB-L26, in isolated scratch
     paths, with PYTHONPATH cleared and a reduced PATH excluding every dev
     Python install -- see that phase's report, not this file, for the
     actual .exe execution evidence.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from collector import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent
FREEZE_DIR = REPO_ROOT / "collector" / "freeze"

sys.path.insert(0, str(FREEZE_DIR))
import dispatcher  # noqa: I001 -- must follow sys.path.insert above, not sortable with the stdlib imports


# --- dispatcher: real, executable dispatch/exit-code proof ----------------


def test_dispatcher_no_subcommand_returns_2(capsys):
    assert dispatcher.main([]) == 2
    assert "Usage" in capsys.readouterr().err


def test_dispatcher_unknown_subcommand_returns_2(capsys):
    assert dispatcher.main(["bogus"]) == 2
    assert "Usage" in capsys.readouterr().err


def test_dispatcher_run_forwards_to_collector_run_main_with_same_exit_code():
    # A nonexistent --config exercises the REAL collector.run.main() exit
    # path (2 = config error) -- not mocked, so this also proves argv was
    # actually forwarded correctly (collector.run.main needs --config to
    # even reach that branch instead of an argparse usage error, which
    # would exit via SystemExit instead).
    exit_code = dispatcher.main(["run", "--config", "does-not-exist.json"])
    assert exit_code == 2


def test_dispatcher_preflight_forwards_to_collector_preflight_main(capsys):
    exit_code = dispatcher.main(["preflight", "--config", "does-not-exist.json"])
    assert exit_code == 2
    assert "config_loads" in capsys.readouterr().out


def test_dispatcher_bootstrap_forwards_to_collector_bootstrap_state_main():
    exit_code = dispatcher.main(["bootstrap", "--config", "does-not-exist.json"])
    assert exit_code == 2


def test_dispatcher_support_info_forwards_to_collector_support_info_main(capsys):
    exit_code = dispatcher.main(["support-info", "--config", "does-not-exist.json"])
    assert exit_code == 2
    assert "did NOT load" in capsys.readouterr().out


def test_dispatcher_default_argv_uses_sys_argv(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["SortViewCollector.exe", "bogus"])
    assert dispatcher.main() == 2


def test_dispatcher_contains_all_six_subcommands():
    # task-xml added for the frozen release-bundle integration phase --
    # deployment/Task-Scheduler-XML generation, not Collector ingestion.
    # version added for release-version hardening -- a config-free report of
    # collector.__version__; the five earlier subcommands are unchanged.
    assert set(dispatcher._SUBCOMMANDS) == {"run", "preflight", "bootstrap", "support-info", "task-xml", "version"}
    assert {"run", "preflight", "bootstrap", "support-info", "task-xml"} <= set(dispatcher._SUBCOMMANDS)


# --- dispatcher: `version` -- config-free, collector.__version__ only -------


def test_dispatcher_version_prints_exactly_collector_version_and_exits_zero(capsys):
    exit_code = dispatcher.main(["version"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"{__version__}\n"
    assert captured.err == ""


def test_dispatcher_version_needs_no_config_token_or_working_files(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)  # an empty directory: no config, state or logs anywhere near

    assert dispatcher.main(["version"]) == 0
    assert capsys.readouterr().out == f"{__version__}\n"
    assert list(tmp_path.iterdir()) == []  # and it wrote nothing


def test_dispatcher_version_as_a_real_source_invocation_matches(tmp_path):
    # The source-mode equivalent of `SortViewCollector.exe version`: run the
    # dispatcher script itself in a fresh interpreter, with no token and no
    # config anywhere in sight.
    env = {k: v for k, v in os.environ.items() if k != "SORTVIEW_API_TOKEN"}
    env["PYTHONPATH"] = str(REPO_ROOT)

    result = subprocess.run(
        [sys.executable, str(FREEZE_DIR / "dispatcher.py"), "version"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip("\r\n") == __version__
    assert result.stdout.strip() == __version__  # nothing else on stdout
    assert result.stderr == ""


def test_dispatcher_version_rejects_arguments_rather_than_ignoring_them(capsys):
    assert dispatcher.main(["version", "--config", "does-not-exist.json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Usage" in captured.err and "version" in captured.err


def test_dispatcher_usage_lists_the_version_subcommand(capsys):
    dispatcher.main([])

    assert "version" in capsys.readouterr().err


def test_dispatcher_takes_the_version_from_collector_and_never_hardcodes_it():
    text = (FREEZE_DIR / "dispatcher.py").read_text(encoding="utf-8")

    assert "from collector import __version__" in text
    assert __version__ not in text
    assert not re.search(r"\d+\.\d+\.\d+", text.split('"""', 2)[-1])  # no version literal in the code body


def test_the_other_subcommands_are_unchanged_by_the_version_addition(capsys):
    # The same forwarding/exit-code contracts asserted individually above,
    # re-checked together after `version` was added ahead of them.
    assert dispatcher.main(["run", "--config", "does-not-exist.json"]) == 2
    assert dispatcher.main(["bootstrap", "--config", "does-not-exist.json"]) == 2
    assert dispatcher.main(["preflight", "--config", "does-not-exist.json"]) == 2
    assert dispatcher.main(["support-info", "--config", "does-not-exist.json"]) == 2
    out = capsys.readouterr().out
    assert "config_loads" in out and "did NOT load" in out
    assert dispatcher.main(["bogus"]) == 2


def test_dispatcher_task_xml_forwards_to_collector_task_settings_main(tmp_path):
    output_path = tmp_path / "task.xml"
    exit_code = dispatcher.main([
        "task-xml",
        "--python-exe", r"C:\SortView\Collector\SortViewCollector.exe",
        "--config-path", r"C:\ProgramData\SortViewCollector\config\collector_config.json",
        "--working-dir", r"C:\SortView\Collector",
        "--output", str(output_path),
        "--frozen",
    ])
    assert exit_code == 0
    xml_text = output_path.read_text(encoding="utf-16")
    assert "run --config " in xml_text
    assert "-m collector.run" not in xml_text


def test_dispatcher_source_never_references_agent_runtime_or_sortviewagent():
    text = (FREEZE_DIR / "dispatcher.py").read_text(encoding="utf-8")
    assert "agent.runtime" not in text
    assert "agent.main" not in text
    assert "SortViewAgent" not in text


def test_dispatcher_imports_are_static_not_dynamic_importlib():
    # PyInstaller's analyzer follows ordinary from-imports even inside
    # if/elif branches, but NOT a computed importlib.import_module(name)
    # call -- see dispatcher.py's own docstring for why this matters
    # (which legitimately mentions "importlib" by name to explain this,
    # hence checking the CODE body below the docstring, not the whole file).
    full_text = (FREEZE_DIR / "dispatcher.py").read_text(encoding="utf-8")
    code_body = full_text.split('"""', 2)[-1]
    assert "importlib" not in code_body
    assert "from collector.run import main" in full_text
    assert "from collector.preflight import main" in full_text
    assert "from collector.bootstrap_state import main" in full_text
    assert "from collector.support_info import main" in full_text
    assert "from collector.task_settings import main" in full_text


# --- static checks on the PyInstaller spec (CI does not build PyInstaller) --


def _spec_text() -> str:
    return (FREEZE_DIR / "sortview_collector.spec").read_text(encoding="utf-8")


def test_spec_repo_root_resolution_uses_two_parents_not_three():
    # Regression guard for the exact bug found and fixed while building
    # this proof: SPECPATH (PyInstaller-injected) is the spec file's
    # DIRECTORY (collector/freeze), not its full path -- two .parent calls
    # reach the repo root; three landed one directory ABOVE it and made
    # the real build fail with "script ... not found" until corrected
    # (verified by an actual successful build afterward, not just this
    # static check).
    text = _spec_text()
    assert "Path(SPECPATH).resolve().parent.parent" in text
    assert "Path(SPECPATH).resolve().parent.parent.parent" not in text


def test_spec_targets_the_dispatcher_script():
    text = _spec_text()
    assert 'collector" / "freeze" / "dispatcher.py"' in text


def test_spec_is_onedir_not_onefile():
    text = _spec_text()
    assert "exclude_binaries=True" in text
    assert "COLLECT(" in text


def test_spec_hiddenimports_cover_all_five_subcommand_targets():
    text = _spec_text()
    for module in (
        "collector.run", "collector.preflight", "collector.bootstrap_state",
        "collector.support_info", "collector.task_settings",
    ):
        assert f'"{module}"' in text


def test_spec_excludes_continuous_agent_runtime():
    text = _spec_text()
    for excluded in (
        "agent.runtime",
        "agent.main",
        "agent.tailer",
        "agent.state",
        "agent.spool",
        "agent.uploader",
    ):
        assert f'"{excluded}"' in text


def test_spec_excludes_backend_and_dashboard_dependencies():
    text = _spec_text()
    for excluded in ("streamlit", "fastapi", "sqlalchemy", "psycopg2"):
        assert f'"{excluded}"' in text


def test_spec_never_references_sortviewagent_tests_or_docs():
    # The spec's own module docstring legitimately explains WHY
    # SortViewAgent/tests/docs are excluded, by name -- what matters is
    # that none of the actual PyInstaller directives (datas/binaries/
    # pathex/etc., i.e. the code after the docstring) reference them.
    full_text = _spec_text()
    code_body = full_text.split('"""', 2)[-1]
    assert "SortViewAgent" not in code_body
    assert '"tests"' not in code_body
    assert '"docs"' not in code_body


# --- static checks on the build script --------------------------------


def _build_script_text() -> str:
    return (FREEZE_DIR / "build_frozen.ps1").read_text(encoding="utf-8")


def test_build_script_requires_isolated_packaging_venv():
    text = _build_script_text()
    assert ".pyinstaller-venv" in text
    # Must refuse (throw) rather than silently fall back to a bare
    # `python` on PATH or the normal project .venv if the isolated venv
    # is missing.
    assert "throw" in text
    assert "not found" in text.lower()


def test_build_script_never_invokes_the_normal_project_venv():
    text = _build_script_text()
    assert "\\.venv\\Scripts\\python.exe" not in text


def test_build_script_is_deterministic_same_spec_same_venv_every_run():
    # No randomness / timestamp-dependent naming / network fetch in the
    # build invocation itself -- always the same spec file, always the
    # same isolated venv, always the same --distpath/--workpath shape.
    text = _build_script_text()
    assert "sortview_collector.spec" in text
    assert "--noconfirm" in text
    assert "Get-Random" not in text
    assert "Invoke-WebRequest" not in text
    assert "Invoke-RestMethod" not in text


def test_build_script_runs_the_built_executables_version_command_after_the_exe_check():
    text = _build_script_text()
    exe_check = text.index('throw "Build reported success but $ExePath was not found."')
    version_run = text.index("& $ExePath version")

    assert exe_check < version_run
    assert "Assert-FrozenRuntimeVersion -ExpectedVersion $ExpectedVersion" in text[version_run:]
    assert "$LASTEXITCODE" in text[version_run - 80 : version_run + 120]  # exit code captured immediately


def test_build_script_gets_the_expected_version_by_importing_this_repos_collector_not_by_regex():
    text = _build_script_text()
    code = text[text.index("function Get-SourceCollectorVersion") : text.index("function Assert-FrozenRuntimeVersion")]

    assert "import collector" in code and "collector.__version__" in code
    assert "-I -c" in code  # isolated: ignores PYTHONPATH, user site and the current directory
    assert "sys.path.insert(0" in code and "collector.__file__" in code  # this repo first, location verified
    assert "Get-SourceCollectorVersion -PythonExe $VenvPython -RepoRoot $RepoRoot" in text
    # ...and never parses the version out of the source file.
    assert "__init__.py" not in text
    assert "Select-String" not in text and "Get-Content" not in text


def test_build_script_never_rewrites_source_or_writes_a_version_file():
    text = _build_script_text()

    for forbidden in ("Set-Content", "Out-File", "WriteAllText", "Add-Content", "-replace", "(Get-Content"):
        assert forbidden not in text, forbidden


def _powershell():
    return shutil.which("pwsh") or shutil.which("powershell")


needs_powershell = pytest.mark.skipif(
    _powershell() is None, reason="no PowerShell available -- the static build-script tests still run"
)


def _ps_function(name: str) -> str:
    match = re.search(rf"^function {re.escape(name)} \{{.*?^\}}", _build_script_text(), re.MULTILINE | re.DOTALL)
    assert match, f"function {name} not found in build_frozen.ps1"
    return match.group(0)


def _run_powershell(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _ps_literal(value) -> str:
    if value is None:
        return "$null"
    if isinstance(value, bool):
        return "$true" if value else "$false"
    if isinstance(value, int):
        return f"({value})"
    if isinstance(value, list):
        return "@(" + ",".join(_ps_literal(v) for v in value) + ")"
    return "'" + str(value).replace("'", "''") + "'"


def _assert_outcomes(cases: dict[str, dict]) -> dict[str, str]:
    """Runs the script's OWN Assert-FrozenRuntimeVersion once per case in one
    PowerShell process; returns each case's thrown message ('' if it passed)."""
    lines = [_ps_function("Assert-FrozenRuntimeVersion"), "$results = [ordered]@{}"]
    for name, args in cases.items():
        rendered = " ".join(f"-{key} {_ps_literal(value)}" for key, value in args.items())
        lines.append(
            f"try {{ Assert-FrozenRuntimeVersion {rendered}; $results['{name}'] = '' }} "
            f"catch {{ $results['{name}'] = $_.Exception.Message }}"
        )
    lines.append("ConvertTo-Json -InputObject $results -Compress")
    return json.loads(_run_powershell("\n".join(lines)))


_ASSERT_CASES = {
    "match": {"ExpectedVersion": "1.0.3", "ActualOutput": ["1.0.3"], "ExitCode": 0},
    "match_padded": {"ExpectedVersion": "1.0.3", "ActualOutput": ["1.0.3 "], "ExitCode": 0},
    "mismatch": {"ExpectedVersion": "1.0.3", "ActualOutput": ["1.0.2"], "ExitCode": 0},
    "mismatch_case": {"ExpectedVersion": "1.0.3-RC1", "ActualOutput": ["1.0.3-rc1"], "ExitCode": 0},
    "nonzero_exit": {"ExpectedVersion": "1.0.3", "ActualOutput": ["1.0.3"], "ExitCode": 2},
    "blank": {"ExpectedVersion": "1.0.3", "ActualOutput": ["", "  "], "ExitCode": 0},
    "null_output": {"ExpectedVersion": "1.0.3", "ActualOutput": None, "ExitCode": 0},
    "malformed_words": {"ExpectedVersion": "1.0.3", "ActualOutput": ["1.0.3 extra"], "ExitCode": 0},
    "malformed_lines": {"ExpectedVersion": "1.0.3", "ActualOutput": ["1.0.3", "1.0.3"], "ExitCode": 0},
    "usage_text": {"ExpectedVersion": "1.0.3", "ActualOutput": ["Usage: SortViewCollector.exe <subcommand>"], "ExitCode": 2},
    "blank_expected": {"ExpectedVersion": " ", "ActualOutput": ["1.0.3"], "ExitCode": 0},
}


@pytest.fixture(scope="module")
def assert_outcomes():
    if _powershell() is None:
        pytest.skip("no PowerShell available")
    return _assert_outcomes(_ASSERT_CASES)


@needs_powershell
def test_build_script_accepts_a_runtime_reporting_exactly_the_expected_version(assert_outcomes):
    assert assert_outcomes["match"] == ""
    assert assert_outcomes["match_padded"] == ""  # only surrounding whitespace is tolerated


@needs_powershell
def test_build_script_fails_loudly_on_a_version_mismatch_showing_expected_and_actual(assert_outcomes):
    for case in ("mismatch", "mismatch_case"):
        message = assert_outcomes[case]
        assert "VERSION MISMATCH" in message, case
        assert "expected '" in message and "reports '" in message, case
    assert "1.0.3" in assert_outcomes["mismatch"] and "1.0.2" in assert_outcomes["mismatch"]


@needs_powershell
def test_build_script_fails_on_nonzero_exit_blank_or_malformed_output(assert_outcomes):
    for case in ("nonzero_exit", "blank", "null_output", "malformed_words", "malformed_lines", "usage_text",
                 "blank_expected"):
        assert "VERSION CHECK FAILED" in assert_outcomes[case], case
    assert "exited 2" in assert_outcomes["nonzero_exit"]
    assert "1.0.3" in assert_outcomes["malformed_words"]  # both sides shown


@needs_powershell
def test_build_script_reads_the_expected_version_from_this_repos_collector_import():
    python_exe = _ps_literal(sys.executable)
    repo = _ps_literal(str(REPO_ROOT))

    out = _run_powershell(
        _ps_function("Get-SourceCollectorVersion")
        + f"\nGet-SourceCollectorVersion -PythonExe {python_exe} -RepoRoot {repo}"
    )

    assert out.strip() == __version__


@needs_powershell
def test_build_script_expected_version_comes_from_the_repo_it_is_pointed_at_not_any_other_collector(tmp_path):
    decoy_repo = tmp_path / "decoy_repo"
    (decoy_repo / "collector").mkdir(parents=True)
    (decoy_repo / "collector" / "__init__.py").write_text('__version__ = "9.8.7"\n', encoding="utf-8")

    out = _run_powershell(
        _ps_function("Get-SourceCollectorVersion")
        + f"\nGet-SourceCollectorVersion -PythonExe {_ps_literal(sys.executable)} -RepoRoot {_ps_literal(str(decoy_repo))}"
    )

    assert out.strip() == "9.8.7"  # THAT repo's version -- not this repo's, not an installed package's


@needs_powershell
def test_build_script_refuses_when_the_repo_has_no_collector_package(tmp_path):
    empty = tmp_path / "empty_repo"
    empty.mkdir()
    script = (
        _ps_function("Get-SourceCollectorVersion")
        + f"\ntry {{ Get-SourceCollectorVersion -PythonExe {_ps_literal(sys.executable)} "
        f"-RepoRoot {_ps_literal(str(empty))}; 'NO-THROW' }} catch {{ 'THREW' }}"
    )

    assert _run_powershell(script).strip().splitlines()[-1] == "THREW"


@needs_powershell
def test_build_script_parses_without_errors():
    path = str(FREEZE_DIR / "build_frozen.ps1").replace("'", "''")
    script = (
        "$errs = $null; $tokens = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errs); "
        "$errs.Count"
    )

    assert _run_powershell(script).strip() == "0"


# --- no secrets / production config in the packaging-only directory -----


def test_freeze_directory_contains_no_config_or_secret_files():
    # Only build tooling belongs here -- no collector_config.json, no
    # .env, no token file of any kind.
    freeze_files = {p.name for p in FREEZE_DIR.iterdir() if p.is_file()}
    for name in freeze_files:
        assert not name.endswith(".json"), f"unexpected config-shaped file in collector/freeze/: {name}"
        assert "token" not in name.lower()
        assert "secret" not in name.lower()


def test_freeze_directory_has_exactly_the_expected_build_tooling_files():
    freeze_files = {p.name for p in FREEZE_DIR.iterdir() if p.is_file()}
    assert freeze_files == {"dispatcher.py", "sortview_collector.spec", "build_frozen.ps1"}
