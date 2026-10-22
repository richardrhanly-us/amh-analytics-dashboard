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
PRODUCTION_SOURCES = [SETUP_DIR / "MainWindow.xaml", SETUP_DIR / "MainWindow.xaml.cs", SETUP_DIR / "App.xaml",
                      SETUP_DIR / "App.xaml.cs", SETUP_DIR / "SingleInstanceLock.cs", SETUP_DIR / "TrustedBundlePin.cs",
                      SETUP_DIR / "BundlePreparation.cs"]
APP_CODE = SETUP_DIR / "App.xaml.cs"
LOCK_CODE = SETUP_DIR / "SingleInstanceLock.cs"
WINDOW_CODE = SETUP_DIR / "MainWindow.xaml.cs"
INSTALLER_LOCK = r"Global\SortView.Setup.Installer"
ALREADY_RUNNING = ("SortView Setup is already running on this computer. Use the setup window that is already open, or wait for it "
                   "to finish.")

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


def _flat(text: str) -> str:
    """Code with every run of whitespace (line breaks included) as one space."""
    return re.sub(r"\s+", " ", text)


def test_startup_takes_the_single_instance_lock_before_the_window_and_refuses_without_it():
    app = _flat(_without_comments(APP_CODE.read_text(encoding="utf-8")))
    assert "StartupUri" not in (SETUP_DIR / "App.xaml").read_text(encoding="utf-8")  # the window is made only after the lock
    startup = app[app.index("protected override void OnStartup"):app.index("protected override void OnExit")]
    take, window = startup.index("SingleInstanceLock.TryAcquireInstaller(out instanceLock)"), startup.index("new MainWindow()")
    refuse = startup[take:window]
    assert take < window and "if (instanceLock == null)" in refuse and "Shutdown(1); return;" in refuse
    # Held by another process: the agreed message. Anything else (access denied, uncertain): the other one.
    assert "outcome == SingleInstanceOutcome.HeldElsewhere ? AlreadyRunningMessage : NotVerifiedMessage" in refuse
    assert f'AlreadyRunningMessage = "{ALREADY_RUNNING}";' in app
    assert "could not verify that it is the only setup running" in app
    # Released on a normal exit and when the window cannot be made; the program never names any other lock.
    assert "catch { ReleaseLock(); throw; }" in startup
    exit_ = app[app.index("protected override void OnExit"):]
    assert exit_.index("ReleaseLock();") < exit_.index("base.OnExit(e);")
    assert re.findall(r"TryAcquire\w*\(", app) == ["TryAcquireInstaller("]


def test_the_production_lock_is_global_and_only_for_system_and_administrators():
    code = _flat(_without_comments(LOCK_CODE.read_text(encoding="utf-8")))
    assert f'public const string InstallerName = @"{INSTALLER_LOCK}";' in code
    assert "TryAcquire(InstallerName, ProductionSecurity(), out held)" in code
    production = code[code.index("public static MutexSecurity ProductionSecurity()"):code.index("public static SingleInstanceOutcome TryAcquire(")]
    assert re.findall(r"WellKnownSidType\.(\w+)", production) == ["LocalSystemSid", "BuiltinAdministratorsSid"]
    for broader in ("WindowsIdentity", "GetCurrent", "WorldSid", "AuthenticatedUserSid", "BuiltinUsersSid", "Everyone", "InteractiveSid"):
        assert broader not in production, broader
    # Holding it is the only way through: being refused, denied access or any error is never "acquired".
    trying = code[code.index("public static SingleInstanceOutcome TryAcquire("):code.index("public void Dispose()")]
    assert "WaitOne(0)" in trying and "catch (AbandonedMutexException)" in trying
    assert "catch (UnauthorizedAccessException) { candidate?.Dispose(); return SingleInstanceOutcome.AccessDenied; }" in trying
    assert "catch (Exception) { candidate?.Dispose(); return SingleInstanceOutcome.Uncertain; }" in trying
    assert trying.count("held = new SingleInstanceLock(candidate);") == 1


