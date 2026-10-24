"""Contract v2 in the two tools the guided setup drives (onboarding stage A3a):

  * install.ps1 (collector/deploy/install-release.ps1) -- the optional -ContractMode / -KeyId / -Timezone inputs, the
    config they add to, and the atomic config write;
  * tools\\finish-install.ps1 (collector/deploy/finish-collector-install.ps1) -- the local-secret step and the v2 cursor.

The finish script is EXECUTED here, not just read: the real script runs in real Windows PowerShell against a tiny
compiled stand-in for SortViewCollector.exe whose exit codes each scenario scripts, with its three sibling tools
stubbed and the Scheduled Task lookup replaced. One line of the script is changed for the run -- its elevation check,
exactly as tests/test_collector_setup.py does for the token tool -- and nothing else: every step, its order, every
stop and every exit code below is the script's own.

The guided setup itself (setup.ps1) is covered in tests/test_collector_setup.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from collector import state as collector_state

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "collector" / "deploy"
INSTALL_SOURCE = DEPLOY / "install-release.ps1"
FINISH_SOURCE = DEPLOY / "finish-collector-install.ps1"

KEY_ID = "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"
TIMEZONE = "America/New_York"
SOURCES = ("checkins", "rejects", "acs")

_POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
needs_windows_powershell = pytest.mark.skipif(
    sys.platform != "win32" or _POWERSHELL is None, reason="these scenarios run the real scripts in Windows PowerShell"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_ps_file(script: str, folder: Path, *, env: dict | None = None) -> subprocess.CompletedProcess:
    assert _POWERSHELL is not None
    path = folder / "run.ps1"
    path.write_text(script, encoding="utf-8")
    return subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)],
        capture_output=True, text=True, timeout=180, check=False, env={**os.environ, **(env or {})},
    )


def _ps_function(name: str, text: str) -> str:
    match = re.search(rf"^function {re.escape(name)} \{{.*?^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"function {name} not found"
    return match.group(0)


INSTALL_TEXT = _read(INSTALL_SOURCE)
FINISH_TEXT = _read(FINISH_SOURCE)


# =====================================================================================
# install.ps1: the Contract v2 config
# =====================================================================================

_BASE_ARGS = ("-CustomerId 7 -BranchId 3 -InstallationId 41 -ApiUrl 'https://api.example.org' "
              "-CheckinsPath 'C:\\TL\\Checkins.txt' -RejectsPath 'C:\\TL\\Rejects.txt' -AcsPath 'C:\\TL\\ACS Log.txt' "
              "-DataRoot 'C:\\ProgramData\\SortViewCollector'")
_LEGACY_CONFIG = {
    "customer_id": 7, "branch_id": 3, "installation_id": 41, "api_url": "https://api.example.org",
    "sources": [{"name": "checkins", "path": "C:\\TL\\Checkins.txt"}, {"name": "rejects", "path": "C:\\TL\\Rejects.txt"},
                {"name": "acs", "path": "C:\\TL\\ACS Log.txt"}],
    "state_path": "C:\\ProgramData\\SortViewCollector\\data\\state.json",
    "status_path": "C:\\ProgramData\\SortViewCollector\\data\\status.json",
    "log_path": "C:\\ProgramData\\SortViewCollector\\logs\\collector.log",
}


def _generated_config(tmp_path: Path, extra: str = "") -> dict:
    script = _ps_function("New-CollectorConfigJson", INSTALL_TEXT) + f"\nNew-CollectorConfigJson {_BASE_ARGS} {extra}"
    completed = _run_ps_file(script, tmp_path)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@needs_windows_powershell
def test_the_legacy_config_is_exactly_what_it_was_key_for_key_and_in_order(tmp_path):
    config = _generated_config(tmp_path)

    assert config == _LEGACY_CONFIG and list(config) == list(_LEGACY_CONFIG)
    assert "contract_mode" not in config and "v2" not in config


@needs_windows_powershell
def test_the_v2_config_adds_the_contract_the_key_id_and_the_time_zone_and_nothing_else(tmp_path):
    config = _generated_config(tmp_path, f"-ContractMode v2 -KeyId {KEY_ID} -Timezone {TIMEZONE}")

    assert config == {**_LEGACY_CONFIG, "contract_mode": "v2", "v2": {"key_id": KEY_ID, "timezone": TIMEZONE}}
    assert list(config) == [*_LEGACY_CONFIG, "contract_mode", "v2"]  # the legacy keys first, untouched
    # Every other v2 setting -- the rules, secret, state, status, cache and quarantine paths, and the limits -- is the
    # Collector's own default: none of them is written.
    assert set(config["v2"]) == {"key_id", "timezone"}
    assert not {"api_token", "token", "secret"} & set(config) and "token" not in json.dumps(config).lower()


def test_the_collector_itself_loads_the_generated_v2_config_with_its_default_paths(tmp_path, monkeypatch):
    """The config install.ps1 writes is the one the runtime reads: same mode, same key, same zone, default paths under the data root."""
    from collector_token_support import store_api_token

    from collector.config import load_config
    from collector.v2_config import load_v2_config, read_contract_mode

    store_api_token(monkeypatch, "test-token")
    root = tmp_path / "SortViewCollector"
    document = {**_LEGACY_CONFIG, "state_path": str(root / "data" / "state.json"), "status_path": str(root / "data" / "status.json"),
                "log_path": str(root / "logs" / "collector.log"), "contract_mode": "v2", "v2": {"key_id": KEY_ID, "timezone": TIMEZONE}}
    path = tmp_path / "collector_config.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert read_contract_mode(path) == "v2"
    assert load_config(path).installation_id == 41
    v2 = load_v2_config(path, require=True)
    assert v2 is not None and (v2.key_id, v2.timezone) == (KEY_ID, TIMEZONE)
    assert v2.rules_path == root / "config" / "classification_rules.json"   # where setup.ps1 installs the server's rules
    assert v2.secret_path == root / "secrets" / "v2_key.dpapi"              # what `v2-key init` creates
    assert v2.state_path == root / "data" / "v2" / "state_v2.json"          # what finish-install.ps1 verifies


@needs_windows_powershell
def test_contract_inputs_are_validated_before_anything_is_touched(tmp_path):
    cases = {
        "nothing": ("", 0),
        "v2_complete": (f"-ContractMode v2 -KeyId {KEY_ID} -Timezone {TIMEZONE}", 0),
        "v2_no_key": (f"-ContractMode v2 -Timezone {TIMEZONE}", 1),
        "v2_no_zone": (f"-ContractMode v2 -KeyId {KEY_ID}", 1),
        "v2_neither": ("-ContractMode v2", 2),
        "v2_key_not_a_uuid": (f"-ContractMode v2 -KeyId CANARY-NOT-A-KEY -Timezone {TIMEZONE}", 1),
        "v2_key_upper_case": (f"-ContractMode v2 -KeyId {KEY_ID.upper()} -Timezone {TIMEZONE}", 1),
        "unknown_mode": (f"-ContractMode v3 -KeyId {KEY_ID} -Timezone {TIMEZONE}", 1),
        "v1_named": ("-ContractMode v1", 1),  # the legacy contract is selected by OMITTING the mode
        "key_without_mode": (f"-KeyId {KEY_ID}", 1),
        "zone_without_mode": (f"-Timezone {TIMEZONE}", 1),
    }
    lines = [_ps_function("Get-ContractInputProblems", INSTALL_TEXT)]
    for name, (args, _count) in cases.items():
        lines.append(f"$p = @(Get-ContractInputProblems {args}); '{name}=' + $p.Count + '|' + ($p -join ' / ')")

    completed = _run_ps_file("\n".join(lines), tmp_path)
    assert completed.returncode == 0, completed.stderr
    results = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)

    for name, (_args, count) in cases.items():
        assert results[name].split("|")[0] == str(count), (name, results[name])
    assert "CANARY-NOT-A-KEY" not in completed.stdout and KEY_ID.upper() not in completed.stdout  # a key id is never echoed
    # ...and they are checked with the other required inputs, before the bundle is verified or anything is created.
    assert INSTALL_TEXT.index("Get-ContractInputProblems -ContractMode $ContractMode") < INSTALL_TEXT.index("$isSourceBundle =")


def test_the_three_contract_parameters_are_optional_and_reach_the_config_builder():
    block = INSTALL_TEXT[INSTALL_TEXT.index("[CmdletBinding()]"):INSTALL_TEXT.index('$ErrorActionPreference = "Stop"')]
    for name in ("ContractMode", "KeyId", "Timezone"):
        assert f"[string]${name}," in block  # plain optional strings: no default, not Mandatory
    assert "Mandatory" not in block
    assert "-ContractMode $ContractMode -KeyId $KeyId -Timezone $Timezone" in INSTALL_TEXT
    # The key id is never printed by the installer.
    for line in INSTALL_TEXT.splitlines():
        if "Write-Host" in line:
            assert "$KeyId" not in line


@needs_windows_powershell
def test_the_config_is_written_atomically_without_a_bom_and_never_replaces_a_file(tmp_path):
    target = tmp_path / "collector_config.json"
    existing = tmp_path / "already-there.json"
    existing.write_text("ORIGINAL", encoding="utf-8")
    script = (
        _ps_function("Write-TextFileAtomically", INSTALL_TEXT)
        # ([char]0xC5, not a literal: Windows PowerShell reads a BOM-less script file as ANSI.)
        + f"\nWrite-TextFileAtomically -Path {ps_quote(target)} -Text ('{{\"name\": \"' + [char]0xC5 + 'SA\"}}')\n"
        + f"try {{ Write-TextFileAtomically -Path {ps_quote(existing)} -Text 'REPLACEMENT'; 'NO_THROW' }} catch {{ 'THREW' }}\n"
    )

    completed = _run_ps_file(script, tmp_path)

    assert "THREW" in completed.stdout and "NO_THROW" not in completed.stdout  # an existing file is never replaced
    assert existing.read_text(encoding="utf-8") == "ORIGINAL"
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and json.loads(raw.decode("utf-8")) == {"name": "\u00c5SA"}
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".tmp") == []  # no temporary file left, either time


def test_the_config_is_written_only_through_the_atomic_writer():
    function = _ps_function("Write-TextFileAtomically", INSTALL_TEXT)
    assert function.index("WriteAllText($temporary") < function.index("[System.IO.File]::Move($temporary, $Path)")
    assert "finally" in function  # the temporary file is removed if the rename did not happen
    assert "Write-TextFileAtomically -Path $ConfigPath -Text $jsonText" in INSTALL_TEXT
    assert "WriteAllText($ConfigPath" not in INSTALL_TEXT  # never written in place


# =====================================================================================
# finish-install.ps1: pure helpers
# =====================================================================================

@needs_windows_powershell
def test_the_contract_mode_follows_the_collectors_own_rule(tmp_path):
    cases = {
        "{}": "v1", '{"contract_mode": "v1"}': "v1", '{"contract_mode": "v2"}': "v2",
        '{"contract_mode": "V2"}': "", '{"contract_mode": "v3"}': "", '{"contract_mode": 2}': "", '{"contract_mode": null}': "",
        '{"contract_mode": ""}': "", '{"contract_mode": true}': "",
    }
    lines = [_ps_function("Get-FinishContractMode", FINISH_TEXT)]
    lines += [f"'[' + (Get-FinishContractMode -Config ({ps_quote(doc)} | ConvertFrom-Json)) + ']'" for doc in cases]

    completed = _run_ps_file("\n".join(lines), tmp_path)

    assert completed.stdout.split() == [f"[{expected}]" for expected in cases.values()], completed.stderr
    # The same answers as the Collector's own reader, for every one of those documents.
    from collector.config import ConfigError
    from collector.v2_config import read_contract_mode
    for doc, expected in cases.items():
        path = tmp_path / "c.json"
        path.write_text(doc, encoding="utf-8")
        try:
            assert read_contract_mode(path) == expected
        except ConfigError:
            assert expected == ""


@needs_windows_powershell
def test_the_v2_paths_are_the_collectors_own_defaults_unless_the_config_names_them(tmp_path):
    default = {"state_path": "C:\\ProgramData\\SortViewCollector\\data\\state.json", "v2": {"key_id": KEY_ID}}
    explicit = {"state_path": "D:\\SV\\data\\state.json",
                "v2": {"state_path": "E:\\cursor\\my_state.json", "secret_path": "E:\\keys\\my_key.dpapi"}}
    lines = [_ps_function("Get-FinishV2Paths", FINISH_TEXT)]
    for doc in (default, explicit, {"state_path": "D:\\SV\\data\\state.json"}):
        lines.append(f"$p = Get-FinishV2Paths -Config ({ps_quote(json.dumps(doc))} | ConvertFrom-Json); $p.StatePath; $p.SecretPath")

    out = _run_ps_file("\n".join(lines), tmp_path).stdout.split("\n")

    assert [line.strip() for line in out if line.strip()] == [
        "C:\\ProgramData\\SortViewCollector\\data\\v2\\state_v2.json", "C:\\ProgramData\\SortViewCollector\\secrets\\v2_key.dpapi",
        "E:\\cursor\\my_state.json", "E:\\keys\\my_key.dpapi",
        "D:\\SV\\data\\v2\\state_v2.json", "D:\\SV\\secrets\\v2_key.dpapi",
    ]


@needs_windows_powershell
def test_the_secret_step_decision_table(tmp_path):
    cases = [(0, "$true", "UseExisting"), (0, "$false", "UseExisting"), (1, "$false", "Create"), (1, "$true", "Refuse"),
             (2, "$true", "Refuse"), (2, "$false", "Create")]
    script = _ps_function("Get-V2SecretStepAction", FINISH_TEXT) + "\n" + "\n".join(
        f"Get-V2SecretStepAction -CheckExitCode {code} -SecretFilePresent {present}" for code, present, _ in cases
    )

    assert _run_ps_file(script, tmp_path).stdout.split() == [expected for *_inputs, expected in cases]


# =====================================================================================
# finish-install.ps1: executed
# =====================================================================================

_FAKE_EXE_SOURCE = r"""
using System;
using System.IO;
public class FakeCollector {
    public static int Main(string[] a) {
        string dir = Environment.GetEnvironmentVariable("FAKE_DIR");
        string key = a.Length == 0 ? "none" : a[0];
        if (a.Length > 1 && !a[1].StartsWith("--")) { key = key + "_" + a[1]; }
        File.AppendAllText(Path.Combine(dir, "calls.txt"), "exe:" + key + "\r\n");
        int code = 0;
        string exits = Path.Combine(dir, "exit_" + key + ".txt");
        if (File.Exists(exits)) {
            string[] lines = File.ReadAllLines(exits);
            code = int.Parse(lines[0].Trim());
            if (lines.Length > 1) {
                string[] rest = new string[lines.Length - 1];
                Array.Copy(lines, 1, rest, 0, rest.Length);
                File.WriteAllLines(exits, rest);
            }
        }
        string action = Path.Combine(dir, "on_" + key + ".txt");
        if (code == 0 && File.Exists(action)) {
            string[] p = File.ReadAllLines(action);
            Directory.CreateDirectory(Path.GetDirectoryName(p[1]));
            File.Copy(p[0], p[1]);
        }
        Console.WriteLine("FAKE " + key + " -> exit " + code);
        string warn = Path.Combine(dir, "warn_" + key + ".txt");
        if (File.Exists(warn)) { Console.Error.WriteLine(File.ReadAllText(warn).Trim()); }
        if (code != 0) {
            string reason = Path.Combine(dir, "stderr_" + key + ".txt");
            Console.Error.WriteLine(File.Exists(reason) ? File.ReadAllText(reason).Trim() : "failed: fake_failure");
        }
        return code;
    }
}
"""

_ELEVATION_CHECK = "if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {"

_WRAPPER = r"""
$ErrorActionPreference = 'Stop'
$FakeDir = $env:FAKE_DIR
function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    $file = Join-Path $FakeDir 'task.json'
    if (-not (Test-Path -LiteralPath $file)) { return $null }
    $t = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
    return [pscustomobject]@{ State = $t.State; Actions = @([pscustomobject]@{ Execute = $t.Execute; Arguments = $t.Arguments }) }
}
& '@@FINISH@@' -InstallRoot '@@INSTALL@@' -ConfigPath '@@CONFIG@@' -UseExistingMachineToken
"FINISH_EXIT=$LASTEXITCODE"
"""


# The WPF setup's way of running a script: a runspace with no console host (SetupEngineHost's), whose only shown output
# is Write-Host's (information records tagged PSHOST).
_HOSTED = r"""
$ErrorActionPreference = 'Stop'
$state = [System.Management.Automation.Runspaces.InitialSessionState]::CreateDefault()
$state.ExecutionPolicy = [Microsoft.PowerShell.ExecutionPolicy]::Bypass
$runspace = [runspacefactory]::CreateRunspace($state)
$runspace.Open()
$ps = [powershell]::Create()
$ps.Runspace = $runspace
$null = $ps.AddScript((Get-Content -LiteralPath '@@WRAPPER@@' -Raw))
$output = $ps.Invoke()
foreach ($record in $ps.Streams.Information) { if ($record.Tags -contains 'PSHOST') { "HOST: $($record.MessageData)" } }
foreach ($item in $output) { "$item" }
foreach ($e in $ps.Streams.Error) { "ERRORSTREAM: $e" }
$runspace.Close()
"""

# Inside it, finish-install.ps1 is called as setup.ps1's Invoke-SetupTool calls it: in-process, output to Out-Host, and
# a thrown error caught and reported as a failure.
_HOSTED_WRAPPER = r"""
$ErrorActionPreference = 'Stop'
$FakeDir = $env:FAKE_DIR
function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    $file = Join-Path $FakeDir 'task.json'
    if (-not (Test-Path -LiteralPath $file)) { return $null }
    $t = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
    return [pscustomobject]@{ State = $t.State; Actions = @([pscustomobject]@{ Execute = $t.Execute; Arguments = $t.Arguments }) }
}
$global:LASTEXITCODE = $null
try {
    & '@@FINISH@@' -InstallRoot '@@INSTALL@@' -ConfigPath '@@CONFIG@@' -UseExistingMachineToken | Out-Host
    "FINISH_EXIT=$LASTEXITCODE"
} catch {
    "FINISH_THREW=$($_.Exception.Message)"
}
"""


@pytest.fixture(scope="module")
def fake_exe(tmp_path_factory) -> Path:
    """A real, tiny console executable standing in for SortViewCollector.exe: it logs which subcommand it was given
    and exits with whatever the scenario scripted for that subcommand."""
    if sys.platform != "win32" or _POWERSHELL is None:
        pytest.skip("needs Windows PowerShell")
    folder = tmp_path_factory.mktemp("fake-exe")
    exe = folder / "SortViewCollector.exe"
    compiled = _run_ps_file(
        f"Add-Type -TypeDefinition {ps_quote(_FAKE_EXE_SOURCE)} -OutputAssembly {ps_quote(exe)} -OutputType ConsoleApplication", folder
    )
    if compiled.returncode != 0 or not exe.exists():
        pytest.skip("cannot compile a tiny test executable on this machine")
    return exe


def _valid_state() -> str:
    return json.dumps({"schema_version": collector_state.STATE_SCHEMA_VERSION,
                       "sources": {name: {"identity": [1, 2], "offset": 10} for name in SOURCES}})


@dataclass
class Machine:
    """One scratch machine: an install root holding the fake runtime, a data root with a config, and a fake folder."""

    root: Path
    fake: Path
    install_root: Path
    data_root: Path
    tools: Path
    exit_code: int | None = None
    output: str = ""
    threw: str | None = None

    @property
    def config_path(self) -> Path:
        return self.data_root / "config" / "collector_config.json"

    @property
    def legacy_state(self) -> Path:
        return self.data_root / "data" / "state.json"

    @property
    def v2_state(self) -> Path:
        return self.data_root / "data" / "v2" / "state_v2.json"

    @property
    def secret(self) -> Path:
        return self.data_root / "secrets" / "v2_key.dpapi"

    def exits(self, subcommand: str, *codes: int) -> None:
        """The exit codes of successive calls of one subcommand (the last one repeats)."""
        (self.fake / f"exit_{subcommand}.txt").write_text("\n".join(str(code) for code in codes) + "\n", encoding="ascii")

    def on_success(self, subcommand: str, creates: Path, content: str) -> None:
        """What a successful call of a subcommand leaves on disk (as the real Collector would)."""
        seed = self.fake / f"seed_{subcommand}.bin"
        seed.write_text(content, encoding="utf-8")
        (self.fake / f"on_{subcommand}.txt").write_text(f"{seed}\n{creates}\n", encoding="utf-8")

    def calls(self) -> list[str]:
        log = self.fake / "calls.txt"
        return log.read_text(encoding="utf-8").split() if log.exists() else []

    def task(self) -> dict | None:
        file = self.fake / "task.json"
        return json.loads(file.read_text(encoding="utf-8-sig")) if file.exists() else None

    def stderr(self, subcommand: str, line: str) -> None:
        """What a subcommand writes to stderr when it exits non-zero (the real Collector: `failed: <code>`)."""
        (self.fake / f"stderr_{subcommand}.txt").write_text(line + "\n", encoding="utf-8")

    def warns(self, subcommand: str, line: str) -> None:
        """A line a subcommand writes to stderr on every call, success included."""
        (self.fake / f"warn_{subcommand}.txt").write_text(line + "\n", encoding="utf-8")

    def run(self, *, hosted: bool = False) -> Machine:
        """Runs finish-install.ps1 in a console PowerShell -- or, hosted, inside a runspace like SetupEngineHost's."""
        script = ((_HOSTED_WRAPPER if hosted else _WRAPPER).replace("@@FINISH@@", str(self.tools / "finish-install.ps1"))
                  .replace("@@INSTALL@@", str(self.install_root)).replace("@@CONFIG@@", str(self.config_path)))
        if hosted:
            wrapper = self.fake / "hosted-wrapper.ps1"
            wrapper.write_text(script, encoding="utf-8")
            script = _HOSTED.replace("@@WRAPPER@@", str(wrapper))
        completed = _run_ps_file(script, self.fake, env={"FAKE_DIR": str(self.fake)})
        self.output = completed.stdout + "\n" + completed.stderr
        match = re.search(r"FINISH_EXIT=(\d+)", completed.stdout)
        self.exit_code = int(match.group(1)) if match else None
        threw = re.search(r"FINISH_THREW=(.*)", completed.stdout)
        self.threw = threw.group(1).strip() if threw else None
        return self


