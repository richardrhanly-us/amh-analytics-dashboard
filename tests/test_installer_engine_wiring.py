"""Tests for the production engine wiring, M2d-1 Stage 2: installer/SortView.Setup/InstallationRunner.cs and the Start path in
installer/SortView.Setup/MainWindow.xaml.cs.

  1. STATIC (run everywhere): the runner's request is the staged bundle's own path and the engine's fixed roots, and only
     the window asks for one.
  2. RUNTIME (Windows): the production window, in-process, driven to the Setup page over a fixture bundle, with the
     engine replaced through the window's EngineFactory seam by a FAKE that runs nothing (no setup.ps1, no PowerShell, no
     process, no enrollment): it only records what it was asked to run and waits until the probe lets it return.
       - The DEFAULT build (gate false, no pin): Start is disabled, even a forced Start never asks for a runner, and
         BeginRun refuses.
       - A gated, pinned build of a COPY of the installer (never the real gate or pin files): Start runs the fake over the
         STAGED copy (never the source folder); while it runs Back and Next refuse, the staged copy stays, and Cancel
         cancels the run's token; without a staged bundle Start runs nothing; Exit waits for the run, and the staged copy
         is removed once the window has closed.
       - InstallationRunner.Create: a runner only for an enabled gate, a configured pin, and a staged, held bundle of the
         pinned version.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_installer_bridge import _MSBUILD, _POWERSHELL, needs_bridge
from test_installer_installation_gate import GATE_FILE, PIN_FILE, _configure_copy
from test_installer_prepare_page import (
    FIXTURE_PREPARATION_CS,
    FIXTURE_VERSION,
    write_fixture_bundle,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "installer"
SETUP_DIR = INSTALLER / "SortView.Setup"
SETUP_PROJECT = SETUP_DIR / "SortView.Setup.csproj"
RUNNER_CODE = SETUP_DIR / "InstallationRunner.cs"
WINDOW_CODE = SETUP_DIR / "MainWindow.xaml.cs"


def _code(path: Path) -> str:
    return re.sub(r"\s+", " ", "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                                         if not line.strip().startswith("//")))


# =====================================================================================================================
# 1. STATIC
# =====================================================================================================================

def test_the_runner_runs_the_staged_bundle_with_the_engines_fixed_roots_only():
    code = _code(RUNNER_CODE)
    assert "new SetupRunRequest { BundleRoot = staged.BundlePath, InstallRoot = InstallRoot, DataRoot = DataRoot }" in code
    assert 'internal const string InstallRoot = @"C:\\SortView\\Collector";' in code
    assert 'internal const string DataRoot = @"C:\\ProgramData\\SortViewCollector";' in code
    assert "staged == null || staged.IsDisposed || !staged.IsStaged || string.IsNullOrEmpty(staged.BundlePath)" in code
    assert "!string.Equals(staged.Version, pin.Version, StringComparison.Ordinal)" in code
    assert code.count("new SetupEngineHost()") == 1 and code.count("BundleRoot =") == 1
    # Immediately before the engine runs: the staged copy again, against the pin; only an explicit success continues.
    assert "BundleVerification verification = BundleVerifier.Verify(request.BundleRoot, expectedSha256, expectedVersion);" in code
    assert "string expectedSha256 = pin.ManifestSha256; string expectedVersion = pin.Version;" in code
    assert "if (verification == null || !verification.IsVerified) { throw new InvalidOperationException(" in code
    verify_at = code.index("BundleVerifier.Verify(")
    assert code.index("throw new InvalidOperationException(", verify_at) < code.index("return run(presenter, onEvent, onConsoleText, cancellationToken);")
    assert code.count("BundleVerifier.Verify(") == 1 and code.count("return run(") == 1
    for forbidden in ("Environment.", "Registry", "GetCommandLineArgs", "File.", "Directory.", "Process", "BundleDirectory", "catch"):
        assert forbidden not in code, forbidden


def test_the_window_asks_for_a_runner_only_from_start_over_the_prepared_bundle():
    window = _code(WINDOW_CODE)
    assert window.count("InstallationRunner.Create(") == 1
    assert "InstallationRunner.Create(gate, pin, preparation.StagedBundle, EngineFactory)" in window
    assert "if (Wizard.IsRunning || CurrentPage != WizardPage.Setup)" in window
    assert "internal Func<SetupRunRequest, SetupRunner> EngineFactory { get; set; } = InstallationRunner.RealEngine;" in window
    # Closing during a run is left to the wizard (it defers it); the staged bundle is released only once the window closes.
    assert "if (Wizard.IsRunning) { return;" in window
    assert "Closed += (s, e) => preparation.DisposePreparation();" in window


# =====================================================================================================================
# 2. RUNTIME
# =====================================================================================================================

_PROBE = r"""
param([string]$BuildDir, [string]$OutPath, [string]$FixtureSource, [string]$FixtureSha, [string]$FixtureVersion, [string]$FixtureRoot,
      [string]$Tamper = '')
