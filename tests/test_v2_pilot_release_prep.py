"""Tests for the Contract v2 NBPL pilot preparation (introduced in 1.0.6; the bounded
identity-collision acceptance gate, collector/identity_collision_diag.py's Step 6b, was added on
top of it later): the release-packaged, build-time-generated classification rules artifact
(collector/build_release.py) and the release-provided target-machine tool
(collector/deploy/prepare_v2_pilot.ps1, shipped as tools/prepare_v2_pilot.ps1). Every version
check here compares against `collector.__version__` (RELEASE_VERSION below), never a literal,
so this file needs no edit on a version bump.

Layers, matching this codebase's established split for .ps1 tooling
(tests/test_collector_setup.py):

  1. Real, executable proof of the BUILD-TIME artifact -- collector.build_release run
     against the real repo, then inspected.
  2. STATIC checks of prepare_v2_pilot.ps1 (run everywhere): it never sets
     contract_mode, never touches a key/secret, never enables the Scheduled Task
     (no such call exists anywhere in the file, so it cannot fire under any input).
  3. PURE-FUNCTION tests: real PowerShell running the acceptance-gate helpers
     (ConvertTo-DryRunCounters, Test-DryRunAcceptance) directly.
  4. ORCHESTRATION scenarios: real PowerShell dot-sources the REAL prepare_v2_pilot.ps1
     in a scratch bundle whose side-effect wrappers (elevation, the Scheduled Task
     lookup, the runtime version probe, the dry run itself) are replaced -- nothing here
     touches the real machine, Task Scheduler or a real executable.
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
from collector import build_release, v2_rules

REPO_ROOT = Path(__file__).resolve().parent.parent
PREP_SOURCE = REPO_ROOT / "collector" / "deploy" / "prepare_v2_pilot.ps1"
BRANCH_SETTINGS = REPO_ROOT / "src" / "branch_settings.json"


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


needs_windows_powershell = pytest.mark.skipif(
    sys.platform != "win32" or _powershell() is None,
    reason="the prepare_v2_pilot scenarios run real Windows PowerShell",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _executable_body(text: str) -> str:
    end = text.index("#>") + 2
    return text[end:]


def _code_only(text: str) -> str:
    """Drops every line whose trimmed content starts with '#' -- so a search for an
    actual code construct isn't confused by the same word appearing in a comment
    explaining what the code deliberately does NOT do."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


PREP_TEXT = _read(PREP_SOURCE)
PREP_BODY = _executable_body(PREP_TEXT)
PREP_CODE = _code_only(PREP_BODY)


# =====================================================================================
# 1. BUILD-TIME artifact: collector/build_release.py
# =====================================================================================


@pytest.fixture(scope="module")
def built_bundle(tmp_path_factory):
    out = tmp_path_factory.mktemp("pilot-bundle-out")
    return build_release.build_release(REPO_ROOT, out, RELEASE_VERSION, built_at="2026-01-01T00:00:00.000000Z")


def test_release_contains_the_pilot_rules_artifact(built_bundle):
    assert (built_bundle.bundle_dir / build_release.PILOT_RULES_DEST).is_file()


def test_release_contains_the_prepare_tool():
    assert ("collector/deploy/prepare_v2_pilot.ps1", "tools/prepare_v2_pilot.ps1") in build_release.DEPLOY_TOOL_FILES


def test_pilot_rules_artifact_matches_seed_from_settings_of_branch_settings(built_bundle):
    settings = json.loads(BRANCH_SETTINGS.read_text(encoding="utf-8"))
    expected = v2_rules.seed_from_settings(settings)
    actual = json.loads((built_bundle.bundle_dir / build_release.PILOT_RULES_DEST).read_text(encoding="utf-8"))
    assert actual == expected


def test_pilot_rules_artifact_matches_the_documented_nbpl_counts(built_bundle):
    # The exact counts an operator already verified onsite (`python -m collector.v2_rules
    # seed`) against src/branch_settings.json -- pinned here so a future settings edit that
    # silently changes what ships is caught immediately, not discovered onsite.
    doc = json.loads((built_bundle.bundle_dir / build_release.PILOT_RULES_DEST).read_text(encoding="utf-8"))
    assert len(doc["destinations"]) == 2
    assert len(doc["branch_services_names"]) == 3
    assert len(doc["collection_services_names"]) == 8
    assert len(doc["branch_services_da_patterns"]) + len(doc["collection_services_da_patterns"]) == 11


def test_pilot_rules_artifact_is_deterministic_given_fixed_settings(tmp_path):
    r1 = build_release.build_release(REPO_ROOT, tmp_path / "a", RELEASE_VERSION, built_at="x")
    r2 = build_release.build_release(REPO_ROOT, tmp_path / "b", RELEASE_VERSION, built_at="x")
    p1 = (r1.bundle_dir / build_release.PILOT_RULES_DEST).read_text(encoding="utf-8")
    p2 = (r2.bundle_dir / build_release.PILOT_RULES_DEST).read_text(encoding="utf-8")
    assert p1 == p2


