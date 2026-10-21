"""Tests for the .NET bridge that hosts the setup engine in-process: installer/SortView.Setup.Core.

The bridge (SetupEngineHost + PromptBridge) loads the REAL setup.ps1 from a bundle into a fresh Windows PowerShell 5.1
runspace, turns on its structured events, registers itself as the prompt handler, and runs Invoke-SetupMain. These
tests drive it through the TEST-ONLY simulation harness (installer/SortView.Setup.Core/Testing), which:

  - accepts only generated mock bundles (make_bundle + a simulation marker, an API address on a reserved .invalid
    host), scratch roots under the temp folder, and
  - replaces every side-effect wrapper with the same recording fakes the setup suite uses (_WRAPPER_OVERRIDES), then
    proves none of the real wrappers is left before the engine runs.

So nothing here touches the network, Task Scheduler, a credential store or a real install location. Two layers:

  1. STATIC checks of the C# sources (run everywhere, including Linux CI).
  2. SIMULATED RUNS (Windows only: they build the projects with MSBuild and run Windows PowerShell 5.1).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_collector_setup import (
    _ALREADY_ENROLLED,
    _WRAPPER_OVERRIDES,
    KEY_ID,
    LEGACY_FRESH_EVENTS,
    RELEASE_VERSION,
    RULE_CANARIES,
    SENTINEL_CODE,
    SENTINEL_TOKEN,
    TIMEZONE,
    Bundle,
    default_http,
    make_bundle,
    ps_quote,
    real_bundle,
    v2_http,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = REPO_ROOT / "installer" / "SortView.Setup.Core"
CORE_PROJECT = CORE_DIR / "SortView.Setup.Core.csproj"
TESTING_DIR = CORE_DIR / "Testing"
TESTING_PROJECT = TESTING_DIR / "SortView.Setup.Core.Testing.csproj"
CORE_SOURCES = sorted(CORE_DIR.glob("*.cs"))
SETUP_SOURCE = REPO_ROOT / "collector" / "deploy" / "setup-collector.ps1"

SIMULATION_API = "https://sortview-api.invalid"
MARKER_FILE = "SORTVIEW-SIMULATION-BUNDLE.txt"
MARKER_TEXT = "Generated SortView setup simulation bundle. It installs nothing."


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# =====================================================================================
# 1. STATIC checks of the bridge sources
# =====================================================================================

def test_the_core_library_has_no_ui_dependency():
    project = _read(CORE_PROJECT)
    for ui in ("PresentationFramework", "PresentationCore", "WindowsBase", "System.Xaml", "System.Windows.Forms",
               "ApplicationDefinition", "<Page "):
        assert ui not in project, ui
    for source in CORE_SOURCES:
        text = _read(source)
        assert "System.Windows" not in text and "Dispatcher" not in text.replace("dispatcher", ""), source.name
    assert "Testing" not in project  # the shipped library never references the simulation harness


def test_the_public_bridge_api_cannot_run_any_other_powershell():
    host = _read(CORE_DIR / "SetupEngineHost.cs")
    # The one script the bridge runs itself: dot-sourcing the bundle's setup.ps1, passed as an argument.
    assert host.count("AddScript(") == 1 and 'ps.AddScript(". $args[0]").AddArgument(scriptPath)' in host
    assert re.findall(r"public \S+ (\w+)\(", host) == ["Run"]
    assert "internal SetupRunResult RunCore(" in host and "Action<Runspace, string> beforeRun" in host
    assert re.findall(r"public string (\w+) \{ get; set; \}", _read(CORE_DIR / "SetupTypes.cs")) == [
        "BundleRoot", "InstallRoot", "DataRoot"]
    assert re.findall(r"InternalsVisibleTo\(\"([^\"]+)\"\)", _read(CORE_DIR / "AssemblyInfo.cs")) == ["SortView.Setup.Core.Testing"]
    # No shippable code names the harness assembly: only test-only projects (in Testing/ folders) and the SIMULATION-ONLY
    # developer test window may -- tests/test_installer_window.py proves that window can run nothing but the harness.
    installer = REPO_ROOT / "installer"
    for path in installer.rglob("*"):
        parts = path.relative_to(installer).parts
        if (path.is_file() and path.suffix in (".cs", ".csproj", ".xaml") and "Testing" not in parts
                and parts[0] != "SortView.Setup.Spike"):
            text = _read(path)
            assert "SortView.Setup.Core.Testing" not in text or path.name == "AssemblyInfo.cs", path


# Every way .NET Framework offers to turn a SecureString (the enrollment code) into readable characters: the Marshal
# SecureString-to-memory copies and the readers that make a string of such memory. Ordinary interop (Marshal.SizeOf,
# GetLastWin32Error, MarshalAs) cannot reveal a code and is allowed.
SECRET_TO_TEXT = ("SecureStringToBSTR", "SecureStringToGlobalAllocUnicode", "SecureStringToGlobalAllocAnsi",
                  "SecureStringToCoTaskMemUnicode", "SecureStringToCoTaskMemAnsi", "PtrToStringBSTR", "PtrToStringUni",
                  "PtrToStringAuto", "PtrToStringAnsi", "Marshal.Copy", "Marshal.ReadInt16", "Marshal.ReadByte",
                  "NetworkCredential")
# The only Core files that may handle an enrollment code at all.
SECURE_STRING_FILES = {"PromptBridge.cs", "SetupTypes.cs"}


def test_the_core_never_turns_a_code_into_text_logs_or_writes_anything():
    for source in CORE_SOURCES:
        code = "\n".join(line for line in _read(source).splitlines() if not line.strip().startswith(("//", "///")))
        for forbidden in (*SECRET_TO_TEXT, "Console.", "Debug.", "Trace.", "EventLog",
                          "File.Write", "File.Append", "File.Create", "SetEnvironmentVariable", "Process.Start", ".Stop("):
            assert forbidden not in code, (source.name, forbidden)
        if source.name not in SECURE_STRING_FILES:
            assert "SecureString" not in code, source.name
    # The bundle verifier and stager in particular never see a code.
    for name in ("BundleVerifier.cs", "BundleStager.cs"):
        assert "SecureString" not in _read(CORE_DIR / name), name
    assert all((CORE_DIR / name).exists() for name in SECURE_STRING_FILES)
    bridge = _read(CORE_DIR / "PromptBridge.cs")
    assert "issuedSecrets.Add(secret)" in bridge and "secret.Dispose()" in bridge  # codes are disposed when the run ends


def test_the_harness_checks_every_side_effect_wrapper_the_engine_has():
    harness = _read(TESTING_DIR / "SimulatedSetupHarness.cs")
    listed = set(re.findall(r'"([A-Z][A-Za-z]+-[A-Za-z]+)"', harness[harness.index("SimulatedWrappers ="):harness.index("};")]))
    overridden = set(re.findall(r"^function ([A-Za-z]+-[A-Za-z]+) ", _WRAPPER_OVERRIDES, re.MULTILINE)) - {"Add-Call", "Set-FakeTaskState"}
    assert listed == overridden  # the harness verifies exactly what the shared overrides replace
    setup = _read(SETUP_SOURCE)
    wrappers = setup[setup.index("# === side-effect wrappers"):setup.index("# === pure helpers")]
    defined = set(re.findall(r"^function ([A-Za-z]+-[A-Za-z]+) \{", wrappers, re.MULTILINE))
    # Every wrapper that touches the machine, the network or the console is simulated. The rest are pure (decoding a
    # response, the one SecureString conversion) or run the bundle's own -- here stub -- tools.
    assert defined - listed == {"ConvertFrom-SecureStringPlain", "Get-ResponseBodyText", "Get-ErrorBodyText", "Invoke-SetupTool"}


# =====================================================================================
# 2. SIMULATED RUNS through the bridge
# =====================================================================================

_POWERSHELL = shutil.which("powershell")


def _find_msbuild() -> str | None:
    explicit = os.environ.get("SORTVIEW_MSBUILD")
    if explicit and Path(explicit).is_file():
        return explicit
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.is_file():
        found = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires", "Microsoft.Component.MSBuild",
                                "-find", r"MSBuild\**\Bin\MSBuild.exe"], capture_output=True, text=True, check=False)
        candidates = [line.strip() for line in found.stdout.splitlines() if line.strip()]
        if candidates:
            return candidates[0]
    return None


_MSBUILD = _find_msbuild() if sys.platform == "win32" else None

needs_bridge = pytest.mark.skipif(
    sys.platform != "win32" or _POWERSHELL is None or _MSBUILD is None,
    reason="the bridge runs are Windows-only: they build with MSBuild and host Windows PowerShell 5.1",
)


@pytest.fixture(scope="module")
def built() -> dict:
    completed = subprocess.run([_MSBUILD, str(TESTING_PROJECT), "/p:Configuration=Release", "/nologo", "/v:minimal"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    out = TESTING_DIR / "bin" / "Release"
    return {"core": out / "SortView.Setup.Core.dll", "testing": out / "SortView.Setup.Core.Testing.dll"}


# Loads the built assemblies, then runs the harness once per run in the spec -- each with its own presenter and
# cancellation source -- and writes what came back as JSON. Nothing a presenter answered is ever written.
_DRIVER = r"""
param([string]$SpecPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
$spec = Get-Content -LiteralPath $SpecPath -Raw -Encoding UTF8 | ConvertFrom-Json
Add-Type -Path $spec.core_dll
Add-Type -Path $spec.testing_dll
$records = @()
$presenters = @()
foreach ($run in @($spec.runs)) {
    foreach ($name in @('SETUPTEST_INSTALL_MODE', 'SETUPTEST_FINISH_MODE', 'SETUPTEST_CHECK_MODE', 'SETUPTEST_TOKEN_MODE',
                        'SETUPTEST_VERIFY_MODE', 'SETUPTEST_ENABLE_FAILS', 'SETUPTEST_PROTECT_FAILS', 'SETUPTEST_NO_CODE')) {
        [Environment]::SetEnvironmentVariable($name, $null)
    }
    [Environment]::SetEnvironmentVariable('SETUPTEST_FAKE_DIR', [string]$spec.fake_dir)
    if ($null -ne $run.env) { foreach ($p in $run.env.PSObject.Properties) { [Environment]::SetEnvironmentVariable($p.Name, [string]$p.Value) } }

    $presenter = New-Object SortView.Setup.Core.Testing.ScriptedPromptPresenter
    foreach ($answer in @($run.answers)) { if ($null -ne $answer) { $presenter.Enqueue([string]$answer.kind, [string]$answer.value) } }
    $cts = New-Object System.Threading.CancellationTokenSource
    $presenter.RunCancellation = $cts
    $presenters += $presenter

    $request = New-Object SortView.Setup.Core.Testing.SimulationRequest
    $request.WorkingRoot = [string]$run.working_root
    $request.BundleRoot = [string]$run.bundle_root
    $request.InstallRoot = [string]$run.install_root
    $request.DataRoot = [string]$run.data_root
    $request.WrapperOverrides = Get-Content -LiteralPath $run.overrides_path -Raw -Encoding UTF8
    $request.ThrowFromEventCallback = [bool]$run.throw_from_event_callback

    $record = [ordered]@{ refused = $null; exit_code = $null; events = @(); console_text = @(); diagnostics = @()
                          runspace_id = $null; runspace_state = $null }
    try {
        $result = [SortView.Setup.Core.Testing.SimulatedSetupHarness]::Run($request, $presenter, $cts.Token)
        $record.exit_code = $result.ExitCode
        $record.events = @(foreach ($e in $result.Events) {
            $o = [ordered]@{ schema = $e.Schema; seq = $e.Sequence; type = $e.Type; stage = $e.Stage }
            foreach ($key in $e.Fields.Keys) { $o[$key] = $e.Fields[$key] }
            $o
        })
        $record.console_text = @($result.ConsoleText)
        $record.diagnostics = @($result.Diagnostics)
        $record.runspace_id = $result.RunspaceId.ToString()
        $record.runspace_state = $result.RunspaceStateAfterRun
    } catch {
        $ex = $_.Exception
        while ($null -ne $ex.InnerException) { $ex = $ex.InnerException }
        $record.refused = $ex.GetType().Name + ': ' + $ex.Message
    }
    $record.prompts = @(foreach ($p in $presenter.Prompts) {
        [ordered]@{ id = $p.PromptId; schema = $p.Schema; context = $p.Context; children = $p.ChildProcessCount }
    })
    $record.secrets_issued = $presenter.IssuedSecretCount
    $record.secrets_disposed = $presenter.AllIssuedSecretsDisposed()
    $record.children_after = [SortView.Setup.Core.Testing.ChildProcesses]::Count()
    $cts.Dispose()
    $records += $record
}
# How many questions each presenter was asked by the END of everything: a handler that outlived its run would show.
for ($i = 0; $i -lt $records.Count; $i++) { $records[$i].final_prompt_count = $presenters[$i].Prompts.Count }
ConvertTo-Json -InputObject @($records) -Depth 10 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


def yes() -> dict:
    return {"kind": "string", "value": "yes"}


def no() -> dict:
    return {"kind": "string", "value": "no"}


def path_answer(value) -> dict:
    return {"kind": "string", "value": str(value)}


def code() -> dict:
    return {"kind": "secure", "value": SENTINEL_CODE}


def simulated_bundle(tmp_path: Path, **kwargs) -> Bundle:
    """make_bundle's mock bundle, marked as a simulation, with its API address on a reserved .invalid host."""
    bundle = make_bundle(tmp_path, template_mutation=lambda template: template.update(api_url=SIMULATION_API), **kwargs)
    (bundle.root / MARKER_FILE).write_text(MARKER_TEXT, encoding="utf-8")
    return bundle


def _overrides(bundle: Bundle) -> str:
    return (f"$FakeDir = {ps_quote(bundle.fake)}\n" + _WRAPPER_OVERRIDES
            .replace("@@ADMIN@@", "$true").replace("@@RUNTIME_VERSION@@", bundle.version).replace("@@CODE@@", SENTINEL_CODE))


class BridgeRun:
    """One run's record from the driver, plus the side effects the fakes recorded during the whole process."""

    def __init__(self, record: dict, calls: list[dict]):
        self.record = record
        self.calls = calls

    def __getitem__(self, key):
        return self.record[key]

    @property
    def exit_code(self):
        return self.record["exit_code"]

    @property
    def events(self) -> list[dict]:
        return self.record["events"] or []

    def prompt_ids(self) -> list[str]:
        return [p["id"] for p in self.record["prompts"] or []]

    def contexts(self, prompt_id: str) -> list[dict]:
        return [p["context"] for p in self.record["prompts"] or [] if p["id"] == prompt_id]

    def tools(self) -> list[str]:
        names = []
        for call in self.calls:
            if call["tool"] == "http":
                names.append(f"http:{call['method'].upper()}")
            elif call["tool"] not in ("prompt", "protect-recovery", "handler"):
                names.append(call["tool"])
        return names

    def stages(self) -> list[str]:
        out = []
        for event in self.events:
            detail = (event.get("outcome") or event.get("reason") or event.get("enrollment_outcome") or event.get("result")
                      or event.get("mode"))
            out.append(":".join(str(part) for part in (event["type"], event["stage"], detail) if part is not None))
        return out


def run_bridge(built: dict, bundle: Bundle, *runs: dict, http: dict | None = None, env: dict | None = None,
               task_state: str | None = None, overrides: str | None = None, roots: dict | None = None) -> list[BridgeRun]:
    """Runs the harness once per answer script in `runs` (each a dict with `answers`, optionally `env` and
    `throw_from_event_callback`), all in ONE PowerShell process -- one after another, as a host would."""
    fake = bundle.fake
    (fake / "http.json").write_text(json.dumps(http if http is not None else default_http()), encoding="utf-8")
    for counter in fake.glob("http_count_*.txt"):
        counter.unlink()
    if task_state is not None:
        (fake / "task.json").write_text(json.dumps({"State": task_state}), encoding="utf-8")
    overrides_path = fake / "bridge_overrides.ps1"
    overrides_path.write_text(overrides if overrides is not None else _overrides(bundle), encoding="utf-8")
    working = bundle.root.parent
    base = {"working_root": str(working), "bundle_root": str(bundle.root), "install_root": str(bundle.install_root),
            "data_root": str(bundle.data_root), "overrides_path": str(overrides_path), **(roots or {})}
    spec = {"core_dll": str(built["core"]), "testing_dll": str(built["testing"]), "fake_dir": str(fake),
            "runs": [{**base, "env": {**(env or {}), **run.get("env", {})}, "answers": run.get("answers", []),
                      "throw_from_event_callback": run.get("throw_from_event_callback", False)} for run in runs]}
    spec_path, out_path, driver_path = fake / "bridge_spec.json", fake / "bridge_out.json", fake / "bridge_driver.ps1"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    driver_path.write_text(_DRIVER, encoding="utf-8")
    calls_file = fake / "calls.jsonl"
    before = len(calls_file.read_text(encoding="utf-8").splitlines()) if calls_file.exists() else 0

    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(driver_path),
                                "-SpecPath", str(spec_path), "-OutPath", str(out_path)],
                               capture_output=True, text=True, timeout=300, check=False)
    assert completed.returncode == 0 and out_path.exists(), completed.stdout + completed.stderr
    records = json.loads(out_path.read_text(encoding="utf-8-sig"))
    lines = calls_file.read_text(encoding="utf-8").splitlines() if calls_file.exists() else []
    calls = [json.loads(line) for line in lines[before:] if line.strip()]
    return [BridgeRun(record, calls) for record in records]


