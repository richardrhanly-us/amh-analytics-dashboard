"""Tests for collector/deploy/configure_v2.ps1: converting an existing PRODUCTION
collector_config.json from Contract v1 to Contract v2 in place, safely (shipped as
tools\\configure_v2.ps1, see collector/build_release.py's DEPLOY_TOOL_FILES).

Layers, matching this codebase's established split for .ps1 tooling (test_v2_pilot_release_prep.py):

  1. STATIC checks of configure_v2.ps1 (run everywhere): it never touches SORTVIEW_V2_INGEST_ENABLED,
     never enables the Scheduled Task, never creates a secret, requires elevation and a well-formed
     -KeyId before anything is read or written.
  2. PURE-FUNCTION tests: real PowerShell running Test-KeyIdFormat and New-V2ConfigDocument directly.
  3. ORCHESTRATION scenarios: real PowerShell dot-sources the REAL configure_v2.ps1 in a scratch
     bundle whose side-effect wrappers (elevation, the Scheduled Task lookup, the runtime version
     probe, support-info, the v2 dry run) are replaced -- nothing here touches the real machine,
     Task Scheduler or a real executable.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from collector import __version__ as RELEASE_VERSION
from collector.v2_events import UUID4_PATTERN

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIGURE_SOURCE = REPO_ROOT / "collector" / "deploy" / "configure_v2.ps1"

REAL_KEY_ID = "b04c3dc1-7651-4803-a593-12272dd3cfc3"  # the actual production key_id issued for NBPL


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


needs_windows_powershell = pytest.mark.skipif(
    sys.platform != "win32" or _powershell() is None,
    reason="the configure_v2 scenarios run real Windows PowerShell",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _executable_body(text: str) -> str:
    end = text.index("#>") + 2
    return text[end:]


def _code_only(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


CONFIGURE_TEXT = _read(CONFIGURE_SOURCE)
CONFIGURE_BODY = _executable_body(CONFIGURE_TEXT)
CONFIGURE_CODE = _code_only(CONFIGURE_BODY)


def ps_quote(path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


# =====================================================================================
# 1. STATIC checks (run everywhere -- no PowerShell needed)
# =====================================================================================


def test_release_contains_the_configure_v2_tool():
    from collector import build_release
    assert ("collector/deploy/configure_v2.ps1", "tools/configure_v2.ps1") in build_release.DEPLOY_TOOL_FILES


def test_never_sets_the_v2_ingest_flag():
    # The success banner explicitly REASSURES the operator the flag was not touched (a
    # deliberate, transparent statement of fact) -- what must never appear is an assignment.
    assert "$env:SORTVIEW_V2_INGEST_ENABLED" not in CONFIGURE_BODY
    assert "SORTVIEW_V2_INGEST_ENABLED =" not in CONFIGURE_BODY
    assert "SORTVIEW_V2_INGEST_ENABLED was not touched" in CONFIGURE_BODY


def test_never_enables_the_scheduled_task():
    lowered = CONFIGURE_BODY.lower()
    assert "enable-scheduledtask" not in lowered


def test_never_touches_dpapi_or_invokes_key_creation():
    # Key creation is a separate, explicit step (SortViewCollector.exe v2-key init) -- this tool
    # never touches DPAPI/ACL logic and only ever MENTIONS "v2-key" inside Write-Host guidance
    # text (advice for a later, separate step), never as an actual exe subcommand it invokes.
    assert "dpapi" not in CONFIGURE_BODY.lower()
    invocation_lines = [line for line in CONFIGURE_BODY.splitlines() if "-Arguments @(" in line]
    assert invocation_lines, "expected at least one Invoke-CollectorExeCommand call building an argument list"
    assert not any("v2-key" in line for line in invocation_lines)
    non_write_host_lines = [line for line in CONFIGURE_BODY.splitlines() if "v2-key" in line and "Write-Host" not in line]
    assert non_write_host_lines == [] or all(line.strip().startswith("#") for line in non_write_host_lines)


def test_never_runs_live_ingestion():
    # Every exe invocation this tool makes is support-info or run --v2-dry-run -- never a bare
    # `run --config ...` without --v2-dry-run (which would be a live ingestion attempt).
    run_lines = [line for line in CONFIGURE_BODY.splitlines() if '"run"' in line]
    assert run_lines, "expected at least one exe invocation building a run subcommand"
    assert all("--v2-dry-run" in line for line in run_lines)


def test_requires_elevation_before_any_read_or_write():
    admin_idx = CONFIGURE_BODY.index("Test-IsAdministrator")
    write_idx = CONFIGURE_BODY.index("WriteAllText")
    assert admin_idx < write_idx


def test_key_id_is_a_mandatory_parameter():
    assert re.search(r"\[Parameter\(Mandatory\)\]\[string\]\$KeyId", CONFIGURE_TEXT)


def test_validates_candidate_before_touching_production_config():
    # The candidate validation (support-info / v2-dry-run against the TEMP candidate path) must
    # appear in the source BEFORE the backup step, which must appear before the final
    # production-path WriteAllText/Move-Item.
    validate_idx = CONFIGURE_BODY.index("Invoke-SupportInfo")
    backup_idx = CONFIGURE_BODY.index("Copy-Item -LiteralPath $ConfigPath -Destination $backupPath")
    final_write_idx = CONFIGURE_BODY.index("Move-Item -LiteralPath $finalTmp -Destination $ConfigPath")
    assert validate_idx < backup_idx < final_write_idx


def test_backup_path_is_timestamped_and_never_overwrites_in_place():
    assert '".bak-$timestamp"' in CONFIGURE_CODE or '.bak-$timestamp' in CONFIGURE_BODY
    assert "ToUniversalTime()" in CONFIGURE_BODY  # UTC, not local time


def test_pass_and_fail_banners_present():
    assert "V2 CONFIG CONVERSION: PASS" in CONFIGURE_TEXT
    assert "V2 CONFIG CONVERSION: FAILED" in CONFIGURE_TEXT


def test_safe_to_dot_source():
    assert 'if ($MyInvocation.InvocationName -ne ".")' in CONFIGURE_BODY


def test_default_paths_match_the_required_programdata_locations():
    assert "C:\\ProgramData\\SortViewCollector\\config\\collector_config.json" in CONFIGURE_TEXT
    assert "C:\\ProgramData\\SortViewCollector\\config\\classification_rules.json" in CONFIGURE_TEXT
    assert '[string]$Timezone = "America/Chicago"' in CONFIGURE_TEXT


def test_uuid4_pattern_matches_the_python_source_exactly():
    # Test-KeyIdFormat's regex must never silently drift from collector/v2_events.py's own
    # UUID4_PATTERN -- both are asserted against the SAME set of cases below (test_key_id_format_*).
    ps_pattern = re.search(r"return \$KeyId -cmatch '(\^.*\$)'", CONFIGURE_BODY).group(1)
    assert ps_pattern == UUID4_PATTERN


# =====================================================================================
# 2. PURE-FUNCTION tests (real PowerShell, no side effects)
# =====================================================================================


def _run_pure(ps_after_dot_source: str) -> str:
    script = f"$ErrorActionPreference = 'Stop'\n. '{CONFIGURE_SOURCE}'\n{ps_after_dot_source}"
    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    return completed.stdout


@needs_windows_powershell
@pytest.mark.parametrize("key_id", [
    REAL_KEY_ID,
    "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e",
    "00000000-0000-4000-8000-000000000000",
])
def test_key_id_format_accepts_well_formed_uuid4(key_id):
    out = _run_pure(f"\"RESULT=$(Test-KeyIdFormat -KeyId '{key_id}')\"")
    assert "RESULT=True" in out


@needs_windows_powershell
@pytest.mark.parametrize("key_id", [
    "not-a-uuid",
    "B04C3DC1-7651-4803-A593-12272DD3CFC3",  # uppercase -- refused, case-sensitive match
    "b04c3dc1-7651-5803-a593-12272dd3cfc3",  # wrong version nibble (5, not 4)
    "b04c3dc1-7651-4803-c593-12272dd3cfc3",  # wrong variant nibble (c, not 8/9/a/b)
    "b04c3dc1-7651-4803-a593-12272dd3cfc",  # too short
    "",
])
def test_key_id_format_rejects_malformed_values(key_id):
    out = _run_pure(f"\"RESULT=$(Test-KeyIdFormat -KeyId '{key_id}')\"")
    assert "RESULT=False" in out


@needs_windows_powershell
def test_new_v2_config_document_preserves_every_existing_field():
    existing = {
        "customer_id": 7, "branch_id": 3, "api_url": "https://example.invalid",
        "sources": [{"name": "acs", "path": "C:\\tech\\acs.txt"}],
        "state_path": "C:\\data\\state.json", "status_path": "C:\\data\\status.json",
        "log_path": "C:\\logs\\collector.log", "installation_id": 42,
        "an_unknown_future_field": "keep-me",
    }
    existing_b64 = base64.b64encode(json.dumps(existing).encode()).decode("ascii")
    out = _run_pure(
        f"$e = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{existing_b64}')) | ConvertFrom-Json\n"
        "$d = New-V2ConfigDocument -ExistingDocument $e -KeyId 'b04c3dc1-7651-4803-a593-12272dd3cfc3' "
        "-Timezone 'America/Chicago' -RulesPath 'C:\\rules.json'\n"
        "$d | ConvertTo-Json -Depth 10"
    )
    result = json.loads(out)
    assert result["customer_id"] == 7
    assert result["branch_id"] == 3
    assert result["api_url"] == "https://example.invalid"
    assert result["sources"] == [{"name": "acs", "path": "C:\\tech\\acs.txt"}]
    assert result["state_path"] == "C:\\data\\state.json"
    assert result["status_path"] == "C:\\data\\status.json"
    assert result["log_path"] == "C:\\logs\\collector.log"
    assert result["installation_id"] == 42
    assert result["an_unknown_future_field"] == "keep-me"
    assert result["contract_mode"] == "v2"
    assert result["v2"] == {"key_id": "b04c3dc1-7651-4803-a593-12272dd3cfc3", "timezone": "America/Chicago", "rules_path": "C:\\rules.json"}


@needs_windows_powershell
def test_new_v2_config_document_does_not_mutate_its_input():
    existing = {"customer_id": 1, "branch_id": 1, "api_url": "https://x.invalid", "sources": [],
                "state_path": "s.json", "status_path": "t.json", "log_path": "l.log"}
    existing_b64 = base64.b64encode(json.dumps(existing).encode()).decode("ascii")
    out = _run_pure(
        f"$e = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{existing_b64}')) | ConvertFrom-Json\n"
        "$before = ($e | ConvertTo-Json -Depth 10)\n"
        "$d = New-V2ConfigDocument -ExistingDocument $e -KeyId 'b04c3dc1-7651-4803-a593-12272dd3cfc3' "
        "-Timezone 'America/Chicago' -RulesPath 'C:\\rules.json'\n"
        "$after = ($e | ConvertTo-Json -Depth 10)\n"
        "\"UNCHANGED=$($before -eq $after)\""
    )
    assert "UNCHANGED=True" in out


@needs_windows_powershell
def test_new_v2_config_document_overwrites_an_existing_contract_mode_and_v2_section():
    existing = {"customer_id": 1, "branch_id": 1, "api_url": "https://x.invalid", "sources": [],
                "state_path": "s.json", "status_path": "t.json", "log_path": "l.log",
                "contract_mode": "v1", "v2": {"key_id": "old-bad-value", "timezone": "UTC"}}
    existing_b64 = base64.b64encode(json.dumps(existing).encode()).decode("ascii")
    out = _run_pure(
        f"$e = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{existing_b64}')) | ConvertFrom-Json\n"
        "$d = New-V2ConfigDocument -ExistingDocument $e -KeyId 'b04c3dc1-7651-4803-a593-12272dd3cfc3' "
        "-Timezone 'America/Chicago' -RulesPath 'C:\\rules.json'\n"
        "$d | ConvertTo-Json -Depth 10"
    )
    result = json.loads(out)
    assert result["contract_mode"] == "v2"
    assert result["v2"]["key_id"] == "b04c3dc1-7651-4803-a593-12272dd3cfc3"
    assert result["v2"]["timezone"] == "America/Chicago"


# =====================================================================================
# 3. ORCHESTRATION scenarios
# =====================================================================================

_SCENARIO_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
. '@@CONFIGURE@@'

function Test-IsAdministrator { @@ADMIN@@ }
function Get-SortViewTaskInfo { param($TaskName) @@TASK@@ }
function Get-CollectorRuntimeVersion { param($ExePath) '@@RUNTIME_VERSION@@' }
function Invoke-SupportInfo {
    param($ExePath, $ConfigPath)
    $outText = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@SUPPORT_INFO_OUTPUT_B64@@'))
    [pscustomobject]@{ ExitCode = @@SUPPORT_INFO_EXIT@@; Output = $outText }
}
function Invoke-V2DryRun {
    param($ExePath, $ConfigPath)
    $outText = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@DRY_RUN_OUTPUT_B64@@'))
    [pscustomobject]@{ ExitCode = @@DRY_RUN_EXIT@@; Output = $outText }
}

$r = @(Invoke-ConfigureV2Main @@ARGS@@)
"CONFIGURE_EXIT_CODE=$([int]$r[-1])"
"""