def test_pilot_rules_artifact_carries_no_patron_or_credential_data(built_bundle):
    text = (built_bundle.bundle_dir / build_release.PILOT_RULES_DEST).read_text(encoding="utf-8")
    for forbidden in ("api_token", "SORTVIEW_API_TOKEN", "key_id", "secret", "password", "barcode"):
        assert forbidden.lower() not in text.lower()


def test_pilot_rules_artifact_is_listed_in_manifest_with_correct_hash(built_bundle):
    import hashlib

    manifest = json.loads(built_bundle.manifest_path.read_text(encoding="utf-8"))
    entry = next(e for e in manifest["files"] if e["path"] == build_release.PILOT_RULES_DEST)
    actual_bytes = (built_bundle.bundle_dir / build_release.PILOT_RULES_DEST).read_bytes()
    assert entry["sha256"] == hashlib.sha256(actual_bytes).hexdigest()
    assert entry["size_bytes"] == len(actual_bytes)


def test_build_refuses_loudly_if_branch_settings_is_missing(tmp_path):
    fake_repo = tmp_path / "fake_repo"
    shutil.copytree(REPO_ROOT / "collector", fake_repo / "collector")
    shutil.copytree(REPO_ROOT / "agent", fake_repo / "agent")
    shutil.copytree(REPO_ROOT / "src", fake_repo / "src")
    (fake_repo / "src" / "branch_settings.json").unlink()
    output = tmp_path / "out"
    with pytest.raises(build_release.BuildError):
        build_release.build_release(fake_repo, output, RELEASE_VERSION, built_at="x")
    assert not output.exists()


def test_frozen_bundle_also_contains_the_pilot_artifact_and_tool(tmp_path):
    runtime = tmp_path / "rt" / "SortViewCollector"
    (runtime / "_internal").mkdir(parents=True)
    (runtime / "SortViewCollector.exe").write_bytes(b"fake-exe")
    result = build_release.build_frozen_release(
        REPO_ROOT, tmp_path / "out", RELEASE_VERSION, runtime,
        built_at="2026-01-01T00:00:00.000000Z", version_probe=lambda _p: RELEASE_VERSION,
    )
    assert (result.bundle_dir / build_release.PILOT_RULES_DEST).is_file()
    assert (result.bundle_dir / "tools" / "prepare_v2_pilot.ps1").is_file()


# =====================================================================================
# 2. STATIC checks on prepare_v2_pilot.ps1
# =====================================================================================


def _dry_run_document_builder() -> str:
    # The ONE place the dry-run config's actual content is assembled -- isolated from the
    # rest of the file (which legitimately mentions "contract_mode"/"key_id" in comments
    # and operator-facing status text explaining what is deliberately absent).
    start = PREP_TEXT.index("function New-DryRunConfigDocument")
    end = PREP_TEXT.index("function ConvertTo-DryRunCounters")
    return _code_only(PREP_TEXT[start:end])


def test_never_writes_contract_mode():
    assert "contract_mode" not in _dry_run_document_builder()


def test_never_touches_a_key_or_secret():
    lowered = _dry_run_document_builder().lower()
    for forbidden in ("key_id", "secret_path"):
        assert forbidden not in lowered
    assert "v2_keys" not in PREP_CODE.lower()
    assert "dpapi" not in PREP_CODE.lower()


def test_never_enables_or_touches_the_scheduled_task_beyond_reading_it():
    # No call anywhere in the file can arm the task -- Get-ScheduledTask (read-only,
    # via the Get-SortViewTaskInfo wrapper) is the ONLY Scheduled-Task cmdlet ever invoked.
    for forbidden in ("Enable-ScheduledTask", "Start-ScheduledTask", "Register-ScheduledTask", "schtasks"):
        assert forbidden not in PREP_BODY
    # Disable-ScheduledTask appears exactly once, inside operator-facing guidance TEXT
    # (telling a human what to run themselves) -- never as an actual invocation by this
    # script, which only ever reads the task (Get-SortViewTaskInfo / Get-ScheduledTask).
    assert PREP_BODY.count("Disable-ScheduledTask") == 1
    assert "Disable-ScheduledTask -TaskName $TaskName |" not in PREP_BODY  # the real-invocation idiom, absent
    assert "Disable-ScheduledTask -TaskName '$TaskName')" in PREP_BODY  # the guidance string itself
    assert "Get-ScheduledTask" in PREP_BODY


def test_never_references_the_v1_state_or_status_cursor():
    lowered = PREP_BODY.lower()
    assert "state_path" not in lowered
    assert "status_path" not in lowered


