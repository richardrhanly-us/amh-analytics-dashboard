"""Tests for the Phase 3C developer test window: installer/SortView.Setup.Spike (SIMULATION ONLY).

The window runs the REAL setup engine through SetupRunSession and the Core simulation harness, against a simulated
machine it generates itself (Simulation/SimulationWorkspace.cs): the repository's setup.ps1, stub tools, a reserved
.invalid API address, scratch roots under %TEMP%, and the test suite's own fakes -- so nothing here touches the network,
Task Scheduler, a credential store or a real install location.

  1. STATIC checks of the window's sources (run everywhere): its fakes are the test suite's, it has no path to a real
     run or a real bundle, it never handles the code as text, and it never calls into its UI thread synchronously.
  2. UI scenarios (Windows only): the built window is launched and driven through UI Automation, as a person would,
     through all five questions, cancellation and closing.

Not automated (manual inspection): the Windows file picker itself, the look of the window, and keyboard use.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from test_collector_setup import (
    _FINISH_STUB_BODY,
    _INSTALL_STUB_BODY,
    _TOKEN_STUB_BODY,
    _WRAPPER_OVERRIDES,
)
from test_installer_bridge import _MSBUILD, _POWERSHELL, needs_bridge

REPO_ROOT = Path(__file__).resolve().parent.parent
SPIKE_DIR = REPO_ROOT / "installer" / "SortView.Setup.Spike"
SPIKE_PROJECT = SPIKE_DIR / "SortView.Setup.Spike.csproj"
SIMULATION_DIR = SPIKE_DIR / "Simulation"
WINDOW_CODE = SPIKE_DIR / "MainWindow.xaml.cs"
WORKSPACE_CODE = SIMULATION_DIR / "SimulationWorkspace.cs"
# The shared wizard (Phase 4B.1): the questions, progress, cancellation and closing the spike hosts now live here.
WIZARD_CODE = REPO_ROOT / "installer" / "SortView.Setup.Wizard" / "SetupWizardView.xaml.cs"
SPIKE_SOURCES = [WINDOW_CODE, WORKSPACE_CODE, SPIKE_DIR / "MainWindow.xaml", SPIKE_DIR / "App.xaml"]

DUMMY_CODE = "SIM-UIA-DUMMY-CODE-7Q2X"  # synthetic: never a real enrollment code
SIMULATION_API = "https://sortview-api.invalid"
LEGACY_TOOLS = ["install-verify", "runtime-version", "http:GET", "http:POST", "http:POST", "set-api-token", "install", "finish-install"]


def _code(path: Path) -> str:
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("//"))


# =====================================================================================
# 1. STATIC checks
# =====================================================================================

@pytest.mark.parametrize(("asset", "constant"), [
    ("wrapper-overrides.ps1", _WRAPPER_OVERRIDES), ("install-stub.ps1", _INSTALL_STUB_BODY),
    ("token-stub.ps1", _TOKEN_STUB_BODY), ("finish-stub.ps1", _FINISH_STUB_BODY),
])
def test_the_windows_fakes_are_exactly_the_test_suites(asset, constant):
    on_disk = (SIMULATION_DIR / asset).read_text(encoding="utf-8").replace("\r\n", "\n")
    assert on_disk == constant  # one set of fakes: change them in tests/test_collector_setup.py and regenerate these


def test_the_window_can_only_run_a_generated_simulation():
    window, workspace, wizard = _code(WINDOW_CODE), _code(WORKSPACE_CODE), _code(WIZARD_CODE)
    # Its ONE way to run setup: the shared wizard, given a runner over the simulation harness with the workspace's own
    # request. The wizard itself starts every run through SetupRunSession and never chooses what runs.
    assert window.count("Wizard.BeginRun(") == 1
    assert "SimulatedSetupHarness.Run(request, presenter, onEvent, onConsoleText, token)" in window
    assert "SimulationRequest request = workspace.Request;" in window
    assert wizard.count("SetupRunSession.Start(") == 1
    for real_path in ("SetupEngineHost", "SetupRunRequest", "BundleRoot =", "FolderBrowserDialog", "CommonOpenFileDialog", "Environment.GetCommandLineArgs"):
        assert real_path not in window, real_path
        assert real_path not in wizard, real_path
    assert "OpenFileDialog" not in window  # the window offers no file picker of its own...
    assert wizard.count("new OpenFileDialog") == 1 and "private void BrowseButton_Click" in wizard  # ...the wizard's is for source files only
    # A picked file is only put in the path box; the customer sends it ("Use this file"). Nothing is submitted for them.
    browse = wizard[wizard.index("private void BrowseButton_Click"):wizard.index("private void CodeBox_PasswordChanged")]
    assert "Answer(" not in browse and "PathBox.Text = dialog.FileName;" in browse
    # The simulated machine lives under %TEMP% and talks only to a reserved .invalid host.
    assert 'Path.Combine(Path.GetTempPath(), "SortViewSetupSimulation"' in workspace
    assert f'public const string SimulationApiUrl = "{SIMULATION_API}";' in workspace
    assert 'File.Copy(Path.Combine(deploy, "setup-collector.ps1"), Path.Combine(BundleRoot, "setup.ps1"));' in workspace  # the REAL engine


def test_the_window_never_handles_the_code_as_text_or_writes_secrets_anywhere():
    window, workspace, wizard = _code(WINDOW_CODE), _code(WORKSPACE_CODE), _code(WIZARD_CODE)
    for source in (window, wizard):
        for forbidden in ("CodeBox.Password", "Marshal", "BSTR", "NetworkCredential", "Console.", "Debug.", "Trace.", "File.",
                          "SetEnvironmentVariable", "Clipboard"):
            assert forbidden not in source, forbidden
    # The code goes from the wizard's PasswordBox to the run as a read-only SecureString copy; the box is cleared at once.
    assert "SecureString code = CodeBox.SecurePassword;" in wizard and "code.MakeReadOnly();" in wizard and "CodeBox.Clear();" in wizard
    # The only environment the workspace sets: the fakes' fixed switches and the scratch record folder.
    set_names = re.findall(r'SetEnvironmentVariable\(("?[\w$]+"?)', workspace)
    assert set(set_names) <= {"name", '"SETUPTEST_FAKE_DIR"', '"SETUPTEST_INSTALL_MODE"', '"SETUPTEST_FINISH_MODE"', "SlowVariable"}
    # The two changes to the shared fakes that keep secrets out of files: the token store keeps no value, and the call
    # recorder never writes an enrollment code.
    assert "-Value 'stored (simulated; the value is not kept)'" in workspace
    assert "$Record.body.enrollment_code = '<not recorded>'" in workspace


def test_the_window_never_calls_into_its_ui_thread_synchronously_and_is_labelled_simulation_only():
    for source in (_code(WINDOW_CODE), _code(WIZARD_CODE)):
        assert not re.search(r"Dispatcher\.Invoke\(|\.Wait\(|\.Result\b(?!\))", source.replace("task.Result", "")), "blocking call"
    assert "Dispatcher.BeginInvoke(" in _code(WIZARD_CODE)  # the wizard posts its run results to the UI thread
    xaml = (SPIKE_DIR / "MainWindow.xaml").read_text(encoding="utf-8")
    assert "SIMULATION ONLY" in re.search(r'Title="([^"]+)"', xaml).group(1)
    assert "SIMULATION ONLY" in xaml[xaml.index('x:Name="SimulationBanner"'):][:400]
    assert not (SPIKE_DIR / "SpikeRunner.cs").exists()  # the Phase 0 synthetic workflow is gone


# =====================================================================================
# 2. UI scenarios: the built window, driven through UI Automation
# =====================================================================================

@pytest.fixture(scope="module")
def window_exe() -> Path:
    completed = subprocess.run([_MSBUILD, str(SPIKE_PROJECT), "/p:Configuration=Release", "/nologo", "/v:minimal"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return SPIKE_DIR / "bin" / "Release" / "SortView.Setup.Spike.exe"


# Steps: select/toggle/click/text (by AutomationId), wait_prompt, wait_status, wait_disabled, snapshot, close, wait_exit,
# sleep. "{workspace}" in a value is the simulated machine the window shows. While waiting, the driver measures how fast
# the UI thread answers, and counts the window's child processes and visible console windows.
_UIA_DRIVER = r"""
param([string]$ExePath, [string]$StepsPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -TypeDefinition @"
using System; using System.Collections.Generic; using System.Runtime.InteropServices; using System.Text;
public static class ConsoleWindowProbe {
    delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc p, IntPtr l);
    [DllImport("user32.dll")] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    public static int VisibleFor(int[] pids) {
        var set = new HashSet<uint>(); foreach (int p in pids) set.Add((uint)p);
        int count = 0;
        EnumWindows((h, l) => { var s = new StringBuilder(64); GetClassName(h, s, 64); uint pid;
            GetWindowThreadProcessId(h, out pid);
            if (s.ToString() == "ConsoleWindowClass" && IsWindowVisible(h) && set.Contains(pid)) count++; return true; }, IntPtr.Zero);
        return count;
    }
}
"@
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$steps = @(Get-Content -LiteralPath $StepsPath -Raw | ConvertFrom-Json | ForEach-Object { $_ })
$out = [ordered]@{ error = $null; error_stack = $null; snapshots = @(); prompts_seen = @(); prompt_history = @(); max_children = 0; max_console_windows = 0
                   latencies = @(); exited = $false; workspace = $null; command_line = $null; second_clicks = @() }
