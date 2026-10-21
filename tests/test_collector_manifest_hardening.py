"""Regression tests for the M2-E1 hardening of the Collector's manifest verification (F1, F3).

Before the fix, Windows PowerShell 5.1's `Get-ChildItem -Recurse -File` (no -Force) skipped hidden files and files in
hidden folders, and never descended into a junction -- so a bundle could hold content its verifier never saw, which the
runtime copy (`Copy-Item -Recurse -Force`) then installed. These tests drive the REAL verifiers against generated
fixture bundles in pytest's temporary folders (never a real release, install or machine setting):

  * install.ps1 `-VerifyBundleOnly`, end to end (its own read-only mode);
  * `Test-ReleaseManifest` from install.ps1 AND update.ps1, each loaded alone with the PowerShell parser -- update.ps1
    has no verify-only mode (it needs elevation, then disables the Scheduled Task), so its workflow is never run;
  * `Test-InstalledManifest` (collector/deploy/CollectorManifest.ps1, a library with no load-time effects), over an
    install folder whose MANIFEST.installed.json the real `New-InstalledManifest` wrote.

"Runtime" files are plain bytes; nothing in a fixture is ever executed.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "collector" / "deploy"
WINDOWS_POWERSHELL = shutil.which("powershell")

# The bug is Windows PowerShell 5.1's own enumeration behavior, and hidden attributes and junctions are Windows features.
needs_windows_powershell = pytest.mark.skipif(
    sys.platform != "win32" or WINDOWS_POWERSHELL is None, reason="Windows PowerShell 5.1 on Windows only"
)

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4


def _set_attributes(path: Path, attributes: int) -> None:
    current = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), current | attributes), path


def _junction(link: Path, target: Path) -> None:
    # mklink /J: needs no privilege, and (unlike New-Item) accepts a target that does not exist.
    completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _remove_links(root: Path) -> None:
    # Junctions removed as links only (os.rmdir never follows one), so cleanup can never reach a target.
    for dirpath, dirnames, _ in os.walk(root, topdown=True, followlinks=False):
        for name in list(dirnames):
            path = Path(dirpath) / name
            if _is_reparse(path):
                os.rmdir(path)
                dirnames.remove(name)


def _is_reparse(path: Path) -> bool:
    return bool(os.lstat(path).st_file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# =====================================================================================================================
# Fixture bundles
# =====================================================================================================================

def _write_bundle(root: Path, *, extra_entries: list[dict] | None = None) -> Path:
    """A minimal frozen bundle: the real install.ps1 and the two libraries it dot-sources, an example config, and
    plain-byte "runtime" files. The manifest lists every file present when it is written (hidden ones included)."""
    (root / "tools").mkdir(parents=True)
    (root / "runtime" / "_internal").mkdir(parents=True)
    shutil.copyfile(DEPLOY / "install-release.ps1", root / "install.ps1")
    shutil.copyfile(DEPLOY / "CollectorAcl.ps1", root / "tools" / "CollectorAcl.ps1")
    shutil.copyfile(DEPLOY / "CollectorManifest.ps1", root / "tools" / "CollectorManifest.ps1")
    (root / "collector_config.example.json").write_text('{"api_url": "https://fixture.invalid"}', encoding="utf-8")
    (root / "runtime" / "SortViewCollector.exe").write_bytes(b"fixture: not a program")
    (root / "runtime" / "_internal" / "library.dll").write_bytes(b"fixture: not a library")
    (root / "runtime" / "_internal" / "module.pyd").write_bytes(b"fixture: not an extension")
    _write_manifest(root, extra_entries)
    return root


def _write_manifest(root: Path, extra_entries: list[dict] | None = None) -> None:
    files = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "MANIFEST.json"):
        data = path.read_bytes()
        files.append({"path": path.relative_to(root).as_posix(), "sha256": _sha256(data), "size_bytes": len(data)})
    files += extra_entries or []
    manifest = {"product": "SortView Collector", "version": "0.0.0-fixture", "built_at": "2026-01-01T00:00:00Z", "files": files}
    (root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


def _bundle_cases(base: Path) -> dict[str, Path]:
    cases: dict[str, Path] = {}

    def case(name: str, **kwargs) -> Path:
        cases[name] = _write_bundle(base / name, **kwargs)
        return cases[name]

    case("clean")
    (case("unlisted_visible") / "runtime" / "_internal" / "planted.dll").write_bytes(b"planted")

    planted = case("unlisted_hidden") / "runtime" / "_internal" / "planted.dll"
    planted.write_bytes(b"planted")
    _set_attributes(planted, FILE_ATTRIBUTE_HIDDEN)

    planted = case("unlisted_system") / "runtime" / "_internal" / "planted.dll"
    planted.write_bytes(b"planted")
    _set_attributes(planted, FILE_ATTRIBUTE_SYSTEM)

    hidden_dir = case("file_in_hidden_folder") / "runtime" / "_internal" / "sub"
    hidden_dir.mkdir()
    (hidden_dir / "planted.dll").write_bytes(b"planted")
    _set_attributes(hidden_dir, FILE_ATTRIBUTE_HIDDEN)

    # A hidden file that IS listed is legitimate and must still verify.
    root = base / "listed_hidden"
    _write_bundle(root)
    (root / "runtime" / "_internal" / "hidden-but-listed.dll").write_bytes(b"listed")
    _set_attributes(root / "runtime" / "_internal" / "hidden-but-listed.dll", FILE_ATTRIBUTE_HIDDEN)
    _write_manifest(root)
    cases["listed_hidden"] = root

    outside = base / "outside-the-bundle"
    outside.mkdir()
    (outside / "from-outside.dll").write_bytes(b"from outside")
    _junction(case("unlisted_junction") / "runtime" / "_internal" / "linked", outside)

    # Dangling: if the verifier ever tried to enter a junction, this would fail with a path error, not a clean refusal.
    _junction(case("dangling_junction") / "runtime" / "_internal" / "linked", base / "does-not-exist")

    root = base / "listed_junction"
    _write_bundle(root, extra_entries=[{"path": "runtime/_internal/linked/from-outside.dll", "sha256": _sha256(b"from outside"),
                                        "size_bytes": 12}])
    _junction(root / "runtime" / "_internal" / "linked", outside)
    cases["listed_junction"] = root

    real = _write_bundle(base / "real-bundle")
    _junction(base / "bundle_root_is_a_junction", real)
    cases["bundle_root_is_a_junction"] = base / "bundle_root_is_a_junction"

    root = base / "listed_brackets"
    _write_bundle(root)
    (root / "runtime" / "_internal" / "data[1].txt").write_bytes(b"brackets")
    _write_manifest(root)
    cases["listed_brackets"] = root

    case("wildcard_entry", extra_entries=[{"path": "runtime/_internal/*.dll", "sha256": "0" * 64, "size_bytes": 0}])
    (case("missing_file") / "runtime" / "_internal" / "library.dll").unlink()
    (case("modified_file") / "runtime" / "_internal" / "library.dll").write_bytes(b"modified after the build")

    root = case("unlisted_file_symlink")
    try:
        os.symlink(outside / "from-outside.dll", root / "runtime" / "_internal" / "link.dll")
    except OSError:
        cases["unlisted_file_symlink"] = None  # this session may not create symbolic links: reported as skipped
    return cases


# Runs Test-ReleaseManifest, loaded ALONE from each script with the PowerShell parser (no workflow), under the same
# strict mode and error preference the scripts run with; and install.ps1 -VerifyBundleOnly end to end.
_RELEASE_SCRIPT = r"""
param([string]$CasesPath, [string]$OutPath, [string]$InstallScript, [string]$UpdateScript)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$cases = Get-Content -LiteralPath $CasesPath -Raw | ConvertFrom-Json
$out = [ordered]@{}
foreach ($source in @(@{ name = 'install'; path = $InstallScript }, @{ name = 'update'; path = $UpdateScript })) {
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($source.path, [ref]$null, [ref]$null)
    $definition = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Test-ReleaseManifest' }, $true)
    . ([scriptblock]::Create($definition.Extent.Text))
    foreach ($case in $cases.PSObject.Properties) {
        $messages = @()
        $verdict = try {
            Test-ReleaseManifest -BundleRoot $case.Value 6>&1 | ForEach-Object { $messages += [string]$_ }
            'VERIFIED'
        } catch { 'REFUSED: ' + $_.Exception.Message }
        $out["$($source.name):$($case.Name)"] = [ordered]@{ verdict = $verdict; messages = $messages }
    }
}
foreach ($case in $cases.PSObject.Properties) {
    $script = Join-Path $case.Value 'install.ps1'
    if (-not (Test-Path -LiteralPath $script)) { continue }
    $ErrorActionPreference = 'Continue'
    $text = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $script -VerifyBundleOnly 2>&1 | Out-String
    $ErrorActionPreference = 'Stop'
    $out["install_e2e:$($case.Name)"] = [ordered]@{ exit_code = $LASTEXITCODE; text = $text }
}
ConvertTo-Json -InputObject $out -Depth 5 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