def test_never_runs_live_v2_ingestion_or_sets_the_ingest_flag():
    assert "SORTVIEW_V2_INGEST_ENABLED" not in PREP_BODY
    # Every "run --config ..." invocation of the exe -- both the real call and the
    # printed preview of it -- carries --v2-dry-run; none is a live-ingestion call.
    occurrences = [line for line in PREP_BODY.splitlines() if "run --config" in line]
    assert occurrences, "expected at least one dry-run invocation of the exe"
    assert all("--v2-dry-run" in line for line in occurrences)


def test_fixed_nbpl_timezone_default_is_america_chicago():
    assert '[string]$Timezone = "America/Chicago"' in PREP_TEXT


def test_default_paths_match_the_required_programdata_locations():
    assert 'collector_config.v2-dry-run.json' in PREP_TEXT
    assert 'classification_rules.json' in PREP_TEXT
    assert "C:\\ProgramData\\SortViewCollector\\config" in PREP_TEXT


def test_required_acceptance_counters_are_exactly_the_documented_set():
    # identical_identity_events is deliberately NOT an exact-match counter here -- it is
    # evaluated by the bounded identity-collision gate instead (Test-IdentityCollisionGatePassed
    # / collector/identity_collision_diag.py), never a flat "must be zero" requirement.
    required = re.findall(r"^\s*([a-z_]+)\s*=\s*(-?\d)\s*$", PREP_BODY, re.MULTILINE)
    names = {name for name, _value in required if name in (
        "source_acs_present", "source_checkins_present", "source_rejects_present", "throwaway_key",
        "persistent_secret_used", "identical_identity_events", "network_calls", "dry_run_complete",
    )}
    assert names == {
        "source_acs_present", "source_checkins_present", "source_rejects_present", "throwaway_key",
        "persistent_secret_used", "network_calls", "dry_run_complete",
    }
    assert "identical_identity_events" not in PREP_CODE.split("$script:RequiredExactCounters")[1].split("}")[0]


def test_source_paths_are_derived_never_hardcoded():
    assert "TLCFinalDlls" not in PREP_TEXT
    assert "function Get-ProductionDryRunSources" in PREP_BODY
    assert "$doc.sources" in PREP_BODY


def test_pass_and_fail_banners_present():
    assert "V2 PILOT DRY RUN: PASS" in PREP_TEXT
    assert "V2 PILOT DRY RUN: FAILED" in PREP_TEXT
    assert "No network calls made." in PREP_TEXT
    assert "Production v1 config unchanged." in PREP_TEXT
    assert "Scheduled Task remains disabled." in PREP_TEXT


def test_safe_to_dot_source():
    assert 'if ($MyInvocation.InvocationName -ne ".")' in PREP_BODY


def test_elevation_checked_first():
    admin_idx = PREP_BODY.index("Test-IsAdministrator")
    dry_run_idx = PREP_BODY.index("Invoke-CollectorDryRun -ExePath")
    assert admin_idx < dry_run_idx


def test_production_config_opened_read_only_and_re_verified_unchanged():
    assert "Set-Content" not in PREP_BODY
    # The ONE file this tool ever writes with WriteAllText is the new dry-run config --
    # never the production config, which is only ever read (Get-Content / Get-FileHash).
    assert PREP_BODY.count("WriteAllText") == 1
    assert "[System.IO.File]::WriteAllText($DryRunConfigPath" in PREP_BODY
    assert "SAFETY VIOLATION" in PREP_BODY
    assert "Get-FileHash -LiteralPath $ProductionConfigPath" in PREP_BODY


# =====================================================================================
# 3. PURE-FUNCTION tests (real PowerShell, no side effects)
# =====================================================================================


def _run_pure(ps_after_dot_source: str) -> str:
    script = f"$ErrorActionPreference = 'Stop'\n. '{PREP_SOURCE}'\n{ps_after_dot_source}"
    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    return completed.stdout


_ALL_CORRECT = (
    "$c = @{ source_acs_present = 1; source_checkins_present = 1; source_rejects_present = 1; "
    "throwaway_key = 1; persistent_secret_used = 0; "
    "network_calls = 0; dry_run_complete = 1 }"
)


@needs_windows_powershell
def test_acceptance_passes_when_all_required_counters_are_correct():
    out = _run_pure(f"{_ALL_CORRECT}\n$p = @(Test-DryRunAcceptance -Counters $c)\n\"COUNT=$($p.Count)\"")
    assert "COUNT=0" in out


@needs_windows_powershell
def test_identity_collision_gate_passes_only_on_an_explicit_pass_line():
    out = _run_pure(
        "\"PASS=$(Test-IdentityCollisionGatePassed -Text 'identity_collision_gate=pass')\"\n"
        "\"FAIL=$(Test-IdentityCollisionGatePassed -Text 'identity_collision_gate=fail')\"\n"
        "\"MISSING=$(Test-IdentityCollisionGatePassed -Text 'no such line here')\"\n"
        "\"EMPTY=$(Test-IdentityCollisionGatePassed -Text '')\""
    )
    assert "PASS=True" in out
    assert "FAIL=False" in out
    assert "MISSING=False" in out  # fail closed: a missing gate line is never treated as passing
    assert "EMPTY=False" in out