$proc = Start-Process -FilePath $ExePath -PassThru
$window = $null
for ($i = 0; $i -lt 300 -and $null -eq $window; $i++) {
    Start-Sleep -Milliseconds 100
    $window = $AE::RootElement.FindFirst($TS::Children, (New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $proc.Id)))
}
function Find([string]$id) {
    $window.FindFirst($TS::Descendants, (New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)))
}
function Sample {
    $kids = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($proc.Id)")
    if ($kids.Count -gt $out.max_children) { $out.max_children = $kids.Count }
    $pids = @($proc.Id) + @($kids | ForEach-Object { [int]$_.ProcessId })
    $visible = [ConsoleWindowProbe]::VisibleFor([int[]]$pids)
    if ($visible -gt $out.max_console_windows) { $out.max_console_windows = $visible }
    if ($proc.HasExited) { return }
    try {
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $null = (Find 'StatusText').Current.Name   # answered by the window's UI thread
        $out.latencies += $watch.ElapsedMilliseconds
        # Every question that appears on screen, in order (a new entry each time a different one shows).
        # (While no question is showing, the panel is collapsed and its id is not in the tree at all.)
        $promptElement = Find 'PromptId'
        $prompt = if ($null -ne $promptElement) { $promptElement.Current.Name } else { $null }
        if (-not [string]::IsNullOrEmpty($prompt) -and $prompt -ne 'none' -and
            ($out.prompt_history.Count -eq 0 -or $out.prompt_history[-1] -ne $prompt)) { $out.prompt_history += $prompt }
    } catch {
        # Only while the window is closing (wait_exit): UI Automation cannot reach a window that is going away.
        if (-not $proc.HasExited -and $_.Exception.Message -notmatch 'shutting down|ElementNotAvailable|not available') { throw }
    }
}
function Selected([string]$id) {
    (Find $id).GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Current.IsSelected
}
function Names($element) {
    @($element.FindAll($TS::Children, [System.Windows.Automation.Condition]::TrueCondition) | ForEach-Object { $_.Current.Name })
}
function PanelText([string]$id) {
    $panel = Find $id
    if ($null -eq $panel) { return $null }
    (@($panel.FindAll($TS::Descendants, [System.Windows.Automation.Condition]::TrueCondition) | ForEach-Object { $_.Current.Name }) -join "`n")
}
function Enabled([string]$id) { $e = Find $id; if ($null -eq $e) { $null } else { $e.Current.IsEnabled } }
function Snapshot([string]$label) {
    $cancel = Find 'CancelButton'
    [ordered]@{
        label = $label
        status = (Find 'StatusText').Current.Name
        cancel_enabled = $cancel.Current.IsEnabled
        cancel_note = (Find 'CancelNote').Current.Name
        prompt = (Find 'PromptId').Current.Name
        details = PanelText 'PromptPanel'
        outcome = PanelText 'OutcomePanel'
        use_path_enabled = Enabled 'UsePathButton'
        enroll_enabled = Enabled 'EnrollButton'
        fault = $(if (Selected 'FaultInstall') { 'install' } elseif (Selected 'FaultVerification') { 'verification' } elseif (Selected 'FaultNone') { 'none' } else { '?' })
        events = Names (Find 'EventList')
        console = Names (Find 'DiagnosticList')
        all_text = (@($window.FindAll($TS::Descendants, [System.Windows.Automation.Condition]::TrueCondition) | ForEach-Object { $_.Current.Name }) -join "`n")
    }
}
function Wait-Until([scriptblock]$condition, [int]$seconds, [string]$what) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while (-not (& $condition)) {
        if ($watch.Elapsed.TotalSeconds -gt $seconds) { throw "timed out waiting for $what" }
        Sample
        Start-Sleep -Milliseconds 150
    }
}
try {
    if ($null -eq $window) { throw "the window did not appear" }
    # The window can be in the UI Automation tree before its controls are -- and an element found that early (class
    # 'HwndWrapper[...]', before WPF's automation peers exist) never gains them. So wait (at most 30 s), finding the
    # window again each time, until it shows its simulated machine: no step then looks for a control that is not there.
    $ready = [Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        $window = $AE::RootElement.FindFirst($TS::Children, (New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $proc.Id)))
        $path = if ($null -ne $window) { Find 'WorkspacePath' } else { $null }
        if ($null -ne $path -and -not [string]::IsNullOrEmpty($path.Current.Name)) { break }
        if ($ready.Elapsed.TotalSeconds -gt 30) {
            throw "the window's controls were not ready within 30 s (window class " + $(if ($null -eq $window) { "none" } else { "'" + $window.Current.ClassName + "'" }) +
                ", WorkspacePath " + $(if ($null -eq $path) { "not found" } else { "empty" }) + ")"
        }
        Start-Sleep -Milliseconds 100
    }
    $out.command_line = (Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)").CommandLine
    $out.workspace = ((Find 'WorkspacePath').Current.Name -replace '^Simulated machine: ', '')
    foreach ($step in $steps) {
        $value = if ($null -ne $step.value) { ([string]$step.value).Replace('{workspace}', $out.workspace) } else { $null }
        switch ($step.op) {
            'select' { (Find $step.id).GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() }
            'toggle' {
                $toggle = (Find $step.id).GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
                if (($toggle.Current.ToggleState -eq 'On') -ne [bool]$step.on) { $toggle.Toggle() }
            }
            'click' { (Find $step.id).GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
            'click_again' {
                # A second press, straight after the first: records whether it could be pressed at all.
                $again = Find $step.id
                $pressed = $false
                if ($null -ne $again -and $again.Current.IsEnabled -and -not $again.Current.IsOffscreen) {
                    try { $again.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); $pressed = $true } catch { }
                }
                $out.second_clicks += $pressed
            }
            'copy' {
                $to = ([string]$step.to).Replace('{workspace}', $out.workspace)
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
                Copy-Item -LiteralPath ([string]$step.from).Replace('{workspace}', $out.workspace) -Destination $to
            }
            'text' { (Find $step.id).GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue($value) }
            'wait_prompt' {
                Wait-Until { (Find 'PromptId').Current.Name -eq $step.prompt } 90 "the question $($step.prompt)"
                Sample   # record it in prompt_history even when it was already showing
                $out.prompts_seen += $step.prompt
            }
            'wait_status' { Wait-Until { (Find 'StatusText').Current.Name -like "*$($step.contains)*" } 90 "status '$($step.contains)'" }
            'wait_disabled' { Wait-Until { -not (Find $step.id).Current.IsEnabled } 30 "$($step.id) disabled" }
            'snapshot' { $out.snapshots += Snapshot $step.label }
            'close' { $window.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close() }
            'wait_exit' {
                $watch = [Diagnostics.Stopwatch]::StartNew()
                while (-not $proc.HasExited -and $watch.Elapsed.TotalSeconds -lt 90) { Sample; Start-Sleep -Milliseconds 150 }
                $out.exited = $proc.HasExited
            }
            'sleep' { Start-Sleep -Milliseconds ([int]$step.ms) }
        }
    }
} catch {
    $out.error = $_.Exception.Message + " (driver line " + $_.InvocationInfo.ScriptLineNumber + ": " + $_.InvocationInfo.Line.Trim() + ")"
    $out.error_stack = $_.ScriptStackTrace
} finally {
    if (-not $proc.HasExited) {
        try { $window.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close() } catch { }
        if (-not $proc.WaitForExit(30000)) { $proc.Kill() }
    }
}
ConvertTo-Json -InputObject $out -Depth 6 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