def test_back_and_next_are_locked_while_the_wizard_runs():
    window = _flat(_without_comments(WINDOW_CODE.read_text(encoding="utf-8")))
    assert "Wizard.RunStateChanged += (s, e) => UpdateButtons();" in window
    buttons = window[window.index("private void UpdateButtons()"):window.index("private void BackButton_Click")]
    assert "bool running = Wizard.IsRunning;" in buttons
    assert "BackButton.IsEnabled = !running &&" in buttons and "NextButton.IsEnabled = !running &&" in buttons
    assert "ExitButton" not in buttons  # Exit stays on: the wizard defers the close while setup runs
    back = window[window.index("private void BackButton_Click"):window.index("private void NextButton_Click")]
    next_ = window[window.index("private void NextButton_Click"):window.index("private void ExitButton_Click")]
    assert "!Wizard.IsRunning" in back and "if (Wizard.IsRunning) { return; }" in next_
    assert "private void ExitButton_Click(object sender, RoutedEventArgs e) => Close();" in window


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
                var progressSteps = new List<string>();
                foreach (var item in ((ItemsControl)find("ProgressSteps")).Items) progressSteps.Add((string)item.GetType().GetProperty("AccessibleName").GetValue(item));
                r["progress_steps"] = progressSteps;
                r["busy_visible"] = find("BusyIndicator").IsVisible;
                r["details_expanded"] = ((Expander)find("DetailsExpander")).IsExpanded;
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
    # The progress view is idle: seven steps pending, nothing busy, and the diagnostic details closed for a customer.
    assert len(report["progress_steps"]) == 7 and all(step.endswith(", Pending") for step in report["progress_steps"])
    assert report["progress_steps"][0] == "Step 1 of 7: Check the computer and installation package, Pending"
    assert report["busy_visible"] is False and report["details_expanded"] is False
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


# =====================================================================================
# 3. RUN SAFETY (M1): the single-instance lock, and navigation while setup runs
# =====================================================================================

def _run_probe(tmp_path: Path, script: str, production_build: Path) -> dict:
    probe, out = tmp_path / "probe.ps1", tmp_path / "probe.json"
    probe.write_text(script, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(probe),
                                "-BuildDir", str(production_build), "-OutPath", str(out)],
                               capture_output=True, text=True, timeout=180, check=False)
    assert out.exists(), completed.stdout + completed.stderr
    report = json.loads(out.read_text(encoding="utf-8-sig"))
    assert "error" not in report, report.get("error")
    return report