def _run(script: str, tmp: Path, **params: str) -> dict:
    (tmp / "run.ps1").write_text(script, encoding="utf-8")
    args = [WINDOWS_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(tmp / "run.ps1")]
    for key, value in params.items():
        args += [f"-{key}", value]
    completed = subprocess.run(args, capture_output=True, text=True, timeout=600, check=False)
    out = tmp / "out.json"
    assert out.exists(), completed.stdout + completed.stderr
    return json.loads(out.read_text(encoding="utf-8-sig"))


@pytest.fixture(scope="module")
def release_results(tmp_path_factory) -> dict:
    if sys.platform != "win32" or WINDOWS_POWERSHELL is None:
        pytest.skip("Windows PowerShell 5.1 on Windows only")
    base = tmp_path_factory.mktemp("bundles")
    cases = _bundle_cases(base)
    runnable = {name: str(path) for name, path in cases.items() if path is not None}
    (base / "cases.json").write_text(json.dumps(runnable), encoding="utf-8")
    try:
        results = _run(_RELEASE_SCRIPT, base, CasesPath=str(base / "cases.json"), OutPath=str(base / "out.json"),
                       InstallScript=str(DEPLOY / "install-release.ps1"), UpdateScript=str(DEPLOY / "update-release.ps1"))
    finally:
        _remove_links(base)
    results["_skipped"] = [name for name, path in cases.items() if path is None]
    return results


