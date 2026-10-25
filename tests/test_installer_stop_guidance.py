"""Tests for the setup wizard's stop guidance, G2: installer/SortView.Setup.Wizard/StopGuidance.cs.

After a run that did not complete, the model says what to do next -- from the engine's structured events only -- and offers
Install again only where the engine's own state makes that safe. Its diagnostic summary (Copy details) holds only fixed text
and values checked against the engine's closed vocabularies.

  1. STATIC checks (run everywhere): every stage the engine can stop in is handled by name, the actions never send anyone
     to PowerShell or to a new enrollment code, and the summary reads only approved fields.
  2. MODEL scenarios (Windows only): the engine's stops, as event sequences, fed to the built model in one PowerShell
     process -- the category, the action, whether Install again is offered, and the summary.
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
GUIDANCE_CODE = WIZARD_DIR / "StopGuidance.cs"
WIZARD_PROJECT = WIZARD_DIR / "SortView.Setup.Wizard.csproj"

#: The actions that tell the person to select Install again: each one a case the engine's state makes safe.
RETRY_ACTIONS = {"CancelledAction", "ResumeAction", "SourcesAction", "ConnectionAction", "SetupFolderAction", "RejectedAction",
                 "RateLimitedAction", "ResumeInstallAction"}
#: The only fields the summary may read: closed vocabularies, numbers, yes/no, an id, a version, a type name.
SUMMARY_FIELDS = {"exit_code", "contract", "enrollment_outcome", "refusal_code", "code_used", "code_attempted", "code_consumed",
                  "code_rejected", "token_stored", "recovery_on_disk", "installation_id", "release_version", "exception_type"}
SUMMARY_KEYS = {"Time", "Result", "Stopped at", "What to do", "Action", "Setup stage", "Exit code", "Mode", "Contract",
                "Cancelled question", "Resume declined", "Enrollment result", "Enrollment code used",
                "Enrollment code sent, result unknown", "Enrollment completed", "Enrollment code not accepted", "API token stored",
                "Saved enrollment on this computer", "Installation", "Collector release", "Unexpected error type"}


def _actions() -> dict[str, str]:
    """Every action constant, its string literals joined."""
    code = GUIDANCE_CODE.read_text(encoding="utf-8")
    found = {}
    for name, body in re.findall(r"internal const string (\w+Action) =(.*?);", code, flags=re.DOTALL):
        found[name] = "".join(json.loads(f'"{literal}"') for literal in re.findall(r'"((?:[^"\\]|\\.)*)"', body))
    return found


def _stop_stages() -> set[str]:
    """Every stage a stop can be reported against: the stage last entered before each stop in the engine's main flow."""
    text = ENGINE.read_text(encoding="utf-8")
    main = text[text.index("function Invoke-SetupMain"):]
    stages, stage = set(), None
    for line in main.splitlines():
        entered = re.search(r'(?:Enter|Skip)-SetupStage -Id "(\w+)"', line)
        if entered:
            stage = entered.group(1)
        if re.search(r"\bStop-Setup(InputCancelled|SourcesCancelled)?\b|\bGet-SourceSelection\b", line) and stage:
            stages.add(stage)
    return stages


# =====================================================================================
# 1. STATIC checks
# =====================================================================================

def test_every_stage_the_engine_stops_in_is_handled_by_name():
    stages = _stop_stages()
    assert {"elevation", "bundle_verification", "bundle", "machine", "sources", "connection", "setup_folder", "enrollment",
            "enrollment_save", "token_store", "install", "rules_install", "verification", "task_enable"} <= stages
    handled = set(re.findall(r'case "(\w+)":', GUIDANCE_CODE.read_text(encoding="utf-8")))
    assert stages <= handled, sorted(stages - handled)


def test_the_actions_never_send_anyone_to_powershell_or_a_new_enrollment_code():
    actions = _actions()
    assert len(actions) == 19 and RETRY_ACTIONS <= set(actions)
    for name, action in actions.items():
        for forbidden in (".ps1", "PowerShell", "Enable-ScheduledTask", "Start-ScheduledTask", "tools\\", "exit code",
                          "new enrollment code", "new code", "another enrollment code", "another code"):
            assert forbidden.lower() not in action.lower(), (name, forbidden)
        assert not re.search(r"(^|\s)-[A-Z]\w+", action), name  # no command-line switch
        # Install again is suggested by exactly the safe actions, and every other action that mentions it says not to.
        suggests = bool(re.search(r"select Install (to|and)\b", action, flags=re.IGNORECASE))
        assert suggests == (name in RETRY_ACTIONS), name
        if "Install again" in action:
            assert "Do not select Install again" in action, name