def make_machine(tmp_path: Path, fake_exe: Path, *, contract_mode: str | None = "v2", v2_extra: dict | None = None,
                 config_mutation=None, finish_text: str = FINISH_TEXT) -> Machine:
    machine = Machine(root=tmp_path, fake=tmp_path / "fake", install_root=tmp_path / "install", data_root=tmp_path / "data",
                      tools=tmp_path / "bundle" / "tools")
    for folder in (machine.fake, machine.install_root, machine.tools, machine.config_path.parent):
        folder.mkdir(parents=True)
    shutil.copy2(fake_exe, machine.install_root / "SortViewCollector.exe")

    assert finish_text.count(_ELEVATION_CHECK) == 1
    (machine.tools / "finish-install.ps1").write_text(finish_text.replace(_ELEVATION_CHECK, "if ($false) {"), encoding="utf-8")
    log = "Add-Content -LiteralPath (Join-Path $env:FAKE_DIR 'calls.txt') -Value "
    (machine.tools / "set-api-token.ps1").write_text(f"{log}'tool:set-api-token'\n", encoding="utf-8")
    (machine.tools / "preflight-system.ps1").write_text(
        "param([string]$InstallRoot, [string]$ConfigPath)\n" + log + "'tool:preflight-system'\n"
        "$code = 0; $f = Join-Path $env:FAKE_DIR 'exit_preflight-system.txt'; if (Test-Path $f) { $code = [int](Get-Content $f -Raw) }\n"
        "exit $code\n", encoding="utf-8")
    (machine.tools / "register-task.ps1").write_text(
        "param([string]$InstallRoot, [string]$ConfigPath)\n" + log + "'tool:register-task'\n"
        "@{ State = 'Disabled'; Execute = (Join-Path $InstallRoot 'SortViewCollector.exe'); Arguments = ('run --config \"' + $ConfigPath + '\"') }"
        " | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $env:FAKE_DIR 'task.json')\n", encoding="utf-8")

    config: dict = {
        "customer_id": 7, "branch_id": 3, "installation_id": 41, "api_url": "https://api.example.org",
        "sources": [{"name": name, "path": f"C:\\TL\\{name}.txt"} for name in SOURCES],
        "state_path": str(machine.legacy_state), "status_path": str(machine.data_root / "data" / "status.json"),
        "log_path": str(machine.data_root / "logs" / "collector.log"),
    }
    if contract_mode is not None:
        config["contract_mode"] = contract_mode
    if contract_mode == "v2":
        config["v2"] = {"key_id": KEY_ID, "timezone": TIMEZONE, **(v2_extra or {})}
    if config_mutation:
        config_mutation(config)
    machine.config_path.write_text(json.dumps(config), encoding="utf-8")
    return machine