def drive(window_exe: Path, scratch: Path, steps: list[dict]) -> dict:
    scratch.mkdir(parents=True, exist_ok=True)
    steps_path, out_path, driver = scratch / "steps.json", scratch / "window_out.json", scratch / "window_driver.ps1"
    steps_path.write_text(json.dumps(steps), encoding="utf-8")
    driver.write_text(_UIA_DRIVER, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(driver),
                                "-ExePath", str(window_exe), "-StepsPath", str(steps_path), "-OutPath", str(out_path)],
                               capture_output=True, text=True, timeout=600, check=False)
    assert out_path.exists(), completed.stdout + completed.stderr
    result = json.loads(out_path.read_text(encoding="utf-8-sig"))
    assert result["error"] is None, (result["error"], result["error_stack"], result["snapshots"][-1:] if result["snapshots"] else None)
    return result


def snapshot(result: dict, label: str) -> dict:
    return next(s for s in result["snapshots"] if s["label"] == label)


def workspace_calls(result: dict) -> list[dict]:
    path = Path(result["workspace"]) / "fake" / "calls.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def workspace_tools(result: dict) -> list[str]:
    names = []
    for call in workspace_calls(result):
        if call["tool"] == "http":
            names.append(f"http:{call['method'].upper()}")
        elif call["tool"] not in ("prompt", "protect-recovery", "handler"):
            names.append(call["tool"])
    return names