_PASSING_SUPPORT_INFO_OUTPUT = "SortView Collector -- support info\nConfig path: x\n"
_PASSING_DRY_RUN_OUTPUT = (
    "source_acs_present=1\nsource_checkins_present=1\nsource_rejects_present=1\n"
    "throwaway_key=1\npersistent_secret_used=0\nidentical_identity_events=0\nnetwork_calls=0\ndry_run_complete=1\n"
)


@dataclass
class Bundle:
    root: Path
    install_root: Path
    config_path: Path
    rules_path: Path
    version: str = RELEASE_VERSION


def make_bundle(tmp_path: Path, *, manifest_version: str | None = None, rules_present: bool = True,
                rules_valid: bool = True, existing_extra: dict | None = None) -> Bundle:
    root = tmp_path / "bundle"
    (root / "tools").mkdir(parents=True)
    shutil.copy2(CONFIGURE_SOURCE, root / "tools" / "configure_v2.ps1")
    (root / "MANIFEST.json").write_text(
        json.dumps({"product": "SortView Collector", "version": manifest_version or RELEASE_VERSION, "files": []}),
        encoding="utf-8",
    )

    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "SortViewCollector.exe").write_bytes(b"fake-exe")

    data_root = tmp_path / "data"
    (data_root / "config").mkdir(parents=True)
    source_dir = tmp_path / "TechLogic"
    source_dir.mkdir()
    production = {
        "customer_id": 1, "branch_id": 1, "api_url": "https://example.invalid",
        "sources": [
            {"name": "acs", "path": str(source_dir / "ACS Log.txt")},
            {"name": "checkins", "path": str(source_dir / "Checkins.txt")},
            {"name": "rejects", "path": str(source_dir / "Rejects.txt")},
        ],
        "state_path": str(data_root / "data" / "state.json"),
        "status_path": str(data_root / "data" / "status.json"),
        "log_path": str(data_root / "logs" / "collector.log"),
        "installation_id": 17,
        **(existing_extra or {}),
    }
    config_path = data_root / "config" / "collector_config.json"
    config_path.write_text(json.dumps(production, indent=2), encoding="utf-8")

    rules_path = data_root / "config" / "classification_rules.json"
    if rules_present:
        content = (
            "{not valid json" if not rules_valid
            else json.dumps({"schema_version": 1, "destinations": [], "branch_services_names": [],
                             "collection_services_names": [], "branch_services_da_patterns": [],
                             "collection_services_da_patterns": []})
        )
        rules_path.write_text(content, encoding="utf-8")

    return Bundle(root=root, install_root=install_root, config_path=config_path, rules_path=rules_path,
                 version=manifest_version or RELEASE_VERSION)