$ErrorActionPreference = 'Stop'
foreach ($name in 'Core', 'Wpf', 'Wizard') { Add-Type -Path (Join-Path $BuildDir "SortView.Setup.$name.dll") }
$references = @('PresentationFramework', 'PresentationCore', 'WindowsBase', 'System.Xaml', 'System.Core') +
    @('Core', 'Wpf', 'Wizard' | ForEach-Object { Join-Path $BuildDir "SortView.Setup.$_.dll" })
Add-Type -ReferencedAssemblies $references -TypeDefinition @"
using System; using System.Collections.Generic; using System.IO; using System.Reflection; using System.Security.Cryptography;
using System.Text; using System.Threading;
using System.Windows; using System.Windows.Controls; using System.Windows.Controls.Primitives; using System.Windows.Threading;
using SortView.Setup.Core; using SortView.Setup.Wpf; using SortView.Setup.Wizard;
@@FIXTURE@@
public static class WiringProbe {
    const BindingFlags Any = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;
    public static readonly ManualResetEventSlim Gate = new ManualResetEventSlim(false);
    public static int Runs, CancelSeen;
    public static readonly List<SetupRunRequest> Requests = new List<SetupRunRequest>();
    // FAKE RUNNER: runs nothing at all; notes whether its run was cancelled, and returns once the probe opens the gate.
    public static SetupRunResult FakeRun(IPromptPresenter presenter, Action<SetupEvent> onEvent, Action<string> onConsoleText,
        CancellationToken cancellationToken) {
        Interlocked.Increment(ref Runs);
        Gate.Wait();
        if (cancellationToken.IsCancellationRequested) Interlocked.Increment(ref CancelSeen);
        return new SetupRunResult();
    }
    // FAKE ENGINE FACTORY: records the request it is given and returns the fake runner.
    public static SetupRunner FakeEngine(SetupRunRequest request) { lock (Requests) Requests.Add(request); return new SetupRunner(FakeRun); }
    static void Pump() {
        var frame = new DispatcherFrame();
        Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() => frame.Continue = false));
        Dispatcher.PushFrame(frame);
    }
    static void Click(FrameworkElement button) { button.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); Pump(); }
    static void WaitUntil(Func<bool> condition) { for (int i = 0; i < 3000 && !condition(); i++) { Thread.Sleep(10); Pump(); } }
    static string Page(Window window) { return window.GetType().GetProperty("CurrentPage").GetValue(window).ToString(); }
    static string Sha256(byte[] bytes) { using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant(); }
    // Changes the STAGED copy after Prepare (the test staging policy lets this user write to it). For "consistent", a file
    // and MANIFEST.json are changed together; returns whether the result is self-consistent (it verifies against its OWN
    // manifest hash), so only the pin can tell it from the real bundle.
    static object TamperWith(string bundle, string kind) {
        string manifest = Path.Combine(bundle, "MANIFEST.json");
        switch (kind) {
            case "none": return null;
            case "modify": File.AppendAllText(Path.Combine(bundle, "install.ps1"), "x"); return null;
            case "unlisted": File.WriteAllText(Path.Combine(bundle, "unlisted.txt"), "x"); return null;
            case "no_manifest": File.Delete(manifest); return null;
            case "consistent":
                string target = Path.Combine(bundle, "install.ps1");
                byte[] before = File.ReadAllBytes(target), after = Encoding.ASCII.GetBytes("# fixture, NOT a script");
                if (after.Length != before.Length) throw new InvalidOperationException("the replacement must keep the size");
                File.WriteAllBytes(target, after);
                // The fixture's setup.ps1 has the same bytes (so the same manifest hash): change it too, to stay consistent.
                File.WriteAllBytes(Path.Combine(bundle, "setup.ps1"), after);
                string text = Encoding.UTF8.GetString(File.ReadAllBytes(manifest));
                if (!text.Contains(Sha256(before))) throw new InvalidOperationException("install.ps1 is not in the manifest");
                byte[] changed = Encoding.UTF8.GetBytes(text.Replace(Sha256(before), Sha256(after)));
                File.WriteAllBytes(manifest, changed);
                return BundleVerifier.Verify(bundle, Sha256(changed)).IsVerified;
        }
        throw new ArgumentException(kind);
    }
    static void PrepareAndGoToSetup(Window window) {
        WaitUntil(() => ((Button)window.FindName("NextButton")).IsEnabled);  // Prepare: the fixture bundle is staged
        Click((FrameworkElement)window.FindName("NextButton"));
    }
    static void GoToSetupPage(Window window) {
        var checks = (PrerequisitesPage)window.FindName("PrerequisitesView");
        var facts = new MachineFacts { WindowsBuild = 19045, WindowsInstallationType = "Client", Is64BitOperatingSystem = true,
            DotNetRelease = 533325, PowerShellVersion = "5.1.19041.1", IsAdministrator = true, SystemDriveFreeBytes = 100L << 30,
            LegacyTasksRead = true, MachinePolicyRead = true, UserPolicyRead = true, LegacyTasks = new LegacyTaskFact[0] };
        checks.ReadFacts = () => facts;
        Click((FrameworkElement)window.FindName("NextButton"));
        WaitUntil(() => checks.CanContinue);
        Click((FrameworkElement)window.FindName("NextButton"));
        Click((FrameworkElement)window.FindName("NextButton"));
        PrepareAndGoToSetup(window);
    }
    static StagedBundle Staged(Window window) {
        object step = window.GetType().GetField("preparation", Any).GetValue(window);
        return (StagedBundle)step.GetType().GetProperty("StagedBundle").GetValue(step);
    }
    // InstallationRunner.Create over every combination that must give no runner, then the one that must.
    static Dictionary<string, object> Matrix(Assembly setup, StagedBundle staged, string sha, string version) {
        Type runner = setup.GetType("SortView.Setup.InstallationRunner", true);
        MethodInfo create = runner.GetMethod("Create", Any);
        Func<string, object> gate = value => setup.GetType("SortView.Setup.InstallationGate", true)
            .GetConstructor(Any, null, new[] { typeof(string[]) }, null).Invoke(new object[] { value == null ? new string[0] : new[] { value } });
        Func<string, string, object> pin = (s, v) => setup.GetType("SortView.Setup.TrustedBundlePin", true)
            .GetConstructor(Any, null, new[] { typeof(string), typeof(string) }, null).Invoke(new object[] { s, v });
        var asked = new List<SetupRunRequest>();
        Func<SetupRunRequest, SetupRunner> engine = request => { asked.Add(request); return new SetupRunner(FakeRun); };
        var cases = new Dictionary<string, object[]> {
            { "gate_false", new object[] { gate("gate:false"), pin(sha, version), staged, engine } },
            { "gate_missing", new object[] { gate(null), pin(sha, version), staged, engine } },
            { "gate_uppercase", new object[] { gate("gate:TRUE"), pin(sha, version), staged, engine } },
            { "gate_null", new object[] { null, pin(sha, version), staged, engine } },
            { "pin_not_configured", new object[] { gate("gate:true"), pin("", ""), staged, engine } },
            { "pin_invalid", new object[] { gate("gate:true"), pin("invalid", "invalid"), staged, engine } },
            { "pin_null", new object[] { gate("gate:true"), null, staged, engine } },
            { "no_staged_bundle", new object[] { gate("gate:true"), pin(sha, version), null, engine } },
            { "other_version", new object[] { gate("gate:true"), pin(sha, "9.9.9"), staged, engine } },
            { "no_engine", new object[] { gate("gate:true"), pin(sha, version), staged, null } },
        };
        var r = new Dictionary<string, object>();
        foreach (var c in cases) r[c.Key] = create.Invoke(null, c.Value) == null ? "none" : "RUNNER";
        r["asked_when_refused"] = asked.Count;
        object valid = create.Invoke(null, new object[] { gate("gate:true"), pin(sha, version), staged, engine });
        r["valid"] = valid == null ? "none" : "RUNNER";
        r["valid_asked"] = asked.Count;
        if (asked.Count == 1) {
            r["valid_bundle_root_is_staged"] = asked[0].BundleRoot == staged.BundlePath;
            r["valid_install_root"] = asked[0].InstallRoot; r["valid_data_root"] = asked[0].DataRoot;
        }
        return r;
    }
    public static Dictionary<string, object> Run(string exePath, string source, string sha, string version, string root, string tamper) {
        var r = new Dictionary<string, object>();
        var thread = new Thread(() => {
            try {
                SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext());  // as in the application
                Assembly setup = Assembly.LoadFrom(exePath);
                var window = (Window)Activator.CreateInstance(setup.GetType("SortView.Setup.MainWindow", true));
                window.WindowStartupLocation = WindowStartupLocation.Manual; window.Left = -32000; window.Top = -32000; window.ShowActivated = false;
                FixturePreparation.Install(window, source, sha, version, root);
                window.GetType().GetProperty("EngineFactory", Any).SetValue(window, new Func<SetupRunRequest, SetupRunner>(FakeEngine));
                window.Show(); Pump();
                GoToSetupPage(window);
                var wizard = (SetupWizardView)window.FindName("Wizard");
                Func<string, Button> button = name => (Button)window.FindName(name);
                Func<string, Button> wizardButton = name => (Button)wizard.FindName(name);
                Func<string> status = () => ((TextBlock)wizard.FindName("StatusText")).Text;
                var steps = new Dictionary<string, object>();
                Action<string> record = label => steps[label] = new Dictionary<string, object> {
                    { "page", Page(window) }, { "running", wizard.IsRunning }, { "back", button("BackButton").IsEnabled },
                    { "next", button("NextButton").IsEnabled }, { "exit", button("ExitButton").IsEnabled }, { "open", window.IsVisible },
                    { "staged", Staged(window) != null && Directory.Exists(Staged(window).BundlePath) }, { "requests", Requests.Count } };
                r["steps"] = steps;
                r["installation_enabled"] = wizard.IsInstallationEnabled;
                r["start_enabled"] = wizardButton("StartButton").IsEnabled;
                StagedBundle staged = Staged(window);
                string stagedPath = staged.BundlePath;
                r["staged_under_root"] = stagedPath.StartsWith(Path.GetFullPath(root).TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase);
                r["staged_is_not_source"] = !string.Equals(Path.GetFullPath(stagedPath).TrimEnd('\\'), Path.GetFullPath(source).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);
                r["matrix"] = Matrix(setup, staged, sha, version);
                record("idle");
                if (tamper.Length > 0) {
                    // Prepared, then the staged copy changed before Start: Start must not reach the engine.
                    r["tamper_self_consistent"] = TamperWith(stagedPath, tamper);
                    Gate.Set();  // the unchanged case runs the fake, which then returns at once
                    Click(wizardButton("StartButton")); WaitUntil(() => !wizard.IsRunning); Pump();
                    record("tampered_start");
                    r["tamper_status"] = status();
                    r["tamper_outcome"] = ((TextBlock)wizard.FindName("OutcomeTitle")).Text;
                    r["tamper_outcome_visible"] = ((FrameworkElement)wizard.FindName("OutcomePanel")).IsVisible;
                    window.Close(); Pump();
                    r["closed"] = !window.IsVisible;
                    r["staged_removed_after_close"] = !Directory.Exists(stagedPath);
                    r["runs"] = Runs; r["requests"] = Requests.Count;
                    return;
                }

                Click(wizardButton("StartButton")); record("started");  // a forced click, if Start is disabled
                window.GetType().GetMethod("OnStartRequested", Any).Invoke(window, new object[] { wizard, EventArgs.Empty }); Pump();
                record("forced_start_requested");  // as if the wizard had asked (it does only when enabled and idle)
                if (!wizard.IsInstallationEnabled) {
                    try { wizard.BeginRun(new SetupRunner(FakeRun), "Fake run (test only)."); r["begin_run"] = "ACCEPTED"; }
                    catch (InvalidOperationException) { r["begin_run"] = "refused"; }
                    record("after_begin_run");
                } else {
                    // The run, as Start began it: on the staged copy, with navigation locked.
                    lock (Requests) {
                        r["request_bundle_root_is_staged"] = Requests.Count > 0 && Requests[0].BundleRoot == stagedPath;
                        r["request_install_root"] = Requests.Count > 0 ? Requests[0].InstallRoot : null;
                        r["request_data_root"] = Requests.Count > 0 ? Requests[0].DataRoot : null;
                    }
                    Click(button("BackButton")); record("forced_back");
                    Click(button("NextButton")); record("forced_next");
                    Click(wizardButton("CancelButton")); record("cancel_requested");
                    Gate.Set(); WaitUntil(() => !wizard.IsRunning); Pump();
                    r["cancel_seen"] = CancelSeen;
                    record("finished");
                    Gate.Reset();

                    // Without a staged bundle (released here behind the page's back), Start runs nothing.
                    window.GetType().GetMethod("DisposePreparation", Any).Invoke(window, null); Pump();
                    r["released_path_gone"] = !Directory.Exists(stagedPath);
                    Click(wizardButton("StartButton")); record("start_without_staging");
                    r["status_without_staging"] = status();

                    // Back to Prepare (which prepares afresh), forward to Setup, Start, then Exit while it runs.
                    Click(button("BackButton"));
                    PrepareAndGoToSetup(window);
                    stagedPath = Staged(window).BundlePath;
                    Click(wizardButton("StartButton")); record("second_run");
                    lock (Requests) r["second_request_is_new_staging"] = Requests.Count == 2 && Requests[1].BundleRoot == stagedPath;
                    Click(button("ExitButton")); record("exit_while_running");   // the window waits for setup
                    Gate.Set(); WaitUntil(() => !window.IsVisible);
                }
                if (window.IsVisible) { window.Close(); Pump(); }
                r["closed"] = !window.IsVisible;
                r["staged_removed_after_close"] = !Directory.Exists(stagedPath);
                r["runs"] = Runs;
                r["requests"] = Requests.Count;
            } catch (Exception ex) { r["error"] = ex.ToString(); }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start(); thread.Join();
        return r;
    }
}
"@
$report = [WiringProbe]::Run((Join-Path $BuildDir 'SortView.Setup.exe'), $FixtureSource, $FixtureSha, $FixtureVersion, $FixtureRoot, $Tamper)
$report['children'] = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $PID").Count
ConvertTo-Json -InputObject $report -Depth 6 | Set-Content -LiteralPath $OutPath -Encoding UTF8
""".replace("@@FIXTURE@@", FIXTURE_PREPARATION_CS)


def test_the_probes_fake_engine_runs_nothing():
    fake = _PROBE[_PROBE.index("public static SetupRunResult FakeRun"):_PROBE.index("static void Pump()")]
    for real in ("setup.ps1", "SetupEngineHost", "SetupRunSession", "PowerShell", "Process", "Enroll", "ApiToken", "api_token",
                 "Dpapi", "ScheduledTask", "File.", "Directory."):
        assert real not in fake, real
    assert "SetupEngineHost" not in _PROBE and "RealEngine" not in _PROBE


def _fixture(tmp_path: Path) -> tuple[list[str], str]:
    source, root = tmp_path / "bundle", tmp_path / "staging"
    sha = write_fixture_bundle(source)
    root.mkdir()
    return ["-FixtureSource", str(source), "-FixtureSha", sha, "-FixtureVersion", FIXTURE_VERSION, "-FixtureRoot", str(root)], sha


def _run(tmp_path: Path, build: Path, tamper: str = "") -> dict:
    args, _ = _fixture(tmp_path)
    if tamper:
        args += ["-Tamper", tamper]
    probe, out = tmp_path / "probe.ps1", tmp_path / "probe.json"
    probe.write_text(_PROBE, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(probe),
                                "-BuildDir", str(build), "-OutPath", str(out), *args],
                               capture_output=True, text=True, timeout=240, check=False)
    assert out.exists(), completed.stdout + completed.stderr
    report = json.loads(out.read_text(encoding="utf-8-sig"))
    assert "error" not in report, report.get("error")
    return report


def _build(project: Path) -> Path:
    completed = subprocess.run([_MSBUILD, str(project), "/p:Configuration=Release", "/nologo", "/v:minimal"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return project.parent / "bin" / "Release"


@pytest.fixture(scope="module")
def default_build() -> Path:
    return _build(SETUP_PROJECT)


@pytest.fixture(scope="module")
def enabled_build(tmp_path_factory) -> Path:
    """A COPY of the installer, its gate on and its pin the fixture bundle's: never the real gate or pin files."""
    copy = tmp_path_factory.mktemp("wiring-copy") / "installer"
    for name in ("SortView.Setup", "SortView.Setup.Core", "SortView.Setup.Wizard", "SortView.Setup.Wpf"):
        shutil.copytree(INSTALLER / name, copy / name, ignore=shutil.ignore_patterns("bin", "obj", "Testing"))
    shutil.copyfile(GATE_FILE, copy / "installation-gate.props")
    shutil.copyfile(PIN_FILE, copy / "bundle-pin.props")
    sha = write_fixture_bundle(tmp_path_factory.mktemp("wiring-pin") / "bundle")  # the same fixed bytes the probe stages
    _configure_copy(copy, "true", sha, FIXTURE_VERSION)
    return _build(copy / "SortView.Setup" / "SortView.Setup.csproj")


