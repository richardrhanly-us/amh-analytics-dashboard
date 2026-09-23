"""Tests for the Contract v2 release-packaging change (government-readiness audit, Part 6/7, items 16-17).

Distinguishes "code exists in the repository" from "code is included in the runtime manifest a real release bundle is
built from" and "the frozen build's spec is told to bundle it too" -- the three gaps the prior cutover audit found.
Actually building a frozen executable is out of scope for this suite (PyInstaller/native builds don't run in this
sandbox -- see tests/test_collector_freeze.py's own equivalent static-only spec assertions, which this file matches).
"""

from pathlib import Path

from collector import build_release

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "collector" / "freeze" / "sortview_collector.spec"

V2_MODULES = (
    "collector/v2_classify.py",
    "collector/v2_config.py",
    "collector/v2_events.py",
    "collector/v2_identity.py",
    "collector/v2_keys.py",
    "collector/v2_normalize.py",
    "collector/v2_patrons.py",
    "collector/v2_quarantine.py",
    "collector/v2_reader.py",
    "collector/v2_rules.py",
    "collector/v2_run.py",
    "collector/v2_safe_errors.py",
    "collector/v2_status.py",
    "collector/v2_transform.py",
    "collector/v2_uploader.py",
)


def _spec_text() -> str:
    return SPEC_PATH.read_text(encoding="utf-8")


# --- item 16: every v2 module is in the production runtime manifest, none left in BUILD_ONLY -------------------------

def test_every_v2_module_is_now_in_the_runtime_manifest():
    for module in V2_MODULES:
        assert module in build_release.COLLECTOR_RUNTIME_FILES, module


def test_no_v2_module_remains_in_build_only_files():
    for module in V2_MODULES:
        assert module not in build_release.BUILD_ONLY_COLLECTOR_FILES, module


def test_runtime_and_build_only_files_still_account_for_every_py_file_with_no_overlap():
    # The same invariant tests/test_collector_build_release.py already enforces -- re-asserted here so this file
    # stands on its own as evidence the move didn't create an overlap or drop a file.
    collector_dir = REPO_ROOT / "collector"
    actual = {f"collector/{p.name}" for p in collector_dir.glob("*.py")}
    combined = set(build_release.COLLECTOR_RUNTIME_FILES) | set(build_release.BUILD_ONLY_COLLECTOR_FILES)
    assert combined == actual
    assert set(build_release.COLLECTOR_RUNTIME_FILES).isdisjoint(build_release.BUILD_ONLY_COLLECTOR_FILES)


def test_deploy_manifest_and_build_release_themselves_are_still_build_only():
    # Sanity check that the move only touched the v2 modules, not the two files that must never ship.
    assert "collector/deploy_manifest.py" in build_release.BUILD_ONLY_COLLECTOR_FILES
    assert "collector/build_release.py" in build_release.BUILD_ONLY_COLLECTOR_FILES


# --- item 17: the frozen build's spec is told to bundle every v2 module too ------------------------------------------

def test_spec_hiddenimports_cover_every_v2_module():
    text = _spec_text()
    for module in V2_MODULES:
        dotted = module.removeprefix("collector/").removesuffix(".py")
        assert f'"collector.{dotted}"' in text, module


def test_spec_still_covers_the_five_original_subcommand_targets():
    # The v2 addition must be additive, not a replacement -- these five must still be present.
    text = _spec_text()
    for module in (
        "collector.run", "collector.preflight", "collector.bootstrap_state",
        "collector.support_info", "collector.task_settings",
    ):
        assert f'"{module}"' in text


def test_spec_still_excludes_the_continuous_agent_and_backend_dependencies():
    # The v2 addition must not have loosened any existing exclusion.
    text = _spec_text()
    for excluded in ("agent.runtime", "agent.main", "agent.tailer", "streamlit", "fastapi", "sqlalchemy", "psycopg2"):
        assert f'"{excluded}"' in text
