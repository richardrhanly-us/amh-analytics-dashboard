"""Tests for the production installer program, Phase 4B.1: installer/SortView.Setup (+ the shared wizard,
installer/SortView.Setup.Wizard).

In this milestone the program is a foundation only: it requires administrator rights, shows the shared setup wizard with
installation DISABLED, and has no setup runner -- so it cannot install, enroll or change anything. These tests hold that:

  1. STATIC architecture checks (run everywhere): what the production program and the wizard may reference, the
     elevation manifest, and that the production program has no path that starts setup.
  2. BUILD and RUNTIME checks (Windows only): the build output holds only the program and its three libraries (no testing
     assembly, no simulation asset or endpoint), the elevation manifest is embedded and enforced by Windows, and the
     production window -- loaded in-process, with no elevation bypass -- shows the notice and refuses to start setup,
     changing nothing on the machine.

The wizard's behaviour with installation enabled is tested through the simulation host (tests/test_installer_window.py).
"""

from __future__ import annotations

import ctypes
import json
import re
import subprocess
import sys
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_installer_bridge import _MSBUILD, _POWERSHELL, needs_bridge

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "installer"
SETUP_DIR = INSTALLER / "SortView.Setup"
SETUP_PROJECT = SETUP_DIR / "SortView.Setup.csproj"
APP_MANIFEST = SETUP_DIR / "app.manifest"
WIZARD_DIR = INSTALLER / "SortView.Setup.Wizard"
WIZARD_PROJECT = WIZARD_DIR / "SortView.Setup.Wizard.csproj"
WIZARD_CODE = WIZARD_DIR / "SetupWizardView.xaml.cs"
PRODUCTION_SOURCES = [SETUP_DIR / "MainWindow.xaml", SETUP_DIR / "MainWindow.xaml.cs", SETUP_DIR / "App.xaml"]

DISABLED_NOTICE = "Installation is not yet enabled in this build. Nothing will be installed or changed on this computer."
MSBUILD_NS = {"m": "http://schemas.microsoft.com/developer/msbuild/2003"}
#: What the production build output may hold: the program and the three libraries it references (symbols allowed).
PRODUCTION_FILES = {"SortView.Setup.exe", "SortView.Setup.Wizard.dll", "SortView.Setup.Wpf.dll", "SortView.Setup.Core.dll"}
#: Simulation-only things that must never be in a production artifact.
SIMULATION_MARKERS = (b"SortView.Setup.Core.Testing", b"SimulatedSetupHarness", b"SimulationWorkspace", b"sortview-api.invalid",
                      b"SETUPTEST_", b"wrapper-overrides")