def test_the_summary_reads_only_approved_fields_and_never_the_engines_words():
    code = GUIDANCE_CODE.read_text(encoding="utf-8")
    summary = code[code.index("public string DiagnosticSummary"):code.index("private static void Line")]
    read = set(re.findall(r'(?:Text|Int|Bool|YesNo)\(\w+, "(\w+)"\)', summary))
    assert read <= SUMMARY_FIELDS, sorted(read - SUMMARY_FIELDS)
    for engine_text in ("problem", "unchanged", "fix", "advice", "message", "step_label"):
        assert f'"{engine_text}"' not in code, engine_text  # the model never reads the engine's words at all
    for forbidden in ("SecureString", "CodeBox", "Password", "ConsoleText", "Clipboard", "File.", "Environment."):
        assert forbidden not in code, forbidden


# =====================================================================================
# 2. MODEL scenarios
# =====================================================================================

def ev(type_: str, stage: str | None = None, **fields) -> dict:
    return {"op": "event", "type": type_, "stage": stage, "fields": fields}


def stop(stage: str, exit_code: int | None, **fields) -> list[dict]:
    flags = {"code_attempted": False, "code_consumed": False, "code_rejected": False, "token_stored": False, "recovery_on_disk": False}
    flags.update(fields)
    if exit_code is not None:
        flags["exit_code"] = exit_code
    flags.setdefault("step_label", "step ? (engine label)")
    return [ev("stage_started", stage), ev("run_stopped", stage, **flags), ev("run_finished", None, result="stopped", exit_code=exit_code or 1),
            {"op": "finish", "fault": False}]


def enrolled(outcome: str, refusal: str | None = None, code_used: str = "not_used") -> dict:
    return ev("enrollment_result", "enrollment", requested_contract=2, enrollment_outcome=outcome, refusal_code=refusal, code_used=code_used)


RESUMABLE = {"code_consumed": True, "token_stored": True, "recovery_on_disk": True, "contract": 2}
FRESH = [ev("mode_detected", "machine", mode="fresh")]