# The lock, taken and released on separate threads (a mutex belongs to the thread that took it), always under a NEW test
# name -- never the installer's. "Test security" lets this (unelevated) user open a test lock; the production security
# (SYSTEM and Administrators only) is used as is, to show that anyone else is refused.
_LOCK_PROBE = r"""
param([string]$BuildDir, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System; using System.Collections.Generic; using System.Reflection; using System.Security.AccessControl;
using System.Security.Principal; using System.Threading;
public static class LockProbe {
    // SingleInstanceLock's public static members, from the built program (an .exe cannot be referenced at compile time).
    static Type lockType;
    static string TryAcquire(string name, MutexSecurity security, out IDisposable held) {
        var args = new object[] { name, security, null };
        string outcome = lockType.GetMethod("TryAcquire").Invoke(null, args).ToString();
        held = (IDisposable)args[2];
        return outcome;
    }
    static MutexSecurity ProductionSecurity() { return (MutexSecurity)lockType.GetMethod("ProductionSecurity").Invoke(null, null); }
    static string NewName() { return @"Global\SortView.Setup.Test-" + Guid.NewGuid().ToString("N"); }
    static MutexSecurity TestSecurity() {
        var security = new MutexSecurity();
        security.AddAccessRule(new MutexAccessRule(WindowsIdentity.GetCurrent().User, MutexRights.FullControl, AccessControlType.Allow));
        return security;
    }
    // Takes the lock on its own thread and keeps it until Finish: then releases it, or (abandon) ends still holding it.
    sealed class Holder {
        public string Outcome;
        readonly ManualResetEventSlim taken = new ManualResetEventSlim(false), done = new ManualResetEventSlim(false);
        readonly Thread thread;
        public Holder(string name, MutexSecurity security, bool abandon) {
            thread = new Thread(() => {
                IDisposable held;
                Outcome = TryAcquire(name, security, out held);
                taken.Set(); done.Wait();
                if (!abandon && held != null) held.Dispose();
            });
            thread.Start(); taken.Wait();
        }
        public void Finish() { done.Set(); thread.Join(); }
    }
    // One attempt on a fresh thread, which releases whatever it took.
    static string TryOnce(string name, MutexSecurity security) {
        string outcome = null;
        var thread = new Thread(() => {
            IDisposable held;
            outcome = TryAcquire(name, security, out held) + (held == null ? "" : "+held");
            if (held != null) held.Dispose();
        });
        thread.Start(); thread.Join();
        return outcome;
    }
    public static Dictionary<string, object> Run(string exePath) {
        lockType = Assembly.LoadFrom(exePath).GetType("SortView.Setup.SingleInstanceLock", true);
        var r = new Dictionary<string, object>();
        string name = NewName();
        var first = new Holder(name, TestSecurity(), false);
        r["first"] = first.Outcome;
        r["second_while_held"] = TryOnce(name, TestSecurity());
        first.Finish();
        r["after_release"] = TryOnce(name, TestSecurity());

        name = NewName();
        var crashed = new Holder(name, TestSecurity(), true);
        crashed.Finish();
        r["abandoned_holder"] = crashed.Outcome;
        r["after_abandoned"] = TryOnce(name, TestSecurity());

        name = NewName();
        var production = new Holder(name, ProductionSecurity(), false);
        r["production_first"] = production.Outcome;
        r["production_second"] = TryOnce(name, ProductionSecurity());
        production.Finish();

        r["bad_name"] = TryOnce(NewName() + @"\not\a\lock", TestSecurity());

        var rules = new List<string>();
        foreach (MutexAccessRule rule in ProductionSecurity().GetAccessRules(true, false, typeof(SecurityIdentifier)))
            rules.Add(rule.IdentityReference.Value + "|" + rule.AccessControlType + "|" + rule.MutexRights);
        r["production_rules"] = rules;
        r["elevated"] = new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator);
        return r;
    }
}
"@
try { $report = [LockProbe]::Run((Join-Path $BuildDir 'SortView.Setup.exe')) } catch { $report = @{ error = $_.Exception.ToString() } }
ConvertTo-Json -InputObject $report -Depth 4 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


@needs_bridge
def test_only_one_installer_holds_the_lock_and_anything_uncertain_is_refused(tmp_path, production_build):
    report = _run_probe(tmp_path, _LOCK_PROBE, production_build)
    # One holder at a time; free again once released.
    assert report["first"] == "Acquired"
    assert report["second_while_held"] == "HeldElsewhere"
    assert report["after_release"] == "Acquired+held"
    # A holder that ended without releasing (a crash) does not block the next installer.
    assert report["abandoned_holder"] == "Acquired" and report["after_abandoned"] == "AcquiredAbandoned+held"
    # Production security: only SYSTEM and Administrators. An unelevated user is refused outright; even an elevated one
    # can only find it held.
    assert report["production_first"] == "Acquired"
    assert report["production_second"] == ("HeldElsewhere" if report["elevated"] else "AccessDenied")
    assert sorted(report["production_rules"]) == ["S-1-5-18|Allow|FullControl", "S-1-5-32-544|Allow|FullControl"]
    # Anything else that goes wrong is never taken for "acquired".
    assert report["bad_name"] == "Uncertain"


# The production window, in-process, driven to the Setup page as a person would. The wizard is then enabled IN THIS TEST'S
# WINDOW ONLY and given a fake runner that runs nothing: no setup.ps1, no bundle, no enrollment, token or Scheduled Task,
# no tool -- it only waits until the probe lets it return.
_RUN_LOCK_PROBE = r"""
param([string]$BuildDir, [string]$OutPath)
$ErrorActionPreference = 'Stop'
foreach ($name in 'Core', 'Wpf', 'Wizard') { Add-Type -Path (Join-Path $BuildDir "SortView.Setup.$name.dll") }
$references = @('PresentationFramework', 'PresentationCore', 'WindowsBase', 'System.Xaml', 'System.Core') +
    @('Core', 'Wpf', 'Wizard' | ForEach-Object { Join-Path $BuildDir "SortView.Setup.$_.dll" })