def _project(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def _project_references(path: Path) -> set[str]:
    return {Path(ref.get("Include").replace("\\", "/")).stem for ref in _project(path).iterfind(".//m:ProjectReference", MSBUILD_NS)}


def _property(path: Path, name: str) -> str | None:
    found = _project(path).find(f".//m:PropertyGroup/m:{name}", MSBUILD_NS)
    return None if found is None else found.text


# =====================================================================================
# 1. STATIC architecture checks
# =====================================================================================

def test_the_production_program_references_only_the_wizard_and_the_bridges():
    assert _project_references(SETUP_PROJECT) == {"SortView.Setup.Wizard", "SortView.Setup.Wpf", "SortView.Setup.Core"}
    project = _project(SETUP_PROJECT)
    assert project.find(".//m:EmbeddedResource", MSBUILD_NS) is None  # no simulation script, or anything else, embedded
    assert (_property(SETUP_PROJECT, "OutputType"), _property(SETUP_PROJECT, "TargetFrameworkVersion")) == ("WinExe", "v4.8")
    assert _property(SETUP_PROJECT, "ApplicationManifest") == "app.manifest"


def test_the_wizard_references_only_the_bridges():
    assert _project_references(WIZARD_PROJECT) == {"SortView.Setup.Core", "SortView.Setup.Wpf"}
    assert _project(WIZARD_PROJECT).find(".//m:EmbeddedResource", MSBUILD_NS) is None
    assert _property(WIZARD_PROJECT, "OutputType") == "Library"


def test_the_production_manifest_requires_administrator():
    level = next(el for el in ET.parse(APP_MANIFEST).getroot().iter() if el.tag.endswith("requestedExecutionLevel"))
    assert (level.get("level"), level.get("uiAccess")) == ("requireAdministrator", "false")


def _without_comments(text: str) -> str:
    """Source text minus C# line/doc comments and XML comments: a boundary is about code, not what a comment mentions."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def test_the_production_program_has_no_path_that_starts_setup():
    # Phase 4B.1: the production program hands the wizard no runner at all, so it has no way to run the engine. (Enabling
    # installation -- one runner over SetupEngineHost -- is a later milestone, which replaces this check.)
    sources = "\n".join(_without_comments(path.read_text(encoding="utf-8")) for path in PRODUCTION_SOURCES)
    for run_path in ("BeginRun", "SetupRunner", "SetupRunSession", "SetupEngineHost", "SimulatedSetupHarness", "StartRequested"):
        assert run_path not in sources, run_path
    window = (SETUP_DIR / "MainWindow.xaml").read_text(encoding="utf-8")
    assert 'IsInstallationEnabled="False"' in window
    assert f'DisabledNotice="{DISABLED_NOTICE}"' in window


def test_the_wizard_is_disabled_by_default_and_refuses_to_run_while_disabled():
    wizard = WIZARD_CODE.read_text(encoding="utf-8")
    assert re.search(r"private bool installationEnabled;", wizard)  # default false: a host must turn installation on
    begin = wizard[wizard.index("public void BeginRun("):]
    first_check = begin[begin.index("{"):begin.index("ArgumentNullException")]
    assert "if (!installationEnabled)" in first_check and "throw new InvalidOperationException" in first_check
    start = wizard[wizard.index("private void StartButton_Click"):]
    assert start.index("if (!installationEnabled)") < start.index("StartRequested?.Invoke")
    # The wizard never chooses what runs.
    for chooser in ("SetupEngineHost", "SimulatedSetupHarness", "SimulationWorkspace", "Core.Testing"):
        assert chooser not in wizard, chooser


# =====================================================================================
# 2. BUILD and RUNTIME checks
# =====================================================================================

@pytest.fixture(scope="module")
def production_build() -> Path:
    completed = subprocess.run([_MSBUILD, str(SETUP_PROJECT), "/p:Configuration=Release", "/nologo", "/v:minimal"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return SETUP_DIR / "bin" / "Release"


@needs_bridge
def test_the_production_build_holds_only_the_program_and_its_libraries(production_build):
    names = {path.name for path in production_build.iterdir() if path.is_file()}
    assert {name for name in names if not name.endswith((".pdb", ".exe.config"))} == PRODUCTION_FILES
    for path in production_build.iterdir():
        if path.is_file():
            content = path.read_bytes()
            if path.name == "SortView.Setup.Core.dll":
                # The ONE permitted mention: Core's [InternalsVisibleTo("SortView.Setup.Core.Testing")] attribute (Phase 3A,
                # AssemblyInfo.cs), which lets the unshipped harness reach Core's internal pre-run seam. It is metadata, not
                # a dependency: no testing code or file ships. Exactly one occurrence, so nothing else rides along.
                assert content.count(b"SortView.Setup.Core.Testing") == 1
                content = content.replace(b"SortView.Setup.Core.Testing", b"", 1)
            for marker in SIMULATION_MARKERS:
                assert marker not in content, (path.name, marker)


@needs_bridge
def test_the_built_program_embeds_the_administrator_manifest(production_build):
    program = (production_build / "SortView.Setup.exe").read_bytes()
    assert b'requestedExecutionLevel level="requireAdministrator" uiAccess="false"' in program
    assert not (production_build / "SortView.Setup.exe.manifest").exists()  # embedded, not a loose file


def _is_elevated() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin()) if sys.platform == "win32" else False


@needs_bridge
@pytest.mark.skipif(_is_elevated(), reason="from an elevated session Windows would start the program instead of refusing it")
def test_windows_refuses_to_start_the_program_without_elevation(production_build):
    # A plain CreateProcess (no shell, so no UAC prompt) of a requireAdministrator program fails with
    # ERROR_ELEVATION_REQUIRED (740): the manifest is in force.
    with pytest.raises(OSError) as refused:
        process = subprocess.Popen([str(production_build / "SortView.Setup.exe")])
        process.kill()  # (only reached if Windows started it after all -- which would be a failure)
    assert getattr(refused.value, "winerror", None) == 740


# Loads the production window IN-PROCESS on a test UI thread (no new process, so no elevation is involved), shows it
# off-screen, goes to the Setup page, inspects the wizard, tries to start setup both ways -- the Start button and
# BeginRun -- and closes it.
# Around that it records the machine state setup would change, to show nothing did.
_PROBE = r"""
param([string]$ExePath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
# Whether a path exists: 'present', 'absent', or 'access_denied' when this session may not look -- e.g. an installed
# Collector's secrets folder, from a session without administrator rights. Only existence is asked: nothing is opened,
# read or changed. Any other error still fails the probe.
function Get-PathState([string]$Path) {
    try { if (Test-Path -LiteralPath $Path -ErrorAction Stop) { 'present' } else { 'absent' } }
    catch [System.UnauthorizedAccessException] { 'access_denied' }
}
function Get-MachineState {
    [ordered]@{
        install_root = (Get-PathState 'C:\SortView\Collector'); data_root = (Get-PathState 'C:\ProgramData\SortViewCollector')
        task = ($null -ne (Get-ScheduledTask -TaskName 'SortView Collector' -ErrorAction SilentlyContinue))
        token = (Get-PathState 'C:\ProgramData\SortViewCollector\secrets\api_token.dpapi')
    }
}
Add-Type -ReferencedAssemblies PresentationFramework, PresentationCore, WindowsBase, System.Xaml -TypeDefinition @"
using System; using System.Collections.Generic; using System.Reflection; using System.Threading;
using System.Windows; using System.Windows.Automation; using System.Windows.Controls; using System.Windows.Controls.Primitives;
using System.Windows.Threading;
public static class ProductionProbe {
    static void Pump() {
        var frame = new DispatcherFrame();
        Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() => frame.Continue = false));
        Dispatcher.PushFrame(frame);
    }
    static void Click(FrameworkElement button) { button.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); Pump(); }
    // Gets to the Setup page the way a user does: Next past Welcome, the checks (on facts that pass, injected because a test
    // session is not elevated -- MachineFacts.Read is never called here), then Next past Ready.
    static void GoToSetupPage(Window window) {
        var checks = (FrameworkElement)window.FindName("PrerequisitesView");
        var factsType = checks.GetType().Assembly.GetType("SortView.Setup.Wizard.MachineFacts", true);
        var facts = Activator.CreateInstance(factsType);
        var values = new Dictionary<string, object> {
            { "WindowsBuild", 19045 }, { "WindowsInstallationType", "Client" }, { "Is64BitOperatingSystem", true },
            { "DotNetRelease", 533325 }, { "PowerShellVersion", "5.1.19041.1" }, { "IsAdministrator", true },
            { "SystemDriveFreeBytes", 100L << 30 }, { "LegacyTasksRead", true }, { "MachinePolicyRead", true }, { "UserPolicyRead", true } };
        foreach (var pair in values) factsType.GetProperty(pair.Key).SetValue(facts, pair.Value);
        factsType.GetProperty("LegacyTasks").SetValue(facts, Array.CreateInstance(factsType.Assembly.GetType("SortView.Setup.Wizard.LegacyTaskFact", true), 0));
        var read = System.Linq.Expressions.Expression.Lambda(typeof(Func<>).MakeGenericType(factsType), System.Linq.Expressions.Expression.Constant(facts, factsType)).Compile();
        checks.GetType().GetProperty("ReadFacts").SetValue(checks, read);
        Click((FrameworkElement)window.FindName("NextButton"));
        for (int i = 0; i < 1000 && !(bool)checks.GetType().GetProperty("CanContinue").GetValue(checks); i++) { Thread.Sleep(10); Pump(); }
        Click((FrameworkElement)window.FindName("NextButton"));
        Click((FrameworkElement)window.FindName("NextButton"));
    }
    public static Dictionary<string, object> Run(string exePath) {
        var r = new Dictionary<string, object>();
        var thread = new Thread(() => {
            try {
                var window = (Window)Activator.CreateInstance(Assembly.LoadFrom(exePath).GetType("SortView.Setup.MainWindow", true));
                window.WindowStartupLocation = WindowStartupLocation.Manual; window.Left = -32000; window.Top = -32000; window.ShowActivated = false;
                window.Show(); Pump();
                GoToSetupPage(window);
                r["page"] = window.GetType().GetProperty("CurrentPage").GetValue(window).ToString();
                var wizard = (FrameworkElement)window.FindName("Wizard");
                var type = wizard.GetType();
                Func<string, FrameworkElement> find = name => (FrameworkElement)wizard.FindName(name);
                r["wizard_type"] = type.FullName;
                r["installation_enabled"] = type.GetProperty("IsInstallationEnabled").GetValue(wizard);
                r["notice"] = ((TextBlock)find("InstallationNotice")).Text;
                r["notice_visible"] = find("InstallationNotice").IsVisible;
                var ids = new List<string>();
                foreach (var name in new[] { "StartButton", "CancelButton", "StatusText", "CancelNote", "PromptPanel", "EventList", "DiagnosticList" })
                    if (find(name) != null && AutomationProperties.GetAutomationId(find(name)) == "" && find(name).Name == name) ids.Add(name);
                r["automation_ids"] = ids;
                r["start_enabled"] = find("StartButton").IsEnabled;
                r["cancel_enabled"] = find("CancelButton").IsEnabled;
                r["prompt_visible"] = find("PromptPanel").IsVisible;
                int requested = 0;
                type.GetEvent("StartRequested").AddEventHandler(wizard, new EventHandler((s, e) => requested++));
                find("StartButton").RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); Pump();  // even a forced click
                r["start_requested"] = requested;
                r["running_after_click"] = type.GetProperty("IsRunning").GetValue(wizard);
                try { type.GetMethod("BeginRun").Invoke(wizard, new object[] { null, null }); r["begin_run"] = "ACCEPTED"; }
                catch (TargetInvocationException ex) { r["begin_run"] = ex.InnerException.GetType().Name; }
                r["running_after_begin_run"] = type.GetProperty("IsRunning").GetValue(wizard);
                window.Close(); Pump();
                r["closed"] = !window.IsVisible;
            } catch (Exception ex) { r["error"] = ex.ToString(); }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();
        return r;
    }
}
"@
$before = Get-MachineState
$report = [ProductionProbe]::Run($ExePath)
$report['children'] = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $PID").Count
$report['before'] = $before
$report['after'] = Get-MachineState
ConvertTo-Json -InputObject $report -Depth 4 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