@dataclass
class Result:
    exit_code: int | None
    output: str
    bundle: Bundle = field(repr=False, default=None)  # type: ignore[assignment]


def run_scenario(bundle: Bundle, *, admin: bool = True, task_enabled: bool | None = False, task_registered: bool = True,
                 runtime_version: str | None = None, key_id: str = REAL_KEY_ID,
                 support_info_output: str = _PASSING_SUPPORT_INFO_OUTPUT, support_info_exit: int = 0,
                 dry_run_output: str = _PASSING_DRY_RUN_OUTPUT, dry_run_exit: int = 0, extra_args: str = "") -> Result:
    task_expr = "$null"
    if task_registered:
        enabled_literal = "$true" if task_enabled else "$false"
        task_expr = f"[pscustomobject]@{{ Settings = [pscustomobject]@{{ Enabled = {enabled_literal} }} }}"

    args = (
        f"-InstallRoot {ps_quote(bundle.install_root)} "
        f"-ConfigPath {ps_quote(bundle.config_path)} "
        f"-KeyId {ps_quote(key_id)} "
        f"-RulesPath {ps_quote(bundle.rules_path)} "
        f"{extra_args}"
    )
    script = (
        _SCENARIO_TEMPLATE
        .replace("@@CONFIGURE@@", str(bundle.root / "tools" / "configure_v2.ps1"))
        .replace("@@ADMIN@@", "$true" if admin else "$false")
        .replace("@@TASK@@", task_expr)
        .replace("@@RUNTIME_VERSION@@", runtime_version or bundle.version)
        .replace("@@SUPPORT_INFO_OUTPUT_B64@@", base64.b64encode(support_info_output.encode("utf-8")).decode("ascii"))
        .replace("@@SUPPORT_INFO_EXIT@@", str(support_info_exit))
        .replace("@@DRY_RUN_OUTPUT_B64@@", base64.b64encode(dry_run_output.encode("utf-8")).decode("ascii"))
        .replace("@@DRY_RUN_EXIT@@", str(dry_run_exit))
        .replace("@@ARGS@@", args)
    )
    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=120, check=False, env={**os.environ},
    )
    output = completed.stdout + "\n" + completed.stderr
    match = re.search(r"CONFIGURE_EXIT_CODE=(\d+)", completed.stdout)
    return Result(exit_code=int(match.group(1)) if match else None, output=output, bundle=bundle)


