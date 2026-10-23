"""File counts in the deployment scripts under Windows PowerShell 5.1 strict mode.

install.ps1 (collector/deploy/install-release.ps1) dot-sources tools\\CollectorAcl.ps1 and tools\\CollectorManifest.ps1,
which both run `Set-StrictMode -Version Latest` -- so strict mode applies to the rest of install.ps1, and to every other
script that loads them. Get-ChildItem returns a single FileInfo (not an array) when exactly one file matches, and Windows
PowerShell 5.1 under strict mode then refuses `.Count` on it ("The property 'Count' cannot be found on this object").
That stopped a test installation whose runtime\\ held one file (M2d-2b). A count must therefore always be taken of an
array: `@(Get-ChildItem ...).Count`.

  1. STATIC (run everywhere): no deployment script that runs in strict mode counts an unwrapped Get-ChildItem result.
  2. RUNTIME (Windows PowerShell): install.ps1's own two count statements, run under `Set-StrictMode -Version Latest`,
     give the right count for zero, one and several matching files.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "collector" / "deploy"
INSTALL_SOURCE = DEPLOY / "install-release.ps1"
STRICT_LIBRARIES = ("CollectorAcl.ps1", "CollectorManifest.ps1")

# A count of a Get-ChildItem result that is not wrapped in @(...): `(Get-ChildItem ...).Count`.
UNWRAPPED_COUNT = re.compile(r"(?<!@)\(\s*Get-ChildItem\b[^()]*\)\s*\.Count\b", re.IGNORECASE)

_WINDOWS_POWERSHELL = shutil.which("powershell")
needs_windows_powershell = pytest.mark.skipif(_WINDOWS_POWERSHELL is None, reason="needs Windows PowerShell 5.1")


def _code(path: Path) -> str:
    """The script without its comments (line comments and <# ... #> blocks)."""
    text = re.sub(r"<#.*?#>", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return "\n".join(line.split("#", 1)[0] if line.lstrip().startswith("#") else line for line in text.splitlines())


def _strict_mode_scripts() -> list[Path]:
    """The strict-mode libraries themselves, and every deployment script that dot-sources one of them."""
    scripts = []
    for path in sorted(DEPLOY.glob("*.ps1")):
        code = _code(path)
        if path.name in STRICT_LIBRARIES or "Set-StrictMode" in code or any(library in code for library in STRICT_LIBRARIES):
            scripts.append(path)
    return scripts


# =====================================================================================================================
# 1. STATIC
# =====================================================================================================================

def test_the_strict_mode_scripts_are_the_ones_expected():
    names = {path.name for path in _strict_mode_scripts()}
    assert {"install-release.ps1", "update-release.ps1", "repair-collector-permissions.ps1", "verify-install.ps1",
            *STRICT_LIBRARIES} <= names


def test_no_strict_mode_script_counts_an_unwrapped_get_childitem_result():
    offenders = {path.name: UNWRAPPED_COUNT.findall(_code(path)) for path in _strict_mode_scripts()}
    assert {name: found for name, found in offenders.items() if found} == {}


def test_the_pattern_catches_the_defect_and_accepts_the_fix():
    assert UNWRAPPED_COUNT.search("$n = (Get-ChildItem -Path $Dir -Recurse -File).Count")
    assert UNWRAPPED_COUNT.search('$n = ( Get-ChildItem -Path $Dir -Filter "*.py" -Recurse -File ).count')
    assert not UNWRAPPED_COUNT.search("$n = @(Get-ChildItem -Path $Dir -Recurse -File).Count")


# =====================================================================================================================
# 2. RUNTIME
# =====================================================================================================================

def _statement(variable: str) -> str:
    """install.ps1's own assignment to `variable`, exactly as written."""
    matches = re.findall(rf"^\s*(\${variable} = .+)$", INSTALL_SOURCE.read_text(encoding="utf-8"), re.MULTILINE)
    assert len(matches) == 1, variable
    return matches[0].strip()


def _count_under_strict_mode(tmp_path: Path, statement: str, variable: str, folder_variable: str, folder: Path) -> dict:
    script = tmp_path / "count.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "Set-StrictMode -Version Latest\n"
        f"${folder_variable} = '{folder}'\n"
        "$result = [ordered]@{ edition = $PSVersionTable.PSEdition; major = $PSVersionTable.PSVersion.Major }\n"
        f"try {{ {statement}; $result['count'] = ${variable} }} catch {{ $result['error'] = $_.Exception.Message }}\n"
        "ConvertTo-Json -Compress -InputObject $result\n",
        encoding="utf-8")
    completed = subprocess.run([_WINDOWS_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                               capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _folder(root: Path, files: list[str]) -> Path:
    root.mkdir(parents=True)
    for name in files:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"fixture")
    return root


@needs_windows_powershell
@pytest.mark.parametrize(("files", "expected"), [
    ([], 0),
    (["SortViewCollector.exe"], 1),  # the stand-in runtime that stopped M2d-2b
    (["SortViewCollector.exe", "_internal/base_library.zip", "_internal/python311.dll", "_internal/lib/x.pyd"], 4),
])
def test_the_frozen_runtime_count_is_right_under_strict_mode(tmp_path, files, expected):
    statement = _statement("frozenFileCount")
    assert statement == "$frozenFileCount = @(Get-ChildItem -Path $FrozenRuntimeDir -Recurse -File).Count"
    result = _count_under_strict_mode(tmp_path, statement, "frozenFileCount", "FrozenRuntimeDir", _folder(tmp_path / "runtime", files))
    assert (result["edition"], result["major"]) == ("Desktop", 5)  # Windows PowerShell 5.1, the target machines' shell
    assert result.get("count") == expected, result


@needs_windows_powershell
@pytest.mark.parametrize(("files", "expected"), [
    (["README.txt"], 0),
    (["parser/one.py", "README.txt"], 1),
    (["parser/one.py", "parser/two.py", "parser/sub/three.py", "README.txt"], 3),
])
def test_the_source_parser_count_is_right_under_strict_mode(tmp_path, files, expected):
    statement = _statement("parserFileCount")
    assert statement == '$parserFileCount = @(Get-ChildItem -Path $SourceAgentDir -Filter "*.py" -Recurse -File).Count'
    result = _count_under_strict_mode(tmp_path, statement, "parserFileCount", "SourceAgentDir", _folder(tmp_path / "agent", files))
    assert result.get("count") == expected, result


@needs_windows_powershell
def test_the_unwrapped_count_fails_under_strict_mode_for_one_file(tmp_path):
    """The defect itself, so the tests above are known to run in a shell where it occurs."""
    result = _count_under_strict_mode(tmp_path, "$frozenFileCount = (Get-ChildItem -Path $FrozenRuntimeDir -Recurse -File).Count",
                                      "frozenFileCount", "FrozenRuntimeDir", _folder(tmp_path / "runtime", ["SortViewCollector.exe"]))
    assert "count" not in result and "'Count' cannot be found" in result["error"]
