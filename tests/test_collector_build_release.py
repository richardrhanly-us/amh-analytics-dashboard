"""Tests for collector/build_release.py and the standalone release-bundle
system it produces (release-bundle packaging phase).

Two kinds of coverage here, deliberately kept distinct -- same split as
tests/test_collector_deploy_manifest.py:

  1. Real, executable proof (most of this file): collector.build_release
     itself, run against a scratch output directory, then inspected --
     real files on disk, real MANIFEST.json, real isolated-subprocess
     import smoke test with neither the repo root NOR the bundle's own
     collector/agent trees confused with each other.
  2. Static/textual checks on the .ps1 scripts themselves (clearly
     labeled where they appear) -- this project's CI cannot execute
     PowerShell. Real end-to-end proof of the .ps1 scripts happens live
     on LIB-L26 in a scratch InstallRoot/DataRoot, separate from this
     suite.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from collector import build_release, deploy_manifest
from collector.config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- the builder itself, against the real repo --------------------------


def test_required_source_files_all_exist_in_the_real_repo():
    # Not a fake/tmp_path repo -- the real one. Fails loudly here rather
    # than silently shipping an incomplete bundle if a listed source file
    # is ever renamed or removed.
    pairs = build_release._required_source_files(REPO_ROOT)
    missing = [str(src) for src, _dest in pairs if not src.is_file()]
    assert missing == []


def test_collector_runtime_files_plus_build_only_files_account_for_every_py_file():
    # Cross-check against the directory's actual contents, in BOTH
    # directions: catches a forgotten addition to COLLECTOR_RUNTIME_FILES
    # (a new real runtime module silently left out of every bundle) and
    # an accidental inclusion (a new build-only module silently shipped).
    actual = {p.name for p in (REPO_ROOT / "collector").glob("*.py")}
    declared = {Path(rel).name for rel in build_release.COLLECTOR_RUNTIME_FILES}
    declared |= {Path(rel).name for rel in build_release.BUILD_ONLY_COLLECTOR_FILES}
    assert actual == declared


def test_build_only_files_are_never_in_the_runtime_list():
    runtime = set(build_release.COLLECTOR_RUNTIME_FILES)
    build_only = set(build_release.BUILD_ONLY_COLLECTOR_FILES)
    assert runtime.isdisjoint(build_only)
    assert "collector/deploy_manifest.py" in build_only
    assert "collector/build_release.py" in build_only


def test_deploy_tool_and_support_file_sources_all_exist():
    for source_rel, _dest_rel in build_release.DEPLOY_TOOL_FILES:
        assert (REPO_ROOT / source_rel).is_file(), source_rel
    for source_rel, _dest_rel in build_release.SUPPORT_FILES:
        assert (REPO_ROOT / source_rel).is_file(), source_rel


# --- build_release(): real bundle on disk --------------------------------


@pytest.fixture
def built_bundle(tmp_path):
    result = build_release.build_release(
        REPO_ROOT, tmp_path, "9.9.9-test", built_at="2026-01-01T00:00:00.000000Z"
    )
    return result


def test_build_release_creates_versioned_directory(built_bundle, tmp_path):
    assert built_bundle.bundle_dir == tmp_path / "SortViewCollector-9.9.9-test"
    assert built_bundle.bundle_dir.is_dir()


def test_build_release_refuses_existing_output_without_force(tmp_path):
    build_release.build_release(REPO_ROOT, tmp_path, "1.0.0", built_at="x")
    with pytest.raises(build_release.BuildError):
        build_release.build_release(REPO_ROOT, tmp_path, "1.0.0", built_at="x")


def test_build_release_force_rebuilds(tmp_path):
    build_release.build_release(REPO_ROOT, tmp_path, "1.0.0", built_at="x")
    marker = tmp_path / "SortViewCollector-1.0.0" / "collector" / "run.py"
    original = marker.read_bytes()
    marker.write_bytes(b"corrupted")
    build_release.build_release(REPO_ROOT, tmp_path, "1.0.0", force=True, built_at="y")
    assert marker.read_bytes() == original


def test_build_release_raises_loudly_and_writes_nothing_on_missing_source(tmp_path, monkeypatch):
    fake_repo = tmp_path / "fake_repo"
    fake_repo.mkdir()
    output = tmp_path / "out"
    with pytest.raises(build_release.BuildError):
        build_release.build_release(fake_repo, output, "1.0.0", built_at="x")
    assert not output.exists()


# --- bundle CONTENTS: contains what it must ------------------------------


def test_bundle_contains_every_required_collector_runtime_file(built_bundle):
    for rel in build_release.COLLECTOR_RUNTIME_FILES:
        assert (built_bundle.bundle_dir / rel).is_file(), rel


def test_bundle_contains_only_the_approved_six_agent_parser_files(built_bundle):
    agent_files = sorted(
        p.relative_to(built_bundle.bundle_dir).as_posix()
        for p in (built_bundle.bundle_dir / "agent").rglob("*")
        if p.is_file()
    )
    expected = sorted(deploy_manifest.PARSER_RUNTIME_FILES)
    assert agent_files == expected


def test_bundle_contains_deploy_tools_and_support_files(built_bundle):
    for _source_rel, dest_rel in build_release.DEPLOY_TOOL_FILES:
        assert (built_bundle.bundle_dir / dest_rel).is_file(), dest_rel
    for _source_rel, dest_rel in build_release.SUPPORT_FILES:
        assert (built_bundle.bundle_dir / dest_rel).is_file(), dest_rel
    assert (built_bundle.bundle_dir / "collector_config.example.json").is_file()
    assert (built_bundle.bundle_dir / "MANIFEST.json").is_file()


def test_bundle_contains_token_script(built_bundle):
    assert (built_bundle.bundle_dir / "tools" / "set-api-token.ps1").is_file()


def test_bundle_contains_requirements(built_bundle):
    text = (built_bundle.bundle_dir / "requirements.txt").read_text(encoding="utf-8")
    assert "requests==" in text
    assert "pandas==" in text


def test_bootstrap_tool_present_in_bundle(built_bundle):
    assert (built_bundle.bundle_dir / "collector" / "bootstrap_state.py").is_file()


# --- bundle CONTENTS: excludes what it must -------------------------------


def test_bundle_never_includes_deploy_manifest_or_build_release_themselves(built_bundle):
    assert not (built_bundle.bundle_dir / "collector" / "deploy_manifest.py").exists()
    assert not (built_bundle.bundle_dir / "collector" / "build_release.py").exists()


def test_bundle_never_includes_agent_runtime_files(built_bundle):
    excluded_relative = [
        "agent/main.py",
        "agent/config.py",
        "agent/run_pipeline.py",
        "agent/uploader.py",
        "agent/tailer.py",
        "agent/state.py",
        "agent/spool.py",
        "agent/discovery.py",
        "agent/identity.py",
        "agent/event_identity.py",
        "agent/runtime/config.py",
        "agent/runtime/supervisor.py",
        "agent/runtime/heartbeat.py",
        "agent/runtime/uploader.py",
    ]
    for rel in excluded_relative:
        assert not (built_bundle.bundle_dir / rel).exists(), rel


def test_bundle_never_includes_sortviewagent(built_bundle):
    for path in built_bundle.bundle_dir.rglob("*"):
        assert "SortViewAgent" not in path.parts


def test_bundle_never_includes_tests_or_docs(built_bundle):
    for path in built_bundle.bundle_dir.rglob("*"):
        assert "tests" not in path.parts
        assert "docs" not in path.parts


def test_bundle_never_includes_backend_dashboard_requirements(built_bundle):
    # The repo's OWN top-level requirements.txt (Streamlit/FastAPI
    # backend) must never be the one that lands in the bundle -- only
    # collector/deploy/requirements.txt's minimal pinned set. Checked
    # against actual PACKAGE lines (not comments -- this file's own
    # comments legitimately explain what it's NOT for, by name).
    bundled = (built_bundle.bundle_dir / "requirements.txt").read_text(encoding="utf-8")
    package_lines = [line.lower() for line in bundled.splitlines() if line.strip() and not line.strip().startswith("#")]
    for line in package_lines:
        for forbidden in ("streamlit", "fastapi", "psycopg2", "sqlalchemy"):
            assert forbidden not in line, f"{forbidden!r} found in a requirement line: {line!r}"


def test_bundle_never_includes_git_metadata(built_bundle):
    for path in built_bundle.bundle_dir.rglob("*"):
        assert path.name != ".git"
        assert ".github" not in path.parts


def test_bundle_never_includes_pycache(built_bundle):
    for path in built_bundle.bundle_dir.rglob("__pycache__"):
        pytest.fail(f"__pycache__ present in bundle: {path}")


def test_bundle_config_template_has_no_production_credentials(built_bundle):
    text = (built_bundle.bundle_dir / "collector_config.example.json").read_text(encoding="utf-8")
    doc = json.loads(text)
    # Template values only -- 0, not a real customer_id/branch_id.
    assert doc["customer_id"] == 0
    assert doc["branch_id"] == 0
    # No real token value under any non-comment key -- the env var NAME is
    # expected to appear in the explanatory comment (that's the whole
    # point of _comment_token), but no actual token belongs anywhere here.
    assert "token" not in json.dumps({k: v for k, v in doc.items() if not k.startswith("_comment")}).lower()


def test_bundle_config_template_comment_points_at_bundle_paths_not_repo_paths(built_bundle):
    text = (built_bundle.bundle_dir / "collector_config.example.json").read_text(encoding="utf-8")
    assert "collector/deploy/install-collector.ps1" not in text
    assert "tools\\\\set-api-token.ps1" in text or "tools\\set-api-token.ps1" in text or "install.ps1" in text


# --- MANIFEST.json ---------------------------------------------------------


def test_manifest_lists_every_bundled_file_with_checksums(built_bundle):
    manifest = json.loads(built_bundle.manifest_path.read_text(encoding="utf-8"))
    assert manifest["product"] == "SortView Collector"
    assert manifest["version"] == "9.9.9-test"
    manifest_paths = {entry["path"] for entry in manifest["files"]}

    on_disk = {
        p.relative_to(built_bundle.bundle_dir).as_posix()
        for p in built_bundle.bundle_dir.rglob("*")
        if p.is_file() and p.name != "MANIFEST.json"
    }
    assert manifest_paths == on_disk

    for entry in manifest["files"]:
        actual_bytes = (built_bundle.bundle_dir / entry["path"]).read_bytes()
        import hashlib

        assert entry["sha256"] == hashlib.sha256(actual_bytes).hexdigest()
        assert entry["size_bytes"] == len(actual_bytes)


def test_build_is_deterministic_given_fixed_built_at(tmp_path):
    out1, out2 = tmp_path / "a", tmp_path / "b"
    r1 = build_release.build_release(REPO_ROOT, out1, "1.2.3", built_at="2026-01-01T00:00:00.000000Z")
    r2 = build_release.build_release(REPO_ROOT, out2, "1.2.3", built_at="2026-01-01T00:00:00.000000Z")
    assert r1.manifest_path.read_text(encoding="utf-8") == r2.manifest_path.read_text(encoding="utf-8")


# --- isolated deployed-runtime import smoke test (proves no repo checkout needed) --


def test_bundled_runtime_never_exposes_build_time_only_tools(built_bundle):
    """The bundle never contains collector/deploy_manifest.py or
    collector/build_release.py themselves (build-time-only tools) -- from
    inside a subprocess whose sys.path is only the bundle's own directory
    (never this repo checkout), importing either must fail."""
    smoke_script = """