def simulated_token(result: dict) -> str:
    server = json.loads((Path(result["workspace"]) / "fake" / "http.json").read_text(encoding="utf-8-sig"))
    for answer in json.dumps(server).split("agent_token")[1:]:
        match = re.search(r"(SimulatedToken[0-9a-f]{32})", answer)
        if match:
            return match.group(1)
    return "SimulatedToken"  # (a later run's server script no longer issues one)


def assert_window_invariants(result: dict) -> None:
    """Every UI scenario: no child process or console window, a responsive UI thread, the code nowhere (UI, files,
    command line), the simulated token only in the simulated server's own script, and only the .invalid host reached."""
    assert result["max_children"] == 0
    assert result["max_console_windows"] == 0
    assert result["latencies"] and max(result["latencies"]) < 1000, result["latencies"]
    assert DUMMY_CODE not in (result["command_line"] or "")
    token = simulated_token(result)
    for shot in result["snapshots"]:
        visible = json.dumps(shot)
        assert DUMMY_CODE not in visible and "SimulatedToken" not in visible, shot["label"]
    for path in Path(result["workspace"]).rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert DUMMY_CODE.encode() not in content, path
            if path.name != "http.json":  # the simulated server's own script is where the simulated token comes from
                assert token.encode() not in content, path
    for call in workspace_calls(result):
        if call["tool"] == "http":
            assert call["url"].startswith(SIMULATION_API + "/"), call["url"]