# name -> (events, expected category, expected action constant, Install again offered, details opened, step)
SCENARIOS = {
    "cancelled_sources": ([ev("input_cancelled", "sources", prompt_id="sources_confirm"), *stop("sources", 1)],
                          "TryAgain", "CancelledAction", True, False, 2),
    "cancelled_code": ([ev("input_cancelled", "enrollment", prompt_id="enrollment_code"), *stop("enrollment", 1)],
                       "TryAgain", "CancelledAction", True, False, 4),
    "cancelled_resume": ([ev("mode_detected", "machine", mode="resume_install", contract=2),
                          ev("input_cancelled", "machine", prompt_id="resume_confirm"), *stop("machine", 1, **RESUMABLE)],
                         "TryAgain", "ResumeAction", True, False, 1),
    "declined_resume": ([ev("mode_detected", "machine", mode="resume_install", contract=1), {"op": "decline"},
                         *stop("machine", 2, **RESUMABLE)], "TryAgain", "ResumeAction", True, False, 1),
    "machine_refusal": ([*stop("machine", 2)], "ContactSupport", "MachineAction", False, False, 1),
    "machine_v2_record_without_token": ([*stop("machine", 2, code_consumed=True, recovery_on_disk=True, contract=2)],
                                        "ContactSupport", "MachineAction", False, False, 1),
    "elevation": ([*stop("elevation", 1)], "NeedsAdministrator", "ElevationAction", False, False, 1),
    "bundle_verification": ([*stop("bundle_verification", 1)], "NeedsAdministrator", "PackageAction", False, False, 1),
    "bundle": ([*stop("bundle", 1)], "NeedsAdministrator", "PackageAction", False, False, 1),
    "sources_missing": ([*FRESH, *stop("sources", 1)], "TryAgain", "SourcesAction", True, False, 2),
    "sources_without_exit_code": ([*FRESH, *stop("sources", None)], "ContactSupport", "SupportAction", False, False, 2),
    "connection_fresh": ([*FRESH, *stop("connection", 1)], "NeedsAdministrator", "ConnectionAction", True, False, 3),
    "connection_resume": ([*stop("connection", 1, **RESUMABLE)], "NeedsAdministrator", "ConnectionAction", True, False, 3),
    "connection_enrolled_without_saved_enrollment": ([*stop("connection", 1, code_consumed=True, token_stored=True)],
                                                     "ContactSupport", "SupportAction", False, False, 3),
    "setup_folder": ([*stop("setup_folder", 1)], "NeedsAdministrator", "SetupFolderAction", True, False, 4),
    "refused_upgrade": ([enrolled("Refused", "collector_upgrade_required"), *stop("enrollment", 1)],
                        "NeedsAdministrator", "UpgradeRequiredAction", False, False, 4),
    "refused_not_ready": ([enrolled("Refused", "enrollment_not_ready"), *stop("enrollment", 1)],
                          "NeedsAdministrator", "NotReadyAction", False, False, 4),
    "refused_unsupported": ([enrolled("Refused", "unsupported_contract_version"), *stop("enrollment", 1)],
                            "ContactSupport", "SupportAction", False, False, 4),
    # The legacy retry: the v2 refusal first, then the legacy request's own result is the one that counts.
    "legacy_retry_rejected": ([enrolled("Refused", "contract_not_permitted"), enrolled("Rejected", code_used="not_used_by_this_request"),
                               *stop("enrollment", 1, code_rejected=True)], "TryAgain", "RejectedAction", True, False, 4),
    "rate_limited": ([enrolled("RateLimited"), *stop("enrollment", 1)], "TryAgain", "RateLimitedAction", True, False, 4),
    "rate_limited_but_flags_disagree": ([enrolled("RateLimited"), *stop("enrollment", 1, code_attempted=True)],
                                        "ContactSupport", "SupportAction", False, False, 4),
    "rejected_without_the_flag": ([enrolled("Rejected", code_used="not_used_by_this_request"), *stop("enrollment", 1)],
                                  "ContactSupport", "SupportAction", False, False, 4),
    "unreachable": ([enrolled("Unreachable", code_used="unknown"), *stop("enrollment", 1, code_attempted=True)],
                    "ContactSupport", "EnrollmentUnknownAction", False, False, 4),
    "server_error": ([enrolled("ServerError", code_used="unknown"), *stop("enrollment", 1, code_attempted=True)],
                     "ContactSupport", "EnrollmentUnknownAction", False, False, 4),
    "bad_response": ([enrolled("BadResponse", code_used="unknown"), *stop("enrollment", 1, code_attempted=True)],
                     "ContactSupport", "EnrollmentUnknownAction", False, False, 4),
    "redirected": ([enrolled("Redirected"), *stop("enrollment", 1)], "ContactSupport", "SupportAction", False, False, 4),
    "enrollment_without_a_result": ([*stop("enrollment", 1)], "ContactSupport", "SupportAction", False, False, 4),
    "enrollment_save_failed": ([*stop("enrollment_save", 1, code_consumed=True, contract=2)],
                               "ContactSupport", "AfterEnrollmentAction", False, False, 4),
    "v2_token_not_stored": ([*stop("token_store", 1, code_consumed=True, recovery_on_disk=True, contract=2)],
                            "ContactSupport", "AfterEnrollmentAction", False, False, 4),
    "legacy_record_not_saved": ([*stop("enrollment_save", 1, code_consumed=True, token_stored=True, contract=1)],
                                "ContactSupport", "AfterEnrollmentAction", False, False, 4),
    "install_resumable": ([*stop("install", 3, **RESUMABLE)], "TryAgain", "ResumeInstallAction", True, True, 5),
    "install_not_resumable": ([*stop("install", 1, code_consumed=True, token_stored=True)],
                              "ContactSupport", "InstallSupportAction", False, True, 5),
    "rules_install_resumable": ([*stop("rules_install", 2, **RESUMABLE)], "TryAgain", "ResumeInstallAction", True, True, 5),
    "verification_step_failed": ([*stop("verification", 1, **RESUMABLE)], "TryAgain", "ResumeInstallAction", True, True, 6),
    "verification_refused": ([*stop("verification", 2, **RESUMABLE)], "ContactSupport", "InstallSupportAction", False, True, 6),
    "verification_not_resumable": ([*stop("verification", 1, code_consumed=True, token_stored=True)],
                                   "ContactSupport", "InstallSupportAction", False, True, 6),
    "task_enable_failed": ([*stop("task_enable", 1, code_consumed=True, token_stored=True, contract=2)],
                           "NeedsAdministrator", "TaskEnableAction", False, False, 7),
    "unknown_stage": ([*stop("a_future_stage", 1)], "ContactSupport", "SupportAction", False, False, None),
    "unexpected_error": ([ev("stage_started", "verification"),
                          ev("run_failed_unexpected", "verification", exception_type="IOException", message="disk full", **RESUMABLE),
                          ev("run_finished", None, result="failed", exit_code=1), {"op": "finish", "fault": False}],
                         "ContactSupport", "SupportAction", False, False, 6),
    "could_not_start": ([{"op": "finish", "fault": True}], "TryAgain", "CouldNotStartAction", False, False, None),
    "faulted_after_it_began": ([ev("stage_started", "elevation"), {"op": "finish", "fault": True}],
                               "ContactSupport", "SupportAction", False, False, None),
    "returned_without_a_final_event": ([ev("stage_started", "machine"), {"op": "finish", "fault": False}],
                                       "ContactSupport", "SupportAction", False, False, None),
    "completed": ([ev("run_finished", None, result="completed", exit_code=0), {"op": "finish", "fault": False}],
                  "None", None, False, False, None),
    "completed_task_question_cancelled": ([ev("input_cancelled", "task_enable", prompt_id="legacy_task_enable"),
                                           ev("run_finished", None, result="completed", exit_code=0), {"op": "finish", "fault": False}],
                                          "None", None, False, False, None),
    "completed_but_nonzero_exit": ([ev("run_finished", None, result="completed", exit_code=1), {"op": "finish", "fault": False}],
                                   "ContactSupport", "SupportAction", False, False, None),
}