VERIFIERS = ["install", "update"]


def _verdict(results: dict, verifier: str, case: str) -> str:
    return results[f"{verifier}:{case}"]["verdict"]


@needs_windows_powershell
@pytest.mark.parametrize("verifier", VERIFIERS)
@pytest.mark.parametrize("case", ["clean", "listed_hidden", "listed_brackets"])
def test_a_bundle_that_matches_its_manifest_verifies(release_results, verifier, case):
    # Includes a LISTED hidden file and a listed name with brackets: both are legitimate.
    assert _verdict(release_results, verifier, case) == "VERIFIED", release_results[f"{verifier}:{case}"]


@needs_windows_powershell
@pytest.mark.parametrize("verifier", VERIFIERS)
@pytest.mark.parametrize("case", ["unlisted_visible", "unlisted_hidden", "unlisted_system", "file_in_hidden_folder"])
def test_an_unlisted_file_is_refused_however_it_is_hidden(release_results, verifier, case):
    result = release_results[f"{verifier}:{case}"]
    assert result["verdict"].startswith("REFUSED"), result
    assert any("UNEXPECTED FILE" in m and "planted.dll" in m for m in result["messages"]), result


@needs_windows_powershell
@pytest.mark.parametrize("verifier", VERIFIERS)
@pytest.mark.parametrize("case", ["unlisted_junction", "dangling_junction", "listed_junction", "bundle_root_is_a_junction"])
def test_a_reparse_point_is_refused_before_anything_is_read_through_it(release_results, verifier, case):
    result = release_results[f"{verifier}:{case}"]
    assert result["verdict"].startswith("REFUSED"), result
    assert any("REPARSE POINT" in m for m in result["messages"]), result
    # Nothing was hashed or compared: a reparse point stops verification before any file is opened -- and the dangling
    # junction proves no attempt was made to enter one (that would have been a path error, not this clean refusal).
    assert not any(word in m for m in result["messages"] for word in ("HASH MISMATCH", "MISSING", "UNEXPECTED FILE")), result