def _answer_code() -> list[dict]:
    return [{"op": "wait_prompt", "prompt": "enrollment_code"}, {"op": "text", "id": "CodeBox", "value": DUMMY_CODE},
            {"op": "click", "id": "EnrollButton"}]


@needs_bridge
def test_a_legacy_setup_through_the_window_answers_every_question_on_screen(tmp_path, window_exe):
    steps = [
        {"op": "snapshot", "label": "start"},
        {"op": "click", "id": "StartButton"},
        {"op": "wait_prompt", "prompt": "sources_confirm"}, {"op": "snapshot", "label": "sources"},
        {"op": "click", "id": "UseStandardFilesButton"},
        {"op": "wait_prompt", "prompt": "enrollment_code"}, {"op": "snapshot", "label": "code_empty"},
        {"op": "text", "id": "CodeBox", "value": DUMMY_CODE}, {"op": "snapshot", "label": "code_typed"},
        {"op": "click", "id": "EnrollButton"}, {"op": "click_again", "id": "EnrollButton"},
        {"op": "snapshot", "label": "code_sent"},
        {"op": "wait_prompt", "prompt": "legacy_task_enable"}, {"op": "snapshot", "label": "enable"},
        {"op": "click", "id": "LeaveTaskDisabledButton"},
        {"op": "wait_status", "contains": "Finished"}, {"op": "snapshot", "label": "done"},
        {"op": "close"}, {"op": "wait_exit"},
    ]

    result = drive(window_exe, tmp_path, steps)

    assert "SIMULATION ONLY" in snapshot(result, "start")["all_text"]
    assert result["prompts_seen"] == ["sources_confirm", "enrollment_code", "legacy_task_enable"]
    # The standard files, each named for its source.
    sources = snapshot(result, "sources")["details"]
    for label, name in (("Checkins", "Checkins.txt"), ("Rejects", "Rejects.txt"), ("ACS", "ACS Log.txt")):
        assert label in sources and f"TechLogic\\{name}" in sources
    # Enroll is offered only once a code is typed, and pressing it again straight away is impossible: one submission.
    assert snapshot(result, "code_empty")["enroll_enabled"] is False
    assert snapshot(result, "code_typed")["enroll_enabled"] is True and "works only once" in snapshot(result, "code_typed")["details"]
    assert result["second_clicks"] == [False]
    assert snapshot(result, "code_sent")["prompt"] in ("none", None)
    enable = snapshot(result, "enable")
    assert enable["cancel_enabled"] is False and "no longer be cancelled safely" in enable["cancel_note"]
    done = snapshot(result, "done")
    assert "exit code 0" in done["status"]
    assert any("FINISHED: completed, exit 0" in line and "task Disabled" in line for line in done["events"])
    assert any("Enrollment (contract 2 requested): Refused [contract_not_permitted]" in line for line in done["events"])
    assert any("Enrollment (contract 1 requested): Enrolled" in line for line in done["events"])
    # The console text is kept apart from the structured events.
    assert any("COMPLETE" in line for line in done["console"])
    assert not any("=== Step" in line for line in done["events"])
    assert "SortView setup is complete" in done["outcome"] and "left disabled" in done["outcome"]
    assert workspace_tools(result) == LEGACY_TOOLS
    assert result["exited"] is True
    assert_window_invariants(result)


def _stop_text(shot: dict) -> str:
    """The engine's "What to do" text from a run's events (as the event list shows it)."""
    return next(line.strip() for line in shot["events"] if line.strip().startswith("What to do: "))


def _config_sources(result: dict) -> dict:
    config = json.loads((Path(result["workspace"]) / "data" / "config" / "collector_config.json").read_text(encoding="utf-8-sig"))
    return {source["name"]: source["path"] for source in config["sources"]}