@needs_windows_powershell
def test_identity_collision_gate_reads_the_line_out_of_full_multiline_output():
    out = _run_pure(
        f"$t = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("
        f"'{base64.b64encode(_PASSING_COLLISION_DIAG_OUTPUT.encode()).decode()}'))\n"
        "\"PASS=$(Test-IdentityCollisionGatePassed -Text $t)\""
    )
    assert "PASS=True" in out


@needs_windows_powershell
def test_acceptance_fails_when_network_calls_nonzero():
    out = _run_pure(f"{_ALL_CORRECT}\n$c['network_calls'] = 1\n$p = @(Test-DryRunAcceptance -Counters $c)\n\"COUNT=$($p.Count)\"")
    assert "COUNT=0" not in out


@needs_windows_powershell
def test_acceptance_fails_when_persistent_secret_used_nonzero():
    out = _run_pure(f"{_ALL_CORRECT}\n$c['persistent_secret_used'] = 1\n$p = @(Test-DryRunAcceptance -Counters $c)\n\"COUNT=$($p.Count)\"")
    assert "COUNT=0" not in out


@needs_windows_powershell
def test_acceptance_fails_when_dry_run_complete_is_not_1():
    out = _run_pure(f"{_ALL_CORRECT}\n$c['dry_run_complete'] = 0\n$p = @(Test-DryRunAcceptance -Counters $c)\n\"COUNT=$($p.Count)\"")
    assert "COUNT=0" not in out


@needs_windows_powershell
def test_acceptance_fails_when_a_required_counter_is_entirely_missing():
    out = _run_pure(
        f"{_ALL_CORRECT}\n$c.Remove('throwaway_key')\n$p = @(Test-DryRunAcceptance -Counters $c)\n"
        "\"COUNT=$($p.Count)\"\n$p -join '|'"
    )
    assert "COUNT=0" not in out
    assert "missing required counter: throwaway_key" in out


@needs_windows_powershell
def test_counter_parser_ignores_non_matching_lines():
    text_b64 = base64.b64encode(
        b"source_acs_present=1\nConfiguration error: rules_missing\n\nnetwork_calls=0\n"
    ).decode("ascii")
    out = _run_pure(
        f"$t = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{text_b64}'))\n"
        "$c = ConvertTo-DryRunCounters -Text $t\n\"COUNT=$($c.Count)\" \n\"ACS=$($c['source_acs_present'])\""
    )
    assert "COUNT=2" in out
    assert "ACS=1" in out


# =====================================================================================
# 4. ORCHESTRATION scenarios
# =====================================================================================

_SCENARIO_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
. '@@PREP@@'

function Test-IsAdministrator { @@ADMIN@@ }
function Get-SortViewTaskInfo { param($TaskName) @@TASK@@ }
function Get-CollectorRuntimeVersion { param($ExePath) '@@RUNTIME_VERSION@@' }
function Invoke-CollectorDryRun {
    param($ExePath, $ConfigPath)
    $outText = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@DRY_RUN_OUTPUT_B64@@'))
    [pscustomobject]@{ ExitCode = @@DRY_RUN_EXIT@@; Output = $outText }
}
function Invoke-IdentityCollisionDiag {
    param($ExePath, $ConfigPath)
    $outText = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@COLLISION_DIAG_OUTPUT_B64@@'))
    [pscustomobject]@{ ExitCode = @@COLLISION_DIAG_EXIT@@; Output = $outText }
}