def _nav(step: dict) -> tuple:
    return step["page"], step["running"], step["back"], step["next"], step["exit"], step["open"], step["staged"], step["requests"]


EXPECTED_MATRIX = {
    "gate_false": "none", "gate_missing": "none", "gate_uppercase": "none", "gate_null": "none", "pin_not_configured": "none",
    "pin_invalid": "none", "pin_null": "none", "no_staged_bundle": "none", "other_version": "none", "no_engine": "none",
    "asked_when_refused": 0, "valid": "RUNNER", "valid_asked": 1, "valid_bundle_root_is_staged": True,
    "valid_install_root": "C:\\SortView\\Collector", "valid_data_root": "C:\\ProgramData\\SortViewCollector",
}


@needs_bridge
def test_the_default_build_never_asks_for_a_runner(tmp_path, default_build):
    report = _run(tmp_path, default_build)
    steps = report["steps"]
    assert report["installation_enabled"] is False and report["start_enabled"] is False
    idle = ("Setup", False, True, False, True, True, True, 0)
    # Neither a forced click nor a forced StartRequested asks for a runner; BeginRun refuses; nothing runs.
    assert _nav(steps["idle"]) == _nav(steps["started"]) == _nav(steps["forced_start_requested"]) == idle
    assert report["begin_run"] == "refused" and _nav(steps["after_begin_run"]) == idle
    assert (report["runs"], report["requests"], report["children"]) == (0, 0, 0)
    # The runner itself refuses unless the gate, the pin and the staged bundle all agree (whatever this build's own gate).
    assert report["matrix"] == EXPECTED_MATRIX
    assert report["closed"] is True and report["staged_removed_after_close"] is True


