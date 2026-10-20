"""Tests for the WPF side of the setup bridge: installer/SortView.Setup.Wpf.

DispatcherPromptPresenter posts the engine's questions to a WPF UI thread (BeginInvoke only) and settles them through a
TaskCompletionSource; SetupRunSession runs setup on a background thread and offers cooperative cancellation and close
deferral. These tests use a REAL WPF Dispatcher on its own STA thread and a scripted view that answers from that UI
thread (installer/SortView.Setup.Wpf/Testing). Full runs reach the real engine orchestration only through the Core
simulation harness (generated mock bundles, every side-effect wrapper replaced -- see tests/test_installer_bridge.py),
so nothing here touches the network, Task Scheduler, a credential store or a real install location.

Static checks run everywhere; the dispatcher scenarios are Windows-only (MSBuild + Windows PowerShell 5.1).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from test_collector_setup import SENTINEL_CODE, Bundle, default_http, v2_http
from test_installer_bridge import (
    _MSBUILD,
    _POWERSHELL,
    SIMULATION_API,
    _overrides,
    needs_bridge,
    simulated_bundle,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WPF_DIR = REPO_ROOT / "installer" / "SortView.Setup.Wpf"
WPF_PROJECT = WPF_DIR / "SortView.Setup.Wpf.csproj"
WPF_SOURCES = sorted(WPF_DIR.glob("*.cs"))
WPF_TESTING_PROJECT = WPF_DIR / "Testing" / "SortView.Setup.Wpf.Testing.csproj"
SPIKE_DIR = REPO_ROOT / "installer" / "SortView.Setup.Spike"

LEGACY_TOOLS = ["install-verify", "runtime-version", "http:GET", "http:POST", "http:POST", "set-api-token", "install", "finish-install"]


def _code(path: Path) -> str:
    """A C# file without its comments."""
    text = path.read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


# =====================================================================================
# 1. STATIC checks of the WPF bridge sources
# =====================================================================================

def test_the_wpf_bridge_never_calls_into_the_ui_thread_synchronously_or_blocks_on_a_task():
    for source in WPF_SOURCES:
        code = _code(source)
        assert not re.search(r"\b[Dd]ispatcher\.Invoke\(", code), source.name  # BeginInvoke only
        assert "InvokeAsync(" not in code and "PushFrame" not in code, source.name
        assert not re.search(r"\.Wait\(|\.Result\b|GetAwaiter\(\)\.GetResult", code), source.name
    presenter = _code(WPF_DIR / "DispatcherPromptPresenter.cs")
    assert presenter.count("dispatcher.BeginInvoke(") == 2  # the question, and its dismissal
    request = _code(WPF_DIR / "PromptRequest.cs")
    assert "new TaskCompletionSource<object>(TaskCreationOptions.RunContinuationsAsynchronously)" in request


def test_cancellation_is_cooperative_only_and_has_no_timeout():
    for source in WPF_SOURCES:
        code = _code(source)
        for forbidden in (".Stop(", "BeginStop", "Abort(", "Interrupt(", "Environment.Exit", "Process.", "Kill(", "Task.Delay",
                          "Timer", "CancelAfter", "TimeSpan"):
            assert forbidden not in code, (source.name, forbidden)
    session = _code(WPF_DIR / "SetupRunSession.cs")
    assert "TaskCreationOptions.LongRunning" in session  # setup runs on its own background thread, never the caller's
    assert "cancellation.Cancel()" in session and session.count("cancellation.Cancel()") == 1


def test_the_wpf_bridge_has_no_windows_and_the_spike_is_untouched_by_it():
    project = WPF_PROJECT.read_text(encoding="utf-8")
    assert "PresentationFramework" not in project and "<Page " not in project  # no windows or dialogs in the bridge
    assert "Testing" not in project
    for path in SPIKE_DIR.rglob("*"):
        if path.is_file() and path.suffix in (".cs", ".csproj", ".xaml"):
            assert "SortView.Setup.Wpf" not in path.read_text(encoding="utf-8"), path


def test_a_late_or_refused_secure_answer_is_disposed():
    request = _code(WPF_DIR / "PromptRequest.cs")
    submit = request[request.index("public bool TrySubmit"):]
    assert submit.index("completion.TrySetResult(answer)") < submit.index("(answer as SecureString)?.Dispose();")


# =====================================================================================
# 2. DISPATCHER scenarios (a real WPF Dispatcher on an STA thread)
# =====================================================================================