$r = @(Invoke-PrepareV2PilotMain @@ARGS@@)
"PREP_EXIT_CODE=$([int]$r[-1])"
"""

_PASSING_DRY_RUN_OUTPUT = (
    "source_acs_present=1\nsource_checkins_present=1\nsource_rejects_present=1\n"
    "throwaway_key=1\npersistent_secret_used=0\nidentical_identity_events=0\n"
    "acs_corrections=0\ndropped_patron_card=0\nnetwork_calls=0\ndry_run_complete=1\n"
)

_PASSING_COLLISION_DIAG_OUTPUT = (
    "=== acs ===\nevents_total=0\nidentical_identity_events=0\ncollision_groups=0\n"
    "=== checkins ===\nevents_total=0\nidentical_identity_events=0\ncollision_groups=0\n"
    "=== rejects ===\nevents_total=0\nidentical_identity_events=0\ncollision_groups=0\n"
    "=== gate ===\nidentity_collision_gate=pass\nkeyless_reject_collision_rate=0.0000\n"
    "max_collision_group_size=0\ncollision_time_spread=1.0000\n"
    "unexpected_item_keyed_collision_groups=0\nunexpected_keyless_collision_groups=0\n"
)


def ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


@dataclass
class Bundle:
    root: Path
    install_root: Path
    production_config_path: Path
    dry_run_config_path: Path
    rules_dest_path: Path
    version: str = RELEASE_VERSION


def make_bundle(tmp_path: Path, *, manifest_version: str | None = None, rules_present: bool = True,
                rules_valid: bool = True) -> Bundle:
    root = tmp_path / "bundle"
    (root / "tools").mkdir(parents=True)
    (root / "pilot").mkdir(parents=True)
    shutil.copy2(PREP_SOURCE, root / "tools" / "prepare_v2_pilot.ps1")
    (root / "MANIFEST.json").write_text(
        json.dumps({"product": "SortView Collector", "version": manifest_version or RELEASE_VERSION, "files": []}),
        encoding="utf-8",
    )
    if rules_present:
        content = (
            "{not valid json" if not rules_valid
            else json.dumps({
                "schema_version": 1, "destinations": [], "branch_services_names": ["JANE STAFF"],
                "collection_services_names": [], "branch_services_da_patterns": [], "collection_services_da_patterns": [],
            })
        )
        (root / "pilot" / "classification_rules.json").write_text(content, encoding="utf-8")

    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "SortViewCollector.exe").write_bytes(b"fake-exe")

    data_root = tmp_path / "data"
    (data_root / "config").mkdir(parents=True)
    source_dir = tmp_path / "TechLogic"
    source_dir.mkdir()
    production = {
        "customer_id": 7, "branch_id": 3, "api_url": "https://example.invalid",
        "sources": [
            {"name": "acs", "path": str(source_dir / "ACS Log.txt")},
            {"name": "checkins", "path": str(source_dir / "Checkins.txt")},
            {"name": "rejects", "path": str(source_dir / "Rejects.txt")},
        ],
        "state_path": str(data_root / "data" / "state.json"),
        "status_path": str(data_root / "data" / "status.json"),
        "log_path": str(data_root / "logs" / "collector.log"),
    }
    production_config_path = data_root / "config" / "collector_config.json"
    production_config_path.write_text(json.dumps(production, indent=2), encoding="utf-8")

    return Bundle(
        root=root, install_root=install_root, production_config_path=production_config_path,
        dry_run_config_path=data_root / "config" / "collector_config.v2-dry-run.json",
        rules_dest_path=data_root / "config" / "classification_rules.json",
        version=manifest_version or RELEASE_VERSION,
    )


@dataclass
class Result:
    exit_code: int | None
    output: str
    bundle: Bundle = field(repr=False, default=None)  # type: ignore[assignment]


def run_scenario(bundle: Bundle, *, admin: bool = True, task_enabled: bool | None = False, task_registered: bool = True,
                 runtime_version: str | None = None, dry_run_output: str = _PASSING_DRY_RUN_OUTPUT,
                 dry_run_exit: int = 0, collision_diag_output: str = _PASSING_COLLISION_DIAG_OUTPUT,
                 collision_diag_exit: int = 0, extra_args: str = "") -> Result:
    task_expr = "$null"
    if task_registered:
        enabled_literal = "$true" if task_enabled else "$false"
        task_expr = f"[pscustomobject]@{{ Settings = [pscustomobject]@{{ Enabled = {enabled_literal} }} }}"

    args = (
        f"-InstallRoot {ps_quote(bundle.install_root)} "
        f"-ProductionConfigPath {ps_quote(bundle.production_config_path)} "
        f"-DryRunConfigPath {ps_quote(bundle.dry_run_config_path)} "
        f"-RulesDestPath {ps_quote(bundle.rules_dest_path)} "
        f"{extra_args}"
    )
    script = (
        _SCENARIO_TEMPLATE
        .replace("@@PREP@@", str(bundle.root / "tools" / "prepare_v2_pilot.ps1"))
        .replace("@@ADMIN@@", "$true" if admin else "$false")
        .replace("@@TASK@@", task_expr)
        .replace("@@RUNTIME_VERSION@@", runtime_version or bundle.version)
        .replace("@@DRY_RUN_OUTPUT_B64@@", base64.b64encode(dry_run_output.encode("utf-8")).decode("ascii"))
        .replace("@@DRY_RUN_EXIT@@", str(dry_run_exit))
        .replace("@@COLLISION_DIAG_OUTPUT_B64@@", base64.b64encode(collision_diag_output.encode("utf-8")).decode("ascii"))
        .replace("@@COLLISION_DIAG_EXIT@@", str(collision_diag_exit))
        .replace("@@ARGS@@", args)
    )
    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=120, check=False, env={**os.environ},
    )
    output = completed.stdout + "\n" + completed.stderr
    match = re.search(r"PREP_EXIT_CODE=(\d+)", completed.stdout)
    return Result(exit_code=int(match.group(1)) if match else None, output=output, bundle=bundle)


@needs_windows_powershell
def test_happy_path_passes_and_leaves_a_pass_banner(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle)
    assert result.exit_code == 0, result.output
    assert "V2 PILOT DRY RUN: PASS" in result.output


@needs_windows_powershell
def test_refuses_when_not_administrator(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle, admin=False)
    assert result.exit_code != 0
    assert "elevated" in result.output.lower()


@needs_windows_powershell
def test_refuses_when_scheduled_task_is_enabled(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle, task_enabled=True)
    assert result.exit_code != 0
    assert "ENABLED" in result.output
    assert "V2 PILOT DRY RUN: FAILED" in result.output


@needs_windows_powershell
def test_proceeds_when_scheduled_task_is_disabled(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle, task_enabled=False)
    assert result.exit_code == 0, result.output


@needs_windows_powershell
def test_proceeds_when_scheduled_task_is_not_registered(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle, task_registered=False)
    assert result.exit_code == 0, result.output


@needs_windows_powershell
def test_refuses_when_installed_version_does_not_match_bundle_manifest(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle, runtime_version="1.0.5")
    assert result.exit_code != 0
    assert "1.0.5" in result.output


@needs_windows_powershell
def test_refuses_when_bundle_is_missing_the_rules_artifact(tmp_path):
    bundle = make_bundle(tmp_path, rules_present=False)
    result = run_scenario(bundle)
    assert result.exit_code != 0
    assert "rules artifact" in result.output.lower()


@needs_windows_powershell
def test_refuses_when_bundle_rules_artifact_is_invalid_json(tmp_path):
    bundle = make_bundle(tmp_path, rules_valid=False)
    result = run_scenario(bundle)
    assert result.exit_code != 0
    assert "not valid json" in result.output.lower()


@needs_windows_powershell
def test_production_config_remains_byte_for_byte_unchanged(tmp_path):
    bundle = make_bundle(tmp_path)
    before = bundle.production_config_path.read_bytes()
    result = run_scenario(bundle)
    assert result.exit_code == 0, result.output
    assert bundle.production_config_path.read_bytes() == before


@needs_windows_powershell
def test_dry_run_config_is_generated_with_derived_source_paths_and_no_secret_or_v2_mode(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle)
    assert result.exit_code == 0, result.output
    generated = json.loads(bundle.dry_run_config_path.read_text(encoding="utf-8"))

    production = json.loads(bundle.production_config_path.read_text(encoding="utf-8"))
    expected_paths = {s["name"]: s["path"] for s in production["sources"]}
    actual_paths = {s["name"]: s["path"] for s in generated["sources"]}
    assert actual_paths == expected_paths

    assert generated["v2"]["timezone"] == "America/Chicago"
    assert generated["v2"]["rules_path"] == str(bundle.rules_dest_path)
    assert "contract_mode" not in generated
    assert "key_id" not in generated["v2"]
    assert "secret_path" not in generated["v2"]
    assert "state_path" not in generated
    assert "status_path" not in generated


@needs_windows_powershell
def test_classification_rules_are_installed_to_the_rules_dest_path(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle)
    assert result.exit_code == 0, result.output
    installed = json.loads(bundle.rules_dest_path.read_text(encoding="utf-8"))
    packaged = json.loads((bundle.root / "pilot" / "classification_rules.json").read_text(encoding="utf-8"))
    assert installed == packaged


@needs_windows_powershell
def test_acceptance_gate_failure_reports_failed_and_exits_nonzero(tmp_path):
    bundle = make_bundle(tmp_path)
    bad_output = _PASSING_COLLISION_DIAG_OUTPUT.replace("identity_collision_gate=pass", "identity_collision_gate=fail")
    result = run_scenario(bundle, collision_diag_output=bad_output)
    assert result.exit_code != 0
    assert "V2 PILOT DRY RUN: FAILED" in result.output
    assert "identity_collision_gate" in result.output


@needs_windows_powershell
def test_identity_collision_diag_nonzero_exit_is_reported_as_failure(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle, collision_diag_exit=2, collision_diag_output="Configuration error: rules_missing")
    assert result.exit_code != 0
    assert "V2 PILOT DRY RUN: FAILED" in result.output
    assert "identity-collision-diag exited with code 2" in result.output


@needs_windows_powershell
def test_nonzero_dry_run_exit_code_is_reported_as_failure(tmp_path):
    bundle = make_bundle(tmp_path)
    result = run_scenario(bundle, dry_run_output="", dry_run_exit=2)
    assert result.exit_code != 0
    assert "V2 PILOT DRY RUN: FAILED" in result.output


# =====================================================================================
# 5. WINDOWS POWERSHELL 5.1 COMPATIBILITY -- the REAL Invoke-CollectorDryRun, unmocked
# =====================================================================================
# Every orchestration scenario above mocks Invoke-CollectorDryRun, so a 5.1-only runtime
# failure inside it (TEST-PC: `ExitCode = (if (...) {...} else {...})` -> "The term 'if'
# is not recognized...") passed the suite. These run the real function body under
# powershell.exe (Windows PowerShell, never pwsh) against a fake collector .cmd.


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
def test_prep_script_parses_cleanly_under_windows_powershell():
    script = (
        "$tokens = $null; $errors = $null\n"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile({ps_quote(PREP_SOURCE)}, [ref]$tokens, [ref]$errors)\n"
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
def test_real_invoke_collector_dry_run_executes_under_windows_powershell(tmp_path, exit_code):
    fake = _fake_collector_cmd(tmp_path, _PASSING_DRY_RUN_OUTPUT, exit_code)
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f". {ps_quote(PREP_SOURCE)}\n"
        f"$r = Invoke-CollectorDryRun -ExePath {ps_quote(fake)} -ConfigPath {ps_quote(tmp_path / 'cfg.json')}\n"
        "\"EXIT_TYPE=$($r.ExitCode.GetType().Name)\"\n"
        "\"EXIT=$($r.ExitCode)\"\n"
        "$r.Output"
    )
    completed = _run_windows_powershell(script)
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    assert "is not recognized" not in completed.stderr
    assert "EXIT_TYPE=Int32" in completed.stdout
    assert f"EXIT={exit_code}" in completed.stdout
    assert "dry_run_complete=1" in completed.stdout


@needs_powershell_exe
@pytest.mark.parametrize("exit_code", [0, 3])
def test_real_invoke_identity_collision_diag_executes_under_windows_powershell(tmp_path, exit_code):
    # Same PS-5.1 stderr-as-ErrorRecord proof as Invoke-CollectorDryRun above, for the new
    # Invoke-IdentityCollisionDiag -- both now share Invoke-CollectorExeCommand, but this proves
    # the fix actually reaches this second caller too, not just the first.
    fake = _fake_collector_cmd(tmp_path, _PASSING_COLLISION_DIAG_OUTPUT, exit_code)
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f". {ps_quote(PREP_SOURCE)}\n"
        f"$r = Invoke-IdentityCollisionDiag -ExePath {ps_quote(fake)} -ConfigPath {ps_quote(tmp_path / 'cfg.json')}\n"
        "\"EXIT_TYPE=$($r.ExitCode.GetType().Name)\"\n"
        "\"EXIT=$($r.ExitCode)\"\n"
        "$r.Output"
    )
    completed = _run_windows_powershell(script)
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    assert "is not recognized" not in completed.stderr
    assert "EXIT_TYPE=Int32" in completed.stdout
    assert f"EXIT={exit_code}" in completed.stdout
    assert "identity_collision_gate=pass" in completed.stdout


_REAL_DRY_RUN_SCENARIO = r"""
$ErrorActionPreference = 'Stop'
. '@@PREP@@'