def _leftover_temp_files(bundle: Bundle) -> list[Path]:
    return [p for p in bundle.config_path.parent.iterdir()
            if p.name.startswith(bundle.config_path.name) and (".tmp" in p.name or "candidate" in p.name)]


@needs_windows_powershell
def test_happy_path_passes_and_writes_contract_mode_v2(tmp_path):
    bundle = make_bundle(tmp_path)
    before = json.loads(bundle.config_path.read_text(encoding="utf-8"))
    result = run_scenario(bundle)
    assert result.exit_code == 0, result.output
    assert "V2 CONFIG CONVERSION: PASS" in result.output

    after = json.loads(bundle.config_path.read_text(encoding="utf-8"))
    assert after["contract_mode"] == "v2"
    assert after["v2"] == {"key_id": REAL_KEY_ID, "timezone": "America/Chicago", "rules_path": str(bundle.rules_path)}
    # every pre-existing field preserved exactly
    for key in ("customer_id", "branch_id", "api_url", "sources", "state_path", "status_path", "log_path", "installation_id"):
        assert after[key] == before[key], key
    assert _leftover_temp_files(bundle) == []


@needs_windows_powershell
def test_happy_path_creates_a_timestamped_backup_matching_the_original(tmp_path):
    bundle = make_bundle(tmp_path)
    original_bytes = bundle.config_path.read_bytes()
    result = run_scenario(bundle)
    assert result.exit_code == 0, result.output

    backups = list(bundle.config_path.parent.glob(bundle.config_path.name + ".bak-*"))
    assert len(backups) == 1, backups
    assert backups[0].read_bytes() == original_bytes