def fresh_v2_machine(tmp_path: Path, fake_exe: Path, **kwargs) -> Machine:
    """A v2 install that has nothing yet: `v2-key check` fails until `v2-key init` has created the secret, and
    `bootstrap` writes a valid v2 cursor."""
    machine = make_machine(tmp_path, fake_exe, **kwargs)
    machine.exits("v2-key_check", 1, 0)
    machine.stderr("v2-key_check", "failed: secret_missing")  # exactly what the real Collector prints when there is none
    machine.on_success("v2-key_init", machine.secret, "dpapi-blob")
    machine.on_success("bootstrap", machine.v2_state, _valid_state())
    return machine


V2_FRESH_CALLS = [
    "exe:api-token_check", "exe:api-token_check",                    # step 1: is a token stored; then the verification
    "exe:v2-key_check", "exe:v2-key_init", "exe:v2-key_check",       # step 1b: no secret -> create -> verify
    "exe:preflight", "tool:preflight-system",                        # steps 2 and 3
    "exe:bootstrap",                                                 # step 4
    "tool:register-task",                                            # step 5
]


@needs_windows_powershell
def test_a_fresh_v2_finish_creates_the_secret_runs_both_preflights_bootstraps_and_registers_disabled(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe).run()

    assert machine.exit_code == 0, machine.output
    assert machine.calls() == V2_FRESH_CALLS  # the order is the gate: the task is registered only after everything else
    assert machine.task() is not None and machine.task()["State"] == "Disabled"  # registered, never enabled, by this script
    assert machine.secret.exists() and machine.v2_state.exists()
    assert not machine.legacy_state.exists()  # a v2 install never gets a legacy state.json
    assert "Contract v2 secret created on this machine" in machine.output and "never sent, never shown" in machine.output
    assert KEY_ID not in machine.output


