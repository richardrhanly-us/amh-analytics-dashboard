"""Tests for the Prepare step, M2c-2: installer/SortView.Setup.Wizard/PreparePage and installer/SortView.Setup/
PreparationStep.cs, wired into the production window (Welcome -> Prerequisites -> Ready -> Prepare -> Setup).

  1. STATIC (run everywhere): the production window uses the installer's own preparation; progress is posted (never a
     synchronous Dispatcher.Invoke), the staging worker is never waited for on the UI thread except where it cannot need
     it, nothing removes earlier runs, and the stage line -- not the file count -- is what screen readers hear.
  2. BEHAVIOR (Windows, unelevated): the production window loaded in-process, its preparation replaced -- through the
     internal PreparationFactory seam -- by one over a fixture bundle, a pin and a disposable staging policy in pytest's
     temporary folders. Nothing is created under Program Files, and nothing runs setup.

Also home to the shared fixture helpers other production-window probes use to get past Prepare.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from test_installer_bridge import _MSBUILD, _POWERSHELL, needs_bridge

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_DIR = REPO_ROOT / "installer" / "SortView.Setup"
WIZARD_DIR = REPO_ROOT / "installer" / "SortView.Setup.Wizard"
SETUP_PROJECT = SETUP_DIR / "SortView.Setup.csproj"
STEP_CODE = SETUP_DIR / "PreparationStep.cs"
WINDOW_CODE = SETUP_DIR / "MainWindow.xaml.cs"
PAGE_XAML = WIZARD_DIR / "PreparePage.xaml"
PAGE_CODE = WIZARD_DIR / "PreparePage.xaml.cs"

FIXTURE_VERSION = "1.2.3"
FIXTURE_FILES = {
    "setup.ps1": b"# fixture, not a script",
    "install.ps1": b"# fixture, not a script",
    "collector_config.example.json": b'{"api_url": "https://fixture.invalid"}',
    "runtime/SortViewCollector.exe": b"fixture: not a program",
    "runtime/_internal/library.dll": b"fixture: not a library",
    **{f"runtime/_internal/part{i:03}.dat": bytes([i % 251]) * 4096 for i in range(40)},
}


def write_fixture_bundle(root: Path, version: str = FIXTURE_VERSION) -> str:
    """A fixture frozen bundle (plain bytes, never a real Collector); returns its manifest SHA-256."""
    root.mkdir(parents=True)
    for path, data in FIXTURE_FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    entries = [{"path": p, "sha256": hashlib.sha256(d).hexdigest(), "size_bytes": len(d)} for p, d in sorted(FIXTURE_FILES.items())]
    manifest = (json.dumps({"product": "SortView Collector", "version": version, "built_at": "2026-01-01T00:00:00Z", "files": entries},
                           indent=2) + "\n").encode()
    (root / "MANIFEST.json").write_bytes(manifest)
    return hashlib.sha256(manifest).hexdigest()


#: C# (reflection only: compiles with WPF and System.Core alone) that replaces a production window's preparation, through
#: its internal PreparationFactory seam, with one over a fixture bundle, a pin and a disposable staging policy that also
#: trusts the current user. ElevationGate holds the preparation at its elevation check while a test closes it.
FIXTURE_PREPARATION_CS = r"""
public static class FixturePreparation {
    public static readonly System.Threading.ManualResetEventSlim ElevationGate = new System.Threading.ManualResetEventSlim(true);
    const System.Reflection.BindingFlags Any = System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic |
        System.Reflection.BindingFlags.Instance;
    public static void Install(System.Windows.Window window, string source, string sha, string version, string root) {
        var setup = window.GetType().Assembly;
        System.Type prepType = setup.GetType("SortView.Setup.BundlePreparation", true);
        System.Type pinType = setup.GetType("SortView.Setup.TrustedBundlePin", true);
        System.Reflection.ConstructorInfo ctor = prepType.GetConstructors(Any)[0];
        System.Type policyType = ctor.GetParameters()[2].ParameterType;
        object pin = pinType.GetConstructor(Any, null, new[] { typeof(string), typeof(string) }, null).Invoke(new object[] { sha, version });
        var me = System.Security.Principal.WindowsIdentity.GetCurrent().User;
        System.Func<string, System.Security.Principal.SecurityIdentifier> sid = s => new System.Security.Principal.SecurityIdentifier(s);
        var trusted = new[] { sid("S-1-5-18"), sid("S-1-5-32-544"), sid("S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"), me };
        var access = new[] { sid("S-1-5-18"), sid("S-1-5-32-544"), me };
        object policy = System.Activator.CreateInstance(policyType, new object[] { root, trusted, access, null });
        System.Func<bool> elevated = () => { ElevationGate.Wait(); return true; };
        var body = System.Linq.Expressions.Expression.New(ctor, System.Linq.Expressions.Expression.Constant(source),
            System.Linq.Expressions.Expression.Constant(pin, pinType), System.Linq.Expressions.Expression.Constant(policy, policyType),
            System.Linq.Expressions.Expression.Constant(elevated));
        System.Delegate factory = System.Linq.Expressions.Expression.Lambda(typeof(System.Func<>).MakeGenericType(prepType), body).Compile();
        window.GetType().GetProperty("PreparationFactory", Any).SetValue(window, factory);
    }
}
"""


# =====================================================================================================================
# 1. STATIC
# =====================================================================================================================

def _code(path: Path) -> str:
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def test_the_window_prepares_with_the_installers_own_preparation_only():
    window = re.sub(r"\s+", " ", _code(WINDOW_CODE))
    assert ("internal Func<BundlePreparation> PreparationFactory { get; set; } = BundlePreparation.CreateForThisInstaller;") in window
    assert "new PreparationStep(PrepareView, Dispatcher, () => PreparationFactory())" in window
    assert "PreparationFactory =" not in window.replace("PreparationFactory { get; set; } =", "")  # nothing else sets it
    for path in SETUP_DIR.glob("*.cs"):
        code = _code(path)
        for forbidden in ("new StagingPolicy(", "RemoveAbandonedRun", "GetCommandLineArgs", "GetEnvironmentVariable"):
            assert forbidden not in code, (path.name, forbidden)


def test_progress_is_posted_and_the_worker_never_waits_for_the_ui():
    step = _code(STEP_CODE)
    assert "new Progress<StagingProgress>(p => OnProgress(current, p))" in step
    # Bound to the window's dispatcher explicitly, whatever context the caller had (restored afterwards), and only ever
    # started on the UI thread.
    start = re.sub(r"\s+", " ", step[step.index("private void Start()"):step.index("private void OnProgress")])
    assert start.index("dispatcher.VerifyAccess();") < start.index("++generation")
    bind = "SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext(dispatcher));"
    assert start.index(bind) < start.index("new Progress<StagingProgress>") < start.index(
        "finally { SynchronizationContext.SetSynchronizationContext(previous); }") < start.index("PrepareAsync(progress)")
    enter = step[step.index("public void Enter()"):step.index("public IReadOnlyList<StagingProblem> Leave()")]
    assert enter.index("dispatcher.VerifyAccess();") < enter.index("createPreparation()")
    assert "dispatcher.BeginInvoke(new Action(() => OnCompleted(current, done)))" in step
    for source in (step, _code(WINDOW_CODE), _code(PAGE_CODE)):
        assert "Dispatcher.Invoke(" not in source and "dispatcher.Invoke(" not in source and ".Invoke(new Action" not in source
        assert ".Wait()" not in source and ".Result;" not in source.replace("done.Result :", "")
    # Late reports and completions are matched against the current generation.
    assert "if (current != generation || cancelling)" in step and "if (current != generation)" in step


def test_navigation_closing_and_exit_respect_the_preparation():
    window = re.sub(r"\s+", " ", _code(WINDOW_CODE))
    assert "private bool IsBusy => Wizard.IsRunning || preparation.IsBusy;" in window
    assert "(CurrentPage == WizardPage.Prepare && preparation.IsPrepared)" in window
    assert "if (previous == WizardPage.Prepare && page == WizardPage.Ready) { preparation.Leave();" in window
    assert "if (Wizard.IsRunning) { return;" in window and "if (preparation.DeferClose(Close)) { e.Cancel = true; }" in window
    app = re.sub(r"\s+", " ", _code(SETUP_DIR / "App.xaml.cs"))
    exit_ = app[app.index("protected override void OnExit"):]
    assert exit_.index("DisposePreparation();") < exit_.index("ReleaseLock();")  # the staged bundle before the lock


def test_screen_readers_hear_stages_not_file_counts():
    page = PAGE_XAML.read_text(encoding="utf-8")
    assert re.search(r'x:Name="StageText"[^>]*AutomationProperties\.LiveSetting="Polite"', page)
    assert "LiveSetting" not in re.search(r'<TextBlock x:Name="CountText"[^>]*/>', page).group(0)
    assert 'AutomationProperties.Name="Preparing the installation files"' in page
    code = _code(PAGE_CODE)
    assert code.count("RaiseAutomationEvent(AutomationEvents.LiveRegionChanged)") == 1  # only in SetStage
    count = code[code.index("public void ShowCount"):code.index("public void ShowCancelling")]
    assert "SetStage" not in count and "RaiseAutomationEvent" not in count


# =====================================================================================================================
# 2. BEHAVIOR
# =====================================================================================================================

_PROBE = r"""
param([string]$ExePath, [string]$CasesPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -ReferencedAssemblies PresentationFramework, PresentationCore, WindowsBase, System.Xaml, System.Core, System.Web.Extensions -TypeDefinition @"
using System; using System.Collections; using System.Collections.Generic; using System.IO; using System.Linq; using System.Reflection;
using System.Threading; using System.Web.Script.Serialization;
using System.Windows; using System.Windows.Controls; using System.Windows.Controls.Primitives; using System.Windows.Threading;
@@FIXTURE@@
public static class PrepareProbe {
    const BindingFlags Any = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
    static void Pump() {
        var frame = new DispatcherFrame();
        Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() => frame.Continue = false));
        Dispatcher.PushFrame(frame);
    }
    static bool WaitUntil(Func<bool> condition, int seconds) {
        var stop = DateTime.UtcNow.AddSeconds(seconds);
        while (!condition()) { if (DateTime.UtcNow > stop) return false; Thread.Sleep(10); Pump(); }
        return true;
    }
    static Window window;
    static FrameworkElement Find(string name) { return (FrameworkElement)window.FindName(name); }
    static FrameworkElement Page(string name) { return (FrameworkElement)Find("PrepareView").FindName(name); }
    static void Click(string name) { (Find(name) ?? Page(name)).RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); Pump(); }
    static string Text(string name) { return ((TextBlock)Page(name)).Text; }
    static string PageName() { return window.GetType().GetProperty("CurrentPage").GetValue(window).ToString(); }
    static bool Busy() { return !(Find("BackButton").IsEnabled); }
    static List<string> Entries(string root) { return Directory.Exists(root) ? Directory.GetFileSystemEntries(root).Select(Path.GetFileName).OrderBy(x => x).ToList() : new List<string>(); }

    static Window Open(Assembly setup) {
        var w = (Window)Activator.CreateInstance(setup.GetType("SortView.Setup.MainWindow", true));
        w.WindowStartupLocation = WindowStartupLocation.Manual; w.Left = -32000; w.Top = -32000; w.ShowActivated = false;
        w.Show(); Pump();
        return w;
    }
    static void ToReady() {
        // Past Prerequisites on facts that pass (injected: the session is not elevated; MachineFacts.Read is never called).
        var checks = Find("PrerequisitesView");
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
        Click("NextButton");
        WaitUntil(() => (bool)checks.GetType().GetProperty("CanContinue").GetValue(checks), 10);
        Click("NextButton");
    }
    static Dictionary<string, object> Snap(string root) {
        return new Dictionary<string, object> {
            { "page", PageName() }, { "prepare_visible", Find("PrepareView").IsVisible }, { "back", Find("BackButton").IsEnabled },
            { "next", Find("NextButton").IsEnabled }, { "exit", Find("ExitButton").IsEnabled }, { "stage", Text("StageText") },
            { "detail", Text("DetailText") }, { "count", Text("CountText") }, { "busy_bar", Page("BusyIndicator").IsVisible },
            { "retry", Page("RetryButton").IsVisible }, { "cancel", Page("CancelPrepareButton").IsVisible },
            { "earlier", Page("EarlierRunsNotice").IsVisible ? Text("EarlierRunsText") : null },
            { "open", window.IsVisible }, { "root", root == null ? null : Entries(root) } };
    }
    static bool ControllerPreparing() {
        var step = window.GetType().GetField("preparation", Any).GetValue(window);
        var prep = step.GetType().GetField("preparation", Any).GetValue(step);
        return prep != null && (bool)prep.GetType().GetProperty("IsPreparing").GetValue(prep);
    }

    public static Dictionary<string, object> RunOnSta(string exePath, string casesJson) {
        Dictionary<string, object> outcome = null; Exception failure = null;
        // As in the application: UI-thread code runs under the dispatcher's synchronization context (Progress<T> captures it).
        var thread = new Thread(() => { try { SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext());
            outcome = Run(exePath, casesJson); } catch (Exception e) { failure = e; } });
        thread.SetApartmentState(ApartmentState.STA); thread.Start(); thread.Join();
        if (failure != null) return new Dictionary<string, object> { { "probe_error", failure.ToString() } };
        return outcome;
    }
    static Dictionary<string, object> Run(string exePath, string casesJson) {
        var cases = (IDictionary<string, object>)new JavaScriptSerializer().DeserializeObject(casesJson);
        var setup = Assembly.LoadFrom(exePath);
        var outcome = new Dictionary<string, object>();
        foreach (var pair in cases) {
            var c = (IDictionary<string, object>)pair.Value; var r = new Dictionary<string, object>(); var steps = new Dictionary<string, object>();
            string root = c.ContainsKey("root") ? (string)c["root"] : null; string kind = (string)c["kind"];
            FixturePreparation.ElevationGate.Set();
            try {
                window = Open(setup);
                if (kind != "no_pin") FixturePreparation.Install(window, (string)c["source"], (string)c["sha"], (string)c["version"], root);
                // No synchronization context at all on the UI thread: the preparation must bind to the dispatcher itself.
                if (kind == "no_ui_context") SynchronizationContext.SetSynchronizationContext(null);
                ToReady();
                if (kind == "cancel_and_retry" || kind == "close_while_preparing") FixturePreparation.ElevationGate.Reset();
                if (kind == "off_ui_thread") {
                    // The step is entered from another thread: refused before anything is created.
                    var step = window.GetType().GetField("preparation", Any).GetValue(window);
                    string refused = "ACCEPTED";
                    var other = new Thread(() => {
                        try { step.GetType().GetMethod("Enter").Invoke(step, null); }
                        catch (TargetInvocationException e) { refused = e.InnerException.GetType().Name; }
                        catch (Exception e) { refused = "other: " + e.GetType().Name; } });
                    other.Start(); other.Join(); Pump();
                    r["off_thread_enter"] = refused;
                    r["created_off_thread"] = step.GetType().GetField("preparation", Any).GetValue(step) != null;
                    r["root_after_refusal"] = Entries(root);
                }
                var uiThread = Thread.CurrentThread.ManagedThreadId; var countThreads = new List<int>();
                System.ComponentModel.DependencyPropertyDescriptor.FromProperty(TextBlock.TextProperty, typeof(TextBlock))
                    .AddValueChanged(Page("CountText"), (s, e) => countThreads.Add(Thread.CurrentThread.ManagedThreadId));
                Click("NextButton");
                steps["entered"] = Snap(root);  // straight after Next: preparing (nothing pumped in between)
                if (kind == "no_ui_context") {
                    r["context_during_start"] = SynchronizationContext.Current == null ? "none" : SynchronizationContext.Current.GetType().Name;
                    // Not pumping: reports cannot reach the page until the UI thread runs them -- they are posted, never run
                    // on the worker (or a pool thread, which would throw on the page and end this process).
                    var stop = DateTime.UtcNow.AddSeconds(30);
                    while (ControllerPreparing() && DateTime.UtcNow < stop) Thread.Sleep(10);
                    r["worker_finished_without_ui"] = !ControllerPreparing();
                    steps["before_pump"] = Snap(root);
                    r["settled"] = WaitUntil(() => !Busy(), 30); steps["settled"] = Snap(root);
                } else if (kind == "cancel_and_retry") {
                    Click("BackButton"); Click("NextButton"); steps["forced_while_busy"] = Snap(root);
                    Click("CancelPrepareButton"); steps["cancelling"] = Snap(root);
                    FixturePreparation.ElevationGate.Set();
                    r["settled"] = WaitUntil(() => !Busy(), 30); steps["cancelled"] = Snap(root);
                    Click("RetryButton");
                    r["settled_again"] = WaitUntil(() => !Busy(), 30); steps["retried"] = Snap(root);
                } else if (kind == "close_while_preparing") {
                    window.Close(); Pump(); steps["close_requested"] = Snap(root);
                    // The UI thread is not blocked while the worker is held: a posted marker runs.
                    bool marker = false; window.Dispatcher.BeginInvoke(new Action(() => marker = true)); Pump(); Pump();
                    r["ui_responsive"] = marker;
                    FixturePreparation.ElevationGate.Set();
                    r["closed"] = WaitUntil(() => !window.IsVisible, 30); steps["after_close"] = Snap(root);
                } else if (kind == "ui_blocked") {
                    // The UI thread does not pump at all: a worker that needed it (a synchronous Invoke) would never finish.
                    var stop = DateTime.UtcNow.AddSeconds(30);
                    while (ControllerPreparing() && DateTime.UtcNow < stop) Thread.Sleep(10);
                    r["worker_finished_without_ui"] = !ControllerPreparing();
                    // Released before any posted progress or completion is delivered (as the application's exit would).
                    window.GetType().GetMethod("DisposePreparation", Any).Invoke(window, null);
                    for (int i = 0; i < 20; i++) Pump();
                    steps["late"] = Snap(root);
                } else {
                    r["settled"] = WaitUntil(() => !Busy(), 30); steps["settled"] = Snap(root);
                    if (kind == "missing_then_retry") {
                        Directory.Move((string)c["stash"], (string)c["source"]);
                        Click("RetryButton");
                        r["settled_again"] = WaitUntil(() => !Busy(), 30); steps["retried"] = Snap(root);
                    }
                    if (kind == "success") {
                        string firstRun = Entries(root).FirstOrDefault();
                        Click("NextButton"); steps["setup"] = Snap(root);
                        var wizard = Find("Wizard");
                        r["installation_enabled"] = wizard.GetType().GetProperty("IsInstallationEnabled").GetValue(wizard);
                        r["start_enabled"] = ((FrameworkElement)wizard.FindName("StartButton")).IsEnabled;
                        try { wizard.GetType().GetMethod("BeginRun").Invoke(wizard, new object[] { null, null }); r["begin_run"] = "ACCEPTED"; }
                        catch (TargetInvocationException e) { r["begin_run"] = e.InnerException.GetType().Name; }
                        Click("BackButton"); steps["back_from_setup"] = Snap(root);
                        Click("BackButton"); steps["back_to_ready"] = Snap(root);
                        Click("NextButton"); r["settled_again"] = WaitUntil(() => !Busy(), 30); steps["prepared_again"] = Snap(root);
                        r["same_run_reused"] = Entries(root).Contains(firstRun);
                    }
                }
                if (window.IsVisible) { window.Close(); WaitUntil(() => !window.IsVisible, 30); }
                r["closed_at_end"] = !window.IsVisible;
                r["root_after"] = root == null ? null : Entries(root);
                r["count_updates"] = countThreads.Count; r["count_updates_on_ui_thread"] = countThreads.All(id => id == uiThread);
            } catch (Exception e) { r["error"] = e.ToString(); }
            SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext());
            FixturePreparation.ElevationGate.Set();
            r["steps"] = steps;
            outcome[pair.Key] = r;
        }
        return outcome;
    }
}
"@
$result = [PrepareProbe]::RunOnSta($ExePath, (Get-Content -LiteralPath $CasesPath -Raw))
ConvertTo-Json -InputObject $result -Depth 8 | Set-Content -LiteralPath $OutPath -Encoding UTF8
""".replace("@@FIXTURE@@", FIXTURE_PREPARATION_CS)


@pytest.fixture(scope="module")
def results(tmp_path_factory) -> dict:
    completed = subprocess.run([_MSBUILD, str(SETUP_PROJECT), "/p:Configuration=Release", "/nologo", "/v:minimal"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    base = tmp_path_factory.mktemp("prepare")
    cases: dict[str, dict] = {}

    def case(name: str, *, kind: str | None = None, sha: str | None = None, version: str = FIXTURE_VERSION, **extra) -> dict:
        source = base / "sources" / name
        real = write_fixture_bundle(source)
        root = base / "roots" / name
        root.mkdir(parents=True)
        cases[name] = {"kind": kind or name, "source": str(source), "sha": real if sha is None else sha, "version": version,
                       "root": str(root), **extra}
        return cases[name]

    case("success")
    cases["no_pin"] = {"kind": "no_pin"}
    case("hash_mismatch", kind="failure", sha=hashlib.sha256(b"another manifest").hexdigest())
    tampered = case("tampered", kind="failure")
    (Path(tampered["source"]) / "runtime/_internal/library.dll").write_bytes(b"FIXTURE: NOT A LIBRARY")
    missing = case("missing_then_retry")
    stash = base / "stash" / "missing_then_retry"
    stash.parent.mkdir(parents=True)
    Path(missing["source"]).rename(stash)
    missing["stash"] = str(stash)
    case("cancel_and_retry")
    case("close_while_preparing")
    case("ui_blocked")
    case("no_ui_context")
    case("off_ui_thread", kind="off_ui_thread")
    earlier = case("earlier_runs", kind="failure_free")
    (Path(earlier["root"]) / "0123456789abcdef0123456789abcdef").mkdir()

    (base / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (base / "probe.ps1").write_text(_PROBE, encoding="utf-8")
    exe = SETUP_DIR / "bin" / "Release" / "SortView.Setup.exe"
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(base / "probe.ps1"),
                                "-ExePath", str(exe), "-CasesPath", str(base / "cases.json"), "-OutPath", str(base / "out.json")],
                               capture_output=True, text=True, timeout=600, check=False)
    assert (base / "out.json").exists(), completed.stdout + completed.stderr
    out = json.loads((base / "out.json").read_text(encoding="utf-8-sig"))
    assert "probe_error" not in out, out["probe_error"]
    return out


def _r(results: dict, name: str) -> dict:
    r = results[name]
    assert "error" not in r, r.get("error")
    return r


def _buttons(step: dict) -> tuple:
    return step["back"], step["next"], step["exit"]


@needs_bridge
def test_prepare_comes_after_ready_and_is_busy_until_it_settles(results):
    entered = _r(results, "success")["steps"]["entered"]
    assert entered["page"] == "Prepare" and entered["prepare_visible"] is True
    assert _buttons(entered) == (False, False, True) and entered["busy_bar"] is True and entered["cancel"] is True
    assert entered["stage"] == "Checking the installation package..."


@needs_bridge
def test_a_verified_bundle_enables_next_and_shows_its_version(results):
    r = _r(results, "success")
    settled = r["steps"]["settled"]
    assert r["settled"] is True and _buttons(settled) == (True, True, True)
    assert settled["stage"] == "The installation package is verified and ready."
    assert settled["detail"].startswith(f"SortView Collector {FIXTURE_VERSION}: {len(FIXTURE_FILES)} files")
    assert settled["count"] == f"Copied {len(FIXTURE_FILES)} of {len(FIXTURE_FILES)} files."
    assert settled["busy_bar"] is False and settled["retry"] is False and len(settled["root"]) == 1  # the held run


@needs_bridge
def test_setup_stays_disabled_after_a_successful_preparation(results):
    r = _r(results, "success")
    assert r["steps"]["setup"]["page"] == "Setup" and len(r["steps"]["setup"]["root"]) == 1  # still held on Setup
    assert r["installation_enabled"] is False and r["start_enabled"] is False and r["begin_run"] == "InvalidOperationException"


@needs_bridge
def test_back_releases_the_bundle_and_coming_forward_prepares_afresh(results):
    r = _r(results, "success")
    steps = r["steps"]
    assert steps["back_from_setup"]["page"] == "Prepare" and steps["back_from_setup"]["next"] is True  # kept from Setup
    assert steps["back_to_ready"]["page"] == "Ready" and steps["back_to_ready"]["root"] == []  # released
    assert r["settled_again"] is True and steps["prepared_again"]["next"] is True
    assert len(steps["prepared_again"]["root"]) == 1 and r["same_run_reused"] is False  # a new run
    assert r["closed_at_end"] is True and r["root_after"] == []


@needs_bridge
def test_no_trusted_pin_refuses_and_keeps_next_off(results):
    settled = _r(results, "no_pin")["steps"]["settled"]
    assert settled["stage"] == "This setup program does not trust any installation package."
    assert _buttons(settled) == (True, False, True) and settled["retry"] is False
    assert not (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "SortView").exists()


@needs_bridge
@pytest.mark.parametrize(("name", "stage"), [("hash_mismatch", "This installation package is not the one this setup program trusts."),
                                             ("tampered", "The installation package is damaged or incomplete.")])
def test_a_package_that_does_not_match_the_pin_is_refused(results, name, stage):
    r = _r(results, name)
    settled = r["steps"]["settled"]
    assert settled["stage"] == stage and _buttons(settled) == (True, False, True) and settled["retry"] is True
    assert settled["root"] == [] and r["root_after"] == []


@needs_bridge
def test_a_missing_bundle_can_be_retried_once_it_is_there(results):
    r = _r(results, "missing_then_retry")
    assert r["steps"]["settled"]["stage"] == "The installation package was not found." and r["steps"]["settled"]["retry"] is True
    assert r["steps"]["settled"]["next"] is False
    retried = r["steps"]["retried"]
    assert r["settled_again"] is True and retried["stage"] == "The installation package is verified and ready." and retried["next"] is True


@needs_bridge
def test_busy_preparation_refuses_navigation_and_can_be_cancelled_and_retried(results):
    r = _r(results, "cancel_and_retry")
    steps = r["steps"]
    assert steps["forced_while_busy"]["page"] == "Prepare" and _buttons(steps["forced_while_busy"]) == (False, False, True)
    assert steps["cancelling"]["detail"].startswith("Cancelling")
    assert r["settled"] is True and steps["cancelled"]["stage"] == "Preparation was cancelled."
    assert steps["cancelled"]["retry"] is True and steps["cancelled"]["next"] is False and steps["cancelled"]["root"] == []
    assert r["settled_again"] is True and steps["retried"]["next"] is True
    assert r["root_after"] == []


@needs_bridge
def test_closing_while_preparing_waits_without_blocking_the_ui_and_cleans_up(results):
    r = _r(results, "close_while_preparing")
    assert r["steps"]["close_requested"]["open"] is True  # deferred: the preparation is still running
    assert r["ui_responsive"] is True  # the UI thread never waits for the worker
    assert r["closed"] is True and r["steps"]["after_close"]["root"] == [] and r["root_after"] == []


@needs_bridge
def test_the_worker_never_needs_the_ui_thread_and_late_updates_are_ignored(results):
    # Cross-thread deadlock regression: the UI thread did not pump at all, and the preparation still finished -- so the
    # worker never waits (synchronously) for the UI. Its progress and completion, delivered only after the step was
    # released, changed nothing on the page.
    r = _r(results, "ui_blocked")
    assert r["worker_finished_without_ui"] is True
    late = r["steps"]["late"]
    assert late["stage"] == "Checking the installation package..." and late["next"] is False and late["count"] == ""
    assert late["root"] == [] and r["root_after"] == []


@needs_bridge
def test_earlier_runs_are_reported_and_never_removed(results):
    r = _r(results, "earlier_runs")
    settled = r["steps"]["settled"]
    assert settled["stage"] == "The installation package is verified and ready." and settled["next"] is True
    assert settled["earlier"].startswith("1 folder from earlier setup attempts remain")
    assert r["root_after"] == ["0123456789abcdef0123456789abcdef"]  # still there


@needs_bridge
def test_progress_reaches_the_page_only_on_the_ui_thread_even_without_a_ui_context(results):
    # The UI thread had NO synchronization context when preparation started (the case that once sent progress to the
    # thread pool). The worker finished without the UI thread pumping, and nothing reached the page until it did: the
    # reports were posted to the dispatcher. Then every count update ran on the UI thread.
    r = _r(results, "no_ui_context")
    assert r["context_during_start"] == "none" and r["worker_finished_without_ui"] is True
    before = r["steps"]["before_pump"]
    assert before["stage"] == "Checking the installation package..." and before["count"] == "" and before["next"] is False
    settled = r["steps"]["settled"]
    assert r["settled"] is True and settled["stage"] == "The installation package is verified and ready." and settled["next"] is True
    assert settled["count"] == f"Copied {len(FIXTURE_FILES)} of {len(FIXTURE_FILES)} files."
    assert r["count_updates"] > 2 and r["count_updates_on_ui_thread"] is True
    assert r["root_after"] == []


@needs_bridge
def test_count_updates_always_run_on_the_ui_thread(results):
    for name in ("success", "cancel_and_retry", "missing_then_retry", "earlier_runs"):
        r = _r(results, name)
        assert r["count_updates"] > 0 and r["count_updates_on_ui_thread"] is True, name


@needs_bridge
def test_the_step_refuses_to_start_off_the_ui_thread(results):
    r = _r(results, "off_ui_thread")
    assert r["off_thread_enter"] == "InvalidOperationException"  # Dispatcher.VerifyAccess
    assert r["created_off_thread"] is False and r["root_after_refusal"] == []  # nothing created, nothing staged
    # The same window then prepares normally from the UI thread.
    assert r["settled"] is True and r["steps"]["settled"]["next"] is True and r["root_after"] == []