@needs_windows_powershell
@pytest.mark.parametrize("verifier", VERIFIERS)
def test_a_wildcard_manifest_path_is_refused_cleanly(release_results, verifier):
    result = release_results[f"{verifier}:wildcard_entry"]
    assert result["verdict"].startswith("REFUSED: Refusing to proceed"), result  # the function's own refusal, no crash
    assert any("UNSAFE MANIFEST PATH: runtime/_internal/*.dll (a wildcard" in m for m in result["messages"]), result


@needs_windows_powershell
@pytest.mark.parametrize("verifier", VERIFIERS)
def test_a_missing_or_modified_file_is_still_refused(release_results, verifier):
    assert any("MISSING: runtime/_internal/library.dll" in m for m in release_results[f"{verifier}:missing_file"]["messages"])
    assert any("HASH MISMATCH: runtime/_internal/library.dll" in m for m in release_results[f"{verifier}:modified_file"]["messages"])


@needs_windows_powershell
@pytest.mark.parametrize("verifier", VERIFIERS)
def test_a_file_symbolic_link_is_refused_where_one_can_be_made(release_results, verifier):
    if "unlisted_file_symlink" in release_results["_skipped"]:
        pytest.skip("this session may not create symbolic links (no privilege or Developer Mode)")
    assert any("REPARSE POINT" in m for m in release_results[f"{verifier}:unlisted_file_symlink"]["messages"])


@needs_windows_powershell
def test_install_verify_bundle_only_end_to_end(release_results):
    def e2e(case: str) -> dict:
        return release_results[f"install_e2e:{case}"]

    assert e2e("clean")["exit_code"] == 0 and "Bundle verified (read-only)" in e2e("clean")["text"]
    assert e2e("listed_brackets")["exit_code"] == 0
    for case, expected in (("unlisted_hidden", "UNEXPECTED FILE"), ("file_in_hidden_folder", "UNEXPECTED FILE"),
                           ("unlisted_junction", "REPARSE POINT"), ("listed_junction", "REPARSE POINT"),
                           ("wildcard_entry", "UNSAFE MANIFEST PATH")):
        assert e2e(case)["exit_code"] != 0 and expected in e2e(case)["text"], (case, e2e(case))
    # The end-to-end run starts from install.ps1 inside the junctioned bundle root, so its own folder is the junction.
    assert e2e("bundle_root_is_a_junction")["exit_code"] != 0 and "REPARSE POINT" in e2e("bundle_root_is_a_junction")["text"]


# =====================================================================================================================
# Installed-file verification (Test-InstalledManifest)
# =====================================================================================================================

def _install_case(base: Path, name: str, *, kind: str = "frozen") -> tuple[Path, Path]:
    """A bundle and an install folder holding exactly what install.ps1 would copy, plus the MANIFEST.installed.json
    the real New-InstalledManifest writes (done in PowerShell, below)."""
    bundle, install = base / name / "bundle", base / name / "install"
    install.mkdir(parents=True)
    if kind == "frozen":
        _write_bundle(bundle)
        shutil.copytree(bundle / "runtime", install, dirs_exist_ok=True)
    else:
        (bundle / "collector").mkdir(parents=True)
        (bundle / "agent" / "parser").mkdir(parents=True)
        (bundle / "collector" / "main.py").write_bytes(b"# fixture")
        (bundle / "agent" / "parser" / "checkins.py").write_bytes(b"# fixture")
        _write_manifest(bundle)
        shutil.copytree(bundle / "collector", install / "collector")
        shutil.copytree(bundle / "agent", install / "agent")
    return bundle, install