@needs_bridge
def test_a_v2_setup_with_files_of_any_name_in_separate_folders_chosen_by_hand(tmp_path, window_exe):
    hidden = "{workspace}\\TechLogic\\"
    checkins = "{workspace}\\Export A\\daily check-ins.log"
    rejects = "{workspace}\\Export B\\Sorter rejects (2026).txt"
    acs = hidden + "ACS Log.txt.hidden"
    steps = [
        {"op": "select", "id": "ScenarioV2"}, {"op": "toggle", "id": "StandardFilesPresent", "on": False},
        {"op": "click", "id": "StartButton"},
        {"op": "wait_prompt", "prompt": "source_path"}, {"op": "snapshot", "label": "first"},
        {"op": "copy", "from": hidden + "Checkins.txt.hidden", "to": checkins},
        {"op": "copy", "from": hidden + "Rejects.txt.hidden", "to": rejects},
        {"op": "text", "id": "PathBox", "value": "{workspace}\\nowhere\\Checkins.txt"}, {"op": "click", "id": "UsePathButton"},
        {"op": "sleep", "ms": 800}, {"op": "wait_prompt", "prompt": "source_path"}, {"op": "snapshot", "label": "refused"},
        # A network path is warned about, never refused by the window -- and nothing is sent until "Use this file".
        {"op": "text", "id": "PathBox", "value": "\\\\library-files\\techlogic\\Checkins.txt"}, {"op": "snapshot", "label": "network"},
        {"op": "text", "id": "PathBox", "value": checkins}, {"op": "click", "id": "UsePathButton"},
        {"op": "sleep", "ms": 800}, {"op": "wait_prompt", "prompt": "source_path"}, {"op": "snapshot", "label": "rejects"},
        {"op": "text", "id": "PathBox", "value": rejects}, {"op": "click", "id": "UsePathButton"},
        {"op": "sleep", "ms": 800}, {"op": "wait_prompt", "prompt": "source_path"}, {"op": "snapshot", "label": "acs"},
        {"op": "text", "id": "PathBox", "value": acs}, {"op": "click", "id": "UsePathButton"},
        *_answer_code(),
        {"op": "wait_status", "contains": "Finished"}, {"op": "snapshot", "label": "done"},
        {"op": "close"}, {"op": "wait_exit"},
    ]

    result = drive(window_exe, tmp_path, steps)

    first = snapshot(result, "first")
    assert "Choose the Tech Logic Checkins file" in first["details"] and "Attempt 1 of 3." in first["details"]
    # The standard file is missing, so the engine's first question already says so; the path is offered for correction.
    assert "Setup could not find a file at:" in first["details"] and "TechLogic\\Checkins.txt" in first["details"]
    # The engine judged the path, and the window says why -- and offers the refused path for correction.
    refused = snapshot(result, "refused")
    assert "Setup could not find a file at:" in refused["details"] and "nowhere\\Checkins.txt" in refused["details"]
    assert "Attempt 2 of 3." in refused["details"]
    network = snapshot(result, "network")
    assert "network location" in network["details"] and network["use_path_enabled"] is True
    assert "Choose the Tech Logic Rejects file" in snapshot(result, "rejects")["details"]
    assert "Choose the Tech Logic ACS file" in snapshot(result, "acs")["details"]
    done = snapshot(result, "done")
    assert "exit code 0" in done["status"] and "SortView setup is complete" in done["outcome"]
    assert any("FINISHED: completed, exit 0" in line and "contract 2" in line and "task Enabled" in line for line in done["events"])
    assert "legacy_task_enable" not in result["prompts_seen"]
    # Exactly the files chosen -- any names, any folders -- are what the installed Collector reads.
    workspace = result["workspace"]
    assert _config_sources(result) == {name: path.replace("{workspace}", workspace)
                                       for name, path in (("checkins", checkins), ("rejects", rejects), ("acs", acs))}
    assert_window_invariants(result)


@needs_bridge
def test_a_missing_source_file_is_asked_for_three_times_then_setup_stops_before_the_code(tmp_path, window_exe):
    missing = "{workspace}\\nowhere\\Checkins.txt"
    steps = [
        {"op": "toggle", "id": "StandardFilesPresent", "on": False},
        {"op": "click", "id": "StartButton"},
        {"op": "wait_prompt", "prompt": "source_path"},
        {"op": "text", "id": "PathBox", "value": missing}, {"op": "click", "id": "UsePathButton"},
        {"op": "sleep", "ms": 800}, {"op": "wait_prompt", "prompt": "source_path"},
        {"op": "text", "id": "PathBox", "value": "relative\\Checkins.txt"}, {"op": "click", "id": "UsePathButton"},
        {"op": "sleep", "ms": 800}, {"op": "wait_prompt", "prompt": "source_path"}, {"op": "snapshot", "label": "last"},
        {"op": "text", "id": "PathBox", "value": missing}, {"op": "click", "id": "UsePathButton"},
        {"op": "wait_status", "contains": "Finished"}, {"op": "snapshot", "label": "done"},
        {"op": "close"}, {"op": "wait_exit"},
    ]

    result = drive(window_exe, tmp_path, steps)

    last = snapshot(result, "last")
    assert "Attempt 3 of 3. If this file is not accepted either, setup stops." in last["details"]
    assert "That is not a full path" in last["details"]
    done = snapshot(result, "done")
    assert "exit code 1" in done["status"]
    assert "Setup stopped before it finished" in done["outcome"] and "The checkins source file was not found" in done["outcome"]
    assert _stop_text(done) in done["outcome"]
    assert "enrollment_code" not in result["prompt_history"]
    assert "http:POST" not in workspace_tools(result)  # no enrollment request
    assert_window_invariants(result)