@needs_bridge
def test_an_enabled_build_runs_the_engine_over_the_staged_copy_and_keeps_it_until_close(tmp_path, enabled_build):
    report = _run(tmp_path, enabled_build)
    steps = report["steps"]
    assert report["installation_enabled"] is True and report["start_enabled"] is True
    assert _nav(steps["idle"]) == ("Setup", False, True, False, True, True, True, 0)
    # Start: the engine is asked once, for the staged copy (under the protected root, not the source) and the fixed roots.
    assert report["staged_under_root"] is True and report["staged_is_not_source"] is True
    assert report["request_bundle_root_is_staged"] is True
    assert (report["request_install_root"], report["request_data_root"]) == ("C:\\SortView\\Collector", "C:\\ProgramData\\SortViewCollector")
    running = ("Setup", True, False, False, True, True, True, 1)
    # A second StartRequested while it runs asks for nothing; Back and Next refuse; the staged copy stays.
    assert _nav(steps["started"]) == _nav(steps["forced_start_requested"]) == running
    assert _nav(steps["forced_back"]) == _nav(steps["forced_next"]) == _nav(steps["cancel_requested"]) == running
    # Cancel reaches the run's token; once the run returns, the staged copy is still held.
    assert report["cancel_seen"] == 1
    assert _nav(steps["finished"]) == ("Setup", False, True, False, True, True, True, 1)
    # Without a staged bundle, Start asks for nothing and says so.
    assert report["released_path_gone"] is True
    assert _nav(steps["start_without_staging"]) == ("Setup", False, True, False, True, True, False, 1)
    assert report["status_without_staging"].startswith("Setup cannot start")
    # Prepared afresh, Start runs over the NEW staged copy; Exit waits for the run, which keeps it until the window closes.
    assert _nav(steps["second_run"]) == ("Setup", True, False, False, True, True, True, 2)
    assert report["second_request_is_new_staging"] is True
    assert _nav(steps["exit_while_running"]) == ("Setup", True, False, False, True, True, True, 2)
    assert report["closed"] is True and report["staged_removed_after_close"] is True
    assert (report["runs"], report["requests"], report["children"]) == (2, 2, 0)
    assert report["matrix"] == EXPECTED_MATRIX


