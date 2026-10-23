"""Tests for the build-time installation gate, M2d-1 Stage 1: installer/installation-gate.props, the SortViewInstallationGate
target in installer/SortView.Setup/SortView.Setup.csproj, and installer/SortView.Setup/InstallationGate.cs.

  1. STATIC (run everywhere): the gate is "false" by default and only its file sets it; the build refuses overrides,
     anything but exactly true/false, and "true" without a trusted bundle pin, and embeds the gate as AssemblyMetadata;
     the gate is read only from the program's own assembly and fails closed; nothing uses it yet (Stage 1), so the Setup
     page stays disabled and no engine is wired.
  2. BUILD (Windows): the default build embeds "gate:false" (disabled); a /p: override is refused; an environment
     variable is ignored; a gated build (of a COPY of the installer sources, never the real gate or pin files) embeds
     "gate:true" only with a pin; a malformed gate, or "true" without a pin, fails the build.
  3. READER (Windows): only exactly one "gate:true" enables; everything else is disabled or invalid.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_installer_bridge import _MSBUILD, _POWERSHELL, needs_bridge

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "installer"
SETUP_DIR = INSTALLER / "SortView.Setup"
SETUP_PROJECT = SETUP_DIR / "SortView.Setup.csproj"
GATE_FILE = INSTALLER / "installation-gate.props"
PIN_FILE = INSTALLER / "bundle-pin.props"
GATE_CODE = SETUP_DIR / "InstallationGate.cs"
MSBUILD_NS = "{http://schemas.microsoft.com/developer/msbuild/2003}"
PIN_SHA, PIN_VERSION = "b" * 64, "0.0.1-gatetest"


def _code(path: Path) -> str:
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def _properties(path: Path) -> dict:
    import xml.etree.ElementTree as ET

    root = ET.parse(path).getroot()
    return {child.tag.replace(MSBUILD_NS, ""): (child.text or "") for group in root for child in group}


# =====================================================================================================================
# 1. STATIC
# =====================================================================================================================

def test_the_gate_is_false_by_default_and_only_its_file_sets_it():
    assert _properties(GATE_FILE) == {"SortViewInstallationEnabled": "false"}
    assert _properties(PIN_FILE) == {"SortViewBundleManifestSha256": "", "SortViewBundleVersion": ""}  # still trusts no bundle
    # Nothing else in the installer names the gate property (no second place to set it).
    for path in INSTALLER.rglob("*"):
        if (path.is_file() and path.suffix in (".props", ".targets", ".csproj", ".cs", ".xaml") and "obj" not in path.parts
                and "bin" not in path.parts and path not in (GATE_FILE, SETUP_PROJECT)):
            assert "SortViewInstallationEnabled" not in path.read_text(encoding="utf-8"), path


def test_the_build_refuses_overrides_bad_values_and_an_unpinned_gate_and_embeds_it():
    project = re.sub(r"\s+", " ", SETUP_PROJECT.read_text(encoding="utf-8"))
    assert '<Import Project="..\\installation-gate.props" />' in project
    assert '<Target Name="SortViewInstallationGate" BeforeTargets="CoreCompile" DependsOnTargets="SortViewBundlePin">' in project
    assert project.count('XmlInputPath="$(MSBuildThisFileDirectory)..\\installation-gate.props"') == 1
    assert "'$(SortViewInstallationEnabled)' != '$(_SortViewGateFileValue)'" in project  # /p: or environment: refused
    # Case-sensitive: MSBuild's own comparisons ignore case, so "True" would otherwise pass.
    assert "!$([System.Text.RegularExpressions.Regex]::IsMatch('$(SortViewInstallationEnabled)', '^(true|false)$'))" in project
    assert ("'$(SortViewInstallationEnabled)' == 'true' and ('$(SortViewBundleManifestSha256)' == '' or "
            "'$(SortViewBundleVersion)' == '')") in project
    assert "<_Parameter1>SortView.InstallationEnabled</_Parameter1> <_Parameter2>gate:$(SortViewInstallationEnabled)</_Parameter2>" in project
    assert '<Compile Include="InstallationGate.cs" />' in project


def test_the_gate_is_read_only_from_the_programs_own_assembly_and_fails_closed():
    code = _code(GATE_CODE)
    assert "public static InstallationGate ForThisInstaller() => Read(typeof(InstallationGate).Assembly);" in code
    assert "internal InstallationGate(string[] values)" in code and "public InstallationGate(" not in code
    for forbidden in ("Environment.", "Registry", "GetCommandLineArgs", "File.", "Directory.", "ConfigurationManager", "AppSettings",
                      "Process", "PowerShell", "SetupEngineHost"):
        assert forbidden not in code, forbidden
    # Enabled ONLY by exactly one value equal (ordinal) to "gate:true".
    flat = re.sub(r"\s+", " ", code)
    assert "values == null || values.Length != 1 || values[0] == null" in flat
    assert 'string.Equals(values[0], ValuePrefix + "true", StringComparison.Ordinal)' in flat
    assert "public bool IsEnabled => State == InstallationGateState.Enabled;" in flat


def test_stage_1_uses_the_gate_nowhere_so_installation_stays_disabled():
    # Stage 1 adds the gate only. Nothing reads it yet: the Setup page keeps installation disabled and no engine is wired.
    # (Stage 2, wiring the runner behind the gate, replaces this test.)
    for path in list(SETUP_DIR.glob("*.cs")) + list(SETUP_DIR.glob("*.xaml")) + list((INSTALLER / "SortView.Setup.Wizard").glob("*.cs")):
        if path != GATE_CODE:
            assert "InstallationGate" not in _code(path), path
    window = (SETUP_DIR / "MainWindow.xaml").read_text(encoding="utf-8")
    assert window.count('IsInstallationEnabled="False"') == 1
    for name in ("MainWindow.xaml.cs", "App.xaml.cs", "BundlePreparation.cs", "PreparationStep.cs"):
        code = _code(SETUP_DIR / name)
        for forbidden in ("SetupEngineHost", "BeginRun", "SetupRunner", "SetupRunSession", "IsInstallationEnabled = true"):
            assert forbidden not in code, (name, forbidden)


# =====================================================================================================================
# 2. BUILD
# =====================================================================================================================

def _build(project: Path, *properties: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([_MSBUILD, str(project), "/p:Configuration=Release", "/nologo", "/v:minimal", *properties],
                          capture_output=True, text=True, check=False, env=env)


_READ_GATE = r"""
param([string]$ExePath)
$ErrorActionPreference = 'Stop'
$assembly = [Reflection.Assembly]::LoadFrom($ExePath)
$metadata = @($assembly.GetCustomAttributes([Reflection.AssemblyMetadataAttribute], $false) | Where-Object { $_.Key -like 'SortView.*' } |
              ForEach-Object { "$($_.Key)=$($_.Value)" })