@needs_windows_powershell
def test_refuses_when_not_administrator(tmp_path):
    bundle = make_bundle(tmp_path)
    before = bundle.config_path.read_bytes()
    result = run_scenario(bundle, admin=False)
    assert result.exit_code != 0
    assert "elevated" in result.output.lower()
    assert bundle.config_path.read_bytes() == before
    assert list(bundle.config_path.parent.glob(bundle.config_path.name + ".bak-*")) == []


@needs_windows_powershell
@pytest.mark.parametrize("bad_key_id", ["not-a-uuid", "B04C3DC1-7651-4803-A593-12272DD3CFC3"])
def test_refuses_a_malformed_key_id_before_touching_anything(tmp_path, bad_key_id):
    # An EMPTY -KeyId is covered separately: PowerShell's own Mandatory/non-empty-string
    # parameter binding refuses it before Invoke-ConfigureV2Main's body ever runs (a different,
    # earlier mechanism than Test-KeyIdFormat) -- see test_key_id_format_rejects_malformed_values
    # for the pure-function proof that Test-KeyIdFormat itself also rejects "".
    bundle = make_bundle(tmp_path)
    before = bundle.config_path.read_bytes()
    result = run_scenario(bundle, key_id=bad_key_id)
    assert result.exit_code != 0
    assert "UUID4" in result.output or "not a lower-case" in result.output
    assert bundle.config_path.read_bytes() == before
    assert list(bundle.config_path.parent.glob(bundle.config_path.name + ".bak-*")) == []


