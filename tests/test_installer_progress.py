"""Tests for the setup wizard's progress model, Phase 4B.4: installer/SortView.Setup.Wizard/SetupProgress.cs.

The model turns the engine's structured events into seven customer steps. It decides nothing and reports a step done only
when the events of every stage in it say so.

  1. STATIC checks (run everywhere): every engine stage maps to exactly one step, every event type is accounted for,
     the step labels are the approved ones, and the model has no way to run setup and no percentages.
  2. MODEL scenarios (Windows only): event sequences in the engine's own order -- fresh legacy and v2 runs, resumes,
     an interrupted, a cancelled, a declined, a stopped and an unexpectedly failed run, a run that could not start --
     fed to the built model in one PowerShell process.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from test_installer_bridge import _MSBUILD, _POWERSHELL, needs_bridge

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE = REPO_ROOT / "collector" / "deploy" / "setup-collector.ps1"
WIZARD_DIR = REPO_ROOT / "installer" / "SortView.Setup.Wizard"
PROGRESS_CODE = WIZARD_DIR / "SetupProgress.cs"
WIZARD_PROJECT = WIZARD_DIR / "SortView.Setup.Wizard.csproj"

STEP_TITLES = [
    "Check the computer and installation package", "Find Tech Logic files", "Connect to SortView", "Enroll this computer",
    "Install SortView Collector", "Verify installation", "Configure the Scheduled Task",
]
EXPECTED_STEPS = {
    "elevation": 1, "bundle_verification": 1, "bundle": 1, "machine": 1, "sources": 2, "connection": 3,
    "setup_folder": 4, "enrollment": 4, "enrollment_save": 4, "token_store": 4, "install": 5, "rules_install": 5,
    "verification": 6, "record_cleanup": 6, "task_enable": 7,
}


def _engine_list(name: str) -> list[str]:
    """A PowerShell @("a", "b", ...) list assigned to $script:<name> in the engine."""
    text = ENGINE.read_text(encoding="utf-8")
    body = re.search(rf"\$script:{name} = @\((.*?)\)", text, flags=re.DOTALL).group(1)
    return re.findall(r'"(\w+)"', body)


def _engine_event_types() -> list[str]:
    text = ENGINE.read_text(encoding="utf-8")
    body = re.search(r"\$script:SetupEventTypes = \[ordered\]@\{(.*?)\n\}", text, flags=re.DOTALL).group(1)
    return re.findall(r"^\s*(\w+)\s*=", body, flags=re.MULTILINE)


def _model_stage_map() -> dict[str, int]:
    code = PROGRESS_CODE.read_text(encoding="utf-8")
    body = code[code.index("StageSteps = new Dictionary"):]
    body = body[:body.index("};")]
    pairs = re.findall(r'\{ "(\w+)", (\d) \}', body)
    assert len(pairs) == len({stage for stage, _ in pairs}), "a stage is mapped twice"
    return {stage: int(step) for stage, step in pairs}


# =====================================================================================
# 1. STATIC checks
# =====================================================================================

def test_every_engine_stage_maps_to_exactly_one_customer_step():
    engine_stages = _engine_list("SetupStageIds")
    assert len(engine_stages) == 15
    model = _model_stage_map()
    assert set(model) == set(engine_stages)
    assert model == EXPECTED_STEPS
    assert sorted(set(model.values())) == list(range(1, 8))


def test_every_engine_event_type_is_accounted_for():
    code = PROGRESS_CODE.read_text(encoding="utf-8")
    handled = set(re.findall(r'case "(\w+)":', code))
    for event_type in _engine_event_types():
        # enrollment_result changes no step: the model names it, and why, rather than handling it.
        assert event_type in handled or (event_type == "enrollment_result" and "// enrollment_result:" in code), event_type


def test_the_steps_are_the_approved_seven_in_order():
    code = PROGRESS_CODE.read_text(encoding="utf-8")
    assert re.findall(r'new ProgressStep\(\d, "([^"]+)"', code) == STEP_TITLES


def test_the_model_cannot_run_setup_and_reports_no_percentages():
    code = "\n".join(line for line in PROGRESS_CODE.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("//"))
    for forbidden in ("BeginRun", "SetupRunner", "SetupRunSession", "SetupEngineHost", "SimulatedSetupHarness", "Core.Testing",
                      "DispatcherTimer", "Thread", "Task.", "percent", "Percent", "%"):
        assert forbidden not in code, forbidden


# =====================================================================================
# 2. MODEL scenarios
# =====================================================================================

def ev(type_: str, stage: str | None = None, **fields) -> dict:
    return {"op": "event", "type": type_, "stage": stage, "fields": fields}


def ran(*stages: str, outcome: str = "ok") -> list[dict]:
    return [a for s in stages for a in (ev("stage_started", s), ev("stage_completed", s, outcome=outcome))]


def skipped(reason: str, *stages: str) -> list[dict]:
    return [ev("stage_skipped", s, reason=reason) for s in stages]


def wait(on: bool) -> dict:
    return {"op": "wait", "on": on}


def mark(label: str) -> dict:
    return {"op": "mark", "label": label}


def finished(result: str, **fields) -> dict:
    return ev("run_finished", None, result=result, **fields)


def machine(mode: str) -> list[dict]:
    return [ev("stage_started", "machine"), ev("mode_detected", "machine", mode=mode), ev("stage_completed", "machine", outcome="ok")]


CHECKS = ran("elevation", "bundle_verification", "bundle")
STOP_AFTER_ENROLLMENT = {"code_attempted": True, "code_consumed": True, "token_stored": True, "recovery_on_disk": True}

SCENARIOS = {
    "fresh_legacy": [
        ev("stage_started", "elevation"), mark("first"), ev("stage_completed", "elevation", outcome="ok"),
        *ran("bundle_verification", "bundle"), *machine("fresh"),
        ev("stage_started", "sources"), wait(True), mark("sources_question"), wait(False), ev("stage_completed", "sources", outcome="ok"),
        *ran("connection", "setup_folder"),
        ev("stage_started", "enrollment"), mark("enrolling"),
        ev("enrollment_result", "enrollment", requested_contract=2, enrollment_outcome="Refused", code_used="not_used"),
        ev("enrollment_result", "enrollment", requested_contract=1, enrollment_outcome="Enrolled", code_used="used"),
        ev("stage_completed", "enrollment", outcome="ok"),
        *ran("token_store", "enrollment_save", "install"), *skipped("not_applicable", "rules_install"),
        *ran("verification", "record_cleanup"),
        ev("stage_started", "task_enable"), wait(True), mark("task_question"), wait(False),
        ev("stage_completed", "task_enable", outcome="left_disabled"), finished("completed", task_state="Disabled"),
        {"op": "finish", "fault": False},
    ],
    "fresh_v2": [
        *CHECKS, *machine("fresh"), *ran("sources", "connection", "setup_folder", "enrollment"),
        # Contract v2: the enrollment is saved BEFORE the token is stored.
        *ran("enrollment_save"), mark("between_enrollment_save_and_token_store"), *ran("token_store"),
        *ran("install", "rules_install", "verification", "record_cleanup"), *ran("task_enable", outcome="enabled"),
        finished("completed", task_state="Enabled"), {"op": "finish", "fault": False},
    ],
    "resume_install_legacy": [
        *CHECKS, ev("stage_started", "machine"), ev("mode_detected", "machine", mode="resume_install", contract=1), mark("found"),
        wait(True), mark("resume_question"), wait(False), ev("stage_completed", "machine", outcome="ok"),
        *ran("sources", "connection"),
        *skipped("resume", "setup_folder", "enrollment", "token_store", "enrollment_save"), mark("resuming"),
        *ran("install"), *skipped("not_applicable", "rules_install"), *ran("verification", "record_cleanup"),
        *ran("task_enable", outcome="enabled"), finished("completed", mode="resume_install", task_state="Enabled"),
        {"op": "finish", "fault": False},
    ],
    "resume_finish_v2": [
        *CHECKS, *machine("resume_finish"), *skipped("already_installed", "sources"), *ran("connection"),
        *skipped("resume", "setup_folder", "enrollment", "enrollment_save", "token_store"),
        *skipped("already_installed", "install"), *ran("rules_install"), *ran("verification", "record_cleanup"),
        *ran("task_enable", outcome="enabled"), finished("completed", mode="resume_finish"), {"op": "finish", "fault": False},
    ],
    "resume_finish_legacy": [
        *CHECKS, *machine("resume_finish"), *skipped("already_installed", "sources"), *ran("connection"),
        *skipped("resume", "setup_folder", "enrollment", "token_store", "enrollment_save"),
        *skipped("already_installed", "install"), *skipped("not_applicable", "rules_install"),
        *ran("verification", "record_cleanup"), *ran("task_enable", outcome="left_disabled"), finished("completed"),
        {"op": "finish", "fault": False},
    ],
    "interrupted_during_install": [
        *CHECKS, *machine("fresh"), *ran("sources", "connection", "setup_folder", "enrollment", "token_store", "enrollment_save"),
        ev("stage_started", "install"), ev("run_stopped", "install", exit_code=3, **STOP_AFTER_ENROLLMENT),
        finished("stopped", exit_code=3), {"op": "finish", "fault": False},
    ],
    "cancelled_at_sources": [
        *CHECKS, *machine("fresh"), ev("stage_started", "sources"), wait(True),
        ev("input_cancelled", "sources", prompt_id="sources_confirm"), wait(False),
        ev("run_stopped", "sources", exit_code=1, recovery_on_disk=False), finished("stopped", exit_code=1),
        {"op": "finish", "fault": False},
    ],
    "declined_resume": [
        *CHECKS, ev("stage_started", "machine"), ev("mode_detected", "machine", mode="resume_install", contract=2), wait(True),
        wait(False), ev("run_stopped", "machine", exit_code=2, recovery_on_disk=True), finished("stopped", exit_code=2),
        {"op": "finish", "fault": False},
    ],
    "stopped_at_connection": [
        *CHECKS, *machine("fresh"), *ran("sources"), ev("stage_started", "connection"),
        ev("run_stopped", "connection", exit_code=1, recovery_on_disk=False), finished("stopped", exit_code=1),
        {"op": "finish", "fault": False},
    ],
    "unexpected_error_in_verification": [
        *CHECKS, *machine("fresh"), *ran("sources", "connection", "setup_folder", "enrollment", "token_store", "enrollment_save"),
        *ran("install"), *skipped("not_applicable", "rules_install"), ev("stage_started", "verification"),
        ev("run_failed_unexpected", "verification", exception_type="IOException", message="disk full", **STOP_AFTER_ENROLLMENT),
        finished("failed", exit_code=1), {"op": "finish", "fault": False},
    ],
    "task_question_cancelled": [
        *CHECKS, *machine("fresh"), *ran("sources", "connection", "setup_folder", "enrollment", "token_store", "enrollment_save"),
        *ran("install"), *skipped("not_applicable", "rules_install"), *ran("verification", "record_cleanup"),
        ev("stage_started", "task_enable"), ev("input_cancelled", "task_enable", prompt_id="legacy_task_enable"),
        ev("stage_completed", "task_enable", outcome="left_disabled"), finished("completed"), {"op": "finish", "fault": False},
    ],
    "could_not_run": [{"op": "finish", "fault": True}],
    "returned_without_a_final_event": [*CHECKS, ev("stage_started", "machine"), {"op": "finish", "fault": False}],
    "unknown_events_change_nothing": [
        ev("stage_started", "a_future_stage"), ev("a_future_event", "elevation"), mark("after_unknown"),
    ],
}

_MODEL_SCRIPT = r"""
param([string]$WizardPath, [string]$CasesPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -Path (Join-Path (Split-Path -Parent $WizardPath) 'SortView.Setup.Core.dll')
Add-Type -Path $WizardPath
$cases = Get-Content -LiteralPath $CasesPath -Raw | ConvertFrom-Json
$out = [ordered]@{}
foreach ($case in $cases.PSObject.Properties) {
    $progress = New-Object SortView.Setup.Wizard.SetupProgress
    $null = $progress.Reset()
    $events = 0; $changes = 0
    $snap = {
        [ordered]@{ states = @($progress.Steps | ForEach-Object { $_.State.ToString() }); ending = $progress.Ending.ToString()
                    resume = $progress.ResumeNotice; activity = $progress.CurrentActivity; events = $events; changes = $changes
                    names = @($progress.Steps | ForEach-Object { $_.AccessibleName }) }
    }
    $marks = [ordered]@{}
    foreach ($action in $case.Value) {
        switch ($action.op) {
            'event' {
                $fields = New-Object 'System.Collections.Generic.Dictionary[string,object]'
                if ($null -ne $action.fields) { foreach ($f in $action.fields.PSObject.Properties) { $fields[$f.Name] = $f.Value } }
                $stage = if ($null -eq $action.stage) { $null } else { [string]$action.stage }
                $events++
                $changes += @($progress.Apply([string]$action.type, $stage, $fields)).Count
            }
            'wait' { $changes += @($progress.SetWaiting([bool]$action.on)).Count }
            'finish' { $changes += @($progress.Finish([bool]$action.fault)).Count }
            'mark' { $marks[$action.label] = & $snap }
        }
    }
    $marks['final'] = & $snap
    $out[$case.Name] = $marks
}
ConvertTo-Json -InputObject $out -Depth 6 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


@pytest.fixture(scope="module")
def model(tmp_path_factory) -> dict:
    completed = subprocess.run([_MSBUILD, str(WIZARD_PROJECT), "/p:Configuration=Release", "/nologo", "/v:minimal"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    work = tmp_path_factory.mktemp("progress")
    (work / "cases.json").write_text(json.dumps(SCENARIOS), encoding="utf-8")
    (work / "model.ps1").write_text(_MODEL_SCRIPT, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                str(work / "model.ps1"), "-WizardPath", str(WIZARD_DIR / "bin" / "Release" / "SortView.Setup.Wizard.dll"),
                                "-CasesPath", str(work / "cases.json"), "-OutPath", str(work / "out.json")],
                               capture_output=True, text=True, timeout=120, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads((work / "out.json").read_text(encoding="utf-8-sig"))


P, W, A, D, E, N, S, R, L = ("Pending", "Working", "WaitingForAnswer", "Done", "DoneEarlier", "NotNeeded", "StoppedHere",
                             "NotReached", "LeftDisabled")


@needs_bridge
def test_a_fresh_legacy_run_moves_through_every_step(model):
    run = model["fresh_legacy"]
    assert run["first"]["states"] == [W, P, P, P, P, P, P] and run["first"]["ending"] == "None"
    assert run["first"]["activity"] == "Step 1 of 7: Check the computer and installation package. Checking the installation files and this computer…"
    assert run["sources_question"]["states"] == [D, A, P, P, P, P, P]
    assert run["sources_question"]["activity"] == "Step 2 of 7: Find Tech Logic files. Waiting for your answer."
    assert run["enrolling"]["states"] == [D, D, D, W, P, P, P]
    assert run["task_question"]["states"] == [D, D, D, D, D, D, A]
    final = run["final"]
    # The task was deliberately left disabled: reported as such, never as Done.
    assert final["states"] == [D, D, D, D, D, D, L] and final["ending"] == "Completed"
    assert final["activity"] is None and final["resume"] is None
    assert final["names"][6] == "Step 7 of 7: Configure the Scheduled Task, Left disabled"
    # One announcement per step change, not per engine event: each step goes Pending -> Working -> its result (14), and
    # each of the two questions turns its step to waiting and back (4) -- out of 33 events.
    assert (final["events"], final["changes"]) == (33, 7 * 2 + 2 * 2)


@needs_bridge
def test_a_fresh_v2_run_is_not_done_with_enrollment_until_both_saves_finish(model):
    run = model["fresh_v2"]
    # Contract v2 saves the enrollment before storing the token: step 4 is still working between the two.
    assert run["between_enrollment_save_and_token_store"]["states"] == [D, D, D, W, P, P, P]
    assert run["final"]["states"] == [D, D, D, D, D, D, D] and run["final"]["ending"] == "Completed"


@needs_bridge
def test_a_resumed_run_shows_the_work_an_earlier_run_did(model):
    run = model["resume_install_legacy"]
    # Found, but not yet resuming: the person may still decline.
    assert run["found"]["resume"] == "An unfinished earlier setup was found on this computer."
    assert run["resume_question"]["states"][0] == A
    assert run["resuming"]["resume"] == "Resuming an earlier setup. No new enrollment code is needed."
    assert run["final"]["states"] == [D, D, D, E, D, D, D] and run["final"]["ending"] == "Completed"
    assert model["resume_finish_v2"]["final"]["states"] == [D, E, D, E, D, D, D]  # the v2 rules still ran
    assert model["resume_finish_legacy"]["final"]["states"] == [D, E, D, E, E, D, L]


@needs_bridge
def test_interrupted_cancelled_stopped_and_unexpected_endings_are_distinct(model):
    assert model["interrupted_during_install"]["final"]["states"] == [D, D, D, D, S, R, R]
    assert model["interrupted_during_install"]["final"]["ending"] == "Interrupted"
    assert model["cancelled_at_sources"]["final"]["states"] == [D, S, R, R, R, R, R]
    assert model["cancelled_at_sources"]["final"]["ending"] == "Cancelled"
    assert model["stopped_at_connection"]["final"]["states"] == [D, D, S, R, R, R, R]
    assert model["stopped_at_connection"]["final"]["ending"] == "Stopped"
    assert model["unexpected_error_in_verification"]["final"]["states"] == [D, D, D, D, D, S, R]
    assert model["unexpected_error_in_verification"]["final"]["ending"] == "UnexpectedError"
    declined = model["declined_resume"]["final"]
    assert declined["states"] == [S, R, R, R, R, R, R] and declined["ending"] == "Interrupted"  # the saved enrollment is kept
    assert declined["resume"] == "An unfinished earlier setup was found on this computer."  # never "Resuming"
    for name in ("interrupted_during_install", "cancelled_at_sources", "stopped_at_connection", "unexpected_error_in_verification"):
        assert model[name]["final"]["activity"] is None


@needs_bridge
def test_cancelling_the_task_question_is_not_cancelling_setup(model):
    final = model["task_question_cancelled"]["final"]
    assert final["ending"] == "Completed" and final["states"][6] == L


@needs_bridge
def test_a_run_that_could_not_start_or_ended_without_a_final_event_is_never_complete(model):
    assert model["could_not_run"]["final"]["states"] == [R] * 7
    assert model["could_not_run"]["final"]["ending"] == "CouldNotRun"
    ended = model["returned_without_a_final_event"]["final"]
    assert ended["states"] == [S, R, R, R, R, R, R] and ended["ending"] == "UnexpectedError"


@needs_bridge
def test_unknown_stages_and_event_types_change_nothing(model):
    after = model["unknown_events_change_nothing"]["after_unknown"]
    assert after["states"] == [P] * 7 and after["changes"] == 0 and after["ending"] == "None"