$gate = $assembly.GetType('SortView.Setup.InstallationGate', $true).GetMethod('ForThisInstaller').Invoke($null, @())
$pin = $assembly.GetType('SortView.Setup.TrustedBundlePin', $true).GetMethod('ForThisInstaller').Invoke($null, @())
ConvertTo-Json -Compress -InputObject ([ordered]@{ metadata = $metadata; gate = $gate.State.ToString(); enabled = $gate.IsEnabled; pin = $pin.State.ToString() })
"""


def _read_gate(tmp: Path, exe: Path) -> dict:
    (tmp / "read_gate.ps1").write_text(_READ_GATE, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(tmp / "read_gate.ps1"),
                                "-ExePath", str(exe)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@needs_bridge
def test_the_default_build_embeds_a_disabled_gate(tmp_path):
    completed = _build(SETUP_PROJECT)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    read = _read_gate(tmp_path, SETUP_DIR / "bin" / "Release" / "SortView.Setup.exe")
    assert "SortView.InstallationEnabled=gate:false" in read["metadata"]
    assert [m for m in read["metadata"] if m.startswith("SortView.InstallationEnabled")] == ["SortView.InstallationEnabled=gate:false"]
    assert (read["gate"], read["enabled"], read["pin"]) == ("Disabled", False, "NotConfigured")


@needs_bridge
@pytest.mark.parametrize("value", ["true", "false", "TRUE", "1"])
def test_a_command_line_override_of_the_gate_is_refused(value):
    completed = _build(SETUP_PROJECT, f"/p:SortViewInstallationEnabled={value}")
    output = completed.stdout + completed.stderr
    if value == "false":
        assert completed.returncode == 0, output  # the same value as the file: nothing overridden
    else:
        assert completed.returncode != 0
        assert "is set outside installer\\installation-gate.props" in output


@needs_bridge
def test_an_environment_variable_cannot_enable_the_gate(tmp_path):
    # A project property (the gate file's) always wins over an environment variable of the same name: ignored, not used.
    env = dict(os.environ, SortViewInstallationEnabled="true")
    completed = _build(SETUP_PROJECT, env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    read = _read_gate(tmp_path, SETUP_DIR / "bin" / "Release" / "SortView.Setup.exe")
    assert (read["gate"], read["enabled"]) == ("Disabled", False)


@pytest.fixture(scope="module")
def installer_copy(tmp_path_factory) -> Path:
    """A copy of the four installer projects and the gate and pin files, so a gated build never touches the real ones."""
    copy = tmp_path_factory.mktemp("gate-copy") / "installer"
    for name in ("SortView.Setup", "SortView.Setup.Core", "SortView.Setup.Wizard", "SortView.Setup.Wpf"):
        shutil.copytree(INSTALLER / name, copy / name, ignore=shutil.ignore_patterns("bin", "obj", "Testing"))
    shutil.copyfile(GATE_FILE, copy / "installation-gate.props")
    shutil.copyfile(PIN_FILE, copy / "bundle-pin.props")
    return copy


def _configure_copy(copy: Path, gate: str, sha: str = "", version: str = "") -> None:
    text = GATE_FILE.read_text(encoding="utf-8")
    text = text.replace("<SortViewInstallationEnabled>false</SortViewInstallationEnabled>",
                        f"<SortViewInstallationEnabled>{gate}</SortViewInstallationEnabled>")
    (copy / "installation-gate.props").write_text(text, encoding="utf-8")
    pin = PIN_FILE.read_text(encoding="utf-8")
    pin = pin.replace("<SortViewBundleManifestSha256></SortViewBundleManifestSha256>",
                      f"<SortViewBundleManifestSha256>{sha}</SortViewBundleManifestSha256>")
    pin = pin.replace("<SortViewBundleVersion></SortViewBundleVersion>", f"<SortViewBundleVersion>{version}</SortViewBundleVersion>")
    (copy / "bundle-pin.props").write_text(pin, encoding="utf-8")


@needs_bridge
def test_a_reviewed_gated_and_pinned_build_embeds_an_enabled_gate(tmp_path, installer_copy):
    _configure_copy(installer_copy, "true", PIN_SHA, PIN_VERSION)
    completed = _build(installer_copy / "SortView.Setup" / "SortView.Setup.csproj")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    output = installer_copy / "SortView.Setup" / "bin" / "Release"
    read = _read_gate(tmp_path, output / "SortView.Setup.exe")
    assert sorted(read["metadata"]) == [f"SortView.BundleManifestSha256=pin:{PIN_SHA}", f"SortView.BundleVersion=pin:{PIN_VERSION}",
                                        "SortView.InstallationEnabled=gate:true"]
    assert (read["gate"], read["enabled"], read["pin"]) == ("Enabled", True, "Configured")
    # Still only the program and its three libraries: the gate adds no file.
    names = {p.name for p in output.iterdir() if p.is_file() and not p.name.endswith((".pdb", ".exe.config"))}
    assert names == {"SortView.Setup.exe", "SortView.Setup.Wizard.dll", "SortView.Setup.Wpf.dll", "SortView.Setup.Core.dll"}


@needs_bridge
@pytest.mark.parametrize(("gate", "sha", "version", "message"), [
    ("true", "", "", "Installation may be enabled only for a pinned bundle"),
    ("yes", "", "", "must be exactly true or false"),
    ("True", PIN_SHA, PIN_VERSION, "must be exactly true or false"),
    ("1", PIN_SHA, PIN_VERSION, "must be exactly true or false"),
    ("", "", "", "must be exactly true or false"),
])
def test_a_malformed_or_unpinned_gate_fails_the_build(installer_copy, gate, sha, version, message):
    _configure_copy(installer_copy, gate, sha, version)
    completed = _build(installer_copy / "SortView.Setup" / "SortView.Setup.csproj")
    assert completed.returncode != 0
    assert message in completed.stdout + completed.stderr


@needs_bridge
def test_a_pinned_build_with_the_gate_off_stays_disabled(tmp_path, installer_copy):
    # A pin alone (as in the M2c-3 test builds) never enables installation.
    _configure_copy(installer_copy, "false", PIN_SHA, PIN_VERSION)
    completed = _build(installer_copy / "SortView.Setup" / "SortView.Setup.csproj")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    read = _read_gate(tmp_path, installer_copy / "SortView.Setup" / "bin" / "Release" / "SortView.Setup.exe")
    assert (read["gate"], read["enabled"], read["pin"]) == ("Disabled", False, "Configured")


# =====================================================================================================================
# 3. READER
# =====================================================================================================================

_PARSE_GATE = r"""
param([string]$ExePath, [string]$CasesPath)
$ErrorActionPreference = 'Stop'
$type = [Reflection.Assembly]::LoadFrom($ExePath).GetType('SortView.Setup.InstallationGate', $true)
$ctor = $type.GetConstructor([Reflection.BindingFlags]'Instance, NonPublic', $null, [Type[]]@([string[]]), $null)
$cases = Get-Content -LiteralPath $CasesPath -Raw | ConvertFrom-Json
$out = [ordered]@{}
foreach ($case in $cases.PSObject.Properties) {
    # Assigned inside each branch: an if-expression's output would unroll a one-element array into a string.
    if ($null -eq $case.Value) { $values = $null } else { [string[]]$values = @($case.Value | ForEach-Object { $_ }) }
    $arguments = New-Object object[] 1
    $arguments[0] = $values  # one argument, a string array (PowerShell would otherwise unroll it)
    $gate = $ctor.Invoke($arguments)
    $out[$case.Name] = "$($gate.State)/$($gate.IsEnabled)"
}
ConvertTo-Json -Compress -InputObject $out
"""

READER_CASES = {
    "exactly_true": (["gate:true"], "Enabled/True"),
    "exactly_false": (["gate:false"], "Disabled/False"),
    "missing": ([], "Invalid/False"),
    "null": (None, "Invalid/False"),
    "repeated_true": (["gate:true", "gate:true"], "Invalid/False"),
    "true_and_false": (["gate:true", "gate:false"], "Invalid/False"),
    "uppercase": (["gate:TRUE"], "Invalid/False"),
    "no_prefix": (["true"], "Invalid/False"),
    "padded": ([" gate:true"], "Invalid/False"),
    "trailing_space": (["gate:true "], "Invalid/False"),
    "empty_value": (["gate:"], "Invalid/False"),
}


@needs_bridge
def test_only_exactly_one_gate_true_enables(tmp_path):
    completed = _build(SETUP_PROJECT)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    (tmp_path / "parse_gate.ps1").write_text(_PARSE_GATE, encoding="utf-8")
    (tmp_path / "cases.json").write_text(json.dumps({k: v for k, (v, _) in READER_CASES.items()}), encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(tmp_path / "parse_gate.ps1"),
                                "-ExePath", str(SETUP_DIR / "bin" / "Release" / "SortView.Setup.exe"), "-CasesPath", str(tmp_path / "cases.json")],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {k: expected for k, (_, expected) in READER_CASES.items()}