#: Engine text and unchecked values planted in every field the summary must not copy.
PLANTED = "PLANTED-SECRET C:\\Users\\alice\\api_token.dpapi SIM-CODE-1234 agent_token=abc"
SUMMARY_SCENARIOS = {
    "install_resumable_with_words": [
        ev("mode_detected", "machine", mode="fresh"),
        enrolled("Enrolled", code_used="used"),
        ev("stage_started", "install"),
        ev("run_stopped", "install", exit_code=3, step_label=PLANTED, problem=PLANTED, unchanged=PLANTED, fix=PLANTED,
           advice=[PLANTED, PLANTED], **RESUMABLE),
        ev("run_finished", None, result="stopped", exit_code=3, installation_id=7, release_version="1.0.14"),
        {"op": "finish", "fault": False},
    ],
    "unexpected_with_unchecked_values": [
        ev("mode_detected", "machine", mode=PLANTED),
        ev("enrollment_result", "enrollment", requested_contract=2, enrollment_outcome=PLANTED, refusal_code=PLANTED, code_used=PLANTED),
        ev("stage_started", "verification"),
        ev("run_failed_unexpected", "verification", exception_type="System.IO.IOException", message=PLANTED, advice=[PLANTED], **RESUMABLE),
        ev("run_finished", None, result="failed", exit_code=1, installation_id=PLANTED, release_version=PLANTED),
        {"op": "finish", "fault": False},
    ],
}