@needs_windows_powershell
def test_refuses_when_scheduled_task_is_enabled(tmp_path):
    bundle = make_bundle(tmp_path)
    before = bundle.config_path.read_bytes()
    result = run_scenario(bundle, task_enabled=True)
    assert result.exit_code != 0
    assert "ENABLED" in result.output
    assert "V2 CONFIG CONVERSION: FAILED" in result.output
    assert bundle.config_path.read_bytes() == before


@needs_windows_powershell
def test_proceeds_when_scheduled_task_is_not_registered(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle, task_registered=False)
    assert result.exit_code == 0, result.output


@needs_windows_powershell
def test_refuses_when_installed_version_does_not_match_bundle_manifest(tmp_path):
    bundle = make_bundle(tmp_path)
    before = bundle.config_path.read_bytes()
    result = run_scenario(bundle, runtime_version="1.0.5")
    assert result.exit_code != 0
    assert "1.0.5" in result.output
    assert bundle.config_path.read_bytes() == before


@needs_windows_powershell
def test_refuses_when_production_config_is_missing(tmp_path):
    bundle = make_bundle(tmp_path)
    bundle.config_path.unlink()
    result = run_scenario(bundle)
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


@needs_windows_powershell
def test_refuses_when_production_config_is_invalid_json(tmp_path):
    bundle = make_bundle(tmp_path)
    bundle.config_path.write_text("{not valid json", encoding="utf-8")
    result = run_scenario(bundle)
    assert result.exit_code != 0
    assert "not valid json" in result.output.lower()


@needs_windows_powershell
def test_refuses_when_rules_file_is_missing(tmp_path):
    bundle = make_bundle(tmp_path, rules_present=False)
    before = bundle.config_path.read_bytes()
    result = run_scenario(bundle)
    assert result.exit_code != 0
    assert "does not exist" in result.output.lower()
    assert bundle.config_path.read_bytes() == before


@needs_windows_powershell
def test_refuses_when_rules_file_is_invalid_json(tmp_path):
    bundle = make_bundle(tmp_path, rules_valid=False)
    before = bundle.config_path.read_bytes()
    result = run_scenario(bundle)
    assert result.exit_code != 0
    assert "not valid json" in result.output.lower()
    assert bundle.config_path.read_bytes() == before


@needs_windows_powershell
def test_refuses_when_support_info_validation_fails_and_leaves_production_untouched(tmp_path):
    bundle = make_bundle(tmp_path)
    before = bundle.config_path.read_bytes()
    result = run_scenario(bundle, support_info_exit=2, support_info_output="Configuration error: boom")
    assert result.exit_code != 0
    assert "V2 CONFIG CONVERSION: FAILED" in result.output
    assert bundle.config_path.read_bytes() == before
    assert list(bundle.config_path.parent.glob(bundle.config_path.name + ".bak-*")) == []  # no backup: never reached that step
    assert _leftover_temp_files(bundle) == []  # candidate temp file cleaned up


@needs_windows_powershell
def test_refuses_when_v2_dry_run_validation_fails_and_leaves_production_untouched(tmp_path):
    bundle = make_bundle(tmp_path)
    before = bundle.config_path.read_bytes()
    result = run_scenario(bundle, dry_run_exit=2, dry_run_output="Configuration error: rules_missing")
    assert result.exit_code != 0
    assert "V2 CONFIG CONVERSION: FAILED" in result.output
    assert bundle.config_path.read_bytes() == before
    assert list(bundle.config_path.parent.glob(bundle.config_path.name + ".bak-*")) == []
    assert _leftover_temp_files(bundle) == []


@needs_windows_powershell
def test_scheduled_task_remains_disabled_throughout(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle)
    assert result.exit_code == 0, result.output
    assert "Scheduled Task remains disabled" in result.output


@needs_windows_powershell
def test_output_never_mentions_the_ingest_flag_or_live_ingestion(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle)
    assert result.exit_code == 0, result.output
    assert "SORTVIEW_V2_INGEST_ENABLED was not touched" in result.output
    assert "No live v2 ingestion has run" in result.output


@needs_windows_powershell
def test_an_unknown_extra_field_in_the_production_config_survives(tmp_path):
    bundle = make_bundle(tmp_path, existing_extra={"run_audit_path": "C:\\data\\runs.jsonl", "max_records_per_batch": 500})
    result = run_scenario(bundle)
    assert result.exit_code == 0, result.output
    after = json.loads(bundle.config_path.read_text(encoding="utf-8"))
    assert after["run_audit_path"] == "C:\\data\\runs.jsonl"
    assert after["max_records_per_batch"] == 500