TAMPERS = ["modify", "unlisted", "consistent", "no_manifest"]


@needs_bridge
def test_an_unchanged_staged_bundle_runs_the_engine_exactly_once(tmp_path, enabled_build):
    report = _run(tmp_path, enabled_build, "none")
    assert (report["runs"], report["requests"], report["children"]) == (1, 1, 0)
    assert _nav(report["steps"]["tampered_start"]) == ("Setup", False, True, False, True, True, True, 1)
    assert report["tamper_status"].startswith("Finished:")
    assert report["closed"] is True and report["staged_removed_after_close"] is True


@needs_bridge
@pytest.mark.parametrize("tamper", TAMPERS)
def test_a_staged_bundle_changed_after_prepare_never_reaches_the_engine(tmp_path, enabled_build, tamper):
    report = _run(tmp_path, enabled_build, tamper)
    # The runner was created (the bundle object is still held), but the engine never ran.
    assert (report["runs"], report["requests"], report["children"]) == (0, 1, 0)
    if tamper == "consistent":
        assert report["tamper_self_consistent"] is True  # the engine's own manifest check would accept it; the pin does not
    # The existing failure outcome: the run ended without running, and the window is usable again.
    assert _nav(report["steps"]["tampered_start"])[:6] == ("Setup", False, True, False, True, True)
    assert report["tamper_outcome"] == "Setup could not run" and report["tamper_outcome_visible"] is True
    assert report["tamper_status"].startswith("Setup could not run: InvalidOperationException")
    # The staged copy stays held until the window closes, and is then removed as usual (extra file included).
    assert report["closed"] is True and report["staged_removed_after_close"] is True