def _installed_cases(base: Path) -> dict[str, dict]:
    cases: dict[str, dict] = {}

    def case(name: str, kind: str = "frozen") -> Path:
        bundle, install = _install_case(base, name, kind=kind)
        cases[name] = {"bundle": str(bundle), "install": str(install), "kind": kind}
        return install

    case("clean")
    case("clean_source", "source")
    for name in ("hidden_file", "file_in_hidden_folder", "junction", "dangling_junction", "install_root_is_a_junction",
                 "source_junction_in_agent", "source_hidden_file_in_collector", "source_pycache_folder"):
        case(name, "source" if name.startswith("source_") else "frozen")
    outside = base / "outside-the-install"
    outside.mkdir()
    (outside / "from-outside.dll").write_bytes(b"from outside")
    cases["_after"] = {"outside": str(outside)}
    return cases


def _plant_installed(base: Path, cases: dict) -> None:
    """Changes made AFTER New-InstalledManifest recorded the install: what a later check must notice."""
    outside = Path(cases["_after"]["outside"])
    install = {name: Path(c["install"]) for name, c in cases.items() if name != "_after"}
    planted = install["hidden_file"] / "_internal" / "planted.dll"
    planted.write_bytes(b"planted")
    _set_attributes(planted, FILE_ATTRIBUTE_HIDDEN)
    hidden_dir = install["file_in_hidden_folder"] / "_internal" / "sub"
    hidden_dir.mkdir()
    (hidden_dir / "planted.dll").write_bytes(b"planted")
    _set_attributes(hidden_dir, FILE_ATTRIBUTE_HIDDEN)
    _junction(install["junction"] / "_internal" / "linked", outside)
    _junction(install["dangling_junction"] / "_internal" / "linked", base / "does-not-exist")
    real = Path(cases["install_root_is_a_junction"]["install"])
    moved = real.with_name("install-real")
    real.rename(moved)
    _junction(real, moved)
    _junction(install["source_junction_in_agent"] / "agent" / "linked", outside)
    hidden = install["source_hidden_file_in_collector"] / "collector" / "planted.py"
    hidden.write_bytes(b"# planted")
    _set_attributes(hidden, FILE_ATTRIBUTE_HIDDEN)
    # Python's own cache folder under collector\ was never scanned (that zone is one level deep) -- still not.
    (install["source_pycache_folder"] / "collector" / "__pycache__").mkdir()
    (install["source_pycache_folder"] / "collector" / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"cache")