@needs_bridge
def test_declining_to_resume_stops_with_the_engines_advice_and_no_new_code(tmp_path, window_exe):
    steps = [
        {"op": "select", "id": "FaultInstall"},
        {"op": "click", "id": "StartButton"},
        {"op": "wait_prompt", "prompt": "sources_confirm"}, {"op": "click", "id": "UseStandardFilesButton"},
        *_answer_code(),
        {"op": "wait_status", "contains": "Finished"},
        {"op": "click", "id": "StartButton"},
        {"op": "wait_prompt", "prompt": "resume_confirm"}, {"op": "click", "id": "DontResumeButton"},
        {"op": "wait_status", "contains": "Finished"}, {"op": "snapshot", "label": "declined"},
        {"op": "close"}, {"op": "wait_exit"},
    ]

    result = drive(window_exe, tmp_path, steps)

    declined = snapshot(result, "declined")
    assert "exit code 2" in declined["status"]
    assert "Resuming the unfinished setup was declined." in declined["outcome"] and _stop_text(declined) in declined["outcome"]
    assert result["prompt_history"] == ["sources_confirm", "enrollment_code", "resume_confirm"]  # the code was asked for once
    assert workspace_tools(result).count("http:POST") == 2  # the first run's only (the v2 refusal, then the legacy request)
    assert_window_invariants(result)


@needs_bridge
@pytest.mark.parametrize("contract", ["legacy", "v2"])
def test_an_interrupted_setup_resumes_on_the_next_start_with_no_option_changed(tmp_path, window_exe, contract):
    # Exactly the manual test: choose "During install (then resume)", run, then simply press Start again. The failure
    # applies to the first run only -- the window puts the option back to "Do not fail" when that run starts.
    v2 = contract == "v2"
    steps = [
        *([{"op": "select", "id": "ScenarioV2"}] if v2 else []),
        {"op": "select", "id": "FaultInstall"}, {"op": "snapshot", "label": "chosen"},
        # (Slow steps keep the first step running for 3 s, so the run's status line can be read before a question replaces it.)
        {"op": "toggle", "id": "SlowSteps", "on": True},
        {"op": "click", "id": "StartButton"},
        {"op": "wait_status", "contains": "applies to this run only"}, {"op": "snapshot", "label": "first_run"},
        {"op": "wait_prompt", "prompt": "sources_confirm"},
        {"op": "click", "id": "UseStandardFilesButton"},
        *_answer_code(),
        {"op": "wait_status", "contains": "Finished"}, {"op": "snapshot", "label": "interrupted"},
        {"op": "click", "id": "StartButton"},  # no option touched
        {"op": "wait_prompt", "prompt": "resume_confirm"}, {"op": "snapshot", "label": "resume"},
        {"op": "click", "id": "ResumeButton"},
        {"op": "wait_prompt", "prompt": "sources_confirm"}, {"op": "click", "id": "UseStandardFilesButton"},
        *([] if v2 else [{"op": "wait_prompt", "prompt": "legacy_task_enable"}, {"op": "click", "id": "EnableTaskButton"}]),
        {"op": "wait_status", "contains": "Finished"}, {"op": "snapshot", "label": "done"},
        {"op": "close"}, {"op": "wait_exit"},
    ]

    result = drive(window_exe, tmp_path, steps)

    # The selection shows the truth: chosen for the first run, back to "Do not fail" once that run started.
    assert snapshot(result, "chosen")["fault"] == "install"
    first_run = snapshot(result, "first_run")
    assert first_run["fault"] == "none"  # while the failing run is still running
    assert "will fail during install; the failure applies to this run only" in first_run["status"]
    # The first run fails during install -- after enrolling -- and says how to recover.
    interrupted = snapshot(result, "interrupted")
    assert "exit code 3" in interrupted["status"] and interrupted["fault"] == "none"
    assert any("run setup.ps1 again to RESUME" in line for line in interrupted["events"])
    assert "Setup stopped before it finished" in interrupted["outcome"]
    assert _stop_text(interrupted) in interrupted["outcome"]  # the engine's own advice, word for word
    # The second run, started with nothing changed, recognises the saved installation and resumes it.
    resume = snapshot(result, "resume")["details"]
    assert "Installation 41" in resume and "No new enrollment code is needed" in resume
    assert f"Contract v{2 if v2 else 1}" in resume
    done = snapshot(result, "done")
    assert "exit code 0" in done["status"]
    assert any("Mode: resume_install" in line for line in done["events"])
    assert any("enrollment skipped (resume)" in line for line in done["events"])
    # The code was asked for once, in the first run only; the resume asked nothing else about enrollment.
    expected = ["sources_confirm", "enrollment_code", "resume_confirm", "sources_confirm"] + ([] if v2 else ["legacy_task_enable"])
    assert result["prompt_history"] == expected
    # No new enrollment request: the only POSTs are the first run's (legacy: the v2 refusal, then the legacy retry).
    tools = workspace_tools(result)
    assert tools.count("http:POST") == (1 if v2 else 2)
    assert tools.count("install") == 2  # failed once, then succeeded
    assert tools[-1] == "enable-task"
    task = json.loads((Path(result["workspace"]) / "fake" / "task.json").read_text(encoding="utf-8-sig"))
    assert task == {"State": "Ready"}  # enabled: by setup itself for v2, by the explicit Enable answer for legacy
    if v2:
        assert any("FINISHED: completed, exit 0" in line and "contract 2" in line and "task Enabled" in line for line in done["events"])
    assert_window_invariants(result)