import collector.deploy_manifest
"""
    result = subprocess.run(
        [sys.executable, "-c", smoke_script],
        cwd=str(built_bundle.bundle_dir),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "No module named 'collector.deploy_manifest'" in result.stderr


def test_bundled_runtime_full_import_and_parse_succeeds_in_isolation(built_bundle):
    smoke_script = f"""
import sys
assert sys.path[0] != r"{REPO_ROOT}", "repo root leaked onto sys.path"
import collector.parsers as parsers
fns = parsers.build_production_parse_fns(customer_id=1, branch_id=1)
line = "Sunny days /|33472004192508|MLEPB|E KERBEL NATURE|000|1|False||4|N|N|N|8/31/2026|4:18:39 PM"
records = fns["checkins"]([line])
assert records[0]["barcode"] == "33472004192508"
print("BUNDLE_ISOLATED_IMPORT_OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", smoke_script],
        cwd=str(built_bundle.bundle_dir),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "BUNDLE_ISOLATED_IMPORT_OK" in result.stdout


# --- CLI --------------------------------------------------------------------


def test_cli_builds_and_prints_summary(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "collector.build_release", "--output", str(tmp_path), "--version", "2.0.0"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    assert "Built release bundle" in result.stdout
    assert (tmp_path / "SortViewCollector-2.0.0" / "install.ps1").is_file()


def test_cli_fails_loudly_on_missing_version(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "collector.build_release", "--output", str(tmp_path), "--version", ""],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0


# --- static checks on the .ps1 scripts (CI cannot execute PowerShell) -------


def _read_ps1(*parts: str) -> str:
    return (REPO_ROOT / "collector" / "deploy" / Path(*parts)).read_text(encoding="utf-8")


def _executable_body(text: str) -> str:
    """Strips the leading <# ... #> comment-based-help block, so a text
    search for an actual code construct (a cmdlet call, a variable used
    in logic) isn't confused by the SAME words appearing in prose inside
    the docstring above it."""
    end_marker = "#>"
    idx = text.find(end_marker)
    return text[idx + len(end_marker):] if idx != -1 else text


def _code_only(text: str) -> str:
    """Drops every line whose trimmed content starts with '#' -- for
    asserting a string appears ONLY in explanatory inline comments
    (legitimate, e.g. documenting what an OLD/replaced approach assumed)
    and never in an actual executed command."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def test_install_release_does_not_require_repo_root():
    text = _read_ps1("install-release.ps1")
    assert "$RepoRoot" not in text
    # The docstring legitimately mentions the OTHER script's mechanism by
    # name for context/comparison -- what must be absent is an actual
    # invocation of it.
    assert "& $PythonExe -m collector.deploy_manifest" not in text
    assert "$PSScriptRoot" in text


def test_install_release_resolves_payload_from_bundle_directory():
    text = _read_ps1("install-release.ps1")
    assert '$BundleRoot = $PSScriptRoot' in text
    assert 'Join-Path $BundleRoot "collector"' in text
    assert 'Join-Path $BundleRoot "agent"' in text


def test_update_release_is_standalone():
    text = _read_ps1("update-release.ps1")
    assert "$RepoRoot" not in text
    assert "collector.deploy_manifest" not in text
    assert 'Split-Path $PSScriptRoot -Parent' in text


def test_update_release_disables_task_before_any_mutation():
    text = _read_ps1("update-release.ps1")
    disable_idx = text.index("Disable-ScheduledTask")
    backup_idx = text.index('New-Item -ItemType Directory -Path (Join-Path $BackupRoot "collector")')
    code_replace_idx = text.index('Copy-Item (Join-Path $SourceCollectorDir "*.py")')
    assert disable_idx < backup_idx < code_replace_idx


def test_update_release_only_stops_running_after_disable_check():
    text = _read_ps1("update-release.ps1")
    # The fixed bug: it must not ONLY check .State -eq "Running" without
    # first having already disabled the task -- both must be present, in
    # that order.
    assert text.index("Disable-ScheduledTask") < text.index('$currentState -eq "Running"')


def test_update_release_restores_state_only_after_successful_preflight():
    body = _executable_body(_read_ps1("update-release.ps1"))
    preflight_idx = body.index("collector.preflight --config")
    # The LAST occurrence is the real success-path restore call; an
    # earlier one is just the rollback function's printed instruction
    # string (Write-Host "... Enable-ScheduledTask ...") for the operator
    # to run manually, not an actual invocation.
    enable_idx = body.rindex("Enable-ScheduledTask")
    assert preflight_idx < enable_idx


def test_update_release_never_unconditionally_starts_the_task():
    body = _executable_body(_read_ps1("update-release.ps1"))
    # Start-ScheduledTask must only appear inside the explicit -StartNow
    # opt-in branch, never called unconditionally after a successful
    # update (the original bug this whole fix addresses: an implicit
    # forced immediate run).
    assert "if ($StartNow)" in body
    start_idx = body.index("Start-ScheduledTask")
    startnow_idx = body.index("if ($StartNow)")
    assert startnow_idx < start_idx


def test_update_release_failure_path_leaves_task_disabled():
    body = _executable_body(_read_ps1("update-release.ps1"))
    assert "function Restore-DisabledTaskAndFail" in body
    function_body = body[body.index("function Restore-DisabledTaskAndFail"):]
    assert "DISABLED" in function_body
    assert "Enable-ScheduledTask" in function_body
    preflight_failure_branch = body[body.index('$preflightExitCode -ne 0'):]
    assert "Restore-DisabledTaskAndFail" in preflight_failure_branch


def test_update_collector_repo_checkout_script_has_the_same_task_safety_fix():
    # The repo-checkout equivalent (developer/QA flow) got the identical
    # fix, not just the new standalone script -- see this module's own
    # test_update_release_* siblings for the same properties proven there.
    body = _executable_body(_read_ps1("update-collector.ps1"))
    assert "Disable-ScheduledTask" in body
    assert "$wasEnabled" in body
    disable_idx = body.index("Disable-ScheduledTask")
    backup_idx = body.index('New-Item -ItemType Directory -Path (Join-Path $BackupRoot "collector")')
    assert disable_idx < backup_idx
    preflight_idx = body.index("collector.preflight --config")
    enable_idx = body.index("Enable-ScheduledTask", preflight_idx)
    assert preflight_idx < enable_idx
    assert "if ($StartNow)" in body


def test_token_script_never_prints_plaintext_token():
    for name in ("set-collector-api-token.ps1",):
        text = _read_ps1(name)
        assert "$plainToken" in text  # the variable exists...
        # ...but is never passed to Write-Host, only its length/hash prefix.
        for line in text.splitlines():
            if "Write-Host" in line:
                assert "$plainToken" not in line


def test_token_script_never_writes_token_to_a_file():
    text = _read_ps1("set-collector-api-token.ps1")
    assert "Set-Content" not in text
    assert "WriteAllText" not in text
    assert 'SetEnvironmentVariable($VariableName, $plainToken, "Machine")' in text


def test_task_registration_default_remains_disabled():
    text = _read_ps1("register-collector-task.ps1")
    assert "[switch]$Enabled" in text
    assert "DEFAULT here is DISABLED" in text or "safe default" in text.lower()


def test_register_and_preflight_and_uninstall_scripts_needed_no_release_fork():
    # Confirms the investigation finding stands: these three scripts
    # already resolved everything from -InstallRoot/-ConfigPath with zero
    # repo-root assumption, so build_release.py copies them verbatim
    # (DEPLOY_TOOL_FILES) rather than shipping a separate release variant.
    for name in ("register-collector-task.ps1", "run-preflight-as-system.ps1", "uninstall-collector.ps1"):
        text = _read_ps1(name)
        assert "$RepoRoot" not in text
        assert "collector.deploy_manifest" not in text


# --- post-review cleanup fixes -------------------------------------------
#
# FIX 1: install-release.ps1's fresh-install guidance wrongly implied
# offset 0 (a valid seed for an empty/new source file, proven during
# scratch validation) was invalid. FIX 2: update-release.ps1's fast-path
# and rollback guidance used to OVERLAY files instead of exactly replacing
# a runtime directory, which could leave a stale (removed/renamed-in-the-
# new-release) module behind. See collector/deploy/install-release.ps1 and
# update-release.ps1 for the fixed text these tests check.


def test_bootstrap_guidance_does_not_call_offset_zero_invalid():
    text = _read_ps1("install-release.ps1")
    assert "non-zero offset" not in text
    assert "offset 0 is a VALID seed" in text


def test_bootstrap_guidance_requires_success_and_every_source_seeded():
    text = _read_ps1("install-release.ps1")
    assert "bootstrap completed" in text
    assert "successfully" in text
    assert "EVERY configured source" in text
    assert "safe current cursor" in text


def test_fast_path_replaces_collector_directory_deterministically_not_overlay():
    body = _executable_body(_read_ps1("update-release.ps1"))
    fast_path = body[body.index("requirements.txt unchanged"):body.index("requirements.txt changed")]
    # A stale collector module (removed/renamed in the new release) can
    # only be guaranteed gone if the directory is actually removed before
    # the fresh copy -- not just overlaid on top of what was already there.
    remove_idx = fast_path.index('Remove-Item -Recurse -Force (Join-Path $InstallRoot "collector")')
    copy_idx = fast_path.index('Copy-Item (Join-Path $SourceCollectorDir "*.py")')
    assert remove_idx < copy_idx


def test_fast_path_replaces_agent_directory_deterministically_not_overlay():
    body = _executable_body(_read_ps1("update-release.ps1"))
    fast_path = body[body.index("requirements.txt unchanged"):body.index("requirements.txt changed")]
    # Same property for the canonical parser runtime slice -- a stale
    # agent/parser/*.py file removed in a future release must not survive
    # a fast-path (unchanged-requirements) update.
    remove_idx = fast_path.index('Remove-Item -Recurse -Force (Join-Path $InstallRoot "agent")')
    copy_idx = fast_path.index('Copy-Item (Join-Path $SourceAgentDir "*")')
    assert remove_idx < copy_idx


def test_fast_path_never_touches_venv_or_datapaths():
    body = _executable_body(_read_ps1("update-release.ps1"))
    fast_path = body[body.index("requirements.txt unchanged"):body.index("requirements.txt changed")]
    # The comment explaining .venv/-DataRoot are untouched is expected and
    # fine -- what must be absent is any actual mutating call referencing
    # them (Remove-Item/Copy-Item/Move-Item/New-Item targeting .venv, or
    # any use of $ConfigPath/-DataRoot at all).
    mutating_lines = [
        line for line in fast_path.splitlines()
        if any(cmd in line for cmd in ("Remove-Item", "Copy-Item", "Move-Item", "New-Item"))
    ]
    for line in mutating_lines:
        assert ".venv" not in line, line
        assert "ConfigPath" not in line, line
        assert "DataRoot" not in line, line


def test_rollback_guidance_removes_before_copying_for_both_trees():
    body = _executable_body(_read_ps1("update-release.ps1"))
    rollback_section = body[body.index("UPDATE FAILED VERIFICATION"):body.index("Restore-DisabledTaskAndFail", body.index("UPDATE FAILED VERIFICATION"))]
    # The code-only (not full-rebuild) rollback branch specifically --
    # exact restoration from the backup, not an overlay that could leave a
    # new-only file (introduced by the failed update) behind.
    code_only_branch = rollback_section[rollback_section.index("} else {"):]
    collector_remove_idx = code_only_branch.index('Remove-Item -Recurse -Force `"$InstallRoot\\collector`"')
    collector_copy_idx = code_only_branch.index('Copy-Item `"$BackupRoot\\collector`"')
    assert collector_remove_idx < collector_copy_idx

    agent_remove_idx = code_only_branch.index('Remove-Item -Recurse -Force `"$InstallRoot\\agent`"')
    agent_copy_idx = code_only_branch.index('Copy-Item `"$BackupRoot\\agent`"')
    assert agent_remove_idx < agent_copy_idx


def test_rollback_guidance_no_longer_uses_bare_overlay_copy():
    body = _executable_body(_read_ps1("update-release.ps1"))
    rollback_section = body[body.index("UPDATE FAILED VERIFICATION"):body.index("Restore-DisabledTaskAndFail", body.index("UPDATE FAILED VERIFICATION"))]
    code_only_branch = rollback_section[rollback_section.index("} else {"):]
    # The old overlay-only forms (bare -Force / \agent\* -Recurse -Force
    # with no preceding Remove-Item) must be gone from the printed guidance.
    assert 'collector\\*.py`" `"$InstallRoot\\collector`" -Force"' not in code_only_branch
    assert 'agent\\*`" `"$InstallRoot\\agent`" -Recurse -Force' not in code_only_branch


def test_task_disabled_before_any_fast_path_directory_mutation():
    body = _executable_body(_read_ps1("update-release.ps1"))
    disable_idx = body.index("Disable-ScheduledTask")
    backup_idx = body.index('New-Item -ItemType Directory -Path (Join-Path $BackupRoot "collector")')
    fast_path_remove_idx = body.index('Remove-Item -Recurse -Force (Join-Path $InstallRoot "collector")')
    fast_path_agent_remove_idx = body.index('Remove-Item -Recurse -Force (Join-Path $InstallRoot "agent")')
    assert disable_idx < backup_idx < fast_path_remove_idx
    assert disable_idx < fast_path_agent_remove_idx


def test_full_rebuild_path_still_replaces_wholesale_unaffected_by_fast_path_fix():
    # The requirements-changed branch already moved the ENTIRE InstallRoot
    # aside and rebuilt from scratch -- already exact, untouched by this
    # fix, and must remain so (regression guard, not new behavior).
    body = _executable_body(_read_ps1("update-release.ps1"))
    full_rebuild = body[body.index("requirements.txt changed"):body.index("=== 4. Verify the new runtime")]
    assert 'Move-Item -Path $InstallRoot -Destination "$BackupRoot-full" -Force' in full_rebuild


def test_config_state_logs_still_never_touched_by_update_release():
    text = _read_ps1("update-release.ps1")
    assert "Never touches -DataRoot" in text


# =========================================================================
# FROZEN release bundle (collector-frozen-release-integration phase)
# =========================================================================
#
# build_frozen_release requires an ALREADY-BUILT PyInstaller onedir output
# (never builds PyInstaller itself -- that's collector/freeze/build_frozen.ps1's
# job, ~25s and its own isolated venv, deliberately kept separate -- see
# build_frozen_release's own docstring). These tests fake a minimal
# onedir-SHAPED directory (SortViewCollector.exe + non-empty _internal\)
# rather than running a real PyInstaller build, which would make this
# suite slow and CI-fragile for no additional coverage -- build_release.py
# only cares about the directory's SHAPE, never its content. Real,
# executable proof against an ACTUAL PyInstaller build happened live on
# LIB-L26 -- see that phase's own report, not this file.


@pytest.fixture
def fake_frozen_runtime_dir(tmp_path):
    runtime_dir = tmp_path / "fake_dist" / "SortViewCollector"
    (runtime_dir / "_internal").mkdir(parents=True)
    (runtime_dir / "SortViewCollector.exe").write_bytes(b"fake-pe-bytes-not-a-real-exe")
    (runtime_dir / "_internal" / "python314.dll").write_bytes(b"fake-dll-bytes")
    return runtime_dir


@pytest.fixture
def built_frozen_bundle(fake_frozen_runtime_dir, tmp_path):
    output_dir = tmp_path / "out"
    return build_release.build_frozen_release(
        REPO_ROOT, output_dir, "9.9.9-frozen-test", fake_frozen_runtime_dir, built_at="2026-01-01T00:00:00.000000Z"
    )


# --- _validate_frozen_runtime_dir / malformed runtime rejected ----------


def test_frozen_release_rejects_nonexistent_runtime_dir(tmp_path):
    with pytest.raises(build_release.FrozenRuntimeError):
        build_release.build_frozen_release(
            REPO_ROOT, tmp_path / "out", "1.0.0", tmp_path / "does-not-exist", built_at="x"
        )


def test_frozen_release_rejects_empty_runtime_dir(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(build_release.FrozenRuntimeError):
        build_release.build_frozen_release(REPO_ROOT, tmp_path / "out", "1.0.0", empty_dir, built_at="x")


def test_frozen_release_rejects_missing_exe(tmp_path):
    runtime_dir = tmp_path / "no_exe"
    (runtime_dir / "_internal").mkdir(parents=True)
    (runtime_dir / "_internal" / "something.dll").write_bytes(b"x")
    with pytest.raises(build_release.FrozenRuntimeError, match="SortViewCollector.exe"):
        build_release.build_frozen_release(REPO_ROOT, tmp_path / "out", "1.0.0", runtime_dir, built_at="x")


def test_frozen_release_rejects_missing_internal_dir(tmp_path):
    runtime_dir = tmp_path / "no_internal"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "SortViewCollector.exe").write_bytes(b"x")
    with pytest.raises(build_release.FrozenRuntimeError, match="_internal"):
        build_release.build_frozen_release(REPO_ROOT, tmp_path / "out", "1.0.0", runtime_dir, built_at="x")


def test_frozen_release_writes_nothing_on_invalid_runtime(tmp_path):
    output_dir = tmp_path / "out"
    with pytest.raises(build_release.FrozenRuntimeError):
        build_release.build_frozen_release(REPO_ROOT, output_dir, "1.0.0", tmp_path / "nope", built_at="x")
    assert not output_dir.exists()


def test_frozen_release_is_a_buildeerror_subclass():
    # FrozenRuntimeError must still be catchable by existing BuildError
    # handlers (e.g. build_release.py's own main()) without a separate
    # except clause.
    assert issubclass(build_release.FrozenRuntimeError, build_release.BuildError)


# --- frozen bundle contents: exact layout, deterministic copy -----------


def test_frozen_bundle_matches_target_layout(built_frozen_bundle):
    root = built_frozen_bundle.bundle_dir
    assert (root / "install.ps1").is_file()
    assert (root / "MANIFEST.json").is_file()
    assert (root / "collector_config.example.json").is_file()
    assert (root / "runtime" / "SortViewCollector.exe").is_file()
    assert (root / "runtime" / "_internal" / "python314.dll").is_file()
    for tool in ("register-task.ps1", "preflight-system.ps1", "update.ps1", "uninstall.ps1", "set-api-token.ps1"):
        assert (root / "tools" / tool).is_file(), tool


def test_frozen_bundle_never_includes_python_source_tree(built_frozen_bundle):
    root = built_frozen_bundle.bundle_dir
    assert not (root / "collector").exists()
    assert not (root / "agent").exists()


def test_frozen_bundle_never_includes_requirements_txt(built_frozen_bundle):
    assert not (built_frozen_bundle.bundle_dir / "requirements.txt").exists()


def test_frozen_bundle_never_includes_build_or_dist_or_pyinstaller_venv(built_frozen_bundle):
    root = built_frozen_bundle.bundle_dir
    for path in root.rglob("*"):
        assert path.name not in ("build", "dist", ".pyinstaller-venv")


def test_frozen_bundle_never_includes_tests_backend_or_continuous_agent(built_frozen_bundle):
    root = built_frozen_bundle.bundle_dir
    for path in root.rglob("*"):
        assert "tests" not in path.parts
        assert "SortViewAgent" not in path.parts
        assert path.name not in ("agent.runtime", "runtime.py")  # sanity: not literally shipping agent/runtime files


def test_frozen_bundle_runtime_copy_is_exact_not_partial(built_frozen_bundle, fake_frozen_runtime_dir):
    installed_files = {
        p.relative_to(built_frozen_bundle.bundle_dir / "runtime").as_posix()
        for p in (built_frozen_bundle.bundle_dir / "runtime").rglob("*")
        if p.is_file()
    }
    source_files = {
        p.relative_to(fake_frozen_runtime_dir).as_posix()
        for p in fake_frozen_runtime_dir.rglob("*")
        if p.is_file()
    }
    assert installed_files == source_files


def test_frozen_bundle_config_template_no_secrets_or_prefilled_values(built_frozen_bundle):
    text = (built_frozen_bundle.bundle_dir / "collector_config.example.json").read_text(encoding="utf-8")
    doc = json.loads(text)
    assert doc["customer_id"] == 0
    assert doc["branch_id"] == 0
    assert "token" not in json.dumps({k: v for k, v in doc.items() if not k.startswith("_comment")}).lower()


def test_frozen_bundle_never_ships_a_token_file(built_frozen_bundle):
    for path in built_frozen_bundle.bundle_dir.rglob("*"):
        assert "token" not in path.name.lower() or path.name == "set-api-token.ps1"


# --- MANIFEST.json covers the frozen runtime too -------------------------


def test_frozen_manifest_includes_runtime_files_with_correct_hashes(built_frozen_bundle):
    manifest = json.loads(built_frozen_bundle.manifest_path.read_text(encoding="utf-8"))
    manifest_paths = {entry["path"] for entry in manifest["files"]}
    assert "runtime/SortViewCollector.exe" in manifest_paths
    assert "runtime/_internal/python314.dll" in manifest_paths

    import hashlib

    for entry in manifest["files"]:
        actual = (built_frozen_bundle.bundle_dir / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(actual).hexdigest()
        assert entry["size_bytes"] == len(actual)


def test_frozen_manifest_matches_every_file_on_disk_exactly(built_frozen_bundle):
    manifest = json.loads(built_frozen_bundle.manifest_path.read_text(encoding="utf-8"))
    manifest_paths = {entry["path"] for entry in manifest["files"]}
    on_disk = {
        p.relative_to(built_frozen_bundle.bundle_dir).as_posix()
        for p in built_frozen_bundle.bundle_dir.rglob("*")
        if p.is_file() and p.name != "MANIFEST.json"
    }
    assert manifest_paths == on_disk


# --- CLI: --frozen-runtime ------------------------------------------------


def test_cli_frozen_runtime_flag_builds_frozen_bundle(fake_frozen_runtime_dir, tmp_path):
    result = subprocess.run(
        [
            sys.executable, "-m", "collector.build_release",
            "--output", str(tmp_path / "out"), "--version", "3.0.0",
            "--frozen-runtime", str(fake_frozen_runtime_dir),
        ],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    assert "Built release bundle" in result.stdout
    bundle = tmp_path / "out" / "SortViewCollector-3.0.0"
    assert (bundle / "runtime" / "SortViewCollector.exe").is_file()
    assert not (bundle / "collector").exists()


def test_cli_without_frozen_runtime_still_builds_source_bundle(tmp_path):
    # Regression guard: the default (no --frozen-runtime) CLI path must
    # remain completely unchanged -- source-mode is still supported.
    result = subprocess.run(
        [sys.executable, "-m", "collector.build_release", "--output", str(tmp_path), "--version", "4.0.0"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    assert "Built release bundle" in result.stdout
    bundle = tmp_path / "SortViewCollector-4.0.0"
    assert (bundle / "collector" / "run.py").is_file()
    assert not (bundle / "runtime").exists()


# --- source-mode build_release remains fully intact (regression guard) --


def test_source_release_still_works_unaffected_by_frozen_addition(built_bundle):
    # built_bundle is the pre-existing source-mode fixture (see above) --
    # this just re-asserts its two most structurally significant
    # properties still hold after the frozen-mode refactor touched shared
    # helpers (_copy_required_files, _write_manifest, _new_bundle_dir).
    assert (built_bundle.bundle_dir / "collector" / "run.py").is_file()
    assert (built_bundle.bundle_dir / "requirements.txt").is_file()
    assert not (built_bundle.bundle_dir / "runtime").exists()


# --- static checks on install.ps1's frozen branch (CI cannot run PowerShell) --


def test_install_release_detects_bundle_kind_from_contents():
    text = _read_ps1("install-release.ps1")
    assert "$isSourceBundle" in text
    assert "$isFrozenBundle" in text
    assert "ambiguous" in text.lower()


def test_install_release_frozen_path_skips_venv_and_pip():
    body = _executable_body(_read_ps1("install-release.ps1"))
    # The frozen branch of "=== 1. Application runtime ===" must never
    # invoke venv/pip -- find that branch specifically (between the
    # isFrozenBundle if and its matching else).
    frozen_section = body[body.index("=== 1. Application runtime ===") : body.index("=== 2. Data directories ===")]
    assert "python -m venv" not in frozen_section
    assert "pip install" not in frozen_section
    assert "SKIPPED (frozen bundle" in body


def test_install_release_verifies_manifest_before_any_mutation():
    body = _executable_body(_read_ps1("install-release.ps1"))
    manifest_call_idx = body.index("Test-ReleaseManifest -BundleRoot")
    first_mutation_idx = body.index('=== 1. Application runtime ===')
    assert manifest_call_idx < first_mutation_idx


def test_install_release_runs_frozen_preflight_and_bootstrap_via_exe_subcommands():
    text = _read_ps1("install-release.ps1")
    assert 'RunnerExe`" preflight --config' in text
    assert 'RunnerExe`" bootstrap --config' in text


# --- static checks on update.ps1's frozen branch --------------------------


def test_update_release_detects_bundle_and_install_kind():
    text = _read_ps1("update-release.ps1")
    assert "$bundleIsSource" in text
    assert "$bundleIsFrozen" in text
    assert "$installIsSource" in text
    assert "$installIsFrozen" in text


def test_update_release_refuses_mismatched_bundle_and_install_kind():
    text = _read_ps1("update-release.ps1")
    assert "Refusing to update across kinds" in text
    # Both directions must be covered, not just one.
    assert text.count("Refusing to update across kinds") >= 2


def test_update_release_frozen_replacement_is_remove_then_copy():
    body = _executable_body(_read_ps1("update-release.ps1"))
    # Ends at "=== 3. Dependency check ===" (the SOURCE branch's own step
    # 3 header) -- NOT "=== 4. ..." which comes after both branches and
    # would wrongly include the source branch's pip/venv logic too.
    frozen_section = body[body.index("Replace frozen runtime"):body.index("=== 3. Dependency check ===")]
    # Whole-InstallRoot replacement (not exe+_internal-specific) -- see
    # test_update_release_frozen_replaces_whole_installroot below for the
    # dedicated coverage of that property; this test keeps the original
    # remove-before-copy ordering check.
    remove_idx = frozen_section.index("Remove-Item -Recurse -Force $InstallRoot")
    copy_idx = frozen_section.index('Copy-Item (Join-Path $FrozenRuntimeDir "*")')
    assert remove_idx < copy_idx
    assert "pip install" not in frozen_section
    assert "python -m venv" not in frozen_section
    assert ".deps-hash" not in frozen_section


def test_update_release_frozen_replaces_whole_installroot_not_just_exe_and_internal():
    # Regression guard for the exact fragility flagged in review: the
    # frozen contract must not assume the runtime is only
    # SortViewCollector.exe + _internal\ -- it must treat -InstallRoot as
    # the whole replaceable unit, so a future build adding any other
    # top-level file/folder is still covered without this script changing.
    body = _executable_body(_read_ps1("update-release.ps1"))
    frozen_section = body[body.index("Replace frozen runtime"):body.index("=== 3. Dependency check ===")]
    assert "Remove-Item -Recurse -Force $InstallRoot" in frozen_section
    code_only = _code_only(frozen_section)
    assert "SortViewCollector.exe" not in code_only
    assert "_internal" not in code_only


def test_update_release_frozen_preflight_uses_exe_subcommand():
    body = _executable_body(_read_ps1("update-release.ps1"))
    assert "& $InstalledFrozenExe preflight --config $ConfigPath" in body


def test_update_release_frozen_rollback_is_remove_then_copy():
    body = _executable_body(_read_ps1("update-release.ps1"))
    rollback_section = body[body.index("UPDATE FAILED VERIFICATION"):body.index("Restore-DisabledTaskAndFail", body.index("UPDATE FAILED VERIFICATION"))]
    frozen_rollback = rollback_section[rollback_section.index("elseif ($isFrozen)"):]
    # Whole-InstallRoot rollback (not exe+_internal-specific) -- $BackupRoot
    # is a full copy of the prior InstallRoot (see the backup step), so
    # restoring it in full is what covers ANY file the failed update added.
    remove_idx = frozen_rollback.index('Remove-Item -Recurse -Force `"$InstallRoot`"')
    copy_idx = frozen_rollback.index('Copy-Item `"$BackupRoot`" `"$InstallRoot`" -Recurse -Force')
    assert remove_idx < copy_idx
    code_only = _code_only(frozen_rollback)
    assert "SortViewCollector.exe" not in code_only
    assert "_internal" not in code_only


def test_update_release_manifest_verified_before_task_disable():
    body = _executable_body(_read_ps1("update-release.ps1"))
    manifest_idx = body.index("Test-ReleaseManifest -BundleRoot")
    disable_section_idx = body.index("Snapshot and disable the task")
    assert manifest_idx < disable_section_idx


def test_update_release_task_disabled_before_frozen_mutation_too():
    body = _executable_body(_read_ps1("update-release.ps1"))
    disable_idx = body.index("Disable-ScheduledTask")
    backup_idx = body.index("Back up current runtime files")
    frozen_replace_idx = body.index("Replace frozen runtime")
    assert disable_idx < backup_idx < frozen_replace_idx


def test_update_release_frozen_backup_happens_before_replacement():
    body = _executable_body(_read_ps1("update-release.ps1"))
    backup_idx = body.index('Copy-Item $InstallRoot -Destination $BackupRoot -Recurse -Force')
    replace_idx = body.index('Remove-Item -Recurse -Force $InstallRoot')
    assert backup_idx < replace_idx


# --- static checks on register-task.ps1's frozen branch -------------------


def test_register_task_detects_frozen_install():
    text = _read_ps1("register-collector-task.ps1")
    assert "$IsFrozen" in text
    assert "SortViewCollector.exe" in text


def test_register_task_frozen_uses_task_xml_subcommand_not_python_module():
    body = _executable_body(_read_ps1("register-collector-task.ps1"))
    assert '@("task-xml")' in body
    assert '"--frozen"' in body


def test_register_task_source_path_still_uses_python_module():
    body = _executable_body(_read_ps1("register-collector-task.ps1"))
    assert '@("-m", "collector.task_settings")' in body


# --- static checks on preflight-system.ps1's frozen branch -----------------


def test_preflight_system_detects_frozen_install():
    text = _read_ps1("run-preflight-as-system.ps1")
    assert "$IsFrozen" in text


def test_preflight_system_frozen_arguments_omit_python_module_flag():
    body = _executable_body(_read_ps1("run-preflight-as-system.ps1"))
    frozen_args_section = body[body.index('$PreflightArgs = if ($IsFrozen)'):body.index("$escapedCommand")]
    assert '"preflight --config' in frozen_args_section
    assert "-m collector.preflight" not in frozen_args_section.split("} else {")[0]


# --- uninstall / set-api-token: verified unchanged-logic, mode-agnostic ---


def test_uninstall_removes_installroot_unconditionally_regardless_of_kind():
    text = _read_ps1("uninstall-collector.ps1")
    assert "Remove-Item -Recurse -Force $InstallRoot" in text


def test_set_api_token_has_no_installed_runtime_awareness():
    # This script must remain completely mode-agnostic -- it only ever
    # touches the Machine-scope env var, never inspects -InstallRoot at all.
    text = _read_ps1("set-collector-api-token.ps1")
    assert "InstallRoot" not in text
    assert "SortViewCollector.exe" not in text


# =========================================================================
# Post-review hardening (manifest completeness, -Force determinism,
# ambiguous install, whole-runtime frozen update/rollback)
# =========================================================================
#
# The manifest function's new logic (unsafe-path/duplicate/extra-file
# detection) and the -Force/whole-InstallRoot behavior are genuine NEW
# runtime behavior, not just wiring -- static text checks below lock in
# the STRUCTURE (the right checks exist, in the right order), but the
# actual PowerShell execution proof (tamper/traversal/duplicate/extra-file
# rejection, -Force determinism, ambiguous-install refusal, whole-runtime
# update/rollback) was performed live on LIB-L26 -- see that phase's own
# report, not this file, for the executed evidence (this project's CI
# cannot run PowerShell, same reasoning as every other .ps1 static check
# in this file).


def _manifest_function_body(script_name: str) -> str:
    text = _read_ps1(script_name)
    start = text.index("function Test-ReleaseManifest")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


# --- manifest: unsafe paths / duplicates / extra files (structure checks) --


def test_manifest_function_rejects_traversal_and_rooted_paths():
    for script in ("install-release.ps1", "update-release.ps1"):
        body = _manifest_function_body(script)
        assert "UNSAFE MANIFEST PATH" in body
        assert r"\.\." in body  # the traversal-segment regex pattern
        assert "^[\\\\/]" in body or "^[\\/]" in body  # rooted-path pattern


def test_manifest_function_rejects_duplicate_paths():
    for script in ("install-release.ps1", "update-release.ps1"):
        body = _manifest_function_body(script)
        assert "DUPLICATE MANIFEST PATH" in body
        assert "manifestKeys.ContainsKey" in body


def test_manifest_function_rejects_unlisted_extra_files():
    for script in ("install-release.ps1", "update-release.ps1"):
        body = _manifest_function_body(script)
        assert "UNEXPECTED FILE" in body
        assert "Get-ChildItem -Path $BundleRoot -Recurse -File" in body


def test_manifest_function_excludes_manifest_json_itself_from_extra_file_check():
    for script in ("install-release.ps1", "update-release.ps1"):
        body = _manifest_function_body(script)
        assert 'if ($relative -eq "MANIFEST.json") { continue }' in body


def test_manifest_function_still_checks_missing_and_hash_mismatch():
    # Regression guard: the new checks must be ADDITIVE, not a replacement
    # for the original missing-file/hash-mismatch checks.
    for script in ("install-release.ps1", "update-release.ps1"):
        body = _manifest_function_body(script)
        assert "MISSING:" in body
        assert "HASH MISMATCH:" in body


def test_manifest_wording_does_not_claim_cryptographic_authenticity():
    # Human-review correction: SHA-256-against-an-unsigned-manifest is an
    # INTEGRITY check, not an authenticated signature. Neither script may
    # claim otherwise, and both must say so explicitly.
    for script in ("install-release.ps1", "update-release.ps1"):
        text = _read_ps1(script)
        assert "tampered with" not in text.lower() or "not an authenticated signature" in text.lower()
        assert "not an authenticated signature" in text.lower() or "not signed" in text.lower()
        assert "code signing" in text.lower()


def test_manifest_verification_runs_before_any_installroot_mutation_both_scripts():
    for script in ("install-release.ps1", "update-release.ps1"):
        body = _executable_body(_read_ps1(script))
        manifest_idx = body.index("Test-ReleaseManifest -BundleRoot")
        # The first real mutation in either script is either the -Force
        # InstallRoot removal (install.ps1) or the task-disable section
        # (update.ps1) -- whichever marker exists, it must come after.
        if "Removing existing InstallRoot before forced reinstall" in body:
            mutation_idx = body.index("Removing existing InstallRoot before forced reinstall")
        else:
            mutation_idx = body.index("Snapshot and disable the task")
        assert manifest_idx < mutation_idx


# --- install.ps1: -Force determinism -------------------------------------


def test_install_release_force_removes_installroot_before_any_copy():
    body = _executable_body(_read_ps1("install-release.ps1"))
    force_remove_idx = body.index("Removing existing InstallRoot before forced reinstall")
    first_copy_idx = body.index('=== 1. Application runtime ===')
    assert force_remove_idx < first_copy_idx


def test_install_release_force_removal_gated_strictly_on_force_switch():
    body = _executable_body(_read_ps1("install-release.ps1"))
    assert "if ($Force -and (Test-Path $InstallRoot))" in body


def test_install_release_force_removal_never_touches_dataroot():
    body = _executable_body(_read_ps1("install-release.ps1"))
    force_section_start = body.index("if ($Force -and (Test-Path $InstallRoot))")
    force_section = body[force_section_start:body.index('=== 1. Application runtime ===')]
    assert "DataRoot" not in _code_only(force_section)


# --- update.ps1: ambiguous installed runtime ------------------------------


def test_update_release_rejects_ambiguous_installed_runtime():
    body = _executable_body(_read_ps1("update-release.ps1"))
    assert "if ($installIsSource -and $installIsFrozen)" in body
    assert "ambiguous/malformed" in body


def test_update_release_ambiguous_check_before_task_or_runtime_mutation():
    body = _executable_body(_read_ps1("update-release.ps1"))
    ambiguous_idx = body.index("if ($installIsSource -and $installIsFrozen)")
    task_mutation_idx = body.index("Snapshot and disable the task")
    assert ambiguous_idx < task_mutation_idx


def test_update_release_ambiguous_check_precedes_no_install_found_check():
    # Ordering sanity: ambiguous (both present) must be distinguished from
    # "neither present" -- checked first, not folded into the same branch.
    body = _executable_body(_read_ps1("update-release.ps1"))
    ambiguous_idx = body.index("if ($installIsSource -and $installIsFrozen)")
    no_install_idx = body.index("No existing install found")
    assert ambiguous_idx < no_install_idx


# --- update.ps1: whole-InstallRoot frozen update/rollback -----------------


def test_update_release_frozen_backup_covers_whole_installroot():
    body = _executable_body(_read_ps1("update-release.ps1"))
    backup_section = body[body.index("=== 2. Back up current runtime files ==="):body.index("function Restore-DisabledTaskAndFail")]
    frozen_backup = backup_section[:backup_section.index("} else {")]
    assert 'Copy-Item $InstallRoot -Destination $BackupRoot -Recurse -Force' in frozen_backup
    assert "BackupRoot must NOT already exist" in frozen_backup  # documents the nesting-avoidance precondition


# --- Installer-B: first-stage installer hardening ---------------------------
#
# collector/deploy/install-release.ps1 is the source file that becomes the
# bundle-root install.ps1 (build_release.SUPPORT_FILES). Same two-part split
# as the rest of this file:
#
#   1. Static checks (CI cannot run the installer itself, and running it
#      would need administrator rights and would query the machine's
#      Scheduled Tasks): what the script must contain, and in what ORDER --
#      each guard before any mutation.
#   2. Behavioral checks of the two PURE functions the script defines,
#      Get-InstallInputProblems and New-CollectorConfigJson. They are
#      extracted verbatim from the script and executed by PowerShell if one
#      is available (skipped otherwise). They touch nothing on the machine.

INSTALL_SCRIPT = "install-release.ps1"


def _install_body() -> str:
    return _executable_body(_read_ps1(INSTALL_SCRIPT))


def _extract_ps_function(name: str) -> str:
    text = _read_ps1(INSTALL_SCRIPT)
    match = re.search(rf"^function {re.escape(name)} \{{.*?^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"function {name} not found in {INSTALL_SCRIPT}"
    return match.group(0)


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


needs_powershell = pytest.mark.skipif(
    _powershell() is None, reason="no PowerShell available -- the static installer tests still run"
)


def _run_powershell(script: str) -> str:
    # -EncodedCommand: no quoting/escaping surprises, and no dependency on
    # the machine's script execution policy (no script file is run).
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _ps_literal(value) -> str:
    if value is None:
        return "$null"
    if isinstance(value, int):
        # Parenthesized: a bare negative literal (`-BranchId -1`) is not bound
        # as a number on a PowerShell command line. The installer itself calls
        # these functions with variables, where this never arises.
        return f"({value})"
    return "'" + str(value).replace("'", "''") + "'"


VALID_INSTALL_INPUT = {
    "CustomerId": 7,
    "BranchId": 3,
    "ApiUrl": "https://api.example.org",
    "CheckinsPath": r"C:\Site Data\Checkins.txt",
    "RejectsPath": r"D:\Rejects.txt",
    "AcsPath": r"\\server\share\ACS Log.txt",
}

INPUT_VALIDATION_CASES = {
    "valid": {},
    "no_customer": {"CustomerId": None},
    "zero_customer": {"CustomerId": 0},
    "no_branch": {"BranchId": None},
    "negative_branch": {"BranchId": -1},
    "no_api_url": {"ApiUrl": None},
    "blank_api_url": {"ApiUrl": "   "},
    "http_api_url": {"ApiUrl": "http://api.example.org"},
    "ftp_api_url": {"ApiUrl": "ftp://api.example.org"},
    "https_without_host": {"ApiUrl": "https://"},
    "uppercase_https": {"ApiUrl": "HTTPS://api.example.org"},
    "no_checkins": {"CheckinsPath": None},
    "no_rejects": {"RejectsPath": None},
    "no_acs": {"AcsPath": None},
    "relative_path": {"RejectsPath": "Rejects.txt"},
    "drive_relative_path": {"AcsPath": "C:ACS.txt"},
    "everything_missing": {key: None for key in VALID_INSTALL_INPUT},
}


@pytest.fixture(scope="module")
def input_validation_results():
    """Runs the installer's OWN Get-InstallInputProblems, once per case, in a
    single PowerShell process."""
    lines = [_extract_ps_function("Get-InstallInputProblems"), "$results = [ordered]@{}"]
    for case, overrides in INPUT_VALIDATION_CASES.items():
        args = " ".join(f"-{key} {_ps_literal(value)}" for key, value in {**VALID_INSTALL_INPUT, **overrides}.items())
        lines.append(f"$results['{case}'] = @(Get-InstallInputProblems {args})")
    lines.append("ConvertTo-Json -InputObject $results -Depth 4 -Compress")

    raw = json.loads(_run_powershell("\n".join(lines)))
    return {case: (value if isinstance(value, list) else ([] if value is None else [value])) for case, value in raw.items()}


# --- which file becomes install.ps1 ---------------------------------------


def test_install_release_ps1_is_the_source_of_the_bundle_install_ps1(built_bundle):
    assert ("collector/deploy/install-release.ps1", "install.ps1") in build_release.SUPPORT_FILES
    shipped = (built_bundle.bundle_dir / "install.ps1").read_bytes()
    assert shipped == (REPO_ROOT / "collector" / "deploy" / INSTALL_SCRIPT).read_bytes()


# --- A. administrator guard -----------------------------------------------


def test_installer_requires_administrator_before_anything_else():
    body = _install_body()
    guard = body.index("IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)")

    assert guard < body.index("Get-InstallInputProblems -CustomerId")
    assert guard < body.index("Test-ReleaseManifest -BundleRoot")
    assert guard < body.index("=== 1. Application runtime ===")
    assert 'Stop-Install "This script must be run from an elevated' in body[guard : guard + 300]


def test_installer_refusals_exit_nonzero_via_exit_not_a_bare_return():
    body = _install_body()
    stop_install = body[body.index("function Stop-Install"):]
    stop_install = stop_install[: stop_install.index("\n}\n")]

    assert "exit $ExitCode" in stop_install
    assert re.search(r"^\s*return\s*$", body, re.MULTILINE) is None


# --- B/C/D. required input ------------------------------------------------


def test_installer_parameters_are_explicit_and_have_no_defaults():
    body = _install_body()
    param_block = body[body.index("param("): body.index("$ErrorActionPreference")]

    for name in ("CustomerId", "BranchId", "ApiUrl", "CheckinsPath", "RejectsPath", "AcsPath"):
        assert f"${name}" in param_block, name
        assert not re.search(rf"\${name}\s*=", param_block), f"{name} must have no default"
    # Existing interface is unchanged.
    for name in ("InstallRoot", "DataRoot", "PythonExe", "Force"):
        assert f"${name}" in param_block, name


def test_installer_has_no_production_api_default_or_hardcoded_source_paths():
    text = _read_ps1(INSTALL_SCRIPT)

    assert "ondigitalocean" not in text
    assert "sortview-app" not in text
    assert "TLCFinalDlls" not in _executable_body(text)


def test_installer_validates_input_before_verifying_the_bundle_or_mutating_anything():
    body = _install_body()
    validation = body.index("Get-InstallInputProblems -CustomerId")

    assert validation < body.index("Test-ReleaseManifest -BundleRoot")
    assert validation < body.index("=== 1. Application runtime ===")
    assert 'nothing was installed or modified." 1' in body


def test_installer_has_no_example_template_fallback_for_the_config():
    body = _install_body()

    assert "Copy-Item $SourceExampleConfig" not in body
    assert "New-CollectorConfigJson -CustomerId $CustomerId" in body
    assert "-CheckinsPath $CheckinsPath -RejectsPath $RejectsPath -AcsPath $AcsPath" in body


def test_existing_config_is_left_untouched_and_the_technician_is_told():
    body = _install_body()
    branch = body[body.index("if (Test-Path $ConfigPath -PathType Leaf)"):]
    branch = branch[: branch.index("} else {")]

    assert "left untouched" in branch
    assert "were NOT applied" in branch
    assert "WriteAllText" not in branch


@needs_powershell
def test_installer_script_parses_without_errors():
    path = str(REPO_ROOT / "collector" / "deploy" / INSTALL_SCRIPT).replace("'", "''")
    script = (
        "$errs = $null; $tokens = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errs); "
        "$errs.Count"
    )

    assert _run_powershell(script).strip() == "0"


@needs_powershell
def test_valid_complete_input_has_no_problems(input_validation_results):
    # Includes a path with a space and a UNC path.
    assert input_validation_results["valid"] == []


@needs_powershell
def test_customer_id_is_required_and_must_be_positive(input_validation_results):
    for case in ("no_customer", "zero_customer"):
        problems = input_validation_results[case]
        assert len(problems) == 1 and "-CustomerId" in problems[0], case


@needs_powershell
def test_branch_id_is_required_and_must_be_positive(input_validation_results):
    for case in ("no_branch", "negative_branch"):
        problems = input_validation_results[case]
        assert len(problems) == 1 and "-BranchId" in problems[0], case


@needs_powershell
def test_api_url_is_required(input_validation_results):
    for case in ("no_api_url", "blank_api_url"):
        problems = input_validation_results[case]
        assert len(problems) == 1 and "-ApiUrl is required" in problems[0], case


@needs_powershell
def test_non_https_api_url_is_rejected(input_validation_results):
    for case in ("http_api_url", "ftp_api_url", "https_without_host"):
        problems = input_validation_results[case]
        assert len(problems) == 1 and "must begin with https://" in problems[0], case


@needs_powershell
def test_https_scheme_check_is_case_insensitive(input_validation_results):
    assert input_validation_results["uppercase_https"] == []


@needs_powershell
def test_each_of_the_three_source_paths_is_required(input_validation_results):
    for case, flag in (("no_checkins", "-CheckinsPath"), ("no_rejects", "-RejectsPath"), ("no_acs", "-AcsPath")):
        problems = input_validation_results[case]
        assert len(problems) == 1 and problems[0].startswith(f"{flag} is required"), case


@needs_powershell
def test_source_paths_must_be_absolute(input_validation_results):
    for case, flag in (("relative_path", "-RejectsPath"), ("drive_relative_path", "-AcsPath")):
        problems = input_validation_results[case]
        assert len(problems) == 1 and problems[0].startswith(f"{flag} must be an absolute path"), case


@needs_powershell
def test_every_missing_input_is_reported_together(input_validation_results):
    problems = input_validation_results["everything_missing"]

    assert len(problems) == 6
    for flag in ("-CustomerId", "-BranchId", "-ApiUrl", "-CheckinsPath", "-RejectsPath", "-AcsPath"):
        assert any(flag in problem for problem in problems), flag


# --- E. generated config uses the supplied values -------------------------


@needs_powershell
def test_generated_config_uses_the_supplied_values_and_loads_in_the_collector(tmp_path, monkeypatch):
    values = {
        "CustomerId": 42,
        "BranchId": 9,
        "ApiUrl": "  https://api.example.org  ",
        "CheckinsPath": r"E:\Tech Logic\Checkins.txt",
        "RejectsPath": r"E:\Tech Logic\Rejects.txt",
        "AcsPath": r"\\server\share\ACS Log.txt",
        "DataRoot": "C:\\Custom Data Root\\",
    }
    args = " ".join(f"-{key} {_ps_literal(value)}" for key, value in values.items())
    text = _run_powershell(_extract_ps_function("New-CollectorConfigJson") + f"\nNew-CollectorConfigJson {args}")

    doc = json.loads(text)
    assert doc["customer_id"] == 42
    assert doc["branch_id"] == 9
    assert doc["api_url"] == "https://api.example.org"  # trimmed
    assert [(s["name"], s["path"]) for s in doc["sources"]] == [
        ("checkins", r"E:\Tech Logic\Checkins.txt"),
        ("rejects", r"E:\Tech Logic\Rejects.txt"),
        ("acs", r"\\server\share\ACS Log.txt"),
    ]
    assert doc["state_path"] == r"C:\Custom Data Root\data\state.json"
    assert doc["status_path"] == r"C:\Custom Data Root\data\status.json"
    assert doc["log_path"] == r"C:\Custom Data Root\logs\collector.log"
    assert "token" not in text.lower()

    # ...and the Collector's own config loader accepts exactly this output.
    config_file = tmp_path / "collector_config.json"
    config_file.write_text(text, encoding="utf-8")
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token-not-a-real-token")
    cfg = load_config(config_file)
    assert (cfg.customer_id, cfg.branch_id) == (42, 9)
    assert [s.name for s in cfg.sources] == ["checkins", "rejects", "acs"]
    assert cfg.source("acs").path == r"\\server\share\ACS Log.txt"


# --- G. existing install without -Force -----------------------------------


def test_existing_install_without_force_refuses_with_a_nonzero_exit_and_modifies_nothing():
    body = _install_body()
    start = body.index("if ($alreadyInstalled -and -not $Force) {")
    block = body[start : body.index("if ($Force) {")]

    assert 'Stop-Install "An existing SortView Collector install was found. Nothing was modified." 2' in block
    for mutation in ("Remove-Item", "Copy-Item", "New-Item", "WriteAllText", "& $PythonExe"):
        assert mutation not in block, mutation
    assert start < body.index("if ($Force -and (Test-Path $InstallRoot))")
    assert start < body.index("=== 1. Application runtime ===")


# --- H. -Force never replaces an install a Scheduled Task points at -------


def test_force_refuses_when_the_collector_task_exists():
    body = _install_body()
    guard_start = body.index("if ($Force) {")
    removal = body.index("if ($Force -and (Test-Path $InstallRoot))")
    guard = body[guard_start:removal]

    assert guard_start < removal < body.index("=== 1. Application runtime ===")
    assert '$TaskName = "SortView Collector"' in body
    assert "Get-ScheduledTask -TaskName $TaskName" in guard
    assert "tools\\update.ps1" in guard
    assert re.search(r"Stop-Install .*Scheduled Task exists.* 2\s*$", guard, re.MULTILINE)
    # Fails closed: a lookup that itself errors is refused, not assumed safe.
    assert "catch {" in guard
    assert "cannot be proven safe" in guard


def test_force_task_guard_only_reads_the_task_and_never_changes_it():
    body = _install_body()
    executable = [
        line
        for line in _code_only(body).splitlines()
        if "Write-Host" not in line and "Stop-Install" not in line  # printed guidance may name these cmdlets
    ]
    joined = "\n".join(executable)

    for cmdlet in (
        "Register-ScheduledTask", "Unregister-ScheduledTask", "Enable-ScheduledTask",
        "Disable-ScheduledTask", "Stop-ScheduledTask", "Start-ScheduledTask",
        "Set-ScheduledTask", "schtasks",
    ):
        assert cmdlet not in joined, cmdlet


# --- no direct DB dependency ----------------------------------------------


def test_installer_has_no_direct_database_dependency():
    text = _read_ps1(INSTALL_SCRIPT).lower()

    for needle in ("psycopg2", "sqlalchemy", "database_url", "postgres", "libpq", "npgsql", "odbc"):
        assert needle not in text, needle