_INSTALLED_SCRIPT = r"""
param([string]$CasesPath, [string]$OutPath, [string]$Library, [string]$Phase)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. $Library
$cases = Get-Content -LiteralPath $CasesPath -Raw | ConvertFrom-Json
$out = [ordered]@{}
foreach ($case in $cases.PSObject.Properties) {
    if ($case.Name -eq '_after') { continue }
    $c = $case.Value
    if ($Phase -eq 'record') {
        $manifest = Get-Content -LiteralPath (Join-Path $c.bundle 'MANIFEST.json') -Raw | ConvertFrom-Json
        New-InstalledManifest -BundleRoot $c.bundle -InstallRoot $c.install -Manifest $manifest -BundleKind $c.kind 6>$null
        continue
    }
    $messages = @()
    $verdict = try {
        Test-InstalledManifest -InstallRoot $c.install 6>&1 | ForEach-Object { $messages += [string]$_ }
        'PASS'
    } catch { 'REFUSED: ' + $_.Exception.Message }
    $out[$case.Name] = [ordered]@{ verdict = $verdict; messages = $messages }
}
ConvertTo-Json -InputObject $out -Depth 5 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


@pytest.fixture(scope="module")
def installed_results(tmp_path_factory) -> dict:
    if sys.platform != "win32" or WINDOWS_POWERSHELL is None:
        pytest.skip("Windows PowerShell 5.1 on Windows only")
    base = tmp_path_factory.mktemp("installs")
    cases = _installed_cases(base)
    (base / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    try:
        _run(_INSTALLED_SCRIPT, base, CasesPath=str(base / "cases.json"), OutPath=str(base / "out.json"),
             Library=str(DEPLOY / "CollectorManifest.ps1"), Phase="record")
        _plant_installed(base, cases)
        return _run(_INSTALLED_SCRIPT, base, CasesPath=str(base / "cases.json"), OutPath=str(base / "out.json"),
                    Library=str(DEPLOY / "CollectorManifest.ps1"), Phase="verify")
    finally:
        _remove_links(base)


@needs_windows_powershell
@pytest.mark.parametrize("case", ["clean", "clean_source", "source_pycache_folder"])
def test_an_install_that_matches_its_record_passes(installed_results, case):
    assert installed_results[case]["verdict"] == "PASS", installed_results[case]


@needs_windows_powershell
@pytest.mark.parametrize("case", ["hidden_file", "file_in_hidden_folder", "source_hidden_file_in_collector"])
def test_an_unexpected_hidden_installed_file_is_refused(installed_results, case):
    result = installed_results[case]
    assert result["verdict"].startswith("REFUSED"), result
    assert any("UNEXPECTED FILE" in m and "planted" in m for m in result["messages"]), result


@needs_windows_powershell
@pytest.mark.parametrize("case", ["junction", "dangling_junction", "install_root_is_a_junction", "source_junction_in_agent"])
def test_a_reparse_point_in_the_install_is_refused_before_anything_is_read(installed_results, case):
    result = installed_results[case]
    assert result["verdict"].startswith("REFUSED"), result
    # In a zone: listed with the other problems. The install folder itself: refused before its record is even read.
    assert "REPARSE POINT" in result["verdict"] or any("REPARSE POINT" in m for m in result["messages"]), result
    assert not any(word in m for m in result["messages"] for word in ("HASH MISMATCH", "MISSING", "UNEXPECTED FILE")), result


# =====================================================================================================================
# Static: the hardened enumeration stays hardened
# =====================================================================================================================

def _function(path: Path, name: str) -> str:
    """The function's code, without its comments."""
    text = path.read_text(encoding="utf-8")
    start = text.index(f"function {name}")
    code = re.sub(r"<#.*?#>", "", text[start:text.index("\n}\n", start)], flags=re.DOTALL)
    return "\n".join(line for line in code.splitlines() if not line.strip().startswith("#"))


@pytest.mark.parametrize("script", ["install-release.ps1", "update-release.ps1"])
def test_release_verification_lists_everything_and_never_follows_a_reparse_point(script):
    body = _function(DEPLOY / script, "Test-ReleaseManifest")
    assert "Get-ChildItem -LiteralPath $item.FullName -Force" in body
    assert "-Recurse" not in body  # walked folder by folder, never through a reparse point
    assert "[System.IO.FileAttributes]::ReparsePoint" in body
    assert body.index("REPARSE POINT") < body.index("Get-FileHash")  # refused before any file is read
    assert "Test-Path -LiteralPath $filePath -PathType Leaf" in body and "Test-Path $" not in body
    assert "a wildcard, never a file name" in body


def test_installed_verification_lists_everything_and_never_follows_a_reparse_point():
    body = _function(DEPLOY / "CollectorManifest.ps1", "Test-InstalledManifest")
    assert "Get-ChildItem -LiteralPath $next.Item.FullName -Force" in body
    assert "-Recurse" not in body
    assert "[System.IO.FileAttributes]::ReparsePoint" in body
    assert body.index("REPARSE POINT") < body.index("Get-FileHash")
    assert "Test-Path -LiteralPath $filePath -PathType Leaf" in body and "Test-Path $" not in body