@needs_bridge
def test_cancel_before_enrollment_stops_safely_without_sending_the_code(tmp_path, window_exe):
    steps = [
        {"op": "click", "id": "StartButton"},
        {"op": "wait_prompt", "prompt": "sources_confirm"}, {"op": "click", "id": "UseStandardFilesButton"},
        {"op": "wait_prompt", "prompt": "enrollment_code"}, {"op": "snapshot", "label": "code"},
        {"op": "click", "id": "CancelButton"},
        {"op": "wait_status", "contains": "Finished"}, {"op": "snapshot", "label": "done"},
        {"op": "close"}, {"op": "wait_exit"},
    ]

    result = drive(window_exe, tmp_path, steps)

    assert snapshot(result, "code")["cancel_enabled"] is True
    done = snapshot(result, "done")
    assert "exit code 1" in done["status"]
    assert any("Question cancelled: enrollment_code" in line for line in done["events"])
    assert any("The enrollment code was NOT used." in line for line in done["events"])
    assert "Entering the enrollment code was cancelled." in done["outcome"] and _stop_text(done) in done["outcome"]
    assert workspace_tools(result) == ["install-verify", "runtime-version", "http:GET"]  # no enrollment request
    assert_window_invariants(result)


@needs_bridge
def test_after_the_code_is_submitted_close_waits_and_setup_runs_to_the_end(tmp_path, window_exe):
    steps = [
        {"op": "toggle", "id": "SlowSteps", "on": True},
        {"op": "click", "id": "StartButton"},
        {"op": "wait_prompt", "prompt": "sources_confirm"}, {"op": "click", "id": "UseStandardFilesButton"},
        *_answer_code(),
        {"op": "wait_disabled", "id": "CancelButton"}, {"op": "snapshot", "label": "after_code"},
        {"op": "close"}, {"op": "sleep", "ms": 300}, {"op": "snapshot", "label": "closing"},
        {"op": "wait_exit"},
    ]

    result = drive(window_exe, tmp_path, steps)

    after = snapshot(result, "after_code")
    assert after["cancel_enabled"] is False and "no longer be cancelled safely" in after["cancel_note"]
    assert "cannot be interrupted" in snapshot(result, "closing")["status"]
    assert result["exited"] is True
    # Nothing was interrupted: enrollment, token, install and verification all ran; the last question was cancelled,
    # so the task stays disabled.
    assert workspace_tools(result) == LEGACY_TOOLS
    assert json.loads((Path(result["workspace"]) / "fake" / "task.json").read_text(encoding="utf-8-sig")) == {"State": "Disabled"}
    assert "legacy_task_enable" not in result["prompts_seen"]
    assert_window_invariants(result)


@needs_bridge
def test_closing_while_a_question_is_open_stops_safely_and_closes(tmp_path, window_exe):
    steps = [
        {"op": "click", "id": "StartButton"},
        {"op": "wait_prompt", "prompt": "sources_confirm"},
        {"op": "close"}, {"op": "wait_exit"},
    ]

    result = drive(window_exe, tmp_path, steps)

    assert result["exited"] is True
    assert workspace_tools(result) == ["install-verify", "runtime-version"]  # stopped at the question: no network, nothing installed
    assert_window_invariants(result)


@needs_bridge
def test_closing_while_setup_is_working_waits_for_the_step_then_stops_at_the_next_question(tmp_path, window_exe):
    steps = [
        {"op": "toggle", "id": "SlowSteps", "on": True},
        {"op": "click", "id": "StartButton"},
        {"op": "sleep", "ms": 700},
        {"op": "close"}, {"op": "sleep", "ms": 300}, {"op": "snapshot", "label": "closing"},
        {"op": "wait_exit"},
    ]

    result = drive(window_exe, tmp_path, steps)

    assert "will close as soon as setup stops at its next question" in snapshot(result, "closing")["status"]
    assert result["exited"] is True and result["prompts_seen"] == []
    assert workspace_tools(result) == ["install-verify", "runtime-version"]  # the running step finished; nothing after it ran
    assert_window_invariants(result)
