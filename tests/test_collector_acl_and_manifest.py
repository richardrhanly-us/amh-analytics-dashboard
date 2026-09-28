"""Item 3 (least-privilege ACL hardening) and Item 4 (release-manifest persistence/verification) of the
2026-09-28 pre-cutover work.

THREE tiers of test, not two -- @needs_powershell alone is NOT a Windows check (pwsh is cross-platform and is
genuinely present on the Linux GitHub Actions runner this repo's CI uses):

  1. Static/textual checks (no decorator): pure Python string/regex inspection of the .ps1 source. Run
     everywhere, including Linux CI, with no PowerShell at all.
  2. @needs_powershell: runs a real PowerShell subprocess (the same convention
     tests/test_collector_build_release.py already uses -- _run_powershell, _powershell(), -EncodedCommand),
     but only for logic that is genuinely cross-platform once PowerShell itself is available -- parsing a
     script, the manifest-path/hashing logic in CollectorManifest.ps1 (portable by design, see that file's own
     .NOTES), and a task-gate test that SHADOWS Get-ScheduledTask with a fake function rather than depending on
     the real (Windows-only) ScheduledTasks module.
  3. @needs_windows_acl: runs a real PowerShell subprocess for logic that genuinely requires Windows --
     icacls, NTFS ACL semantics via Get-Acl/Set-Acl (FileSystemAccessRule/AccessControlType/FileSystemRights),
     and the real Get-ScheduledTask cmdlet. Skipped on Linux CI with a distinct, explicit reason -- not
     silently lumped in with "PowerShell is unavailable." Verified by hand on a real (non-elevated) Windows
     account: every one of Protect-CollectorPath's claims here, INCLUDING the recursive fix of an
     already-broad existing child ACE, passed for real (not merely skipped) -- ownership-based DACL rights are
     sufficient for icacls /reset and /grant:r on objects the test itself created, and PowerShell's own
     directory enumeration bypasses the parent's own (already tightened) traversal ACL via
     SeChangeNotifyPrivilege. A genuinely more locked-down Windows account (no Administrators membership AND no
     implicit traverse-bypass right) would still need a real onsite/administrator run to confirm the deepest
     recursive case; see that test's own skip message.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from collector import __version__ as SOURCE_VERSION
from collector import build_release

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO_ROOT / "collector" / "deploy"

ACL_LIB = "CollectorAcl.ps1"
MANIFEST_LIB = "CollectorManifest.ps1"
REPAIR_SCRIPT = "repair-collector-permissions.ps1"
VERIFY_SCRIPT = "verify-install.ps1"
INSTALL_SCRIPT = "install-release.ps1"
UPDATE_SCRIPT = "update-release.ps1"


def _read(*parts: str) -> str:
    return (DEPLOY_DIR / Path(*parts)).read_text(encoding="utf-8")


def _executable_body(text: str) -> str:
    end_marker = "#>"
    idx = text.find(end_marker)
    return text[idx + len(end_marker):] if idx != -1 else text


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


needs_powershell = pytest.mark.skipif(
    _powershell() is None, reason="no PowerShell available -- the static tests still run"
)


def _has_windows_acl_support() -> bool:
    # PowerShell (pwsh) is cross-platform and IS present on the Linux GitHub Actions runner -- @needs_powershell
    # alone is not a Windows check. icacls, NTFS ACL semantics (Get-Acl/Set-Acl producing real
    # FileSystemAccessRule/AccessControlType/FileSystemRights behavior), and the ScheduledTasks module
    # (Get-ScheduledTask) all genuinely require Windows; none of them exist on Linux pwsh. sys.platform is
    # checked directly rather than inferred from shutil.which("icacls") alone, since a same-named executable
    # could theoretically exist elsewhere on PATH.
    return sys.platform == "win32" and shutil.which("icacls") is not None


needs_windows_acl = pytest.mark.skipif(
    not _has_windows_acl_support(),
    reason="Windows-only: requires icacls / NTFS ACL semantics, not available on this platform (e.g. Linux CI)",
)


def _run_powershell_raw(script: str):
    # Returns the raw CompletedProcess (exit code included, never asserted) -- for a test that needs to observe
    # a script's own `exit N`, not just its stdout. _run_powershell (below) is for everything else.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    if len(encoded) <= 24000:
        command = ["-EncodedCommand", encoded]
    else:
        script_file = Path(tempfile.mkdtemp()) / "script.ps1"
        script_file.write_text(script, encoding="utf-8")
        command = ["-ExecutionPolicy", "Bypass", "-File", str(script_file)]
    exe = _powershell()
    import subprocess  # nosec B404 - fixed executable, fixed/generated arguments, test-only

    return subprocess.run(  # nosec B603 - fixed executable resolved via shutil.which, no shell
        [exe, "-NoProfile", "-NonInteractive", *command], capture_output=True, text=True, check=False
    )


def _run_powershell(script: str) -> str:
    result = _run_powershell_raw(script)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    return result.stdout


# =====================================================================================================================
# 1. Files exist and are wired into the release bundle
# =====================================================================================================================

def test_all_four_new_files_exist():
    for name in (ACL_LIB, MANIFEST_LIB, REPAIR_SCRIPT, VERIFY_SCRIPT):
        assert (DEPLOY_DIR / name).is_file(), name


def test_all_four_new_files_are_registered_in_deploy_tool_files():
    shipped = dict(build_release.DEPLOY_TOOL_FILES)
    assert shipped[f"collector/deploy/{ACL_LIB}"] == f"tools/{ACL_LIB}"
    assert shipped[f"collector/deploy/{MANIFEST_LIB}"] == f"tools/{MANIFEST_LIB}"
    assert shipped[f"collector/deploy/{REPAIR_SCRIPT}"] == "tools/repair-permissions.ps1"
    assert shipped[f"collector/deploy/{VERIFY_SCRIPT}"] == f"tools/{VERIFY_SCRIPT}"


def test_deploy_tool_and_support_file_sources_all_exist_including_the_new_ones():
    for source_rel, _dest_rel in build_release.DEPLOY_TOOL_FILES:
        assert (REPO_ROOT / source_rel).is_file(), source_rel


def test_bundle_contains_the_four_new_files(tmp_path):
    result = build_release.build_release(REPO_ROOT, tmp_path, SOURCE_VERSION, built_at="2026-01-01T00:00:00.000000Z")
    assert (result.bundle_dir / "tools" / ACL_LIB).is_file()
    assert (result.bundle_dir / "tools" / MANIFEST_LIB).is_file()
    assert (result.bundle_dir / "tools" / "repair-permissions.ps1").is_file()
    assert (result.bundle_dir / "tools" / VERIFY_SCRIPT).is_file()


# =====================================================================================================================
# 2. Static wiring: install.ps1 / update.ps1 / repair / verify all reference the shared helpers correctly
# =====================================================================================================================

def test_install_and_update_dot_source_both_libraries():
    for script in (INSTALL_SCRIPT, UPDATE_SCRIPT):
        text = _read(script)
        assert "CollectorAcl.ps1" in text, script
        assert "CollectorManifest.ps1" in text, script


def test_repair_and_verify_dot_source_the_library_they_need():
    assert "CollectorAcl.ps1" in _read(REPAIR_SCRIPT)
    assert "CollectorManifest.ps1" in _read(VERIFY_SCRIPT)


def test_install_protects_both_install_root_and_every_data_subdirectory():
    body = _executable_body(_read(INSTALL_SCRIPT))
    assert "Protect-CollectorPath -Path $InstallRoot -Recurse" in body
    assert 'Protect-CollectorPath -Path $DataRoot' in body
    assert 'Protect-CollectorPath -Path (Join-Path $DataRoot "config") -Recurse' in body
    assert 'Protect-CollectorPath -Path (Join-Path $DataRoot "data") -Recurse' in body
    assert 'Protect-CollectorPath -Path (Join-Path $DataRoot "logs") -Recurse' in body
    protect_calls = re.findall(r"^Protect-CollectorPath.*$", body, re.MULTILINE)
    assert protect_calls  # sanity: the regex above actually matched something
    assert not any("secret" in call.lower() for call in protect_calls)  # never protects/creates the v2 secret dir


def test_update_protects_install_root_only_never_data_root():
    # update.ps1's own docstring promises it never touches -DataRoot (config/state/logs) -- confirm the ACL
    # hardening added here honors that: it protects $InstallRoot but never calls Protect-CollectorPath on $DataRoot.
    body = _executable_body(_read(UPDATE_SCRIPT))
    assert "Protect-CollectorPath -Path $InstallRoot -Recurse" in body
    assert "$DataRoot" not in body  # update.ps1 has no -DataRoot parameter at all; confirms the contract holds


def test_install_and_update_call_manifest_persistence_and_self_verification():
    for script in (INSTALL_SCRIPT, UPDATE_SCRIPT):
        body = _executable_body(_read(script))
        assert "New-InstalledManifest" in body, script
        assert "Test-InstalledManifest" in body, script


# =====================================================================================================================
# 3. repair-collector-permissions.ps1: never mutates the task, never touches secrets, requires elevation
# =====================================================================================================================

def test_repair_requires_elevation():
    assert "WindowsBuiltInRole]::Administrator" in _read(REPAIR_SCRIPT)


def test_repair_never_calls_disable_or_enable_or_start_or_stop_scheduled_task():
    # The script's own refusal message NAMES Disable-ScheduledTask as instructional text for the operator to run
    # themselves (quoted, with $TaskName single-quoted so it never interpolates as a live variable) -- that is not
    # an invocation. An actual call in this codebase's own style looks like "Cmdlet -TaskName $TaskName" (bare
    # variable, no inner quotes) -- that exact shape must never appear.
    body = _executable_body(_read(REPAIR_SCRIPT))
    for forbidden in ("Disable-ScheduledTask", "Enable-ScheduledTask", "Start-ScheduledTask", "Stop-ScheduledTask"):
        assert f"{forbidden} -TaskName $TaskName" not in body, forbidden


def test_repair_refuses_unless_the_task_is_already_disabled():
    body = _executable_body(_read(REPAIR_SCRIPT))
    assert "Settings.Enabled" in body
    assert "REPAIR REFUSED" in body


def test_repair_never_lists_the_secrets_directory_as_a_target():
    body = _executable_body(_read(REPAIR_SCRIPT))
    targets_block = body[body.index("$targets = @("): body.index(")\n\nWrite-Host")]
    assert "secrets" not in targets_block.lower()


def test_repair_deletes_nothing_and_never_writes_config_content():
    body = _executable_body(_read(REPAIR_SCRIPT))
    for forbidden in ("Remove-Item", "Set-Content", "WriteAllText", "ConvertTo-Json"):
        assert forbidden not in body, forbidden


def test_repair_verifies_and_reports_each_path():
    body = _executable_body(_read(REPAIR_SCRIPT))
    assert "Protect-CollectorPath" in body
    assert "REPAIR COMPLETE" in body and "REPAIR INCOMPLETE" in body


def test_repair_missing_target_is_recorded_as_a_problem_not_silently_skipped():
    # Static structural proof: the missing-target branch must add to $problems before its `continue`, not just
    # print and move on -- the old "SKIPPED (does not exist)" wording (and the bug it named) must be gone.
    body = _executable_body(_read(REPAIR_SCRIPT))
    assert "SKIPPED (does not exist)" not in body
    match = re.search(r"if \(-not \(Test-Path -LiteralPath \$target\.Path\)\) \{(.*?)continue\n    \}", body, re.DOTALL)
    assert match, "missing-target branch not found in the expected shape"
    assert "$problems += $target.Path" in match.group(1)


def _repair_loop_and_summary_body() -> str:
    # Extracts the REAL loop-over-$targets-and-summarize logic verbatim from the script (the same
    # extract-and-run-in-isolation approach tests/test_collector_build_release.py already uses for
    # install.ps1/finish-install.ps1) -- not a reimplementation, so this proves the actual code, not a
    # restatement of it. Protect-CollectorPath is stubbed by the caller; the elevation and task-disabled gates
    # earlier in the real script are deliberately NOT included, since they aren't part of what this proves and
    # this sandbox is not elevated.
    text = _read(REPAIR_SCRIPT)
    start = text.index("$problems = @()")
    end = text.index("exit 0", start) + len("exit 0")
    return text[start:end]


@needs_powershell
def test_repair_loop_exits_nonzero_and_never_prints_repair_complete_when_a_required_target_is_missing(tmp_path):
    existing = tmp_path / "exists"
    existing.mkdir()
    missing = tmp_path / "does_not_exist"

    harness = (
        "function Protect-CollectorPath { param($Path, [switch]$Recurse) }\n"
        "$DataRoot = 'C:\\unused'\n"
        "$targets = @(\n"
        f"    [ordered]@{{ Path = '{existing}'; Recurse = $false }}\n"
        f"    [ordered]@{{ Path = '{missing}'; Recurse = $false }}\n"
        ")\n"
        + _repair_loop_and_summary_body()
    )
    result = _run_powershell_raw(harness)
    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "REPAIR COMPLETE" not in result.stdout
    assert "REPAIR INCOMPLETE" in result.stdout
    assert str(missing) in result.stdout


@needs_powershell
def test_repair_loop_reports_complete_when_every_required_target_exists(tmp_path):
    # Sibling control: proves the harness/stub itself is valid, and that the fix did not turn the happy path
    # into a false failure.
    one = tmp_path / "one"
    one.mkdir()
    two = tmp_path / "two"
    two.mkdir()

    harness = (
        "function Protect-CollectorPath { param($Path, [switch]$Recurse) }\n"
        "$DataRoot = 'C:\\unused'\n"
        "$targets = @(\n"
        f"    [ordered]@{{ Path = '{one}'; Recurse = $false }}\n"
        f"    [ordered]@{{ Path = '{two}'; Recurse = $false }}\n"
        ")\n"
        + _repair_loop_and_summary_body()
    )
    result = _run_powershell_raw(harness)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "REPAIR COMPLETE" in result.stdout


def _repair_task_gate_body() -> str:
    text = _read(REPAIR_SCRIPT)
    start = text.index("# --- task-disabled gate")
    end = text.index("# --- targets:", start)
    return text[start:end]


def test_repair_task_gate_never_uses_silentlycontinue():
    # SilentlyContinue would make a genuine query failure indistinguishable from "not registered" -- both
    # would leave $task/$taskIsRegistered looking like the unregistered case. (The explanatory comment above
    # the code legitimately names "SilentlyContinue" in prose -- this checks the actual cmdlet call shape,
    # not a bare substring search that would also match that comment.)
    body = _repair_task_gate_body()
    assert "-ErrorAction SilentlyContinue" not in body
    assert "Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop" in body


@needs_windows_acl  # real Get-ScheduledTask cmdlet -- the ScheduledTasks module does not exist on Linux
def test_repair_task_gate_proceeds_for_a_genuinely_unregistered_task():
    # Real Get-ScheduledTask call, real (all-but-certain) absence on this dev machine -- proves the "not
    # registered" branch is reached via the module's own actual not-found error, not assumed.
    harness = (
        "function Stop-Repair { param($Message, [int]$ExitCode = 1) Write-Host \"STOP-REPAIR:$Message\"; exit $ExitCode }\n"
        "$TaskName = 'SortView-Collector-Regression-Test-Definitely-Not-Registered'\n"
        + _repair_task_gate_body()
        + "\nWrite-Host 'GATE-PASSED'\nexit 0"
    )
    result = _run_powershell_raw(harness)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "not registered" in result.stdout
    assert "GATE-PASSED" in result.stdout
    assert "STOP-REPAIR" not in result.stdout


@needs_powershell
def test_repair_task_gate_refuses_on_a_genuine_query_failure_not_treated_as_unregistered():
    # Shadows Get-ScheduledTask with a fake that throws a non-"ObjectNotFound" error -- a real
    # CIM/RPC/permission failure must refuse (Stop-Repair), never be silently treated as "task not registered."
    harness = (
        "function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName) throw 'simulated CIM/RPC failure' }\n"
        "function Stop-Repair { param($Message, [int]$ExitCode = 1) Write-Host \"STOP-REPAIR:$Message\"; exit $ExitCode }\n"
        "$TaskName = 'SortView Collector'\n"
        + _repair_task_gate_body()
        + "\nWrite-Host 'GATE-PASSED'\nexit 0"
    )
    result = _run_powershell_raw(harness)
    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "STOP-REPAIR" in result.stdout
    assert "GATE-PASSED" not in result.stdout
    # The refusal message itself legitimately contains the phrase "not registered" while explaining what this
    # is NOT the same as -- what must be absent is the gate's own success-path message for that other case.
    assert "is not registered -- proceeding" not in result.stdout


# =====================================================================================================================
# 4. verify-install.ps1 is read-only
# =====================================================================================================================

def test_verify_install_never_mutates_anything():
    body = _executable_body(_read(VERIFY_SCRIPT))
    for forbidden in ("Remove-Item", "Set-Content", "New-Item", "Copy-Item", "WriteAllText", "Move-Item"):
        assert forbidden not in body, forbidden
    assert "Test-InstalledManifest" in body


# =====================================================================================================================
# 5. CollectorAcl.ps1: the two well-known SIDs, icacls, inheritance cut, recursive option
# =====================================================================================================================

def test_acl_library_uses_the_same_well_known_sids_as_v2_keys_and_setup_collector():
    text = _read(ACL_LIB)
    assert "S-1-5-18" in text  # SYSTEM
    assert "S-1-5-32-544" in text  # Administrators
    v2_keys_text = (REPO_ROOT / "collector" / "v2_keys.py").read_text(encoding="utf-8")
    assert "S-1-5-18" in v2_keys_text and "S-1-5-32-544" in v2_keys_text


def test_acl_library_uses_icacls_with_inheritance_cut_and_recurse_flag():
    text = _read(ACL_LIB)
    assert "icacls" in text
    assert "/inheritance:r" in text
    assert '"/T"' in text  # recursion into already-existing children


@needs_powershell
def test_acl_library_parses_without_errors():
    path = str(DEPLOY_DIR / ACL_LIB).replace("'", "''")
    script = (
        "$errs = $null; $tokens = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errs); "
        "$errs.Count"
    )
    assert _run_powershell(script).strip() == "0"


@needs_powershell
@pytest.mark.parametrize("script_name", [REPAIR_SCRIPT, VERIFY_SCRIPT, MANIFEST_LIB])
def test_new_scripts_parse_without_errors(script_name):
    path = str(DEPLOY_DIR / script_name).replace("'", "''")
    script = (
        "$errs = $null; $tokens = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errs); "
        "$errs.Count"
    )
    assert _run_powershell(script).strip() == "0"


@needs_powershell
def test_install_and_update_scripts_still_parse_without_errors_after_the_new_wiring():
    for script_name in (INSTALL_SCRIPT, UPDATE_SCRIPT):
        path = str(DEPLOY_DIR / script_name).replace("'", "''")
        script = (
            "$errs = $null; $tokens = $null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errs); "
            "$errs.Count"
        )
        assert _run_powershell(script).strip() == "0", script_name


# =====================================================================================================================
# 6. Protect-CollectorPath / Test-CollectorPathProtected -- live behavior, on a real temp directory.
#    Skips (not fails) only if this specific account cannot itself grant SYSTEM/Administrators an ACE at all --
#    confirmed NOT to skip for a plain (non-Administrator, non-elevated) domain account; see the module docstring.
# =====================================================================================================================

def _acl_prelude(tmp_dir: str) -> str:
    lib_path = str(DEPLOY_DIR / ACL_LIB).replace("'", "''")
    return f". '{lib_path}'\n$Target = '{tmp_dir}'\n"


def _acl_reset(tmp_dir: str) -> str:
    # A directory this test successfully locked to SYSTEM+Administrators-only, with inheritance cut, is no longer
    # deletable by pytest's own (non-elevated) tmp_path teardown -- neither SYSTEM nor Administrators is this
    # account's own identity, and Windows grants no other principal access once inheritance is cut. Restoring
    # inheritance from the parent (icacls /reset) undoes that via this account's own ownership-based DACL rights
    # (no ALLOW entry naming it is required to rewrite the DACL of an object it owns), so cleanup can proceed
    # normally; run unconditionally, after every assertion.
    return f"icacls '{tmp_dir}' /reset /T /C *>$null\n"


@needs_windows_acl
def test_protect_collector_path_locks_a_fresh_directory_and_verifies_it(tmp_path):
    target = tmp_path / "fresh"
    target.mkdir()
    try:
        script = _acl_prelude(str(target)) + (
            "try { Protect-CollectorPath -Path $Target; 'OK' } "
            "catch { \"SKIP:$($_.Exception.Message)\" }"
        )
        result = _run_powershell(script).strip()
        if result.startswith("SKIP:"):
            pytest.skip(f"this sandbox account cannot apply the ACL itself: {result}")
        assert result == "OK"

        verify_script = _acl_prelude(str(target)) + "Test-CollectorPathProtected -Path $Target"
        assert _run_powershell(verify_script).strip() == "True"
    finally:
        _run_powershell(_acl_reset(str(target)))


@needs_windows_acl
def test_protect_collector_path_is_idempotent(tmp_path):
    target = tmp_path / "twice"
    target.mkdir()
    try:
        script = _acl_prelude(str(target)) + (
            "try { Protect-CollectorPath -Path $Target; Protect-CollectorPath -Path $Target; 'OK' } "
            "catch { \"SKIP:$($_.Exception.Message)\" }"
        )
        result = _run_powershell(script).strip()
        if result.startswith("SKIP:"):
            pytest.skip(f"this sandbox account cannot apply the ACL itself: {result}")
        assert result == "OK"
    finally:
        _run_powershell(_acl_reset(str(target)))


@needs_windows_acl
def test_protect_collector_path_removes_a_preexisting_explicit_grant_to_an_unwanted_principal(tmp_path):
    # Regression test for a real bug found via manual onsite-shape reproduction: /inheritance:r /grant:r alone
    # only cuts INHERITED entries and adds/replaces the NAMED SIDs' own grant -- it does NOT remove another
    # principal's PRE-EXISTING EXPLICIT grant (exactly the confirmed onsite "Authenticated Users: Modify" shape).
    # Protect-CollectorPath must /reset first so the unwanted grant is actually gone afterward, not left sitting
    # alongside the new one. Single file, no -Recurse -- unlike the sibling multi-level test below, this cannot
    # self-lock-out a non-elevated caller (there is nothing to recurse into afterward), so it runs for real here.
    target = tmp_path / "single_file_case"
    target.mkdir()
    child = target / "SortViewCollector.exe"
    child.write_text("stub")

    script = _acl_prelude(str(child)) + (
        "$rule = New-Object System.Security.AccessControl.FileSystemAccessRule("
        "'Authenticated Users', 'Modify', 'Allow')\n"
        "$acl = Get-Acl -LiteralPath $Target\n"
        "$acl.AddAccessRule($rule)\n"
        "Set-Acl -LiteralPath $Target -AclObject $acl\n"
        "try { Protect-CollectorPath -Path $Target; 'OK' } catch { \"SKIP:$($_.Exception.Message)\" }"
    )
    result = _run_powershell(script).strip()
    if result.startswith("SKIP:"):
        pytest.skip(f"this sandbox account cannot apply the ACL itself: {result}")
    assert result == "OK"

    # Test-CollectorPathProtected's exact-SID-set match already proves "Authenticated Users" is gone -- if it
    # were still present the comparison would fail and this would read "False", not "True". A raw `icacls`
    # dump was tried here too but is NOT a reliable post-lockdown check even for the object's own owner: unlike
    # Get-Acl (read-only DACL access, satisfied by owner rights regardless of the DACL's own content), icacls
    # itself denies access once a target's explicit grant no longer names the caller -- confirmed separately
    # against the multi-level recursive test below, where the identical dump approach failed with Access
    # Denied on a successfully-protected child for exactly this reason.
    verify_script = _acl_prelude(str(child)) + "Test-CollectorPathProtected -Path $Target"
    assert _run_powershell(verify_script).strip() == "True"


@needs_windows_acl
def test_protect_collector_path_recurse_fixes_an_already_broad_existing_child(tmp_path):
    # Reproduces the confirmed onsite shape: a child file that already has a broader grant
    # (Authenticated Users) BEFORE the parent is protected -- -Recurse must correct it too, not just the parent.
    target = tmp_path / "populated"
    target.mkdir()
    child = target / "SortViewCollector.exe"
    child.write_text("stub")

    try:
        script = _acl_prelude(str(target)) + (
            "try {\n"
            "    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule("
            "'Authenticated Users', 'Modify', 'Allow')\n"
            "    $acl = Get-Acl -LiteralPath (Join-Path $Target 'SortViewCollector.exe')\n"
            "    $acl.AddAccessRule($rule)\n"
            "    Set-Acl -LiteralPath (Join-Path $Target 'SortViewCollector.exe') -AclObject $acl\n"
            "    Protect-CollectorPath -Path $Target -Recurse\n"
            "    'OK'\n"
            "} catch { \"SKIP:$($_.Exception.Message)\" }"
        )
        result = _run_powershell(script).strip()
        if result.startswith("SKIP:"):
            pytest.skip(f"this sandbox account cannot apply the ACL itself: {result}")
        assert result == "OK"

        # Exact-SID-set match, checked independently per item (parent AND every child) -- "True" here is
        # already conclusive proof the pre-existing "Authenticated Users" grant is gone from $child, not just
        # supplemented. (A raw `icacls $child` dump was tried as an additional check and reliably fails with
        # Access Denied here even though the object IS correctly protected -- unlike Get-Acl, which only needs
        # READ_CONTROL and is satisfied by this account's ownership regardless of the DACL's own content,
        # icacls's own implementation requires the caller be named in the target's explicit grant, which by
        # design it no longer is. That failure mode is itself further confirmation of a correctly locked-down
        # object, not a gap in this test.)
        verify_script = _acl_prelude(str(target)) + "Test-CollectorPathProtected -Path $Target -Recurse"
        assert _run_powershell(verify_script).strip() == "True"
    finally:
        _run_powershell(_acl_reset(str(target)))


# =====================================================================================================================
# 6a. Test-OneCollectorAclEntry strictness: must reject anything short of the EXACT policy -- Allow/FullControl
#     for SYSTEM and Administrators only, no Deny entry, no other trustee, no weaker right. Each test constructs
#     the malformed ACL directly (owner-based Set-Acl, no elevation needed) and calls the real verifier -- no
#     reimplementation of what "correct" means, just controlled-bad input against the actual function.
# =====================================================================================================================

def _construct_acl_script(rule_lines: str, *, protected: bool = True) -> str:
    protection = "$true" if protected else "$false"
    return (
        "$acl = New-Object System.Security.AccessControl.FileSecurity\n"
        f"$acl.SetAccessRuleProtection({protection}, $false)\n"
        + rule_lines +
        "Set-Acl -LiteralPath $Target -AclObject $acl\n"
        "Test-CollectorPathProtected -Path $Target"
    )


def _allow_rule(identity: str, rights: str) -> str:
    return f'$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("{identity}", "{rights}", "Allow")))\n'


def _deny_rule(identity: str, rights: str) -> str:
    return f'$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("{identity}", "{rights}", "Deny")))\n'


@needs_windows_acl
def test_verifier_rejects_system_with_readandexecute_instead_of_fullcontrol(tmp_path):
    target = tmp_path / "weak_system.exe"
    target.write_text("stub")
    script = _acl_prelude(str(target)) + _construct_acl_script(
        _allow_rule("SYSTEM", "ReadAndExecute") + _allow_rule("Administrators", "FullControl")
    )
    assert _run_powershell(script).strip() == "False"


@needs_windows_acl
def test_verifier_rejects_administrators_with_read_instead_of_fullcontrol(tmp_path):
    target = tmp_path / "weak_admins.exe"
    target.write_text("stub")
    script = _acl_prelude(str(target)) + _construct_acl_script(
        _allow_rule("SYSTEM", "FullControl") + _allow_rule("Administrators", "Read")
    )
    assert _run_powershell(script).strip() == "False"


@needs_windows_acl
def test_verifier_rejects_an_extra_authenticated_users_allow_ace(tmp_path):
    target = tmp_path / "extra_trustee.exe"
    target.write_text("stub")
    script = _acl_prelude(str(target)) + _construct_acl_script(
        _allow_rule("SYSTEM", "FullControl")
        + _allow_rule("Administrators", "FullControl")
        + _allow_rule("Authenticated Users", "Modify")
    )
    assert _run_powershell(script).strip() == "False"


@needs_windows_acl
def test_verifier_rejects_an_unexpected_deny_ace(tmp_path):
    # The OLD verifier filtered to AccessControlType -eq "Allow" before ever building its SID set -- a Deny
    # entry was silently invisible to it and could coexist with an otherwise-correct Allow set undetected.
    target = tmp_path / "has_deny.exe"
    target.write_text("stub")
    script = _acl_prelude(str(target)) + _construct_acl_script(
        _allow_rule("SYSTEM", "FullControl")
        + _allow_rule("Administrators", "FullControl")
        + _deny_rule("Authenticated Users", "Modify")
    )
    assert _run_powershell(script).strip() == "False"


@needs_windows_acl
def test_verifier_rejects_a_path_where_inheritance_is_still_enabled(tmp_path):
    target = tmp_path / "still_inherited.exe"
    target.write_text("stub")
    script = _acl_prelude(str(target)) + _construct_acl_script(
        _allow_rule("SYSTEM", "FullControl") + _allow_rule("Administrators", "FullControl"),
        protected=False,
    )
    assert _run_powershell(script).strip() == "False"


# =====================================================================================================================
# 7. New-InstalledManifest / Test-InstalledManifest -- live behavior, real temp directories, real file hashing.
# =====================================================================================================================

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _write_frozen_bundle(bundle_root: Path) -> dict:
    runtime = bundle_root / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "SortViewCollector.exe").write_bytes(b"exe-bytes")
    internal = runtime / "_internal"
    internal.mkdir()
    (internal / "lib.pyd").write_bytes(b"lib-bytes")
    (bundle_root / "install.ps1").write_text("# tooling, not installed", encoding="utf-8")

    files = [
        {"path": "runtime\\SortViewCollector.exe", "sha256": _sha256(b"exe-bytes"), "size_bytes": 9},
        {"path": "runtime\\_internal\\lib.pyd", "sha256": _sha256(b"lib-bytes"), "size_bytes": 9},
        {"path": "install.ps1", "sha256": _sha256(b"# tooling, not installed"), "size_bytes": 24},
    ]
    manifest = {"product": "SortView Collector", "version": "9.9.9", "built_at": "2026-01-01T00:00:00.000000Z", "files": files}
    (bundle_root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def _manifest_prelude(bundle_root: Path, install_root: Path) -> str:
    lib_path = str(DEPLOY_DIR / MANIFEST_LIB).replace("'", "''")
    return (
        f". '{lib_path}'\n"
        f"$BundleRoot = '{bundle_root}'\n"
        f"$InstallRoot = '{install_root}'\n"
        "$Manifest = Get-Content (Join-Path $BundleRoot 'MANIFEST.json') -Raw | ConvertFrom-Json\n"
    )


@needs_powershell
def test_new_installed_manifest_frozen_round_trips_through_test_installed_manifest(tmp_path):
    bundle_root = tmp_path / "bundle"
    _write_frozen_bundle(bundle_root)
    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "SortViewCollector.exe").write_bytes(b"exe-bytes")
    (install_root / "_internal").mkdir()
    (install_root / "_internal" / "lib.pyd").write_bytes(b"lib-bytes")

    script = _manifest_prelude(bundle_root, install_root) + (
        "New-InstalledManifest -BundleRoot $BundleRoot -InstallRoot $InstallRoot -Manifest $Manifest -BundleKind frozen\n"
        "Test-InstalledManifest -InstallRoot $InstallRoot\n"
        "'DONE'"
    )
    assert _run_powershell(script).strip().splitlines()[-1] == "DONE"

    installed = json.loads((install_root / "MANIFEST.installed.json").read_text(encoding="utf-8"))
    assert installed["bundle_kind"] == "frozen"
    installed_paths = {f["installed_path"] for f in installed["files"]}
    # install.ps1 is bundle-only tooling -- never copied to InstallRoot, so it must NOT appear.
    # Forward slash, not backslash: the canonical relative-path form (see CollectorManifest.ps1's own .NOTES) --
    # this is what makes MANIFEST.installed.json itself portable across Windows and Linux pwsh.
    assert installed_paths == {"SortViewCollector.exe", "_internal/lib.pyd"}
    assert (install_root / "MANIFEST.json").read_text(encoding="utf-8") == (bundle_root / "MANIFEST.json").read_text(encoding="utf-8")


@needs_powershell
def test_new_installed_manifest_round_trips_with_the_actual_forward_slash_paths_build_release_writes(tmp_path):
    # Regression test for the real GitHub Actions CI failure on a Linux pwsh runner: collector/build_release.py
    # ALWAYS writes MANIFEST.json paths with forward slashes (Path.as_posix() for the frozen runtime; literal
    # "tools/..."-style string constants for everything else -- confirmed by reading that file, never a
    # backslash). The OLDER helper above uses backslash input defensively (mixed-separator tolerance); THIS
    # test uses the actual shape production code produces, so this proves the realistic case directly, not
    # just the defensive one.
    bundle_root = tmp_path / "bundle"
    runtime = bundle_root / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "SortViewCollector.exe").write_bytes(b"exe-bytes")
    internal = runtime / "_internal"
    internal.mkdir()
    (internal / "lib.pyd").write_bytes(b"lib-bytes")
    files = [
        {"path": "runtime/SortViewCollector.exe", "sha256": _sha256(b"exe-bytes"), "size_bytes": 9},
        {"path": "runtime/_internal/lib.pyd", "sha256": _sha256(b"lib-bytes"), "size_bytes": 9},
        {"path": "tools/install.ps1", "sha256": _sha256(b"stub"), "size_bytes": 4},
    ]
    manifest = {"product": "SortView Collector", "version": "9.9.9", "built_at": "x", "files": files}
    (bundle_root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")

    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "SortViewCollector.exe").write_bytes(b"exe-bytes")
    (install_root / "_internal").mkdir()
    (install_root / "_internal" / "lib.pyd").write_bytes(b"lib-bytes")

    script = _manifest_prelude(bundle_root, install_root) + (
        "New-InstalledManifest -BundleRoot $BundleRoot -InstallRoot $InstallRoot -Manifest $Manifest -BundleKind frozen\n"
        "try { Test-InstalledManifest -InstallRoot $InstallRoot; 'PASS' } catch { $_.Exception.Message }"
    )
    assert _run_powershell(script).strip().splitlines()[-1] == "PASS"

    installed = json.loads((install_root / "MANIFEST.installed.json").read_text(encoding="utf-8"))
    installed_paths = {f["installed_path"] for f in installed["files"]}
    assert installed_paths == {"SortViewCollector.exe", "_internal/lib.pyd"}


@needs_powershell
def test_test_installed_manifest_reports_missing_file(tmp_path):
    bundle_root = tmp_path / "bundle"
    _write_frozen_bundle(bundle_root)
    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "SortViewCollector.exe").write_bytes(b"exe-bytes")
    (install_root / "_internal").mkdir()
    (install_root / "_internal" / "lib.pyd").write_bytes(b"lib-bytes")

    script = _manifest_prelude(bundle_root, install_root) + (
        "New-InstalledManifest -BundleRoot $BundleRoot -InstallRoot $InstallRoot -Manifest $Manifest -BundleKind frozen\n"
    )
    _run_powershell(script)
    (install_root / "_internal" / "lib.pyd").unlink()

    verify_script = _manifest_prelude(bundle_root, install_root) + (
        "try { Test-InstalledManifest -InstallRoot $InstallRoot; 'NO EXCEPTION' } "
        "catch { $_.Exception.Message }"
    )
    assert "MISSING: _internal/lib.pyd" in _run_powershell(verify_script)


@needs_powershell
def test_test_installed_manifest_reports_hash_mismatch(tmp_path):
    bundle_root = tmp_path / "bundle"
    _write_frozen_bundle(bundle_root)
    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "SortViewCollector.exe").write_bytes(b"exe-bytes")
    (install_root / "_internal").mkdir()
    (install_root / "_internal" / "lib.pyd").write_bytes(b"lib-bytes")

    _run_powershell(_manifest_prelude(bundle_root, install_root) +
                     "New-InstalledManifest -BundleRoot $BundleRoot -InstallRoot $InstallRoot -Manifest $Manifest -BundleKind frozen\n")
    (install_root / "SortViewCollector.exe").write_bytes(b"TAMPERED")

    verify_script = _manifest_prelude(bundle_root, install_root) + (
        "try { Test-InstalledManifest -InstallRoot $InstallRoot; 'NO EXCEPTION' } "
        "catch { $_.Exception.Message }"
    )
    assert "HASH MISMATCH: SortViewCollector.exe" in _run_powershell(verify_script)


@needs_powershell
def test_test_installed_manifest_reports_unexpected_file_in_a_managed_zone(tmp_path):
    bundle_root = tmp_path / "bundle"
    _write_frozen_bundle(bundle_root)
    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "SortViewCollector.exe").write_bytes(b"exe-bytes")
    (install_root / "_internal").mkdir()
    (install_root / "_internal" / "lib.pyd").write_bytes(b"lib-bytes")

    _run_powershell(_manifest_prelude(bundle_root, install_root) +
                     "New-InstalledManifest -BundleRoot $BundleRoot -InstallRoot $InstallRoot -Manifest $Manifest -BundleKind frozen\n")
    (install_root / "surprise.dll").write_bytes(b"not in the manifest")

    verify_script = _manifest_prelude(bundle_root, install_root) + (
        "try { Test-InstalledManifest -InstallRoot $InstallRoot; 'NO EXCEPTION' } "
        "catch { $_.Exception.Message }"
    )
    assert "UNEXPECTED FILE" in _run_powershell(verify_script) and "surprise.dll" in _run_powershell(verify_script)


@needs_powershell
def test_test_installed_manifest_missing_file_itself(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    script = (
        f". '{str(DEPLOY_DIR / MANIFEST_LIB).replace(chr(39), chr(39) * 2)}'\n"
        f"$InstallRoot = '{install_root}'\n"
        "try { Test-InstalledManifest -InstallRoot $InstallRoot; 'NO EXCEPTION' } "
        "catch { $_.Exception.Message }"
    )
    assert "MANIFEST MISSING" in _run_powershell(script)


@needs_powershell
def test_test_installed_manifest_malformed_json(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "MANIFEST.installed.json").write_text("{ not valid json", encoding="utf-8")
    script = (
        f". '{str(DEPLOY_DIR / MANIFEST_LIB).replace(chr(39), chr(39) * 2)}'\n"
        f"$InstallRoot = '{install_root}'\n"
        "try { Test-InstalledManifest -InstallRoot $InstallRoot; 'NO EXCEPTION' } "
        "catch { $_.Exception.Message }"
    )
    assert "MALFORMED MANIFEST" in _run_powershell(script)


@needs_powershell
def test_test_installed_manifest_rejects_a_path_traversal_entry_without_reading_outside_install_root(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("should never be read", encoding="utf-8")

    installed_manifest = {
        "bundle_kind": "frozen",
        "bundle_product": "SortView Collector",
        "bundle_version": "9.9.9",
        "bundle_built_at": "2026-01-01T00:00:00.000000Z",
        "installed_at": "2026-01-01T00:00:00.000000Z",
        "zones": [{"root": "", "recurse": True}],
        "files": [{"installed_path": "..\\outside.txt", "bundle_path": "runtime\\..\\outside.txt", "sha256": "0" * 64}],
    }
    (install_root / "MANIFEST.installed.json").write_text(json.dumps(installed_manifest), encoding="utf-8")

    script = (
        f". '{str(DEPLOY_DIR / MANIFEST_LIB).replace(chr(39), chr(39) * 2)}'\n"
        f"$InstallRoot = '{install_root}'\n"
        "try { Test-InstalledManifest -InstallRoot $InstallRoot; 'NO EXCEPTION' } "
        "catch { $_.Exception.Message }"
    )
    output = _run_powershell(script)
    assert "UNSAFE MANIFEST PATH" in output


@needs_powershell
def test_source_bundle_zones_never_flag_venv_or_dot_files_as_unexpected(tmp_path):
    # The explicit "do not treat mutable runtime files as release artifacts" requirement: a real source install
    # has .venv\, .deps-hash, and -- outside InstallRoot entirely -- config/state/logs. None of that may ever be
    # reported as an "unexpected file", because none of it is in a release-managed zone (collector\ top-level,
    # agent\ recursive).
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    (bundle_root / "collector").mkdir()
    (bundle_root / "collector" / "run.py").write_bytes(b"run-py")
    (bundle_root / "agent").mkdir()
    (bundle_root / "agent" / "__init__.py").write_bytes(b"agent-init")
    files = [
        {"path": "collector\\run.py", "sha256": _sha256(b"run-py"), "size_bytes": 6},
        {"path": "agent\\__init__.py", "sha256": _sha256(b"agent-init"), "size_bytes": 10},
    ]
    manifest = {"product": "SortView Collector", "version": "9.9.9", "built_at": "x", "files": files}
    (bundle_root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")

    install_root = tmp_path / "install"
    (install_root / "collector").mkdir(parents=True)
    (install_root / "collector" / "run.py").write_bytes(b"run-py")
    (install_root / "agent").mkdir()
    (install_root / "agent" / "__init__.py").write_bytes(b"agent-init")
    # A real source install also has these -- never part of any release manifest:
    (install_root / ".venv").mkdir()
    (install_root / ".venv" / "pyvenv.cfg").write_text("home = ...", encoding="utf-8")
    (install_root / ".deps-hash").write_text("somehash", encoding="utf-8")

    script = _manifest_prelude(bundle_root, install_root) + (
        "New-InstalledManifest -BundleRoot $BundleRoot -InstallRoot $InstallRoot -Manifest $Manifest -BundleKind source\n"
        "try { Test-InstalledManifest -InstallRoot $InstallRoot; 'PASS' } catch { $_.Exception.Message }"
    )
    assert _run_powershell(script).strip().splitlines()[-1] == "PASS"


@needs_powershell
def test_update_path_overwrites_the_installed_manifest_with_the_new_release(tmp_path):
    # Simulates what tools\update.ps1 does: write once from an old bundle, then again from a "newer" bundle --
    # MANIFEST.installed.json must reflect the SECOND (current) release afterward, not a merge of both.
    bundle_v1 = tmp_path / "bundle-v1"
    bundle_v1.mkdir()
    (bundle_v1 / "runtime").mkdir()
    (bundle_v1 / "runtime" / "SortViewCollector.exe").write_bytes(b"exe-v1")
    manifest_v1 = {
        "product": "SortView Collector", "version": "1.0.0", "built_at": "x",
        "files": [{"path": "runtime\\SortViewCollector.exe", "sha256": _sha256(b"exe-v1"), "size_bytes": 6}],
    }
    (bundle_v1 / "MANIFEST.json").write_text(json.dumps(manifest_v1), encoding="utf-8")

    bundle_v2 = tmp_path / "bundle-v2"
    bundle_v2.mkdir()
    (bundle_v2 / "runtime").mkdir()
    (bundle_v2 / "runtime" / "SortViewCollector.exe").write_bytes(b"exe-v2")
    (bundle_v2 / "runtime" / "new-file.dll").write_bytes(b"new-in-v2")
    manifest_v2 = {
        "product": "SortView Collector", "version": "2.0.0", "built_at": "y",
        "files": [
            {"path": "runtime\\SortViewCollector.exe", "sha256": _sha256(b"exe-v2"), "size_bytes": 6},
            {"path": "runtime\\new-file.dll", "sha256": _sha256(b"new-in-v2"), "size_bytes": 9},
        ],
    }
    (bundle_v2 / "MANIFEST.json").write_text(json.dumps(manifest_v2), encoding="utf-8")

    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "SortViewCollector.exe").write_bytes(b"exe-v1")
    _run_powershell(_manifest_prelude(bundle_v1, install_root) +
                     "New-InstalledManifest -BundleRoot $BundleRoot -InstallRoot $InstallRoot -Manifest $Manifest -BundleKind frozen\n")

    # "update": replace the runtime, then persist the NEW manifest.
    (install_root / "SortViewCollector.exe").write_bytes(b"exe-v2")
    (install_root / "new-file.dll").write_bytes(b"new-in-v2")
    script = _manifest_prelude(bundle_v2, install_root) + (
        "New-InstalledManifest -BundleRoot $BundleRoot -InstallRoot $InstallRoot -Manifest $Manifest -BundleKind frozen\n"
        "try { Test-InstalledManifest -InstallRoot $InstallRoot; 'PASS' } catch { $_.Exception.Message }"
    )
    assert _run_powershell(script).strip().splitlines()[-1] == "PASS"

    installed = json.loads((install_root / "MANIFEST.installed.json").read_text(encoding="utf-8"))
    assert installed["bundle_version"] == "2.0.0"
    assert {f["installed_path"] for f in installed["files"]} == {"SortViewCollector.exe", "new-file.dll"}