# =====================================================================================
# 4. WINDOWS POWERSHELL 5.1 COMPATIBILITY -- the REAL Invoke-SupportInfo / Invoke-V2DryRun,
#    unmocked (same shared PS-5.1-safe wrapper as prepare_v2_pilot.ps1's Invoke-CollectorExeCommand)
# =====================================================================================


def _windows_powershell() -> str | None:
    return shutil.which("powershell")


needs_powershell_exe = pytest.mark.skipif(
    sys.platform != "win32" or _windows_powershell() is None,
    reason="Windows PowerShell 5.1 compatibility is only checkable with powershell.exe",
)


def _fake_collector_cmd(tmp_path: Path, stdout_text: str, exit_code: int, stderr_text: str = "") -> Path:
    lines = (
        ["@echo off"]
        + [f"echo {line}" for line in stdout_text.splitlines() if line]
        + [f"echo {line} 1>&2" for line in stderr_text.splitlines() if line]
        + [f"exit /b {exit_code}"]
    )
    fake = tmp_path / "fake_collector.cmd"
    fake.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    return fake


def _run_windows_powershell(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_windows_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=120, check=False,
    )


@needs_powershell_exe
def test_configure_v2_script_parses_cleanly_under_windows_powershell():
    script = (
        "$tokens = $null; $errors = $null\n"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile({ps_quote(CONFIGURE_SOURCE)}, [ref]$tokens, [ref]$errors)\n"
        "\"PARSE_ERRORS=$($errors.Count)\"\n"
        "$errors | ForEach-Object { $_.ToString() }\n"
        "\"PS_MAJOR=$($PSVersionTable.PSVersion.Major)\""
    )
    completed = _run_windows_powershell(script)
    assert completed.returncode == 0, completed.stderr
    assert "PS_MAJOR=5" in completed.stdout, completed.stdout
    assert "PARSE_ERRORS=0" in completed.stdout, completed.stdout


@needs_powershell_exe
@pytest.mark.parametrize("exit_code", [0, 3])
def test_real_invoke_support_info_executes_under_windows_powershell(tmp_path, exit_code):
    fake = _fake_collector_cmd(tmp_path, _PASSING_SUPPORT_INFO_OUTPUT, exit_code)
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f". {ps_quote(CONFIGURE_SOURCE)}\n"
        f"$r = Invoke-SupportInfo -ExePath {ps_quote(fake)} -ConfigPath {ps_quote(tmp_path / 'cfg.json')}\n"
        "\"EXIT_TYPE=$($r.ExitCode.GetType().Name)\"\n"
        "\"EXIT=$($r.ExitCode)\"\n"
        "$r.Output"
    )
    completed = _run_windows_powershell(script)
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    assert "is not recognized" not in completed.stderr
    assert "EXIT_TYPE=Int32" in completed.stdout
    assert f"EXIT={exit_code}" in completed.stdout


@needs_powershell_exe
@pytest.mark.parametrize("exit_code", [0, 2])
def test_real_invoke_v2_dry_run_executes_under_windows_powershell(tmp_path, exit_code):
    fake = _fake_collector_cmd(tmp_path, _PASSING_DRY_RUN_OUTPUT, exit_code, stderr_text="Configuration error: boom\n")
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f". {ps_quote(CONFIGURE_SOURCE)}\n"
        f"$r = Invoke-V2DryRun -ExePath {ps_quote(fake)} -ConfigPath {ps_quote(tmp_path / 'cfg.json')}\n"
        "\"EXIT_TYPE=$($r.ExitCode.GetType().Name)\"\n"
        "\"EXIT=$($r.ExitCode)\"\n"
        "'---OUTPUT---'\n"
        "$r.Output"
    )
    completed = _run_windows_powershell(script)
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    assert "NativeCommandError" not in completed.stderr
    assert "is not recognized" not in completed.stderr
    assert "EXIT_TYPE=Int32" in completed.stdout
    assert f"EXIT={exit_code}" in completed.stdout
    output = completed.stdout.split("---OUTPUT---", 1)[1]
    assert "dry_run_complete=1" in output
    assert "Configuration error: boom" in output