@needs_windows_powershell
def test_a_valid_existing_secret_is_reused_and_never_recreated(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.secret.parent.mkdir(parents=True)
    machine.secret.write_text("the-saved-installations-secret", encoding="utf-8")
    machine.exits("v2-key_check", 0)

    machine.run()

    assert machine.exit_code == 0, machine.output
    assert "exe:v2-key_init" not in machine.calls() and machine.calls().count("exe:v2-key_check") == 1
    assert machine.secret.read_text(encoding="utf-8") == "the-saved-installations-secret"
    assert "Contract v2 secret already present" in machine.output


@needs_windows_powershell
@pytest.mark.parametrize("check_exit", [1, 2])
def test_an_existing_secret_the_collector_cannot_use_stops_and_is_never_overwritten(tmp_path, fake_exe, check_exit):
    """Damaged, unverifiable, or bound to ANOTHER key id (a different installation): all the same answer."""
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.secret.parent.mkdir(parents=True)
    machine.secret.write_text("another-installations-secret", encoding="utf-8")
    machine.exits("v2-key_check", check_exit)

    machine.run()

    assert machine.exit_code == 2 and "SETUP STOPPED at step 1b (Contract v2 secret)" in machine.output
    assert machine.secret.read_text(encoding="utf-8") == "another-installations-secret"  # untouched
    assert machine.calls() == ["exe:api-token_check", "exe:api-token_check", "exe:v2-key_check"]  # no init, no preflight, no task
    assert machine.task() is None and not machine.v2_state.exists()
    assert "never overwritten" in machine.output and "Contact SortView support" in machine.output


@needs_windows_powershell
def test_a_failed_secret_creation_stops_before_any_preflight(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.exits("v2-key_init", 1)

    machine.run()

    assert machine.exit_code == 1 and "could not create the Contract v2 secret" in machine.output
    assert machine.calls() == ["exe:api-token_check", "exe:api-token_check", "exe:v2-key_check", "exe:v2-key_init"]
    assert machine.task() is None and not machine.secret.exists()


@needs_windows_powershell
def test_a_created_secret_that_does_not_verify_stops_before_any_preflight(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.exits("v2-key_check", 1, 1)

    machine.run()

    assert machine.exit_code == 1 and "could not verify it" in machine.output
    assert machine.calls()[-1] == "exe:v2-key_check" and "exe:preflight" not in machine.calls()
    assert machine.task() is None


@needs_windows_powershell
@pytest.mark.parametrize(("failing", "step"), [("preflight", "step 2 of 5"), ("preflight-system", "step 3 of 5")])
def test_either_preflight_failing_stops_before_bootstrap_and_before_any_task(tmp_path, fake_exe, failing, step):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.exits(failing, 1)

    machine.run()

    assert machine.exit_code == 1 and f"SETUP STOPPED at {step}" in machine.output
    assert "exe:bootstrap" not in machine.calls() and "tool:register-task" not in machine.calls()
    assert machine.task() is None and not machine.v2_state.exists()


@needs_windows_powershell
def test_the_interactive_preflight_must_pass_before_the_system_preflight_runs(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.exits("preflight", 1)

    assert "tool:preflight-system" not in machine.run().calls()


@needs_windows_powershell
def test_bootstrap_exit_3_is_a_refusal_never_a_success(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.exits("bootstrap", 3)

    machine.run()

    assert machine.exit_code == 2 and "Bootstrap REFUSED to start a fresh Contract v2 cursor (exit code 3)" in machine.output
    assert "tool:register-task" not in machine.calls() and machine.task() is None
    assert not machine.v2_state.exists()


@needs_windows_powershell
def test_a_legacy_state_file_is_not_mistaken_for_the_v2_cursor(tmp_path, fake_exe):
    """A machine that has run v1: the legacy state.json is there, the v2 one is not. The script neither skips bootstrap
    because of it nor verifies it afterwards -- it asks the Collector, which refuses (exit 3)."""
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.legacy_state.parent.mkdir(parents=True)
    machine.legacy_state.write_text(_valid_state(), encoding="utf-8")
    machine.exits("bootstrap", 3)

    machine.run()

    assert "exe:bootstrap" in machine.calls()  # NOT skipped: a valid legacy cursor is not a v2 cursor
    assert machine.exit_code == 2 and machine.task() is None
    assert machine.legacy_state.read_text(encoding="utf-8") == _valid_state()  # untouched


@needs_windows_powershell
def test_bootstrap_that_exits_0_without_a_v2_state_file_is_a_failure(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    (machine.fake / "on_bootstrap.txt").unlink()
    # An older runtime would seed the LEGACY file and report success: the v2 file is what must exist afterwards.
    machine.on_success("bootstrap", machine.legacy_state, _valid_state())

    machine.run()

    assert machine.exit_code == 1 and "Bootstrap exited 0 but the state file is not valid" in machine.output
    assert "tool:register-task" not in machine.calls() and machine.task() is None


@needs_windows_powershell
def test_a_bootstrap_that_seeds_only_some_sources_is_a_failure(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    (machine.fake / "on_bootstrap.txt").unlink()
    partial = json.dumps({"schema_version": 1, "sources": {"acs": {"identity": [1, 2], "offset": 10}}})
    machine.on_success("bootstrap", machine.v2_state, partial)

    machine.run()

    assert machine.exit_code == 1 and "tool:register-task" not in machine.calls()
    assert "has no entry" in machine.output


@needs_windows_powershell
def test_on_a_rerun_a_valid_v2_state_skips_bootstrap_and_is_not_overwritten(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.secret.parent.mkdir(parents=True)
    machine.secret.write_text("secret", encoding="utf-8")
    machine.exits("v2-key_check", 0)
    machine.v2_state.parent.mkdir(parents=True)
    machine.v2_state.write_text(_valid_state(), encoding="utf-8")
    machine.exits("bootstrap", 3)  # what the Collector WOULD answer if it were asked -- it must not be

    machine.run()

    assert machine.exit_code == 0, machine.output
    assert "exe:bootstrap" not in machine.calls()
    assert machine.v2_state.read_text(encoding="utf-8") == _valid_state()
    assert "bootstrap skipped" in machine.output and machine.task()["State"] == "Disabled"


@needs_windows_powershell
def test_an_invalid_existing_v2_state_is_refused_and_never_overwritten(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.v2_state.parent.mkdir(parents=True)
    machine.v2_state.write_text("{ not a state file", encoding="utf-8")

    machine.run()

    assert machine.exit_code == 2 and "exe:bootstrap" not in machine.calls() and machine.task() is None
    assert machine.v2_state.read_text(encoding="utf-8") == "{ not a state file"


@needs_windows_powershell
def test_an_explicit_v2_state_path_in_the_config_is_the_one_verified(tmp_path, fake_exe):
    elsewhere = tmp_path / "elsewhere" / "cursor.json"
    machine = make_machine(tmp_path, fake_exe, v2_extra={"state_path": str(elsewhere)})
    machine.exits("v2-key_check", 1, 0)
    machine.on_success("v2-key_init", machine.secret, "dpapi-blob")
    machine.on_success("bootstrap", elsewhere, _valid_state())

    machine.run()

    assert machine.exit_code == 0, machine.output
    assert elsewhere.exists() and not machine.v2_state.exists()


@needs_windows_powershell
@pytest.mark.parametrize("contract_mode", [None, "v1"])
def test_a_legacy_install_takes_exactly_its_old_steps_with_no_secret_and_the_legacy_cursor(tmp_path, fake_exe, contract_mode):
    machine = make_machine(tmp_path, fake_exe, contract_mode=contract_mode)
    machine.on_success("bootstrap", machine.legacy_state, _valid_state())

    machine.run()

    assert machine.exit_code == 0, machine.output
    assert machine.calls() == ["exe:api-token_check", "exe:api-token_check", "exe:preflight", "tool:preflight-system",
                               "exe:bootstrap", "tool:register-task"]  # no v2-key step at all
    assert machine.legacy_state.exists() and not machine.v2_state.exists() and not machine.secret.exists()
    assert "Contract v2" not in machine.output


@needs_windows_powershell
def test_an_unsupported_contract_mode_stops_at_step_0_before_anything_runs(tmp_path, fake_exe):
    machine = make_machine(tmp_path, fake_exe, contract_mode="v3").run()

    assert machine.exit_code == 1 and "SETUP STOPPED at step 0 (checks)" in machine.output and "contract_mode" in machine.output
    assert machine.calls() == [] and machine.task() is None


# --- the WPF setup's hosted runspace: the Collector's stderr (M2d-2b) ---------------------------------------------------

def _committed_finish_text_before_the_fix() -> str:
    """finish-collector-install.ps1 as it was before Invoke-CollectorCommand: its Collector calls made directly."""
    import subprocess as _subprocess

    for revision in ("8cc14f8", "HEAD"):
        text = _subprocess.run(["git", "show", f"{revision}:collector/deploy/finish-collector-install.ps1"], cwd=REPO_ROOT,
                               capture_output=True, check=False).stdout.decode("utf-8")
        if "& $ExePath v2-key check --config $ConfigPath | Out-Host" in text:
            return text
    pytest.skip("the pre-fix finish-collector-install.ps1 is not in this checkout's history")


@needs_windows_powershell
def test_hosted_the_pre_fix_script_stops_on_the_expected_secret_missing_line(tmp_path, fake_exe):
    """The defect itself: in a runspace like SetupEngineHost's, `v2-key check`'s expected stderr line ended the script
    before its own exit-code handling could create the secret."""
    machine = fresh_v2_machine(tmp_path, fake_exe, finish_text=_committed_finish_text_before_the_fix()).run(hosted=True)

    assert machine.threw == "failed: secret_missing" and machine.exit_code is None, machine.output
    assert machine.calls() == ["exe:api-token_check", "exe:api-token_check", "exe:v2-key_check"]  # no init, nothing after
    assert not machine.secret.exists() and machine.task() is None


@needs_windows_powershell
def test_hosted_a_fresh_v2_finish_proceeds_through_check_init_check_and_completes(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe).run(hosted=True)

    assert machine.threw is None and machine.exit_code == 0, machine.output
    assert machine.calls() == V2_FRESH_CALLS
    assert machine.secret.exists() and machine.v2_state.exists() and machine.task()["State"] == "Disabled"
    # The Collector's line is SHOWN (on the channel the WPF setup displays), followed by the script's own explanation.
    assert "HOST: failed: secret_missing" in machine.output
    assert "HOST: No Contract v2 secret yet (the line above is expected on a first install)" in machine.output
    assert "ERRORSTREAM" not in machine.output and "RemoteException" not in machine.output


@needs_windows_powershell
@pytest.mark.parametrize(("subcommand", "reason", "message", "calls_end"), [
    ("v2-key_init", "failed: secret_not_writable", "could not create the Contract v2 secret", "exe:v2-key_init"),
    ("preflight", "failed: fake_preflight", "SETUP STOPPED at step 2 of 5", "exe:preflight"),
])
def test_hosted_an_unexpected_non_zero_exit_still_stops_safely_with_its_reason(tmp_path, fake_exe, subcommand, reason, message, calls_end):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.exits(subcommand, 1)
    machine.stderr(subcommand, reason)

    machine.run(hosted=True)

    assert machine.threw is None and machine.exit_code == 1, machine.output  # the script's own stop, not a thrown error
    assert message in machine.output and f"HOST: {reason}" in machine.output
    assert machine.calls()[-1] == calls_end and "tool:register-task" not in machine.calls() and machine.task() is None


@needs_windows_powershell
def test_hosted_bootstrap_exit_3_is_still_the_scripts_own_refusal(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.exits("bootstrap", 3)
    machine.stderr("bootstrap", "Refusing to bootstrap (reason=v2_state_exists)")

    machine.run(hosted=True)

    assert machine.threw is None and machine.exit_code == 2, machine.output
    assert "Bootstrap REFUSED to start a fresh Contract v2 cursor (exit code 3)" in machine.output
    assert machine.task() is None


@needs_windows_powershell
def test_hosted_a_successful_command_that_writes_to_stderr_does_not_stop_setup(tmp_path, fake_exe):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    for subcommand in ("api-token_check", "preflight", "bootstrap"):
        machine.warns(subcommand, f"warning: {subcommand} wrote to stderr and succeeded")

    machine.run(hosted=True)

    assert machine.threw is None and machine.exit_code == 0, machine.output
    assert machine.calls() == V2_FRESH_CALLS and machine.task()["State"] == "Disabled"
    assert "HOST: warning: preflight wrote to stderr and succeeded" in machine.output


@needs_windows_powershell
@pytest.mark.parametrize("hosted", [False, True])
def test_the_collectors_stderr_is_reported_and_nothing_secret_is(tmp_path, fake_exe, hosted):
    machine = fresh_v2_machine(tmp_path, fake_exe)
    machine.run(hosted=hosted)

    assert machine.exit_code == 0, machine.output
    assert "failed: secret_missing" in machine.output                    # the reason, as the Collector gave it
    assert "dpapi-blob" not in machine.output                            # the secret's contents never
    assert KEY_ID not in machine.output                                  # nor the key id
    assert "NativeCommandError" not in machine.output and "RemoteException" not in machine.output


def test_every_collector_call_in_the_finish_script_goes_through_the_one_helper():
    body = FINISH_TEXT[FINISH_TEXT.index("#>") + 2:]
    code = "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))
    helper = code[code.index("function Invoke-CollectorCommand {"):]
    helper = helper[:helper.index("\n}\n") + 3]
    assert '$ErrorActionPreference = "Continue"' in helper and "& $ExePath @args 2>&1" in helper and "Write-Host" in helper
    assert code.replace(helper, "").count("& $ExePath") == 0  # no direct call anywhere else
    assert code.count("Invoke-CollectorCommand ") == 7 + 1    # the seven calls, and the definition


def test_the_finish_script_never_enables_starts_or_forces_anything_for_v2():
    body = FINISH_TEXT[FINISH_TEXT.index("#>") + 2:]
    code = "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))
    for forbidden in ("Enable-ScheduledTask -", "Start-ScheduledTask -", "-Enabled", "v2-key init --force", "Remove-Item"):
        assert forbidden not in code.replace('Write-Host \'    Enable-ScheduledTask -TaskName "SortView Collector"\'', "").replace(
            'Write-Host \'    Start-ScheduledTask -TaskName "SortView Collector"\'', ""), forbidden
    # The secret step sits between the token and the first preflight, and bootstrap after both preflights.
    order = [code.index(marker) for marker in ("Invoke-CollectorCommand v2-key check --config $ConfigPath",
                                               "Invoke-CollectorCommand preflight --config $ConfigPath", "& $PreflightSystemScript",
                                               "Invoke-CollectorCommand bootstrap --config $ConfigPath", "& $RegisterTaskScript")]
    assert order == sorted(order)
    assert code.count("Invoke-CollectorCommand v2-key init --config $ConfigPath") == 1  # one place creates a secret, only after `Create`
    assert "$V2SecretPath" in code and "Out-File" not in code and "Set-Content" not in code  # the script itself writes nothing