Add-Type -ReferencedAssemblies $references -TypeDefinition @"
using System; using System.Collections.Generic; using System.Reflection; using System.Threading;
using System.Windows; using System.Windows.Controls; using System.Windows.Controls.Primitives; using System.Windows.Threading;
using SortView.Setup.Core; using SortView.Setup.Wpf; using SortView.Setup.Wizard;
public static class RunLockProbe {
    public static readonly ManualResetEventSlim Gate = new ManualResetEventSlim(false);
    public static int Runs;
    // FAKE RUNNER: runs nothing at all; returns an empty result once the probe opens the gate.
    public static SetupRunResult FakeRun(IPromptPresenter presenter, Action<SetupEvent> onEvent, Action<string> onConsoleText,
        CancellationToken cancellationToken) {
        Interlocked.Increment(ref Runs);
        Gate.Wait();
        return new SetupRunResult();
    }
    static void Pump() {
        var frame = new DispatcherFrame();
        Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() => frame.Continue = false));
        Dispatcher.PushFrame(frame);
    }
    static void Click(FrameworkElement button) { button.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); Pump(); }
    static void WaitUntil(Func<bool> condition) { for (int i = 0; i < 1000 && !condition(); i++) { Thread.Sleep(10); Pump(); } }
    static void GoToSetupPage(Window window) {
        var checks = (FrameworkElement)window.FindName("PrerequisitesView");
        var facts = new MachineFacts { WindowsBuild = 19045, WindowsInstallationType = "Client", Is64BitOperatingSystem = true,
            DotNetRelease = 533325, PowerShellVersion = "5.1.19041.1", IsAdministrator = true, SystemDriveFreeBytes = 100L << 30,
            LegacyTasksRead = true, MachinePolicyRead = true, UserPolicyRead = true, LegacyTasks = new LegacyTaskFact[0] };
        ((PrerequisitesPage)checks).ReadFacts = () => facts;
        Click((FrameworkElement)window.FindName("NextButton"));
        WaitUntil(() => ((PrerequisitesPage)checks).CanContinue);
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
                var wizard = (SetupWizardView)window.FindName("Wizard");
                Func<string, Button> button = name => (Button)window.FindName(name);
                var steps = new Dictionary<string, object>();
                Action<string> record = label => steps[label] = new Dictionary<string, object> {
                    { "page", window.GetType().GetProperty("CurrentPage").GetValue(window).ToString() }, { "running", wizard.IsRunning },
                    { "back", button("BackButton").IsEnabled }, { "next", button("NextButton").IsEnabled },
                    { "exit", button("ExitButton").IsEnabled }, { "open", window.IsVisible } };

                record("idle");
                wizard.IsInstallationEnabled = true;  // this test's window only
                wizard.BeginRun(new SetupRunner(FakeRun), "Fake run (test only)."); Pump();
                record("running");
                Click(button("BackButton")); record("forced_back");   // a forced click is refused too
                Click(button("NextButton")); record("forced_next");
                Gate.Set(); WaitUntil(() => !wizard.IsRunning); Pump();
                record("finished");
                Click(button("BackButton")); record("back_after");
                Click(button("NextButton")); record("next_after");

                Gate.Reset();
                wizard.BeginRun(new SetupRunner(FakeRun), "Fake run (test only)."); Pump();
                Click(button("ExitButton")); record("exit_while_running");   // the window waits for setup
                r["status_after_exit"] = ((TextBlock)wizard.FindName("StatusText")).Text;
                Gate.Set(); WaitUntil(() => !window.IsVisible);
                r["closed_when_run_ended"] = !window.IsVisible;
                r["runs"] = Runs;
                r["steps"] = steps;
            } catch (Exception ex) { r["error"] = ex.ToString(); }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start(); thread.Join();
        return r;
    }
}
"@
$report = [RunLockProbe]::Run((Join-Path $BuildDir 'SortView.Setup.exe'))
$report['children'] = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $PID").Count
ConvertTo-Json -InputObject $report -Depth 5 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


def test_the_navigation_probes_fake_runner_runs_nothing():
    fake = _RUN_LOCK_PROBE[_RUN_LOCK_PROBE.index("public static SetupRunResult FakeRun"):_RUN_LOCK_PROBE.index("static void Pump()")]
    for real in ("setup.ps1", "SetupEngineHost", "SetupRunSession", "PowerShell", "Process", "Bundle", "Enroll", "ApiToken",
                 "api_token", "Dpapi", "ScheduledTask", "File.", "Directory."):
        assert real not in fake, real
    assert "SetupEngineHost" not in _RUN_LOCK_PROBE and "BundleRoot" not in _RUN_LOCK_PROBE


@needs_bridge
def test_back_and_next_refuse_navigation_while_setup_runs_and_exit_waits_for_it(tmp_path, production_build):
    report = _run_probe(tmp_path, _RUN_LOCK_PROBE, production_build)
    steps = report["steps"]

    def nav(label: str) -> tuple:
        step = steps[label]
        return step["page"], step["running"], step["back"], step["next"], step["exit"], step["open"]

    assert nav("idle") == ("Setup", False, True, False, True, True)  # as today: Back to Ready, no Next past Setup
    # While setup runs: Back and Next off, and even a forced click does not move; Exit stays available.
    assert nav("running") == ("Setup", True, False, False, True, True)
    assert nav("forced_back") == nav("running") and nav("forced_next") == nav("running")
    # The run ended: navigation as before.
    assert nav("finished") == ("Setup", False, True, False, True, True)
    assert nav("back_after")[0] == "Ready" and nav("back_after")[3] is True
    assert nav("next_after")[0] == "Setup"
    # Exit during a run: the window stays open, says why, and closes by itself once setup has returned.
    assert nav("exit_while_running") == ("Setup", True, False, False, True, True)
    assert "will close as soon as setup stops at its next question" in report["status_after_exit"]
    assert report["closed_when_run_ended"] is True
    assert report["runs"] == 2 and report["children"] == 0