def assert_bridge_invariants(run: BridgeRun) -> None:
    """Every completed bridge run: a closed runspace, gap-free events ending in exactly one run_finished whose exit
    code is the run's, the console text kept apart, every code disposed, no child process, nothing sensitive out."""
    assert run["refused"] is None, run["refused"]
    assert run["runspace_state"] == "Closed"
    events = run.events
    assert events and [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(e["schema"] == "sortview.setup.event/1" for e in events)
    assert [e["type"] for e in events].count("run_finished") == 1 and events[-1]["type"] == "run_finished"
    assert events[-1]["exit_code"] == run.exit_code
    assert run["console_text"] and not any("sortview.setup.event" in line for line in run["console_text"])
    assert run["secrets_disposed"] is True
    assert run["children_after"] == 0 and all(p["children"] == 0 for p in run["prompts"] or [])
    assert all(p["schema"] == "sortview.setup.prompt/1" for p in run["prompts"] or [])
    exposed = json.dumps({key: run[key] for key in ("events", "console_text", "diagnostics", "prompts")})
    for value in (SENTINEL_CODE, SENTINEL_TOKEN, KEY_ID, TIMEZONE, *RULE_CANARIES):
        assert value not in exposed, value


def assert_nothing_real_was_touched(run: BridgeRun) -> None:
    """Every HTTP call went to the scripted fake (whose log records it), never a SortView endpoint; the only task,
    token and install activity is the fakes'."""
    for call in run.calls:
        if call["tool"] == "http":
            assert call["url"].startswith(SIMULATION_API + "/"), call["url"]


# ---- fresh installs ---------------------------------------------------------------------------------------------

@needs_bridge
def test_a_fresh_legacy_setup_runs_end_to_end_through_the_bridge_with_the_fallback(tmp_path, built):
    bundle = simulated_bundle(tmp_path)

    (run,) = run_bridge(built, bundle, {"answers": [yes(), code(), no()]})

    assert run.exit_code == 0, run["console_text"]
    assert run.stages() == LEGACY_FRESH_EVENTS
    assert run.prompt_ids() == ["sources_confirm", "enrollment_code", "legacy_task_enable"]
    assert run.contexts("sources_confirm") == [{"checkins": str(bundle.source_dir / "Checkins.txt"),
                                                "rejects": str(bundle.source_dir / "Rejects.txt"),
                                                "acs": str(bundle.source_dir / "ACS Log.txt")}]
    assert run.tools() == ["install-verify", "runtime-version", "http:GET", "http:POST", "http:POST", "set-api-token", "install",
                           "finish-install"]
    posts = [c for c in run.calls if c["tool"] == "http" and c["method"] == "Post"]
    assert posts[0]["body"]["enrollment_code"] == SENTINEL_CODE and posts[0]["body"]["contract_version"] == 2
    assert "contract_version" not in posts[1]["body"]  # the one legacy retry, with the same code
    assert run["secrets_issued"] == 1 and json.loads((bundle.fake / "task.json").read_text()) == {"State": "Disabled"}
    assert any("COMPLETE" in line for line in run["console_text"])
    assert not [c for c in run.calls if c["tool"] in ("prompt", "read-code")]  # the console was never asked
    assert_bridge_invariants(run)
    assert_nothing_real_was_touched(run)


@needs_bridge
def test_a_fresh_v2_setup_runs_end_to_end_through_the_bridge(tmp_path, built):
    bundle = simulated_bundle(tmp_path)

    (run,) = run_bridge(built, bundle, {"answers": [yes(), code()]}, http=v2_http())

    assert run.exit_code == 0, run["console_text"]
    assert run.prompt_ids() == ["sources_confirm", "enrollment_code"]
    stages = run.stages()
    assert stages[-9:] == [
        "stage_started:rules_install", "stage_completed:rules_install:ok",
        "stage_started:verification", "stage_completed:verification:ok",
        "stage_started:record_cleanup", "stage_completed:record_cleanup:ok",
        "stage_started:task_enable", "stage_completed:task_enable:enabled",
        "run_finished:task_enable:completed",
    ]
    assert run.events[-1]["task_state"] == "Enabled"
    assert stages.index("stage_completed:enrollment_save:ok") < stages.index("stage_started:token_store")  # v2 order
    assert run.events[-1]["contract"] == 2 and run.events[-1]["installation_id"] == 41
    assert json.loads((bundle.fake / "task.json").read_text()) == {"State": "Ready"}
    assert_bridge_invariants(run)
    assert_nothing_real_was_touched(run)


@needs_bridge
def test_the_engine_still_judges_every_path_the_presenter_gives(tmp_path, built):
    bundle = simulated_bundle(tmp_path)
    missing = tmp_path / "nowhere" / "Checkins.txt"
    answers = [no(), path_answer(missing), path_answer("relative.txt"), path_answer(bundle.source_dir / "Checkins.txt"),
               path_answer(bundle.source_dir / "Rejects.txt"), path_answer(bundle.source_dir / "ACS Log.txt"), code(), no()]

    (run,) = run_bridge(built, bundle, {"answers": answers})

    assert run.exit_code == 0, run["console_text"]
    assert [(c["source"], c["attempt"], c["problem"], c["rejected_path"]) for c in run.contexts("source_path")] == [
        ("checkins", 1, None, None), ("checkins", 2, "not_found", str(missing)), ("checkins", 3, "not_absolute", "relative.txt"),
        ("rejects", 1, None, None), ("acs", 1, None, None)]
    assert_bridge_invariants(run)


# ---- resume -----------------------------------------------------------------------------------------------------

@needs_bridge
def test_resume_install_through_the_bridge_makes_no_enrollment_request(tmp_path, built):
    bundle = simulated_bundle(tmp_path)
    (first,) = run_bridge(built, bundle, {"answers": [yes(), code()]}, env={"SETUPTEST_INSTALL_MODE": "fail"})
    assert first.exit_code == 3

    (second,) = run_bridge(built, bundle, {"answers": [yes(), yes(), no()]})

    assert second.exit_code == 0, second["console_text"]
    assert second.prompt_ids() == ["resume_confirm", "sources_confirm", "legacy_task_enable"]
    assert second.contexts("resume_confirm") == [{"mode": "resume_install", "contract": 1, "release_version": RELEASE_VERSION,
                                                  "installation_id": 41}]
    assert second.tools() == ["install-verify", "runtime-version", "http:GET", "install", "finish-install"]  # no POST
    assert "stage_skipped:enrollment:resume" in second.stages() and "mode_detected:machine:resume_install" in second.stages()
    assert second["secrets_issued"] == 0
    assert_bridge_invariants(second)


@needs_bridge
def test_resume_finish_through_the_bridge_skips_the_install(tmp_path, built):
    bundle = simulated_bundle(tmp_path)
    (first,) = run_bridge(built, bundle, {"answers": [yes(), code()]}, http=v2_http(), env={"SETUPTEST_FINISH_MODE": "fail_bootstrap"})
    assert first.exit_code == 1

    (second,) = run_bridge(built, bundle, {"answers": [yes()]}, http=_ALREADY_ENROLLED)

    assert second.exit_code == 0, second["console_text"]
    assert second.prompt_ids() == ["resume_confirm"]
    assert second.contexts("resume_confirm")[0]["mode"] == "resume_finish"
    assert "stage_skipped:install:already_installed" in second.stages()
    assert [t for t in second.tools() if t not in ("enable-task",)] == ["install-verify", "runtime-version", "http:GET", "finish-install"]
    assert_bridge_invariants(second)


# ---- cancellation -----------------------------------------------------------------------------------------------

#: (answers up to and including the cancelled one, prompt id, exit code, expected tools)
_CANCEL_POINTS = {
    "sources_confirm": ([{"kind": "null"}], 1, ["install-verify", "runtime-version"]),
    "source_path": ([no(), {"kind": "null"}], 1, ["install-verify", "runtime-version"]),
    "enrollment_code": ([yes(), {"kind": "null"}], 1, ["install-verify", "runtime-version", "http:GET"]),
    "legacy_task_enable": ([yes(), code(), {"kind": "null"}], 0,
                           ["install-verify", "runtime-version", "http:GET", "http:POST", "http:POST", "set-api-token", "install",
                            "finish-install"]),
}


@needs_bridge
@pytest.mark.parametrize("prompt_id", list(_CANCEL_POINTS))
def test_a_null_answer_at_each_question_is_a_safe_cancellation(tmp_path, built, prompt_id):
    answers, exit_code, tools = _CANCEL_POINTS[prompt_id]

    (run,) = run_bridge(built, simulated_bundle(tmp_path), {"answers": answers})

    assert run.exit_code == exit_code
    assert run.tools() == tools  # cancelling never went one step further (and the code was never sent unless asked for after)
    cancelled = [e for e in run.events if e["type"] == "input_cancelled"]
    assert [e["prompt_id"] for e in cancelled] == [prompt_id]
    assert set(cancelled[0]) == {"schema", "seq", "type", "stage", "prompt_id"}
    if prompt_id == "legacy_task_enable":
        assert run.events[-1]["task_state"] == "Disabled"  # never an implicit yes
    assert_bridge_invariants(run)


@needs_bridge
def test_a_null_answer_to_resume_continues_nothing_and_is_not_a_decline(tmp_path, built):
    bundle = simulated_bundle(tmp_path)
    run_bridge(built, bundle, {"answers": [yes(), code()]}, env={"SETUPTEST_INSTALL_MODE": "fail"})

    (run,) = run_bridge(built, bundle, {"answers": [{"kind": "null"}]})

    assert run.exit_code == 1  # a decline is 2
    assert run.tools() == ["install-verify", "runtime-version"]
    assert [e["prompt_id"] for e in run.events if e["type"] == "input_cancelled"] == ["resume_confirm"]
    assert (bundle.data_root / "setup" / "enrollment-recovery.json").exists()
    assert_bridge_invariants(run)


@needs_bridge
@pytest.mark.parametrize("failure", ["throw", "fault", "object", "int", "plain_text_code", "cancel_run"])
def test_every_way_a_presenter_can_fail_at_the_code_question_stops_before_any_enrollment_request(tmp_path, built, failure):
    answer = {"kind": "string", "value": SENTINEL_CODE} if failure == "plain_text_code" else {"kind": failure}

    (run,) = run_bridge(built, simulated_bundle(tmp_path), {"answers": [yes(), answer]})

    assert run.exit_code == 1
    assert run.tools() == ["install-verify", "runtime-version", "http:GET"]  # no POST: the code was never sent
    assert [e["prompt_id"] for e in run.events if e["type"] == "input_cancelled"] == ["enrollment_code"]
    stop = next(e for e in run.events if e["type"] == "run_stopped")
    assert (stop["stage"], stop["code_attempted"]) == ("enrollment", False)
    assert_bridge_invariants(run)


@needs_bridge
def test_cancelling_the_run_while_a_question_waits_cancels_only_that_question(tmp_path, built):
    (run,) = run_bridge(built, simulated_bundle(tmp_path), {"answers": [{"kind": "cancel_run"}]})

    assert run.exit_code == 1 and run.tools() == ["install-verify", "runtime-version"]
    assert [e["prompt_id"] for e in run.events if e["type"] == "input_cancelled"] == ["sources_confirm"]
    assert_bridge_invariants(run)


# ---- lifecycle and isolation ----------------------------------------------------------------------------------

@needs_bridge
def test_each_run_gets_a_new_runspace_and_a_handler_that_never_outlives_it(tmp_path, built):
    bundle = simulated_bundle(tmp_path)

    first, second = run_bridge(built, bundle, {"answers": [{"kind": "null"}]}, {"answers": [yes(), code(), no()]})

    assert first.exit_code == 1 and second.exit_code == 0
    assert first["runspace_id"] != second["runspace_id"]
    assert first["runspace_state"] == second["runspace_state"] == "Closed"
    # The first run's presenter was asked exactly once -- by its own run -- even after the second run asked three times.
    assert first["final_prompt_count"] == 1 and second["final_prompt_count"] == 3
    assert [e["seq"] for e in second.events][:1] == [1]  # numbering started again
    assert second["secrets_issued"] == 1 and second["secrets_disposed"] is True
    assert_bridge_invariants(first)
    assert_bridge_invariants(second)


@needs_bridge
def test_a_failing_event_callback_never_changes_the_run(tmp_path, built):
    normal_tmp, failing_tmp = tmp_path / "normal", tmp_path / "failing"
    (normal,) = run_bridge(built, simulated_bundle(normal_tmp), {"answers": [yes(), code(), no()]})
    (failing,) = run_bridge(built, simulated_bundle(failing_tmp), {"answers": [yes(), code(), no()], "throw_from_event_callback": True})

    assert failing.exit_code == normal.exit_code == 0
    assert failing.stages() == normal.stages() and failing.tools() == normal.tools()
    assert any(d.startswith("A host callback failed") for d in failing["diagnostics"])


# ---- the harness refuses anything that is not a simulation ------------------------------------------------------

def _assert_refused(run: BridgeRun, reason: str) -> None:
    assert run["refused"] is not None and run["refused"].startswith("SimulationRefusedException:"), run["refused"]
    assert reason in run["refused"], run["refused"]
    assert run.exit_code is None and run.calls == [] and run["prompts"] == []


@needs_bridge
def test_a_real_release_bundle_is_refused(tmp_path, built):
    bundle = real_bundle(tmp_path)  # (built under tmp_path/out/, so the scratch root is tmp_path itself)
    roots = {"working_root": str(tmp_path)}
    (run,) = run_bridge(built, bundle, {"answers": [yes()]}, roots=roots)
    _assert_refused(run, "no simulation marker")

    (bundle.root / MARKER_FILE).write_text(MARKER_TEXT, encoding="utf-8")  # even when someone marks a real bundle
    (run,) = run_bridge(built, bundle, {"answers": [yes()]}, roots=roots)
    _assert_refused(run, "lists files")


@needs_bridge
def test_a_bundle_pointing_at_a_real_api_address_is_refused(tmp_path, built):
    bundle = make_bundle(tmp_path)  # the template's production API address
    (bundle.root / MARKER_FILE).write_text(MARKER_TEXT, encoding="utf-8")

    (run,) = run_bridge(built, bundle, {"answers": [yes()]})

    _assert_refused(run, ".invalid")


@needs_bridge
@pytest.mark.parametrize("root", ["install_root", "data_root"])
def test_real_install_locations_are_refused(tmp_path, built, root):
    real = {"install_root": r"C:\SortView\Collector", "data_root": r"C:\ProgramData\SortViewCollector"}[root]

    (run,) = run_bridge(built, simulated_bundle(tmp_path), {"answers": [yes()]}, roots={root: real})

    _assert_refused(run, "must be inside WorkingRoot")


@needs_bridge
@pytest.mark.parametrize("wrapper", ["Invoke-SetupHttp", "Enable-SortViewTask", "Test-ApiTokenStored"])
def test_a_wrapper_left_real_is_refused_before_the_engine_runs(tmp_path, built, wrapper):
    bundle = simulated_bundle(tmp_path)
    overrides = _overrides(bundle)
    start = overrides.index(f"function {wrapper} ")
    end = overrides.index("\nfunction ", start + 1) if "\nfunction " in overrides[start + 1:] else len(overrides)
    without = overrides[:start] + overrides[end + 1:]

    (run,) = run_bridge(built, bundle, {"answers": [yes()]}, overrides=without)

    _assert_refused(run, f"the wrapper {wrapper} is still the real one")


@needs_bridge
def test_simulated_runs_reach_nothing_but_the_fakes(tmp_path, built):
    bundle = simulated_bundle(tmp_path)
    before = {path for path in tmp_path.rglob("*")}

    (run,) = run_bridge(built, bundle, {"answers": [yes(), code(), no()]})

    assert run.exit_code == 0
    assert_nothing_real_was_touched(run)
    # Every HTTP request was answered by the scripted fake: the two enrollment requests and the connection check.
    assert [c["url"] for c in run.calls if c["tool"] == "http"] == [
        f"{SIMULATION_API}/", f"{SIMULATION_API}/collector/enroll", f"{SIMULATION_API}/collector/enroll"]
    # The token went to the stub's record folder, the task to the fake task file -- both inside the scratch folder.
    assert (bundle.fake / "machine_token.txt").read_text(encoding="utf-8") == SENTINEL_TOKEN
    assert json.loads((bundle.fake / "task.json").read_text()) == {"State": "Disabled"}
    created = {path for path in tmp_path.rglob("*")} - before
    assert created and all(path.is_relative_to(tmp_path) for path in created)
    assert {path.relative_to(tmp_path).parts[0] for path in created} <= {"fake", "install", "data"}