function Test-IsAdministrator { $true }
function Get-SortViewTaskInfo { param($TaskName) [pscustomobject]@{ Settings = [pscustomobject]@{ Enabled = $false } } }
function Get-CollectorRuntimeVersion { param($ExePath) '@@RUNTIME_VERSION@@' }
# Keep the REAL Invoke-CollectorDryRun body; only swap the exe for the fake collector.
$realDryRun = ${function:Invoke-CollectorDryRun}
function Invoke-CollectorDryRun {
    param($ExePath, $ConfigPath)
    & $realDryRun -ExePath '@@FAKE_EXE@@' -ConfigPath $ConfigPath
}
# This section is specifically about Invoke-CollectorDryRun's own PS-5.1 behavior -- the
# identity-collision gate step is mocked out (always passing) so it stays out of scope here.
function Invoke-IdentityCollisionDiag {
    param($ExePath, $ConfigPath)
    [pscustomobject]@{ ExitCode = 0; Output = 'identity_collision_gate=pass' }
}

$r = @(Invoke-PrepareV2PilotMain @@ARGS@@)
"PREP_EXIT_CODE=$([int]$r[-1])"
"""


def _run_real_dry_run_scenario(tmp_path: Path, stdout_text: str, exit_code: int, stderr_text: str = "") -> Result:
    bundle = make_bundle(tmp_path)
    fake = _fake_collector_cmd(tmp_path, stdout_text, exit_code, stderr_text)
    args = (
        f"-InstallRoot {ps_quote(bundle.install_root)} "
        f"-ProductionConfigPath {ps_quote(bundle.production_config_path)} "
        f"-DryRunConfigPath {ps_quote(bundle.dry_run_config_path)} "
        f"-RulesDestPath {ps_quote(bundle.rules_dest_path)}"
    )
    script = (
        _REAL_DRY_RUN_SCENARIO
        .replace("@@PREP@@", str(bundle.root / "tools" / "prepare_v2_pilot.ps1"))
        .replace("@@RUNTIME_VERSION@@", bundle.version)
        .replace("@@FAKE_EXE@@", str(fake))
        .replace("@@ARGS@@", args)
    )
    completed = _run_windows_powershell(script)
    output = completed.stdout + "\n" + completed.stderr
    match = re.search(r"PREP_EXIT_CODE=(\d+)", completed.stdout)
    return Result(exit_code=int(match.group(1)) if match else None, output=output, bundle=bundle)


@needs_powershell_exe
def test_happy_path_with_real_dry_run_function_passes_under_windows_powershell(tmp_path):
    result = _run_real_dry_run_scenario(tmp_path, _PASSING_DRY_RUN_OUTPUT, 0)
    assert "is not recognized" not in result.output, result.output
    assert result.exit_code == 0, result.output
    assert "V2 PILOT DRY RUN: PASS" in result.output


@needs_powershell_exe
def test_nonzero_exit_with_real_dry_run_function_fails_closed_under_windows_powershell(tmp_path):
    result = _run_real_dry_run_scenario(tmp_path, "dry_run_complete=0\n", 2)
    assert "is not recognized" not in result.output, result.output
    assert result.exit_code != 0
    assert "exited with code 2" in result.output
    assert "V2 PILOT DRY RUN: FAILED" in result.output


# --- native stderr under the script-wide $ErrorActionPreference = "Stop" -----------------
# 5.1 turns merged native stderr lines into ErrorRecords; before the fix the first one
# aborted Invoke-CollectorDryRun (raw "NativeCommandError") before the exit code was read.


def _invoke_real_dry_run(tmp_path: Path, fake: Path) -> subprocess.CompletedProcess:
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f". {ps_quote(PREP_SOURCE)}\n"
        f"$r = Invoke-CollectorDryRun -ExePath {ps_quote(fake)} -ConfigPath {ps_quote(tmp_path / 'cfg.json')}\n"
        "\"EXIT_TYPE=$($r.ExitCode.GetType().Name)\"\n"
        "\"EXIT=$($r.ExitCode)\"\n"
        "\"EAP_AFTER=$ErrorActionPreference\"\n"
        "'---OUTPUT---'\n"
        "$r.Output"
    )
    return _run_windows_powershell(script)


def _dry_run_output_section(stdout: str) -> str:
    return stdout.split("---OUTPUT---", 1)[1]


@needs_powershell_exe
def test_real_dry_run_stdout_only_exit_zero(tmp_path):
    completed = _invoke_real_dry_run(tmp_path, _fake_collector_cmd(tmp_path, _PASSING_DRY_RUN_OUTPUT, 0))
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    assert "EXIT_TYPE=Int32" in completed.stdout
    assert "EXIT=0" in completed.stdout
    assert "EAP_AFTER=Stop" in completed.stdout
    assert "dry_run_complete=1" in _dry_run_output_section(completed.stdout)


@needs_powershell_exe
def test_real_dry_run_stdout_and_stderr_nonzero_exit_is_captured_not_thrown(tmp_path):
    fake = _fake_collector_cmd(tmp_path, "network_calls=0\n", 4, stderr_text="Configuration error: boom\nsecond line\n")
    completed = _invoke_real_dry_run(tmp_path, fake)
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    assert "NativeCommandError" not in completed.stderr
    assert "EXIT_TYPE=Int32" in completed.stdout
    assert "EXIT=4" in completed.stdout
    assert "EAP_AFTER=Stop" in completed.stdout
    output = _dry_run_output_section(completed.stdout)
    assert "network_calls=0" in output
    assert "Configuration error: boom" in output
    assert "second line" in output


@needs_powershell_exe
def test_real_dry_run_stderr_only_configuration_error_preserves_exit_code(tmp_path):
    fake = _fake_collector_cmd(tmp_path, "", 2, stderr_text="Configuration error: boom\n")
    completed = _invoke_real_dry_run(tmp_path, fake)
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    assert "NativeCommandError" not in completed.stderr
    assert "EXIT=2" in completed.stdout
    assert "Configuration error: boom" in _dry_run_output_section(completed.stdout)


@needs_powershell_exe
def test_real_dry_run_still_raises_non_native_powershell_errors(tmp_path):
    # Relaxing the preference for the native call must not swallow a real PowerShell error.
    completed = _invoke_real_dry_run(tmp_path, tmp_path / "does_not_exist.exe")
    assert completed.returncode != 0
    assert "EXIT=" not in completed.stdout
    assert "CommandNotFoundException" in completed.stderr


@needs_powershell_exe
def test_full_prep_fails_through_controlled_path_when_collector_writes_stderr(tmp_path):
    result = _run_real_dry_run_scenario(tmp_path, "", 2, stderr_text="Configuration error: boom\n")
    assert result.exit_code == 1, result.output
    assert "NativeCommandError" not in result.output
    assert "Configuration error: boom" in result.output
    assert "The dry run exited with code 2 -- see output above." in result.output
    assert "V2 PILOT DRY RUN: FAILED" in result.output
    assert "V2 PILOT DRY RUN: PASS" not in result.output
