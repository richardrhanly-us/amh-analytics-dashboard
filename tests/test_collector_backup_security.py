"""1.0.15: restricted, verified upgrade backups and a verified rollback.

Why: on NBPL's production sorter (2026-11-16) the update backup `C:\\SortView\\Collector.backup-20261002-081805`
inherited Authenticated Users Modify from `C:\\SortView`, because tools\\update.ps1 made its backup with a plain
Copy-Item into that parent; and the printed rollback copied the backup, unverified, into the folder a SYSTEM task
runs. See docs/PROJECT_MANIFEST.md (canonical NBPL production sorter profile).

Tiers, as in tests/test_collector_acl_and_manifest.py:
  1. Static: the order and shape of tools\\update.ps1 (collector/deploy/update-release.ps1) and
     tools\\restore-backup.ps1. Run everywhere.
  2. @needs_powershell / @needs_windows: the real CollectorBackup.ps1 functions against throwaway folders --
     integrity, redirect (junction/link), pre-existing paths, the trust anchor, and the abort-before-replacing
     sequencing (with the ACL step shadowed, which needs no elevation).
  3. @needs_elevated_windows: creating a restricted backup for real (the test account must be able to write into
     and read back a folder restricted to SYSTEM+Administrators). Skipped, with that reason, in a non-elevated
     session. The full update and restore scripts (real task, real frozen runtime, preflight) need a representative
     VM and are not run here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess  # nosec B404 - fixed executables, test-only
import sys
import tempfile
from pathlib import Path

import pytest

from collector import build_release

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO_ROOT / "collector" / "deploy"

BACKUP_LIB = "CollectorBackup.ps1"
RESTORE_SCRIPT = "restore-backup.ps1"
UPDATE_SCRIPT = "update-release.ps1"


def _read(name: str) -> str:
    return (DEPLOY_DIR / name).read_text(encoding="utf-8")


def _body(text: str) -> str:
    end = text.find("#>")
    return text[end + 2:] if end != -1 else text


def _code_only(text: str) -> str:
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def _is_elevated() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


needs_powershell = pytest.mark.skipif(_powershell() is None, reason="no PowerShell available -- the static tests still run")
needs_windows = pytest.mark.skipif(
    sys.platform != "win32" or _powershell() is None or shutil.which("icacls") is None,
    reason="Windows-only: junctions, NTFS ACLs and icacls",
)
needs_elevated_windows = pytest.mark.skipif(
    not _is_elevated() or _powershell() is None,
    reason="needs an elevated Windows session: the test must write into and read back a folder restricted to "
           "SYSTEM+Administrators -- run elevated, or on the representative VM",
)


def _run(script: str) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    if len(encoded) <= 24000:
        args = ["-EncodedCommand", encoded]
    else:
        path = Path(tempfile.mkdtemp()) / "script.ps1"
        path.write_text(script, encoding="utf-8")
        args = ["-ExecutionPolicy", "Bypass", "-File", str(path)]
    return subprocess.run(  # nosec B603 - fixed executable, no shell
        [_powershell(), "-NoProfile", "-NonInteractive", *args], capture_output=True, text=True, check=False
    )


def _ps(script: str) -> str:
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    return result.stdout


def _q(value: object) -> str:
    return str(value).replace("'", "''")


def _prelude() -> str:
    libs = ("CollectorAcl.ps1", "CollectorManifest.ps1", BACKUP_LIB)
    return "$ErrorActionPreference = 'Stop'\n" + "".join(f". '{_q(DEPLOY_DIR / lib)}'\n" for lib in libs)


def _junction(link: Path, target: Path) -> None:
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],  # nosec B603 B607
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Every file under `root` and its exact bytes -- to prove a folder is unchanged."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# --- a fake frozen release, installed the way install.ps1/update.ps1 install one --------------------------------------

RELEASE_FILES = {
    "runtime/SortViewCollector.exe": b"frozen exe 1.0.13",
    "runtime/_internal/base_library.zip": b"frozen library",
    "tools/update.ps1": b"# a bundle tool, never installed under the install root",
}


def _manifest_bytes(files: dict[str, bytes], version: str = "1.0.13") -> bytes:
    doc = {"product": "SortView Collector", "version": version, "built_at": "2026-10-02T00:33:47.407285Z",
           "files": [{"path": path, "sha256": _sha(data)} for path, data in sorted(files.items())]}
    return (json.dumps(doc, indent=2) + "\n").encode("ascii")


def _install(root: Path, files: dict[str, bytes] = RELEASE_FILES, *, extra: dict[str, bytes] | None = None) -> bytes:
    """A frozen install at `root`; returns its MANIFEST.json bytes (the release's own)."""
    root.mkdir(parents=True)
    manifest = _manifest_bytes(files)
    installed = []
    for path, data in files.items():
        if path.startswith("runtime/"):
            relative = path[len("runtime/"):]
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_bytes(data)
            installed.append({"installed_path": relative, "bundle_path": path, "sha256": _sha(data).upper()})
    (root / "MANIFEST.json").write_bytes(manifest)
    (root / "MANIFEST.installed.json").write_text(json.dumps(
        {"bundle_kind": "frozen", "bundle_version": "1.0.13", "zones": [{"root": "", "recurse": True}], "files": installed}))
    for relative, data in (extra or {}).items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(data)
    return manifest


# =====================================================================================================================
# 1. Static: shipped, wired, and in the right order
# =====================================================================================================================

def test_the_new_files_exist_and_ship_in_every_bundle():
    shipped = dict(build_release.DEPLOY_TOOL_FILES)
    assert shipped[f"collector/deploy/{BACKUP_LIB}"] == f"tools/{BACKUP_LIB}"
    assert shipped[f"collector/deploy/{RESTORE_SCRIPT}"] == f"tools/{RESTORE_SCRIPT}"
    assert (DEPLOY_DIR / BACKUP_LIB).is_file()
    assert (DEPLOY_DIR / RESTORE_SCRIPT).is_file()


def test_update_and_restore_dot_source_all_three_libraries():
    for name in (UPDATE_SCRIPT, RESTORE_SCRIPT):
        code = _code_only(_read(name))
        for lib in ("CollectorAcl.ps1", "CollectorManifest.ps1", BACKUP_LIB):
            assert f'. (Join-Path $PSScriptRoot "{lib}")' in code, (name, lib)


def test_update_refuses_a_taken_or_redirected_backup_path_before_touching_the_task():
    body = _body(_read(UPDATE_SCRIPT))
    disable = body.index("Disable-ScheduledTask")
    assert body.index("Assert-CollectorPathNotRedirected -Path $InstallRoot") < disable
    assert body.index("Assert-CollectorBackupPathAvailable -Path $BackupRoot") < disable


def test_update_backup_failure_stops_before_the_install_is_changed():
    body = _body(_read(UPDATE_SCRIPT))
    step2 = body.index("=== 2. Back up current runtime files ===")
    failure = body.index("BACKUP FAILED", step2)
    # The whole backup (both kinds) is inside one try; its catch fails the update, task left disabled ...
    assert body.index("try {", step2) < body.index("New-ProtectedCollectorDirectory -Path $BackupRoot") < failure
    catch = body[body.rindex("} catch {", step2, failure):failure]
    assert "Restore-DisabledTaskAndFail" in catch
    # ... and every change to the install root comes after it.
    for mutation in ("Remove-Item -Recurse -Force $InstallRoot", 'Remove-Item -Recurse -Force (Join-Path $InstallRoot "collector")',
                     'Move-Item -Path $InstallRoot -Destination "$BackupRoot-full" -Force'):
        assert failure < body.index(mutation), mutation


def test_update_source_backup_is_created_restricted_and_verified_before_any_runtime_change():
    body = _body(_read(UPDATE_SCRIPT))
    create = body.index("New-ProtectedCollectorDirectory -Path $BackupRoot")
    copy = body.index('New-Item -ItemType Directory -Path (Join-Path $BackupRoot "collector")')
    verify = body.index("Complete-ProtectedCollectorBackup -SourceRoot $InstallRoot -BackupRoot $BackupRoot -RelativePaths $backedUp")
    mutate = body.index('Remove-Item -Recurse -Force (Join-Path $InstallRoot "collector")')
    assert create < copy < verify < mutate


def test_update_full_rebuild_folder_is_checked_then_restricted_before_the_rebuild():
    body = _body(_read(UPDATE_SCRIPT))
    rebuild = body[body.index("requirements.txt changed"):body.index("=== 4. Verify the new runtime")]
    check = rebuild.index('Assert-CollectorBackupPathAvailable -Path "$BackupRoot-full"')
    move = rebuild.index('Move-Item -Path $InstallRoot -Destination "$BackupRoot-full" -Force')
    protect = rebuild.index('Protect-CollectorBackupTree -Path "$BackupRoot-full"')
    rebuild_start = rebuild.index('New-Item -ItemType Directory -Path (Join-Path $InstallRoot "collector")')
    assert check < move < protect < rebuild_start


def test_update_recreates_the_frozen_install_root_restricted_never_reusing_a_planted_folder():
    body = _body(_read(UPDATE_SCRIPT))
    frozen = body[body.index("Replace frozen runtime"):body.index("=== 3. Dependency check ===")]
    remove = frozen.index("Remove-Item -Recurse -Force $InstallRoot")
    create = frozen.index("New-ProtectedCollectorDirectory -Path $InstallRoot")
    copy = frozen.index('Copy-Item (Join-Path $FrozenRuntimeDir "*")')
    assert remove < create < copy
    assert "New-Item -ItemType Directory -Path $InstallRoot -Force" not in frozen


def test_restore_requires_elevation_and_one_trust_anchor_before_anything_else():
    body = _body(_read(RESTORE_SCRIPT))
    elevation = body.index("IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)")
    anchor = body.index("Give exactly one trust anchor")
    assert elevation < anchor < body.index("=== 1. Verify the backup")
    assert "if ($hasHash -eq $hasPath)" in body  # neither, or both, is refused
    assert "a backup cannot vouch for itself" in body  # -TrustedManifestPath inside the backup is refused


def test_restore_verifies_everything_before_stopping_the_task_and_changing_the_install():
    body = _body(_read(RESTORE_SCRIPT))
    order = [
        "Get-CollectorTreeFiles -Root $BackupRoot",
        "Test-CollectorPathProtected -Path $BackupRoot -Recurse",
        "Test-CollectorOwnerTrusted -Path $BackupRoot -Recurse",
        "Get-CollectorTrustedBackup -BackupRoot $BackupRoot -TrustedManifestSha256 $trustedSha",
        "Disable-ScheduledTask -TaskName $TaskName",
        "Move-Item -LiteralPath $InstallRoot -Destination $ReplacedRoot",
        "New-ProtectedCollectorDirectory -Path $InstallRoot",
        "Copy-Item -LiteralPath (Join-Path $BackupRoot $relative) -Destination $destination",
        "Protect-CollectorPath -Path $InstallRoot -Recurse",
        "Test-InstalledManifest -InstallRoot $InstallRoot",
        'preflight --config $ConfigPath',
    ]
    positions = [body.index(step) for step in order]
    assert positions == sorted(positions), list(zip(order, positions))
    # Collection resumes only after preflight, only if it was enabled, and never as an immediate run.
    assert body.rindex("Enable-ScheduledTask -TaskName $TaskName | Out-Null") > body.index('preflight --config $ConfigPath')
    assert "if ($wasRegistered -and $wasEnabled)" in body
    assert "Start-ScheduledTask" not in _code_only(body)


def test_restore_failure_leaves_collection_disabled_with_recovery_steps():
    body = _body(_read(RESTORE_SCRIPT))
    start = body.index("function Stop-RestoreWithCollectionDisabled")
    function = body[start:body.index("\n}\n", start)]
    assert "left DISABLED" in function
    assert "exit 1" in function
    assert 'Move-Item `"$ReplacedRoot`" `"$InstallRoot`"' in function  # how to return to the replaced install
    assert 'verify-install.ps1`" -InstallRoot `"$InstallRoot`"' in function  # verifies THAT install, not the default
    for line in function.splitlines():
        if "Enable-ScheduledTask" in line:
            assert line.strip().startswith("Write-Host"), line  # printed for the operator, never run here
    # Everything from moving the install aside to preflight is inside the try that calls it.
    tried = body[body.index("try {\n    Write-Host \"=== 3."):body.index("} catch {\n    Stop-RestoreWithCollectionDisabled")]
    assert "Move-Item -LiteralPath $InstallRoot" in tried and "preflight --config" in tried


def test_restore_copies_only_verified_release_files_and_never_trusts_the_backups_own_record():
    body = _code_only(_body(_read(RESTORE_SCRIPT)))
    copies = [line.strip() for line in body.splitlines() if "Copy-Item" in line]
    assert copies == [
        "Copy-Item -LiteralPath (Join-Path $BackupRoot $relative) -Destination $destination",
        'Copy-Item -LiteralPath (Join-Path $BackupRoot "MANIFEST.json") -Destination (Join-Path $InstallRoot "MANIFEST.json") -Force',
    ]
    assert "foreach ($entry in @($trusted.Files))" in body
    lib = _read(BACKUP_LIB)
    trusted_fn = lib[lib.index("function Get-CollectorTrustedBackup"):]
    assert "MANIFEST.installed.json" not in _code_only(_body(trusted_fn))


@pytest.mark.parametrize("name", [UPDATE_SCRIPT, RESTORE_SCRIPT, BACKUP_LIB])
def test_data_root_is_never_touched_config_secrets_rules_and_cursors_preserved(name):
    # The data root (config, secrets\api_token.dpapi and the v2 key, config\classification_rules.json, the cursors
    # under data\) is never a target: no path under it appears, and $ConfigPath is only ever handed to preflight.
    default = '[string]$ConfigPath = "C:\\ProgramData\\SortViewCollector\\config\\collector_config.json"'
    code = _code_only(_body(_read(name))).replace(default, "")  # the parameter's default is the only other mention
    for forbidden in ("$DataRoot", "secrets", "classification_rules", "api_token", "v2_key", "state.json", "cursor",
                      "ProgramData"):
        assert forbidden not in code, (name, forbidden)
    for line in code.splitlines():
        if "$ConfigPath" in line:
            assert "preflight" in line, (name, line)


# =====================================================================================================================
# 2. The functions, for real, against throwaway folders (no elevation needed)
# =====================================================================================================================

@needs_powershell
@pytest.mark.parametrize("name", [BACKUP_LIB, RESTORE_SCRIPT, UPDATE_SCRIPT])
def test_scripts_parse_without_errors(name):
    script = (
        "$errs = $null; $tokens = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{_q(DEPLOY_DIR / name)}', [ref]$tokens, [ref]$errs); "
        "$errs.Count"
    )
    assert _ps(script).strip() == "0"


def _call(expression: str) -> str:
    return _ps(_prelude() + f"try {{ {expression}; 'OK' }} catch {{ 'ERR:' + $_.Exception.Message }}").strip()


@needs_windows
def test_redirect_check_accepts_a_plain_path_and_refuses_a_junction_and_anything_under_one(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _junction(link, real)
    assert _call(f"Assert-CollectorPathNotRedirected -Path '{_q(real / 'not-yet')}'") == "OK"
    assert _call(f"Assert-CollectorPathNotRedirected -Path '{_q(link)}'").startswith("ERR:REDIRECTED PATH")
    assert _call(f"Assert-CollectorPathNotRedirected -Path '{_q(link / 'Collector.backup-1')}'").startswith("ERR:REDIRECTED PATH")


@needs_windows
def test_redirect_check_refuses_a_symbolic_link(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "symlink"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"this account cannot create symbolic links: {error}")
    assert _call(f"Assert-CollectorPathNotRedirected -Path '{_q(link)}'").startswith("ERR:REDIRECTED PATH")


@needs_windows
@pytest.mark.parametrize("planted", ["folder", "file", "junction", "dangling_junction"])
def test_a_backup_path_that_is_already_taken_is_refused_and_left_alone(tmp_path, planted):
    parent = tmp_path / "SortView"
    parent.mkdir()
    path = parent / "Collector.backup-20261120-091500"
    if planted == "folder":
        path.mkdir()
        (path / "planted.dll").write_bytes(b"x")
    elif planted == "file":
        path.write_bytes(b"x")
    else:
        target = tmp_path / "elsewhere"
        target.mkdir()
        _junction(path, target)
        if planted == "dangling_junction":
            target.rmdir()
    for function in ("Assert-CollectorBackupPathAvailable", "New-ProtectedCollectorDirectory"):
        assert _call(f"{function} -Path '{_q(path)}'").startswith("ERR:ALREADY EXISTS"), function
    if planted == "folder":
        assert (path / "planted.dll").read_bytes() == b"x"  # refused, never reused or cleaned


@needs_windows
def test_a_backup_path_under_a_redirected_parent_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "SortView"
    _junction(link, real)
    assert _call(f"New-ProtectedCollectorDirectory -Path '{_q(link / 'Collector.backup-1')}'").startswith("ERR:REDIRECTED PATH")
    assert not any(real.iterdir())


def _problems(expression: str) -> list[str]:
    out = _ps(_prelude() + f"@({expression}) | ForEach-Object {{ 'P:' + $_ }}")
    return [line[2:] for line in out.splitlines() if line.startswith("P:")]


@needs_powershell
def test_a_faithful_copy_has_no_problems_and_every_kind_of_difference_is_one(tmp_path):
    source = tmp_path / "Collector"
    _install(source, extra={"logs/parser.acs.log": b"pre-1.0.14 parser log"})
    backup = tmp_path / "backup"
    shutil.copytree(source, backup)
    check = f"Test-CollectorBackupCopy -SourceRoot '{_q(source)}' -BackupRoot '{_q(backup)}'"
    assert _problems(check) == []

    (backup / "SortViewCollector.exe").write_bytes(b"tampered")
    (backup / "_internal" / "base_library.zip").unlink()
    (backup / "_internal" / "planted.dll").write_bytes(b"x")
    problems = _problems(check)
    assert "COPY DIFFERS FROM THE INSTALL: SortViewCollector.exe" in problems
    assert "NOT COPIED: _internal/base_library.zip" in problems
    assert "UNEXPECTED FILE IN BACKUP: _internal/planted.dll" in problems


@needs_powershell
def test_a_copy_check_limited_to_relative_paths_still_refuses_anything_else(tmp_path):
    source = tmp_path / "src"
    (source / "collector").mkdir(parents=True)
    (source / "collector" / "run.py").write_bytes(b"code")
    (source / ".venv").mkdir()
    (source / ".venv" / "python.exe").write_bytes(b"not backed up")
    backup = tmp_path / "backup"
    (backup / "collector").mkdir(parents=True)
    (backup / "collector" / "run.py").write_bytes(b"code")
    check = f"Test-CollectorBackupCopy -SourceRoot '{_q(source)}' -BackupRoot '{_q(backup)}' -RelativePaths @('collector/run.py')"
    assert _problems(check) == []
    (backup / "collector" / "extra.py").write_bytes(b"x")
    assert _problems(check) == ["UNEXPECTED FILE IN BACKUP: collector/extra.py"]


@needs_windows
def test_a_junction_inside_a_backup_is_a_problem_and_never_followed(tmp_path):
    source = tmp_path / "Collector"
    _install(source)
    backup = tmp_path / "backup"
    shutil.copytree(source, backup)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.dll").write_bytes(b"x")
    _junction(backup / "_internal" / "linked", outside)
    problems = _problems(f"Test-CollectorBackupCopy -SourceRoot '{_q(source)}' -BackupRoot '{_q(backup)}'")
    assert len(problems) == 1 and problems[0].startswith("REPARSE POINT")
    assert not any("evil.dll" in problem for problem in problems)


@needs_powershell
def test_recorded_release_files_are_checked_and_unrecorded_files_are_allowed(tmp_path):
    root = tmp_path / "Collector"
    _install(root, extra={"logs/parser.acs.log": b"NBPL's install root holds these (pre-1.0.14 parsers)"})
    check = f"Test-CollectorRecordedFiles -Root '{_q(root)}'"
    assert _problems(check) == []
    (root / "SortViewCollector.exe").write_bytes(b"tampered")
    assert _problems(check) == ["HASH MISMATCH: SortViewCollector.exe"]
    (root / "MANIFEST.installed.json").unlink()
    assert _problems(check)[0].startswith("MANIFEST MISSING: MANIFEST.installed.json")


def _complete(source: Path, backup: Path, protect: str = "") -> str:
    # The ACL step is shadowed (no elevation here); everything after it -- the comparison, the recorded-file check
    # and the decision to throw -- is the real code.
    shadow = "function Protect-CollectorBackupTree { param([string]$Path) " + protect + " }\n"
    return _ps(_prelude() + shadow + (
        f"try {{ Complete-ProtectedCollectorBackup -SourceRoot '{_q(source)}' -BackupRoot '{_q(backup)}' -RequireReleaseManifest; 'OK' }} "
        "catch { 'ERR:' + $_.Exception.Message }")).strip()


@needs_powershell
def test_complete_backup_passes_a_faithful_copy_and_aborts_on_tampering_or_an_acl_failure(tmp_path):
    source = tmp_path / "Collector"
    _install(source, extra={"logs/parser.acs.log": b"log"})
    backup = tmp_path / "backup"
    shutil.copytree(source, backup)
    assert _complete(source, backup) == "OK"

    assert _complete(source, backup, protect="throw 'BACKUP NOT PROTECTED: test'") == "ERR:BACKUP NOT PROTECTED: test"

    (backup / "_internal" / "base_library.zip").write_bytes(b"tampered")
    result = _complete(source, backup)
    assert result.startswith("ERR:BACKUP FAILED VERIFICATION")
    assert "COPY DIFFERS FROM THE INSTALL: _internal/base_library.zip" in result
    assert "HASH MISMATCH: _internal/base_library.zip" in result


@needs_powershell
def test_complete_backup_refuses_an_install_without_release_manifest_records(tmp_path):
    source = tmp_path / "Collector"
    _install(source)
    (source / "MANIFEST.installed.json").unlink()
    backup = tmp_path / "backup"
    shutil.copytree(source, backup)
    assert "MANIFEST MISSING: MANIFEST.installed.json" in _complete(source, backup)


@needs_windows
def test_protecting_a_backup_tree_refuses_a_junction_before_changing_any_acl(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _junction(backup / "linked", outside)
    assert _call(f"Protect-CollectorBackupTree -Path '{_q(backup)}'").startswith("ERR:BACKUP REDIRECTED")
    assert _ps(f"(Get-Acl -LiteralPath '{_q(outside)}').AreAccessRulesProtected").strip() == "False"


def _trusted(backup: Path, sha: str) -> dict:
    out = _ps(_prelude() + (
        f"$r = Get-CollectorTrustedBackup -BackupRoot '{_q(backup)}' -TrustedManifestSha256 '{sha}'\n"
        "[pscustomobject]@{ problems = @($r.Problems); files = @($r.Files | ForEach-Object { $_.installed_path }); "
        "unlisted = @($r.Unlisted) } | ConvertTo-Json -Compress"))
    return json.loads(out)


@needs_powershell
def test_a_backup_matching_the_trusted_release_is_restorable_and_unreleased_files_are_left_out(tmp_path):
    backup = tmp_path / "backup"
    manifest = _install(backup, extra={"logs/parser.acs.log": b"log"})
    result = _trusted(backup, _sha(manifest).upper())
    assert result["problems"] == []
    assert sorted(result["files"]) == ["SortViewCollector.exe", "_internal/base_library.zip"]
    assert result["unlisted"] == ["logs/parser.acs.log"]


@needs_powershell
def test_the_backups_own_installed_record_is_never_trusted(tmp_path):
    backup = tmp_path / "backup"
    manifest = _install(backup)
    (backup / "MANIFEST.installed.json").write_text('{"files": [], "zones": []}')  # a doctored self-description
    assert _trusted(backup, _sha(manifest))["problems"] == []
    (backup / "SortViewCollector.exe").write_bytes(b"tampered")
    assert _trusted(backup, _sha(manifest))["problems"] == ["ALTERED: SortViewCollector.exe differs from the trusted release"]


@needs_powershell
def test_an_altered_backup_is_refused_even_when_its_manifest_was_rewritten_to_match(tmp_path):
    backup = tmp_path / "backup"
    trusted_sha = _sha(_install(backup))
    doctored = dict(RELEASE_FILES, **{"runtime/SortViewCollector.exe": b"tampered"})
    (backup / "SortViewCollector.exe").write_bytes(b"tampered")
    (backup / "MANIFEST.json").write_bytes(_manifest_bytes(doctored))
    problems = _trusted(backup, trusted_sha)["problems"]
    assert len(problems) == 1 and problems[0].startswith("MANIFEST NOT TRUSTED")


@needs_powershell
@pytest.mark.parametrize("anchor, expected", [("not-a-hash", "TRUSTED HASH MALFORMED"), ("0" * 64, "MANIFEST NOT TRUSTED")])
def test_a_wrong_or_malformed_trust_anchor_is_refused(tmp_path, anchor, expected):
    backup = tmp_path / "backup"
    _install(backup)
    assert _trusted(backup, anchor)["problems"][0].startswith(expected)


@needs_powershell
def test_a_backup_missing_a_release_file_is_refused(tmp_path):
    backup = tmp_path / "backup"
    manifest = _install(backup)
    (backup / "_internal" / "base_library.zip").unlink()
    assert _trusted(backup, _sha(manifest))["problems"] == ["MISSING FROM BACKUP: _internal/base_library.zip"]


@needs_powershell
def test_a_source_release_backup_is_refused(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    manifest = _manifest_bytes({"collector/run.py": b"x", "agent/parser/acs.py": b"y"})
    (backup / "MANIFEST.json").write_bytes(manifest)
    assert _trusted(backup, _sha(manifest))["problems"][0].startswith("NOT A FROZEN RELEASE")


@needs_windows
def test_a_redirected_backup_is_refused_by_the_trust_check(tmp_path):
    backup = tmp_path / "backup"
    manifest = _install(backup)
    outside = tmp_path / "outside"
    outside.mkdir()
    _junction(backup / "_internal" / "linked", outside)
    problems = _trusted(backup, _sha(manifest))["problems"]
    assert len(problems) == 1 and problems[0].startswith("REPARSE POINT")


@needs_windows
def test_an_nbpl_shaped_backup_reads_as_insecure(tmp_path):
    # The 2026-11-16 production shape: a backup inheriting Authenticated Users Modify from its parent, owned by
    # whoever made it. Both checks restore-backup.ps1 applies before anything else must reject it.
    parent = tmp_path / "SortView"
    parent.mkdir()
    _ps(
        f"$acl = Get-Acl -LiteralPath '{_q(parent)}'\n"
        "$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("
        "'Authenticated Users', 'Modify', 'ContainerInherit, ObjectInherit', 'None', 'Allow')))\n"
        f"Set-Acl -LiteralPath '{_q(parent)}' -AclObject $acl"
    )
    backup = parent / "Collector.backup-20261002-081805"
    _install(backup)
    out = _ps(_prelude() + (
        f"Test-CollectorPathProtected -Path '{_q(backup)}' -Recurse\n"
        f"Test-CollectorOwnerTrusted -Path '{_q(backup)}' -Recurse"
    )).split()
    assert out == ["False", "False"]


@needs_windows
def test_restore_refuses_to_run_unelevated_and_changes_nothing(tmp_path):
    if _is_elevated():
        pytest.skip("elevated session: the script would proceed to the real Scheduled Task")
    install = tmp_path / "Collector"
    _install(install)
    backup = tmp_path / "Collector.backup-1"
    manifest = _install(backup)
    result = _run(
        f"& '{_q(DEPLOY_DIR / RESTORE_SCRIPT)}' -BackupRoot '{_q(backup)}' -InstallRoot '{_q(install)}' "
        f"-ExpectedManifestSha256 '{_sha(manifest)}'"
    )
    assert result.returncode != 0
    assert "elevated (Administrator)" in result.stdout + result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Collector", "Collector.backup-1"]
    assert (install / "SortViewCollector.exe").read_bytes() == RELEASE_FILES["runtime/SortViewCollector.exe"]


# =====================================================================================================================
# 3. Elevated: a real restricted backup in an NBPL-shaped parent
# =====================================================================================================================

@needs_elevated_windows
def test_a_backup_made_in_an_nbpl_shaped_parent_is_restricted_owned_and_verified(tmp_path):
    parent = tmp_path / "SortView"
    parent.mkdir()
    install = parent / "Collector"
    _install(install, extra={"logs/parser.acs.log": b"log"})
    data_root = tmp_path / "ProgramData" / "SortViewCollector"
    preserved = {"config/collector_config.json": b"{}", "config/classification_rules.json": b"{}",
                 "secrets/api_token.dpapi": b"token", "data/v2_state.json": b"cursor"}
    for relative, data in preserved.items():
        (data_root / relative).parent.mkdir(parents=True, exist_ok=True)
        (data_root / relative).write_bytes(data)
    backup = parent / "Collector.backup-20261120-091500"
    source_before = _tree_bytes(install)
    try:
        out = _ps(_prelude() + (
            f"$acl = Get-Acl -LiteralPath '{_q(parent)}'\n"
            "$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("
            "'Authenticated Users', 'Modify', 'ContainerInherit, ObjectInherit', 'None', 'Allow')))\n"
            f"Set-Acl -LiteralPath '{_q(parent)}' -AclObject $acl\n"
            f"New-ProtectedCollectorDirectory -Path '{_q(backup)}'\n"
            f"foreach ($item in @(Get-ChildItem -LiteralPath '{_q(install)}' -Force)) "
            f"{{ Copy-Item -LiteralPath $item.FullName -Destination '{_q(backup)}' -Recurse -Force }}\n"
            f"Complete-ProtectedCollectorBackup -SourceRoot '{_q(install)}' -BackupRoot '{_q(backup)}' -RequireReleaseManifest\n"
            f"Test-CollectorPathProtected -Path '{_q(backup)}' -Recurse\n"
            f"Test-CollectorOwnerTrusted -Path '{_q(backup)}' -Recurse"
        )).split()
        assert out[-2:] == ["True", "True"]
        for relative, data in preserved.items():
            assert (data_root / relative).read_bytes() == data, relative
        assert _tree_bytes(install) == source_before  # making the backup leaves the installation it copies unchanged
    finally:
        _run(f"icacls '{_q(tmp_path)}' /reset /T /C *>$null")


# What a real machine has, read-only: the top level of the production paths (names, timestamps, ACL) and the real
# Scheduled Task's state. The rollback test below must leave all of it exactly as it found it.
_REAL_STATE = (
    "foreach ($p in 'C:\\SortView', 'C:\\ProgramData\\SortViewCollector') {\n"
    "  if (Test-Path -LiteralPath $p) { $p; (Get-Acl -LiteralPath $p).Sddl\n"
    "    Get-ChildItem -LiteralPath $p -Force | Sort-Object Name | ForEach-Object { $_.Name + '|' + $_.LastWriteTimeUtc.Ticks } }\n"
    "  else { \"$p absent\" } }\n"
    "$t = Get-ScheduledTask -TaskName 'SortView Collector' -ErrorAction SilentlyContinue\n"
    "if ($t) { 'task|' + $t.State + '|' + $t.Settings.Enabled } else { 'task absent' }\n"
)

ORIGINAL_FILES = {  # the installation being rolled back -- deliberately not the backup's release
    "runtime/SortViewCollector.exe": b"frozen exe 1.0.15, the installation in place before the rollback",
    "runtime/_internal/base_library.zip": b"frozen library 1.0.15",
    "runtime/_internal/added_in_1_0_15.dll": b"a file only the newer release has",
}


@needs_elevated_windows
def test_a_rollback_that_fails_after_moving_the_install_aside_keeps_it_intact_and_collection_disabled(tmp_path):
    # Everything is disposable and under tmp_path: the install, its backup, the configuration path. The Scheduled
    # Task cmdlets are SHADOWED by recording functions (PowerShell resolves a function before a cmdlet), so the
    # script believes an ENABLED task exists -- and, belt and braces, the task name it is given is a unique one no
    # real task has. The failure is real: the restored SortViewCollector.exe is not a runnable program, so preflight
    # fails AFTER the original installation was moved aside and the backup restored.
    task_name = f"SortView Collector rollback test {tmp_path.name}"
    assert "task absent" in _ps(f"if (Get-ScheduledTask -TaskName '{_q(task_name)}' -ErrorAction SilentlyContinue) {{ 'task exists' }} else {{ 'task absent' }}")
    real_before = _ps(_REAL_STATE)

    parent = tmp_path / "SortView"
    install = parent / "Collector"
    _install(install, ORIGINAL_FILES)
    original = _tree_bytes(install)
    backup = parent / "Collector.backup-20261120-091500"
    manifest = _install(backup, extra={"logs/parser.acs.log": b"pre-1.0.14 parser log, never restored"})
    _ps(_prelude() + f"Protect-CollectorBackupTree -Path '{_q(backup)}'")  # a backup as 1.0.15 makes one
    config = tmp_path / "config" / "collector_config.json"

    try:
        out = _ps(
            "$global:TaskCalls = New-Object System.Collections.Generic.List[string]\n"
            "function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $global:TaskCalls.Add(\"Get:$TaskName\")\n"
            "  [pscustomobject]@{ State = 'Ready'; Settings = [pscustomobject]@{ Enabled = $true } } }\n"
            "function Disable-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $global:TaskCalls.Add(\"Disable:$TaskName\") }\n"
            "function Enable-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $global:TaskCalls.Add(\"Enable:$TaskName\") }\n"
            "function Stop-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $global:TaskCalls.Add(\"Stop:$TaskName\") }\n"
            "function Start-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $global:TaskCalls.Add(\"Start:$TaskName\") }\n"
            f"try {{ & '{_q(DEPLOY_DIR / RESTORE_SCRIPT)}' -BackupRoot '{_q(backup)}' -InstallRoot '{_q(install)}' "
            f"-ConfigPath '{_q(config)}' -TaskName '{_q(task_name)}' -ExpectedManifestSha256 '{_sha(manifest)}' *>&1 | "
            "ForEach-Object { 'OUT:' + $_ } } catch { 'THROWN:' + $_.Exception.Message }\n"
            "'EXIT:' + $LASTEXITCODE\n"
            "$global:TaskCalls | ForEach-Object { 'CALL:' + $_ }\n"
        )
        lines = out.splitlines()
        printed = [line[4:] for line in lines if line.startswith("OUT:")]
        calls = [line[5:] for line in lines if line.startswith("CALL:")]
        text = "\n".join(printed)

        # It failed, after moving the install aside and restoring -- at preflight -- and said so.
        assert "EXIT:1" in lines, out
        assert not any(line.startswith("THROWN:") for line in lines), out
        assert "Moved '" in text and "Restored 2 release file(s)" in text, text
        assert "RESTORE FAILED" in text and "preflight" in text.lower(), text
        # Collection was stopped and NOT re-enabled (or started).
        assert f"Disable:{task_name}" in calls, calls
        assert not any(call.startswith(("Enable:", "Start:")) for call in calls), calls
        assert "Collection is left DISABLED." in text and "Re-enabled" not in text, text

        # The original installation is intact, unchanged, in its .replaced-* folder.
        replaced = [p for p in parent.iterdir() if p.name.startswith("Collector.replaced-")]
        assert len(replaced) == 1, [p.name for p in parent.iterdir()]
        assert _tree_bytes(replaced[0]) == original
        assert f"preserved, unchanged, at:\n  {replaced[0]}" in text, text

        # The printed recovery is usable: run it exactly as printed, and the original installation is back.
        recovery = printed[printed.index("To return to it:") + 1:printed.index("To return to it:") + 4]
        assert [line.split()[0] for line in recovery] == ["if", "Move-Item", "&"], recovery
        assert f'-InstallRoot "{install}"' in recovery[2]  # verifies THIS install, never the default path
        verified = _ps("$ErrorActionPreference = 'Stop'\n" + "\n".join(recovery) +
                       "\nif ($LASTEXITCODE -ne 0) { throw 'verify-install.ps1 failed' }")
        assert f"VERIFIED: {install} matches its recorded release manifest." in verified, verified
        assert _tree_bytes(install) == original
        assert not any(p.name.startswith("Collector.replaced-") for p in parent.iterdir())
        assert backup.exists()  # the backup itself is never consumed or changed by a failed restore

        # Nothing real was touched: the production paths' top level, their ACLs, the real task, the test task name.
        assert _ps(_REAL_STATE) == real_before
        assert "task absent" in _ps(f"if (Get-ScheduledTask -TaskName '{_q(task_name)}' -ErrorAction SilentlyContinue) {{ 'task exists' }} else {{ 'task absent' }}")
    finally:
        _run(f"icacls '{_q(tmp_path)}' /reset /T /C *>$null")