@pytest.fixture(scope="module")
def wpf_built() -> Path:
    completed = subprocess.run([_MSBUILD, str(WPF_TESTING_PROJECT), "/p:Configuration=Release", "/nologo", "/v:minimal"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return WPF_DIR / "Testing" / "bin" / "Release"


_DRIVER = r"""
param([string]$SpecPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
$spec = Get-Content -LiteralPath $SpecPath -Raw -Encoding UTF8 | ConvertFrom-Json
Add-Type -AssemblyName WindowsBase
foreach ($dll in @('SortView.Setup.Core.dll', 'SortView.Setup.Core.Testing.dll', 'SortView.Setup.Wpf.dll', 'SortView.Setup.Wpf.Testing.dll')) {
    Add-Type -Path (Join-Path $spec.bin $dll)
}
$scenarios = [SortView.Setup.Wpf.Testing.DispatcherScenarios]
function New-SimulationRequest {
    foreach ($name in @('SETUPTEST_INSTALL_MODE', 'SETUPTEST_FINISH_MODE', 'SETUPTEST_CHECK_MODE')) { [Environment]::SetEnvironmentVariable($name, $null) }
    [Environment]::SetEnvironmentVariable('SETUPTEST_FAKE_DIR', [string]$spec.fake_dir)
    $request = New-Object SortView.Setup.Core.Testing.SimulationRequest
    $request.WorkingRoot = [string]$spec.working_root
    $request.BundleRoot = [string]$spec.bundle_root
    $request.InstallRoot = [string]$spec.install_root
    $request.DataRoot = [string]$spec.data_root
    $request.WrapperOverrides = Get-Content -LiteralPath $spec.overrides_path -Raw -Encoding UTF8
    $request
}
switch ($spec.kind) {
    'session' {
        $result = $scenarios::RunSession((New-SimulationRequest), [string[]]@($spec.actions), [string[]]@($spec.values), [int]$spec.delay,
            [bool]$spec.cancel_at_start)
    }
    'two_sessions' {
        $result = $scenarios::RunTwoSessions((New-SimulationRequest), [string[]]@($spec.first), [string[]]@($spec.actions),
            [string[]]@($spec.values), [int]$spec.delay)
    }
    'present_once' { $result = $scenarios::PresentOnce([string]$spec.mode) }
    'console_text' { $result = $scenarios::DeliverConsoleText([int]$spec.count) }
}
ConvertTo-Json -InputObject $result -Depth 8 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


def _drive(bin_dir: Path, scratch: Path, spec: dict):
    scratch.mkdir(parents=True, exist_ok=True)
    spec_path, out_path, driver = scratch / "dispatcher_spec.json", scratch / "dispatcher_out.json", scratch / "dispatcher_driver.ps1"
    spec_path.write_text(json.dumps({"bin": str(bin_dir), **spec}), encoding="utf-8")
    driver.write_text(_DRIVER, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(driver),
                                "-SpecPath", str(spec_path), "-OutPath", str(out_path)],
                               capture_output=True, text=True, timeout=300, check=False)
    assert completed.returncode == 0 and out_path.exists(), completed.stdout + completed.stderr
    return json.loads(out_path.read_text(encoding="utf-8-sig"))


def _session_spec(bundle: Bundle, http: dict | None, env: dict | None) -> dict:
    fake = bundle.fake
    (fake / "http.json").write_text(json.dumps(http if http is not None else default_http()), encoding="utf-8")
    for counter in fake.glob("http_count_*.txt"):
        counter.unlink()
    overrides_path = fake / "bridge_overrides.ps1"
    overrides_path.write_text(_overrides(bundle), encoding="utf-8")
    if env:
        raise AssertionError("per-test environment is not needed by these scenarios")
    return {"fake_dir": str(fake), "working_root": str(bundle.root.parent), "bundle_root": str(bundle.root),
            "install_root": str(bundle.install_root), "data_root": str(bundle.data_root), "overrides_path": str(overrides_path)}


def _calls(bundle: Bundle) -> list[dict]:
    path = bundle.fake / "calls.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def _tools(bundle: Bundle) -> list[str]:
    names = []
    for call in _calls(bundle):
        if call["tool"] == "http":
            names.append(f"http:{call['method'].upper()}")
        elif call["tool"] not in ("prompt", "protect-recovery", "handler"):
            names.append(call["tool"])
    return names


def run_session(wpf_built: Path, tmp_path: Path, actions: list[str], values: list[str] | None = None, *, http: dict | None = None,
                delay: int = 300, cancel_at_start: bool = False) -> tuple[dict, Bundle]:
    bundle = simulated_bundle(tmp_path)
    spec = {"kind": "session", **_session_spec(bundle, http, None), "actions": actions,
            "values": values or [""] * len(actions), "delay": delay, "cancel_at_start": cancel_at_start}
    return _drive(wpf_built, bundle.fake, spec), bundle


def assert_session_invariants(report: dict, bundle: Bundle) -> None:
    """Every run: setup off the UI thread, Start returning at once, questions shown and answered on the UI thread, the
    UI thread responsive while the engine waits, a closed runspace, every code disposed, only the fakes reached."""
    assert not report["TimedOut"] and report["Refused"] is None, report
    assert report["RunnerThreadId"] != report["UiThreadId"] and report["RunnerIsBackground"] and not report["RunnerOnUiThread"]
    assert report["StartMilliseconds"] < 1000 and report["RunningWhenStartReturned"]
    for shown in report["Shown"]:
        assert shown["ShownOnUiThread"] and shown["ThreadId"] == report["UiThreadId"], shown
        assert shown["RunId"] == report["RunId"]
        if shown["Action"] not in ("none", "throw", "shutdown_dispatcher"):
            assert shown["AnsweredOnUiThread"], shown
    assert all(0 <= ms < 1000 for ms in report["ResponsivenessWhileWaiting"]), report["ResponsivenessWhileWaiting"]
    assert report["RunspaceState"] == "Closed"
    assert report["SecretsDisposed"] is True
    assert report["DeferCloseAfterRun"] is False and report["CancelAfterRun"] == "NotRunning"
    for call in _calls(bundle):
        if call["tool"] == "http":
            assert call["url"].startswith(SIMULATION_API + "/"), call["url"]
    assert SENTINEL_CODE not in json.dumps(report)


# ---- one question, straight through the presenter --------------------------------------------------------------

_PRESENT_ONCE = {
    "answer": ("yes", True, False),
    "dispose_while_waiting": ("null", True, True),
    "shutdown_while_waiting": ("null", True, False),
    "already_shut_down": ("null", False, False),
    "view_throws": ("null", True, False),
    "token_cancel": ("null", True, True),
    "late_answer": ("null", True, True),
}


@needs_bridge
@pytest.mark.parametrize("mode", list(_PRESENT_ONCE))
def test_every_way_a_question_ends_settles_it_promptly_and_safely(tmp_path, wpf_built, mode):
    result, shown, dismissed = _PRESENT_ONCE[mode]

    report = _drive(wpf_built, tmp_path, {"kind": "present_once", "mode": mode})

    assert report["Settled"] and report["SettledMilliseconds"] < 5000, report  # nothing left waiting for ever
    assert report["Result"] == result
    assert (report["ShownCount"] == 1) == shown
    if shown:
        assert report["ShownOnUiThread"]
    assert report["Dismissed"] == dismissed  # the view is told to close a question it did not settle itself
    if mode == "late_answer":
        assert report["LateAnswerAccepted"] is False and report["SecretsDisposed"] is True


@needs_bridge
def test_the_ui_thread_stays_responsive_while_a_question_waits(tmp_path, wpf_built):
    report = _drive(wpf_built, tmp_path, {"kind": "present_once", "mode": "responsive_while_waiting"})

    assert 0 <= report["ResponsivenessWhileWaiting"] < 1000
    assert report["Settled"] and report["Result"] == "null"


@needs_bridge
def test_console_text_from_the_engine_thread_arrives_on_the_ui_thread_complete_and_in_order(tmp_path, wpf_built):
    received = _drive(wpf_built, tmp_path, {"kind": "console_text", "count": 200})

    assert received == [f"line {i}|ui" for i in range(200)]


# ---- full simulated runs through SetupRunSession ---------------------------------------------------------------

@needs_bridge
def test_a_legacy_setup_runs_off_the_ui_thread_with_every_question_on_it(tmp_path, wpf_built):
    report, bundle = run_session(wpf_built, tmp_path, ["yes", "secure", "no"], ["", SENTINEL_CODE, ""])

    assert report["ExitCode"] == 0, report
    assert [s["PromptId"] for s in report["Shown"]] == ["sources_confirm", "enrollment_code", "legacy_task_enable"]
    assert all(s["AnswerAccepted"] for s in report["Shown"])
    assert report["ResponsivenessWhileWaiting"]  # measured while each answer was pending
    assert _tools(bundle) == LEGACY_TOOLS
    assert report["SecretCount"] == 1 and report["EnrollmentMayHaveBegun"] is True
    assert report["Events"][-1] == "run_finished:task_enable:completed"
    assert_session_invariants(report, bundle)


@needs_bridge
def test_a_v2_setup_runs_through_the_session(tmp_path, wpf_built):
    report, bundle = run_session(wpf_built, tmp_path, ["yes", "secure"], ["", SENTINEL_CODE], http=v2_http())

    assert report["ExitCode"] == 0, report
    assert [s["PromptId"] for s in report["Shown"]] == ["sources_confirm", "enrollment_code"]
    assert "stage_completed:task_enable:enabled" in report["Events"]
    assert_session_invariants(report, bundle)


@needs_bridge
@pytest.mark.parametrize("action", ["cancel", "throw", "shutdown_dispatcher"])
def test_a_question_cancelled_by_the_person_the_view_or_shutdown_stops_before_any_network_request(tmp_path, wpf_built, action):
    report, bundle = run_session(wpf_built, tmp_path, [action])

    assert report["ExitCode"] == 1, report
    assert report["CancelledPromptIds"] == ["sources_confirm"]
    assert _tools(bundle) == ["install-verify", "runtime-version"]
    assert_session_invariants(report, bundle)


@needs_bridge
def test_cancelling_the_session_while_the_code_question_waits_sends_no_enrollment_request(tmp_path, wpf_built):
    report, bundle = run_session(wpf_built, tmp_path, ["yes", "cancel_session"])

    assert report["ExitCode"] == 1
    assert report["CancelOutcome"] == "CancelledAtQuestion" and report["EnrollmentMayHaveBegun"] is False
    assert report["CancelledPromptIds"] == ["enrollment_code"]
    assert report["Shown"][1]["Dismissed"] is True  # the view is told to close the dialog
    assert _tools(bundle) == ["install-verify", "runtime-version", "http:GET"]
    assert_session_invariants(report, bundle)


@needs_bridge
def test_cancelling_before_the_first_question_stops_at_it_without_showing_it(tmp_path, wpf_built):
    report, bundle = run_session(wpf_built, tmp_path, ["yes"], cancel_at_start=True)

    assert report["ExitCode"] == 1
    assert report["CancelOutcomeAtStart"] in ("WillStopAtNextQuestion", "CancelledAtQuestion")
    assert report["CancelledPromptIds"] == ["sources_confirm"]
    assert _tools(bundle) == ["install-verify", "runtime-version"]
    assert_session_invariants(report, bundle)


@needs_bridge
def test_cancelling_after_the_code_was_handed_over_never_interrupts_the_enrollment_or_the_install(tmp_path, wpf_built):
    report, bundle = run_session(wpf_built, tmp_path, ["yes", "secure_then_cancel_session"], ["", SENTINEL_CODE])

    assert report["CancelOutcome"] == "FinishingAfterEnrollment" and report["EnrollmentMayHaveBegun"] is True
    assert report["ExitCode"] == 0, report  # the run went on to the end...
    assert _tools(bundle) == LEGACY_TOOLS  # ...enrollment, token, install and verification all ran
    assert report["CancelledPromptIds"] == ["legacy_task_enable"]  # only the last question was cancelled
    assert [s["PromptId"] for s in report["Shown"]] == ["sources_confirm", "enrollment_code"]  # and never even shown
    assert json.loads((bundle.fake / "task.json").read_text()) == {"State": "Disabled"}
    assert_session_invariants(report, bundle)


@needs_bridge
def test_a_window_can_defer_closing_until_the_run_has_returned(tmp_path, wpf_built):
    report, bundle = run_session(wpf_built, tmp_path, ["yes", "defer_close"])

    assert report["DeferCloseResult"] is True  # while running: keep the window open
    assert report["ExitCode"] == 1 and report["CancelledPromptIds"] == ["enrollment_code"]
    assert report["DeferCloseAfterRun"] is False  # once the run has returned: close
    assert_session_invariants(report, bundle)


@needs_bridge
def test_an_answer_after_cancellation_is_refused_and_its_code_disposed(tmp_path, wpf_built):
    report, bundle = run_session(wpf_built, tmp_path, ["yes", "late_secure"], ["", SENTINEL_CODE])

    assert report["LateAnswerAccepted"] is False and report["SecretCount"] == 1 and report["SecretsDisposed"] is True
    assert report["ExitCode"] == 1 and report["CancelledPromptIds"] == ["enrollment_code"]
    assert _tools(bundle) == ["install-verify", "runtime-version", "http:GET"]  # the late code was never sent
    assert_session_invariants(report, bundle)


@needs_bridge
def test_one_run_can_never_answer_another_runs_question(tmp_path, wpf_built):
    bundle = simulated_bundle(tmp_path)
    spec = {"kind": "two_sessions", **_session_spec(bundle, None, None), "first": ["keep_and_cancel"],
            "actions": ["stale_then_yes", "secure", "no"], "values": ["", SENTINEL_CODE, ""], "delay": 200}

    first, second = _drive(wpf_built, bundle.fake, spec)

    assert first["ExitCode"] == 1 and first["CancelledPromptIds"] == ["sources_confirm"]
    assert second["StaleAnswerAccepted"] is False  # the first run's question cannot be answered again
    assert second["ExitCode"] == 0, second  # and the second run went on with its own answers
    assert first["RunId"] != second["RunId"]
    assert all(s["RunId"] == second["RunId"] for s in second["Shown"])
    assert_session_invariants(first, bundle)
    assert_session_invariants(second, bundle)