@needs_bridge
def test_production_startup_shows_the_notice_and_cannot_start_setup_or_change_anything(tmp_path, production_build):
    probe, out = tmp_path / "probe.ps1", tmp_path / "probe.json"
    probe.write_text(_PROBE, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(probe),
                                "-ExePath", str(production_build / "SortView.Setup.exe"), "-OutPath", str(out)],
                               capture_output=True, text=True, timeout=180, check=False)
    assert out.exists(), completed.stdout + completed.stderr
    report = json.loads(out.read_text(encoding="utf-8-sig"))
    assert "error" not in report, report.get("error")

    # Past Welcome, Prerequisites and Ready, the shared wizard is there, with installation disabled and the notice shown.
    assert report["page"] == "Setup"
    assert report["wizard_type"] == "SortView.Setup.Wizard.SetupWizardView"
    assert report["installation_enabled"] is False
    assert report["notice"] == DISABLED_NOTICE and report["notice_visible"] is True
    # The same automation identifiers the simulation host's UI tests drive.
    assert report["automation_ids"] == ["StartButton", "CancelButton", "StatusText", "CancelNote", "PromptPanel", "EventList", "DiagnosticList"]
    # Nothing can be started: Start is disabled, a forced click asks nothing of a host, and BeginRun refuses.
    assert (report["start_enabled"], report["cancel_enabled"], report["prompt_visible"]) == (False, False, False)
    assert report["start_requested"] == 0 and report["running_after_click"] is False
    assert report["begin_run"] == "InvalidOperationException" and report["running_after_begin_run"] is False
    assert report["closed"] is True
    # And nothing changed: no child process, no install or data root, task or token appeared.
    assert report["children"] == 0
    assert report["after"] == report["before"]
    # A path this session may not look at stays 'access_denied' before and after: that shows only that access is still
    # denied, NOT that what is behind it is unchanged. The guarantee there is the one above -- no way to start setup.
    paths = ("install_root", "data_root", "token")
    assert {report[when][name] for when in ("before", "after") for name in paths} <= {"present", "absent", "access_denied"}
    unverified = [name for name in paths if report["before"][name] == "access_denied"]
    if unverified:
        warnings.warn(f"not verified without administrator rights (access denied): {', '.join(unverified)}", stacklevel=1)
