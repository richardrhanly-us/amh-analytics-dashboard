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

import json
import subprocess
import sys
from pathlib import Path

import pytest

from collector import build_release, deploy_manifest

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
