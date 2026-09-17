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

import sys
from pathlib import Path

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


def test_dispatcher_contains_all_four_subcommands():
    assert set(dispatcher._SUBCOMMANDS) == {"run", "preflight", "bootstrap", "support-info"}


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


def test_spec_hiddenimports_cover_all_four_subcommand_targets():
    text = _spec_text()
    for module in ("collector.run", "collector.preflight", "collector.bootstrap_state", "collector.support_info"):
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