_MODEL_SCRIPT = r"""
param([string]$WizardPath, [string]$CasesPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -Path (Join-Path (Split-Path -Parent $WizardPath) 'SortView.Setup.Core.dll')
Add-Type -Path $WizardPath
$cases = Get-Content -LiteralPath $CasesPath -Raw | ConvertFrom-Json
$at = New-Object DateTimeOffset 2026, 10, 25, 9, 30, 0, ([TimeSpan]::FromHours(-5))
$out = [ordered]@{}
foreach ($case in $cases.PSObject.Properties) {
    $guidance = New-Object SortView.Setup.Wizard.StopGuidance
    foreach ($action in $case.Value) {
        switch ($action.op) {
            'event' {
                $fields = New-Object 'System.Collections.Generic.Dictionary[string,object]'
                foreach ($f in $action.fields.PSObject.Properties) {
                    $value = $f.Value
                    if ($value -is [array]) { $value = [string[]]$value }
                    $fields[$f.Name] = $value
                }
                $stage = if ($null -eq $action.stage) { $null } else { [string]$action.stage }
                $guidance.Apply([string]$action.type, $stage, $fields)
            }
            'decline' { $guidance.NoteResumeDeclined() }
            'finish' { $guidance.Finish([bool]$action.fault) }
        }
    }
    $out[$case.Name] = [ordered]@{ category = $guidance.Category.ToString(); action = $guidance.Action; retry = $guidance.MayRetryInstall
                                   details = $guidance.ShowDetails; step = $guidance.StepNumber; step_text = $guidance.StepText
                                   summary = $guidance.DiagnosticSummary($at) }
}
ConvertTo-Json -InputObject $out -Depth 4 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


@pytest.fixture(scope="module")
def model(tmp_path_factory) -> dict:
    completed = subprocess.run([_MSBUILD, str(WIZARD_PROJECT), "/p:Configuration=Release", "/nologo", "/v:minimal"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    work = tmp_path_factory.mktemp("guidance")
    cases = {name: events for name, (events, *_rest) in SCENARIOS.items()} | SUMMARY_SCENARIOS
    (work / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (work / "model.ps1").write_text(_MODEL_SCRIPT, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                str(work / "model.ps1"), "-WizardPath", str(WIZARD_DIR / "bin" / "Release" / "SortView.Setup.Wizard.dll"),
                                "-CasesPath", str(work / "cases.json"), "-OutPath", str(work / "out.json")],
                               capture_output=True, text=True, timeout=120, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads((work / "out.json").read_text(encoding="utf-8-sig"))


@needs_bridge
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_each_stop_gets_its_category_action_and_retry_decision(model, name):
    _events, category, action, retry, details, step = SCENARIOS[name]
    got = model[name]
    assert got["category"] == category
    assert got["action"] == (None if action is None else _actions()[action])
    assert got["retry"] is retry and got["details"] is details and got["step"] == step


@needs_bridge
def test_install_again_is_offered_only_for_the_safe_cases(model):
    offered = {name for name, got in model.items() if got["retry"]}
    assert offered == {name for name, (_e, _c, _a, retry, _d, _s) in SCENARIOS.items() if retry} | {"install_resumable_with_words"}
    # Never after an unknown enrollment result, a failure after enrolling (outside a resumable install), a refusal, a
    # task-enable failure, an unexpected error or an unknown stop.
    for name in ("unreachable", "server_error", "bad_response", "redirected", "enrollment_save_failed", "v2_token_not_stored",
                 "verification_refused", "verification_not_resumable", "install_not_resumable", "task_enable_failed",
                 "unexpected_error", "unknown_stage", "machine_refusal", "faulted_after_it_began"):
        assert model[name]["retry"] is False, name


@needs_bridge
def test_the_step_is_the_seven_step_lists_own(model):
    assert model["install_resumable"]["step_text"] == "Step 5 of 7: Install SortView Collector"
    assert model["refused_upgrade"]["step_text"] == "Step 4 of 7: Enroll this computer"
    assert model["task_enable_failed"]["step_text"] == "Step 7 of 7: Configure the Scheduled Task"
    assert model["unknown_stage"]["step_text"] is None


@needs_bridge
def test_the_summary_is_useful_and_holds_none_of_the_engines_words(model):
    summary = model["install_resumable_with_words"]["summary"]
    lines = summary.splitlines()
    assert lines[0] == "SortView Setup: diagnostic summary"
    assert "Time: 2026-10-25 09:30:00 -05:00" in lines and "Result: stopped" in lines
    assert "Stopped at: Step 5 of 7: Install SortView Collector" in lines and "What to do: You can try again" in lines
    assert "Action: " + _actions()["ResumeInstallAction"] in lines
    for expected in ("Setup stage: install", "Exit code: 3", "Mode: fresh", "Contract: 2", "Enrollment result: Enrolled",
                     "Enrollment code used: used", "Enrollment completed: yes", "API token stored: yes",
                     "Saved enrollment on this computer: yes", "Installation: 7", "Collector release: 1.0.14"):
        assert expected in lines, expected
    assert lines[-1] == "This summary holds no enrollment code, API token, secret, file path or setup log."


@needs_bridge
@pytest.mark.parametrize("name", list(SUMMARY_SCENARIOS) + list(SCENARIOS))
def test_the_summary_never_copies_engine_text_or_unchecked_values(model, name):
    summary = model[name]["summary"]
    for planted in ("PLANTED", "alice", "api_token", "SIM-CODE", "agent_token", "\\", "engine label"):
        assert planted not in summary, (name, planted)
    for line in summary.splitlines()[1:-1]:
        assert line.split(": ", 1)[0] in SUMMARY_KEYS, (name, line)


@needs_bridge
def test_an_unexpected_error_summary_keeps_only_the_checked_values(model):
    lines = model["unexpected_with_unchecked_values"]["summary"].splitlines()
    assert "Unexpected error type: System.IO.IOException" in lines and "Result: failed" in lines
    for dropped in ("Mode:", "Enrollment result:", "Enrollment code used:", "Installation:", "Collector release:"):
        assert not any(line.startswith(dropped) for line in lines), dropped
