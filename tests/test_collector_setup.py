"""Tests for the guided, enrollment-driven setup: collector/deploy/setup-collector.ps1
(shipped as setup.ps1 at the root of a frozen release bundle) and the small
additive switches it relies on in install-release.ps1, set-collector-api-token.ps1
and finish-collector-install.ps1.

Three layers, deliberately distinct:

  1. STATIC checks of the scripts (run everywhere): secret handling, no direct
     database dependency, HTTPS-only, no -Force / -Enabled, the task is enabled
     in exactly one place, one canonical source for the production URL.
  2. PURE-FUNCTION tests: real PowerShell running setup.ps1's own helpers
     (response validation, URL rules, bundle defaults, ...).
  3. ORCHESTRATION scenarios: real PowerShell dot-sources the REAL setup.ps1 in a
     scratch "bundle" whose install.ps1 / set-api-token.ps1 / finish-install.ps1
     are stubs carrying the REAL parameter blocks (so a parameter the real tool does
     not have fails binding here). Only the side-effect wrappers are replaced: the
     admin check, HTTP, prompts, the stored API token (api_token.dpapi -- modelled by
     the fake "machine_token.txt", its name kept from when it was a Machine-scope
     variable), and the Scheduled Task --
     nothing here touches the real machine, registry, Task Scheduler or network.
     Invoke-SetupTool, the ordering, the gating and the error handling all run for real.
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
from collector import build_release

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "collector" / "deploy"
SETUP_SOURCE = DEPLOY / "setup-collector.ps1"
INSTALL_SOURCE = DEPLOY / "install-release.ps1"
TOKEN_SOURCE = DEPLOY / "set-collector-api-token.ps1"
FINISH_SOURCE = DEPLOY / "finish-collector-install.ps1"
EXAMPLE_CONFIG = DEPLOY / "collector_config.example.json"

SENTINEL_TOKEN = "SentinelTokenValueQ7ZxK2mNpR9sT4vWyB6cDeFgHjL"  # 43 URL-safe chars, like a real one
SENTINEL_CODE = "SV-TEST-CODE-ABCD-EFGH"
IDS = {"customer_id": 7, "branch_id": 3, "installation_id": 41}


_POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _powershell() -> str:
    assert _POWERSHELL is not None
    return _POWERSHELL


needs_windows_powershell = pytest.mark.skipif(
    sys.platform != "win32" or _POWERSHELL is None,
    reason="the setup scenarios run real Windows PowerShell (SecureString semantics)",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _executable_body(text: str) -> str:
    """The script minus its leading comment-based help block."""
    end = text.index("#>") + 2
    return text[end:]


# =====================================================================================
# 1. STATIC checks
# =====================================================================================

SETUP_TEXT = _read(SETUP_SOURCE)
SETUP_BODY = _executable_body(SETUP_TEXT)


def _code_lines(text: str) -> list[str]:
    """Lines of code with `#` comments removed (a `#` inside a quoted string is left alone)."""
    lines = []
    for line in text.splitlines():
        stripped = re.sub(r"(^|\s)#(?!>).*$", "", line) if not re.search(r"[\"'].*#.*[\"']", line) else line
        if stripped.strip():
            lines.append(stripped)
    return lines


def test_setup_ships_from_a_source_file_named_for_the_bundle_setup_ps1():
    assert SETUP_SOURCE.is_file()
    assert ("collector/deploy/setup-collector.ps1", "setup.ps1") in build_release.FROZEN_ONLY_SUPPORT_FILES


def test_setup_writes_only_the_resume_record_and_the_servers_rules_and_only_through_two_writers():
    for forbidden in ("Set-Content", "Add-Content", "Out-File", "Export-", "[IO.File]::Write", "Start-Transcript", "Tee-Object",
                      "New-Item", "Remove-Item", "Move-Item", "Copy-Item", "Rename-Item", "WriteAllBytes", "WriteAllLines",
                      "AppendAllText"):
        assert forbidden not in SETUP_BODY, forbidden
    assert ">>" not in "\n".join(_code_lines(SETUP_BODY))
    # Two writers, and no other write anywhere. (1) Save-SetupRecovery -- whose only inputs are the data root and the
    # record JSON; (2) Write-SetupFileAtomically, used for exactly two things: the staged rules and the write probe.
    assert SETUP_BODY.count("WriteAllText") == 2
    save = _ps_function("Save-SetupRecovery")
    assert "WriteAllText" in save and "param([string]$DataRoot, [string]$Json)" in save
    atomic = _ps_function("Write-SetupFileAtomically")
    assert "WriteAllText" in atomic and atomic.index("WriteAllText") < atomic.index("[System.IO.File]::Move")
    callers = [m.start() for m in re.finditer(r"Write-SetupFileAtomically -Path", SETUP_BODY)]
    assert len(callers) == 2
    assert "Write-SetupFileAtomically -Path $path -Text (ConvertTo-ClassificationRulesJson -Rules $Rules)" in _ps_function("Save-StagedRules")
    assert 'Write-SetupFileAtomically -Path $probe -Text "probe"' in _ps_function("Initialize-SetupStaging")
    # The only other way a file comes to exist: the staged rules COPIED into the config folder, then renamed into place.
    assert SETUP_BODY.count("[System.IO.File]::Copy") == 1 and "[System.IO.File]::Copy" in _ps_function("Install-ClassificationRules")
    assert SETUP_BODY.count("[System.IO.File]::Move") == 2
    # ...and the record's JSON comes only from the typed, allow-listed builder (no free-form field can carry a secret).
    builder = _ps_function("ConvertTo-SetupRecoveryJson")
    assert ("param([int]$CustomerId, [int]$BranchId, [int]$InstallationId, [string]$ApiUrl, [string]$ReleaseVersion,\n"
            "        [int]$ContractVersion = 1, [string]$KeyId, [string]$Timezone, [string]$RulesSha256)") in builder
    builder_code = chr(10).join(_code_lines(builder)).lower()
    for secret_word in ("token", "code", "secret", "password", "rules ="):
        assert secret_word not in builder_code  # no field or parameter that could carry a secret, or a rule
    assert len(re.findall(r"\$script:RecoveryKeys = @\(", SETUP_BODY)) == 1
    assert len(re.findall(r"\$script:RecoveryKeysV2 = \$script:RecoveryKeys \+ @\(", SETUP_BODY)) == 1


def test_the_only_deletions_are_setups_own_files_and_its_empty_folder():
    # Four places delete a file, each one a file setup itself wrote: the record and the staged rules (when setup
    # completes), an unrenamed temporary file (twice), and the write probe. One place removes a folder, only if empty.
    assert SETUP_BODY.count("[System.IO.File]::Delete") == 4 and SETUP_BODY.count("[System.IO.Directory]::Delete") == 1
    remove = _ps_function("Remove-SetupRecovery")
    assert "[System.IO.File]::Delete($file)" in remove and "[System.IO.Directory]::Delete($directory)" in remove
    assert "foreach ($file in @($path, (Get-StagedRulesPath -DataRoot $DataRoot)))" in remove  # exactly those two files
    assert "[System.IO.File]::Delete($temporary)" in _ps_function("Write-SetupFileAtomically")
    assert "[System.IO.File]::Delete($temporary)" in _ps_function("Install-ClassificationRules")
    assert "[System.IO.File]::Delete($probe)" in _ps_function("Initialize-SetupStaging")
    assert "-Recurse" not in SETUP_BODY and "recurse" not in SETUP_BODY.lower().replace("never recursive", "")
    assert '.Count -eq 0' in remove  # the folder goes only if nothing else is in it
    # No rules file, secret, token, state file or config is ever deleted or overwritten by setup.
    install = _ps_function("Install-ClassificationRules")
    assert "Delete($live)" not in install and "Delete((Get-StagedRulesPath" not in install


def test_the_resume_record_folder_is_locked_down_before_the_record_is_written():
    save = _ps_function("Save-SetupRecovery")

    assert save.index("Protect-SetupRecoveryDirectory") < save.index("WriteAllText")
    protect = _ps_function("Protect-SetupRecoveryDirectory")
    assert "SetAccessRuleProtection($true, $false)" in protect  # inheritance cut, nothing carried over
    assert "S-1-5-18" in protect and "S-1-5-32-544" in protect  # SYSTEM and Administrators, by well-known SID
    assert "AreAccessRulesProtected" in protect and "throw" in protect  # verified; a weak folder is an error
    protect_code = chr(10).join(_code_lines(protect))
    for weak in ("Users", "Everyone", "S-1-1-0", "S-1-5-11", "S-1-5-32-545"):
        assert weak not in protect_code, weak


def test_setup_never_prints_a_secret_variable():
    printing = re.compile(r"Write-(Host|Output|Verbose|Debug|Warning|Information|Error)\b[^\n]*")
    for match in printing.finditer(SETUP_BODY):
        line = match.group(0)
        for secret in ("$plainCode", "$body", "$parsed.AgentToken", "$enrollment.Token", "$Code", "$code", "$EnrollmentCode",
                       "$secureToken", "$stored", "$existingToken", "$plain"):
            assert secret not in line, (secret, line)
        # (The token hash is also never printed.)
        assert "TokenHash" not in line and "$storedHash" not in line, line


def test_setup_has_exactly_one_plaintext_conversion_of_each_secret_and_never_exports_secure_strings():
    assert SETUP_BODY.count("-AsPlainText") == 1  # only to hold the freshly validated token as a SecureString
    assert not re.search(r"ConvertFrom-SecureString(?!Plain)", SETUP_BODY)  # (the cmdlet would export an encrypted blob)
    assert SETUP_BODY.count("SecureStringToBSTR") == 1  # ConvertFrom-SecureStringPlain, the one place
    assert "ZeroFreeBSTR" in SETUP_BODY


def test_the_token_reaches_the_token_tool_as_a_secure_string_parameter_never_a_command_line():
    assert "Token = $enrollment.Token; ExePath = $runtimeExe; CustomerId = $customerId; BranchId = $branchId; DataRoot = $DataRoot" in SETUP_BODY
    for forbidden in ("Start-Process", "cmd /c", "cmd.exe", "-ArgumentList", "Invoke-Expression", "iex "):
        assert forbidden not in SETUP_BODY, forbidden
    # The child tools run in-process through & (so a SecureString never becomes an argv entry).
    assert "& $Path @Parameters" in SETUP_BODY


def test_setup_never_uses_force_or_enables_the_task_when_registering():
    # -Force appears twice, neither on a tool: ConvertTo-SecureString requires it, and Get-ChildItem uses it only to
    # LIST hidden files when deciding whether the (otherwise empty) record folder may be removed.
    forced = [line for line in SETUP_BODY.splitlines() if re.search(r"(?<![A-Za-z])-Force\b", line)]
    assert len(forced) == 2
    assert any("ConvertTo-SecureString" in line for line in forced) and any("Get-ChildItem" in line for line in forced)
    for line in forced:
        assert "Invoke-SetupTool" not in line and ".ps1" not in line
    assert "-Enabled" not in SETUP_BODY
    assert "Unregister-ScheduledTask" not in SETUP_BODY
    assert "Remove-Item" not in SETUP_BODY  # existing state is never deleted


def test_the_scheduled_task_is_enabled_in_exactly_one_wrapper_called_from_one_place():
    # The cmdlet itself is invoked in one place (the wrapper); other mentions are guidance text.
    invocations = [l for l in SETUP_BODY.splitlines() if re.match(r"\s+Enable-ScheduledTask -TaskName", l)]
    assert len(invocations) == 1
    enable_wrapper = SETUP_BODY[SETUP_BODY.index("function Enable-SortViewTask"):]
    assert enable_wrapper.index("Enable-ScheduledTask") < enable_wrapper.index("function Disable-SortViewTask")
    # ...and the wrapper is called from exactly one place: the explicit-confirmation branch.
    calls = re.findall(r"(?m)^\s+Enable-SortViewTask\s*$", SETUP_BODY)
    assert len(calls) == 1
    call_site = SETUP_BODY.index(calls[0].strip() + "\n", SETUP_BODY.index("$enableNow = "))
    assert SETUP_BODY.index("if ($enableNow) {") < call_site
    assert "Start-ScheduledTask" not in "\n".join(l for l in _code_lines(SETUP_BODY) if "Write-Host" not in l)


def test_setup_never_starts_a_collector_run():
    for line in _code_lines(SETUP_BODY):
        if "Write-Host" in line:
            continue
        assert "SortViewCollector.exe run" not in line and "Start-ScheduledTask" not in line, line
    assert " run --config" not in SETUP_BODY


def test_no_direct_database_dependency_in_setup_or_its_tools():
    for path in (SETUP_SOURCE, TOKEN_SOURCE, FINISH_SOURCE, INSTALL_SOURCE):
        text = _read(path).lower()
        for needle in ("psycopg", "sqlalchemy", "database_url", "npgsql", "odbc", "oledb", "invoke-sqlcmd",
                       "system.data.sqlclient", "postgres", "5432", "sslmode"):
            assert needle not in text, (path.name, needle)


def test_setup_talks_only_https_and_never_follows_redirects():
    assert "-notmatch '^https://'" in SETUP_BODY or "-notmatch '^https://'" in SETUP_TEXT
    assert "MaximumRedirection = 0" in SETUP_BODY  # a redirect would carry the request body elsewhere
    assert "http://" not in re.sub(r"'\^https\?://'", "", SETUP_BODY.replace("https://", ""))
    assert "-AllowUnencryptedAuthentication" not in SETUP_BODY
    assert "ServerCertificateValidationCallback" not in SETUP_BODY  # never disables certificate checks
    assert "-SkipCertificateCheck" not in SETUP_BODY


def test_the_production_url_and_standard_paths_have_one_canonical_home():
    # The bundle's config template is the single recorded source...
    template = json.loads(_read(EXAMPLE_CONFIG))
    assert template["api_url"].startswith("https://")
    assert {s["name"]: s["path"] for s in template["sources"]} == {
        "checkins": r"C:\TLCFinalDlls\Checkins.txt",
        "rejects": r"C:\TLCFinalDlls\Rejects.txt",
        "acs": r"C:\TLCFinalDlls\ACS Log.txt",
    }
    # ...and no script that ships in the bundle repeats either.
    host = template["api_url"].split("//")[1].split("/")[0]
    for path in (SETUP_SOURCE, INSTALL_SOURCE, TOKEN_SOURCE, FINISH_SOURCE):
        text = _read(path)
        assert host not in text, (path.name, "repeats the production host")
        assert "TLCFinalDlls" not in _executable_body(text), (path.name, "repeats the standard path")


def test_setup_identifies_nothing_by_hostname():
    # The host name is sent as informational metadata and never used to choose anything.
    # its definition, and the two enrollment calls (the Contract v2 request and the one legacy retry) -- nowhere else
    assert SETUP_BODY.count("Get-LocalHostName") == 3
    assert SETUP_BODY.count("-HostName (Get-LocalHostName)") == 2
    for line in _code_lines(SETUP_BODY):
        if "$HostName" in line or "hostname" in line.lower():
            assert "InstallationId" not in line and "customer_id" not in line.replace("enrollment_code", ""), line


def test_setup_parameters_and_the_main_functions_parameters_match():
    script_block = SETUP_TEXT[SETUP_TEXT.index("[CmdletBinding()]\nparam("):SETUP_TEXT.index("$ErrorActionPreference")]
    function_block = SETUP_BODY[SETUP_BODY.index("function Invoke-SetupMain"):]
    function_block = function_block[function_block.index("param("):function_block.index("$script:State")]
    declared_script = set(re.findall(r"\]\$([A-Za-z]+)", script_block))
    declared_function = set(re.findall(r"\]\$([A-Za-z]+)", function_block))
    assert declared_script == declared_function == {
        "EnrollmentCode", "ApiUrl", "CheckinsPath", "RejectsPath", "AcsPath", "InstallRoot", "DataRoot", "EnableTask",
    }
    assert "ReplaceExistingToken" not in SETUP_TEXT  # a first install never replaces a stored token, by any switch


def test_the_enrollment_code_parameter_is_a_secure_string_so_plain_text_cannot_be_passed_by_accident():
    assert "[SecureString]$EnrollmentCode" in SETUP_TEXT
    assert "[string]$EnrollmentCode" not in SETUP_TEXT


def test_setup_runs_only_when_executed_and_is_safe_to_dot_source():
    assert 'if ($MyInvocation.InvocationName -ne ".")' in SETUP_BODY
    tail = SETUP_BODY[SETUP_BODY.index('if ($MyInvocation.InvocationName -ne ".")'):]
    assert "Invoke-SetupMain @PSBoundParameters" in tail and "exit ([int]$result[-1])" in tail


def test_setup_checks_elevation_before_anything_else_in_the_run():
    run = SETUP_BODY[SETUP_BODY.index("function Invoke-SetupMain"):]
    first_effect = min(run.index(needle) for needle in ("Get-RuntimeVersion", "Get-BundleDefaults", "Get-ExistingInstallProblem",
                                                        "Get-SourceSelection", "Invoke-Enrollment", "Invoke-SetupTool"))
    assert run.index("Test-IsAdministrator") < first_effect


def test_the_enrollment_code_is_requested_only_after_every_local_check_and_the_connection_test():
    run = SETUP_BODY[SETUP_BODY.index("function Invoke-SetupMain"):]
    code_prompt = run.index("Read-EnrollmentCodeSecure")
    for earlier in ("VerifyBundleOnly", "Get-RuntimeVersion", "Get-BundleDefaults", "Get-ExistingInstallProblem",
                    "Get-SourceSelection", "Test-ApiReachable"):
        assert run.index(earlier) < code_prompt, earlier
    assert run.index("Invoke-Enrollment -ApiUrl") > code_prompt


def test_each_contract_saves_and_stores_in_its_own_order_before_anything_is_installed():
    run = SETUP_BODY[SETUP_BODY.index("function Invoke-SetupMain"):]
    enroll = run.index("Invoke-Enrollment -ApiUrl")
    branch = run.index("\n          if ($enrollment.Contract -eq 2) {")
    legacy = run.index("# === STEP 6: store the token, immediately; then save what a resume needs ====")
    install = run.index("Invoke-SetupTool -Path $installScript -Parameters $installParameters")
    finish = run.index("Invoke-SetupTool -Path $finishScript")
    assert enroll < branch < legacy < install < finish

    # CONTRACT V2: the rules and the record are saved, read back and checked FIRST; only then is the token stored.
    v2 = run[branch:legacy]
    order = [v2.index(step) for step in ("Save-StagedRules -DataRoot", "Save-SetupRecovery -DataRoot", "Confirm-SetupRecovery -DataRoot",
                                         "if ($null -ne $recoveryFailure)", "Invoke-SetupTool -Path $setTokenScript",
                                         "Invoke-ApiTokenCheck")]
    assert order == sorted(order)
    assert v2.count("Invoke-SetupTool -Path $setTokenScript") == 1

    # LEGACY: exactly as before -- the token first, then the record, read back and checked.
    old = run[legacy:install]
    order = [old.index(step) for step in ("Invoke-SetupTool -Path $setTokenScript", "Save-SetupRecovery -DataRoot",
                                          "Confirm-SetupRecovery -DataRoot", "if ($null -ne $recoveryFailure)")]
    assert order == sorted(order)
    assert "Save-StagedRules" not in old and "ContractVersion" not in old  # the legacy record is the legacy record


def test_a_record_that_cannot_be_saved_or_verified_stops_setup_and_is_never_only_a_warning():
    run = SETUP_BODY[SETUP_BODY.index("function Invoke-SetupMain"):]
    gate = run[run.index("if ($null -ne $recoveryFailure)"):run.index("$script:State.RecoveryOnDisk = $true\n            Write-Host")]

    assert "Stop-Setup" in gate and "-ExitCode 1" in gate
    assert "Remove-SetupRecovery" in gate  # an unverified record is not left behind to steer a later resume
    assert "WARNING" not in run[run.index("$recoveryFailure = $null"):run.index("Invoke-SetupTool -Path $installScript -Parameters $installParameters")]
    assert "Setup continues" not in SETUP_BODY
    # RecoveryOnDisk (what the closing messages promise) becomes true only after the gate has been passed.
    assert run.index("$script:State.RecoveryOnDisk = $true\n            Write-Host") > run.index("if ($null -ne $recoveryFailure)")
    # Verification compares every value that the resume will later rely on.
    confirm = _ps_function("Confirm-SetupRecovery")
    for compared in ("ReleaseVersion", "ApiUrl", "CustomerId", "BranchId", "InstallationId"):
        assert f"$saved.{compared}" in confirm, compared
    assert "Get-SetupRecovery" in confirm  # through the same strict reader a resume uses (so no extra/secret field can pass)


def test_the_defensive_disable_only_applies_after_this_run_started_installing():
    finally_block = SETUP_BODY[SETUP_BODY.index("} finally {", SETUP_BODY.index("function Invoke-SetupMain")):]
    assert "$script:State.InstallStarted -and -not $script:State.TaskEnabledOnPurpose" in finally_block
    # InstallStarted is set only after every refusal check, immediately before install/finish -- never earlier.
    run = SETUP_BODY[SETUP_BODY.index("function Invoke-SetupMain"):]
    assert run.index("Get-ExistingInstallProblem") < run.index("InstallStarted = $true")
    assert run.index("InstallStarted = $true") < run.index("Invoke-SetupTool -Path $installScript -Parameters $installParameters")
    assert run.count("InstallStarted = $true") == 1


# --- the additive switches on the existing scripts -------------------------------------

def test_install_script_gains_only_an_optional_switch_that_hides_the_next_steps_text():
    text = _read(INSTALL_SOURCE)

    assert "[switch]$SuppressNextSteps" in text
    guard = text[text.index("if ($SuppressNextSteps)"):text.index("Write-Host \"=== Install complete. Next steps: ===\"")]
    assert "exit 0" in guard and "Write-Host" in guard
    # Every install step precedes the guard -- it changes only the closing output.
    assert text.index("=== 4. Configuration ===") < text.index("if ($SuppressNextSteps)")
    assert text.count("SuppressNextSteps") == 2  # the declaration and the one guard; nothing else reads it


def test_token_script_takes_an_optional_secure_string_and_never_prints_anything_derived_from_the_token():
    text = _read(TOKEN_SOURCE)
    code = "\n".join(l for l in _executable_body(text).splitlines() if not l.strip().startswith("#"))

    assert "[SecureString]$Token" in text
    assert '$PSBoundParameters.ContainsKey("Token")' in text
    assert 'Read-Host -AsSecureString -Prompt "Paste the SortView Collector API token (input hidden)"' in text  # default unchanged
    assert "sha256" not in code.lower() and ".length" not in code.lower().replace("$secure.length", "")  # nothing derived
    # One storage implementation -- the Collector's own `api-token set`, fed on STDIN; never the environment.
    assert "SetEnvironmentVariable" not in code
    assert code.count("api-token set @tenantArgs") == 1 and code.count("-m collector.api_token_store set @tenantArgs") == 1
    # Guided setup's explicit tenant: all three together, or none.
    assert "[int]$CustomerId" in text and "[int]$BranchId" in text and "[string]$DataRoot" in text and "[string]$ExePath" in text
    assert "-CustomerId, -BranchId and -DataRoot must be given together." in text


def test_finish_script_gains_only_the_use_existing_token_switch():
    text = _read(FINISH_SOURCE)

    assert "[switch]$UseExistingMachineToken" in text
    assert "function Get-TokenStepAction" in text
    step = text[text.index("$tokenAction = Get-TokenStepAction"):text.index("if ($runTokenTool) {")]
    assert "UseExisting" in step and "MissingExisting" in step and "AskKeepOrReplace" in step
    # The original interactive branch is intact.
    assert "Keep the existing token, or replace it?" in step
    # The new branch neither prompts nor runs the token tool: it only uses what setup.ps1 stored.
    branch = step[step.index('if ($tokenAction -eq "UseExisting") {'):step.index('} elseif ($tokenAction -eq "AskKeepOrReplace")')]
    assert "Read-Host" not in branch and "$runTokenTool = $false" in branch and "SetTokenScript" not in branch
    # ...and it stops (never prompts) when the token it was promised is not there.
    missing = step[step.index('if ($tokenAction -eq "MissingExisting") {'):step.index('if ($tokenAction -eq "UseExisting")')]
    assert "Stop-Setup" in missing and "Read-Host" not in missing


# --- the release bundle ------------------------------------------------------------------------

def test_frozen_bundle_contains_setup_ps1_at_its_root_and_the_source_bundle_does_not(tmp_path):
    fake_runtime = tmp_path / "rt" / "SortViewCollector"
    (fake_runtime / "_internal").mkdir(parents=True)
    (fake_runtime / "SortViewCollector.exe").write_bytes(b"fake")
    (fake_runtime / "_internal" / "x.dll").write_bytes(b"fake")

    frozen = build_release.build_frozen_release(
        REPO_ROOT, tmp_path / "frozen", RELEASE_VERSION, fake_runtime, built_at="x", version_probe=lambda _p: RELEASE_VERSION
    )
    source = build_release.build_release(REPO_ROOT, tmp_path / "source", RELEASE_VERSION, built_at="x")

    shipped = frozen.bundle_dir / "setup.ps1"
    assert shipped.read_bytes() == SETUP_SOURCE.read_bytes()
    manifest = json.loads(frozen.manifest_path.read_text(encoding="utf-8"))
    assert "setup.ps1" in {entry["path"] for entry in manifest["files"]}  # integrity-verified by install.ps1
    assert not (source.bundle_dir / "setup.ps1").exists()
    for tool in ("set-api-token.ps1", "finish-install.ps1", "preflight-system.ps1", "register-task.ps1"):
        assert (frozen.bundle_dir / "tools" / tool).is_file(), tool  # everything setup.ps1 calls ships beside it


# =====================================================================================
# helpers for the PowerShell layers
# =====================================================================================

def ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_ps(script: str, *, env: dict | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    if len(encoded) > 24000:
        raise AssertionError("script too long for -EncodedCommand; use a file")
    return subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=timeout, check=False, env={**os.environ, **(env or {})},
    )


def _ps_function(name: str, text: str = SETUP_TEXT) -> str:
    match = re.search(rf"^function {re.escape(name)} \{{.*?^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"function {name} not found"
    return match.group(0)


def _param_block(script: Path) -> str:
    text = _read(script)
    match = re.search(r"^\[CmdletBinding\(\)\]\s*\nparam\(.*?^\)", text, re.MULTILINE | re.DOTALL)
    assert match, f"param block not found in {script.name}"
    return match.group(0)


# =====================================================================================
# 2. PURE-FUNCTION tests (real PowerShell, setup.ps1's own helpers)
# =====================================================================================

def _dot_source_and_run(body: str) -> subprocess.CompletedProcess:
    return _run_ps(f". {ps_quote(SETUP_SOURCE)}\n{body}")


@needs_windows_powershell
def test_response_validation_accepts_exactly_the_four_field_object():
    body = json.dumps({**IDS, "agent_token": SENTINEL_TOKEN})
    result = _dot_source_and_run(
        f"$r = ConvertTo-EnrollmentResult -Content {ps_quote(body)}\n"
        "\"$($r.Valid)|$($r.CustomerId)|$($r.BranchId)|$($r.InstallationId)|$($r.AgentToken.Length)\""
    )

    assert result.stdout.strip() == f"True|7|3|41|{len(SENTINEL_TOKEN)}", result.stderr


_MALFORMED_BODIES = {
    "empty": "",
    "not_json": "not json at all",
    "array": json.dumps([IDS]),
    "string": json.dumps("hello"),
    "missing_token": json.dumps(IDS),
    "missing_customer": json.dumps({"branch_id": 3, "installation_id": 41, "agent_token": SENTINEL_TOKEN}),
    "missing_installation": json.dumps({"customer_id": 7, "branch_id": 3, "agent_token": SENTINEL_TOKEN}),
    "extra_field": json.dumps({**IDS, "agent_token": SENTINEL_TOKEN, "organization_id": 1}),
    "renamed_field": json.dumps({"Customer_ID": 7, "branch_id": 3, "installation_id": 41, "agent_token": SENTINEL_TOKEN}),
    "string_id": json.dumps({**IDS, "customer_id": "7", "agent_token": SENTINEL_TOKEN}),
    "float_id": json.dumps({**IDS, "branch_id": 3.5, "agent_token": SENTINEL_TOKEN}),
    "bool_id": json.dumps({**IDS, "installation_id": True, "agent_token": SENTINEL_TOKEN}),
    "null_id": json.dumps({**IDS, "customer_id": None, "agent_token": SENTINEL_TOKEN}),
    "zero_id": json.dumps({**IDS, "customer_id": 0, "agent_token": SENTINEL_TOKEN}),
    "negative_id": json.dumps({**IDS, "branch_id": -1, "agent_token": SENTINEL_TOKEN}),
    "id_too_big_for_installer": json.dumps({**IDS, "installation_id": 2**31, "agent_token": SENTINEL_TOKEN}),
    "token_not_a_string": json.dumps({**IDS, "agent_token": 12345678901234567890}),
    "token_null": json.dumps({**IDS, "agent_token": None}),
    "token_too_short": json.dumps({**IDS, "agent_token": "short"}),
    "token_with_spaces": json.dumps({**IDS, "agent_token": "has space " + "x" * 30}),
    "token_with_quote": json.dumps({**IDS, "agent_token": "bad'token" + "x" * 30}),
    "token_empty": json.dumps({**IDS, "agent_token": ""}),
}


@needs_windows_powershell
@pytest.mark.parametrize("case", list(_MALFORMED_BODIES))
def test_response_validation_rejects_every_malformed_body(case):
    body = _MALFORMED_BODIES[case]
    result = _dot_source_and_run(
        f"$r = ConvertTo-EnrollmentResult -Content {ps_quote(body)}\n\"$($r.Valid)|$($r.Problem)\""
    )

    valid, _, problem = result.stdout.strip().partition("|")
    assert valid == "False", (case, result.stdout, result.stderr)
    assert problem  # a reason is always given
    assert SENTINEL_TOKEN not in problem  # ...and it never quotes the token


@needs_windows_powershell
def test_url_rules_are_https_only_with_no_credentials_query_or_fragment():
    cases = {
        "https://api.example.org": True, "https://api.example.org/": True, "https://api.example.org:8443/base": True,
        "HTTPS://api.example.org": True,
        "http://api.example.org": False, "ftp://x": False, "https://": False, "": False, "   ": False,
        "https://user:pw@api.example.org": False, "https://api.example.org?x=1": False,
        "https://api.example.org#frag": False, "//api.example.org": False, "https://api example.org": False,
    }
    script = ". " + ps_quote(SETUP_SOURCE) + "\n" + "\n".join(
        f"$p = Test-EnrollmentApiUrl -Url {ps_quote(url)}; \"$(if ($null -eq $p) {{'OK'}} else {{'BAD'}})\"" for url in cases
    )
    result = _run_ps(script)

    assert result.stdout.split() == ["OK" if ok else "BAD" for ok in cases.values()], (result.stdout, result.stderr)


@needs_windows_powershell
def test_bundle_defaults_come_from_the_bundles_example_config(tmp_path):
    result = _dot_source_and_run(
        f"$d = Get-BundleDefaults -BundleRoot {ps_quote(EXAMPLE_CONFIG.parent)}\n"
        "\"$($d.Problems.Count)|$($d.ApiUrl)|$($d.Sources['checkins'])|$($d.Sources['acs'])\""
    )
    template = json.loads(_read(EXAMPLE_CONFIG))

    assert result.stdout.strip() == f"0|{template['api_url'].rstrip('/')}|C:\\TLCFinalDlls\\Checkins.txt|C:\\TLCFinalDlls\\ACS Log.txt"


@needs_windows_powershell
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing_file", "missing"),
        ("bad_json", "not valid JSON"),
        ("http_url", "https://"),
        ("no_url", "unusable"),
        ("missing_source", "no standard path for source 'rejects'"),
    ],
)
def test_bundle_defaults_report_an_unusable_bundle(tmp_path, mutation, expected):
    template = json.loads(_read(EXAMPLE_CONFIG))
    target = tmp_path / "collector_config.example.json"
    if mutation == "bad_json":
        target.write_text("{not json", encoding="utf-8")
    elif mutation != "missing_file":
        if mutation == "http_url":
            template["api_url"] = "http://insecure.example.org"
        elif mutation == "no_url":
            template["api_url"] = ""
        elif mutation == "missing_source":
            template["sources"] = [s for s in template["sources"] if s["name"] != "rejects"]
        target.write_text(json.dumps(template), encoding="utf-8")

    result = _dot_source_and_run(
        f"$d = Get-BundleDefaults -BundleRoot {ps_quote(tmp_path)}\n"
        "$d.Problems | ForEach-Object { $_ }\n\"ApiUrl=[$($d.ApiUrl)]\""
    )

    assert expected in result.stdout, result.stdout
    if mutation != "missing_source":
        assert "ApiUrl=[]" in result.stdout  # no usable URL is ever returned from a bad bundle


@needs_windows_powershell
def test_get_runtime_version_runs_a_real_executable_and_parses_strictly(tmp_path):
    exe = tmp_path / "SortViewCollector.exe"
    source = (
        "public class P { public static int Main(string[] a) {"
        " if (a.Length > 0 && a[0] == \"version\") { System.Console.WriteLine(System.Environment.GetEnvironmentVariable(\"FAKE_VERSION_OUT\")); "
        " return int.Parse(System.Environment.GetEnvironmentVariable(\"FAKE_VERSION_EXIT\")); }"
        " System.Console.Error.WriteLine(\"usage\"); return 2; } }"
    )
    compiled = _run_ps(
        f"Add-Type -TypeDefinition {ps_quote(source)} -OutputAssembly {ps_quote(exe)} -OutputType ConsoleApplication"
    )
    if compiled.returncode != 0 or not exe.exists():
        pytest.skip("cannot compile a tiny test executable on this machine")

    def run(out, exit_code):
        return _run_ps(
            f". {ps_quote(SETUP_SOURCE)}\ntry {{ Get-RuntimeVersion -ExePath {ps_quote(exe)} }} catch {{ 'THREW: ' + $_.Exception.Message }}",
            env={"FAKE_VERSION_OUT": out, "FAKE_VERSION_EXIT": str(exit_code)},
        ).stdout.strip()

    assert run("1.2.3", 0) == "1.2.3"
    assert run("1.2.3", 0) == run("  1.2.3  ", 0)  # surrounding whitespace only
    for bad_out, bad_exit in (("1.2.3", 3), ("", 0), ("1.2.3 extra", 0), ("Usage: x <sub>", 0), ("version 1.2.3", 0)):
        assert run(bad_out, bad_exit).startswith("THREW"), (bad_out, bad_exit)


@needs_windows_powershell
def test_get_runtime_version_refuses_a_file_that_cannot_run(tmp_path):
    fake = tmp_path / "SortViewCollector.exe"
    fake.write_bytes(b"not a real executable")

    result = _dot_source_and_run(f"try {{ Get-RuntimeVersion -ExePath {ps_quote(fake)}; 'NO' }} catch {{ 'THREW' }}")

    assert result.stdout.strip().splitlines()[-1] == "THREW"


@needs_windows_powershell
def test_the_real_http_wrapper_refuses_anything_but_https_and_never_throws():
    script = (
        f". {ps_quote(SETUP_SOURCE)}\n"
        "$a = Invoke-SetupHttp -Method 'Post' -Url 'http://127.0.0.1:1/x' -Body '{}'\n"
        "$b = Invoke-SetupHttp -Method 'Get' -Url 'https://127.0.0.1:1/' -Body $null\n"
        "\"A=$($a.StatusCode)|$($a.Failure)\"\n\"B=$($b.StatusCode)|$($b.Failure)\""
    )
    result = _run_ps(script)

    assert "A=0|refused: the URL is not https://" in result.stdout, result.stderr
    assert re.search(r"B=0\|no response \(", result.stdout), result.stdout  # a real refused TLS connection


@needs_windows_powershell
def test_existing_install_detection_reports_files_and_never_modifies_them(tmp_path):
    install_root, data_root = tmp_path / "install", tmp_path / "data"
    script = (
        f". {ps_quote(SETUP_SOURCE)}\n"
        "function Get-SortViewTask { $null }\n"
        f"$none = Get-ExistingInstallProblem -InstallRoot {ps_quote(install_root)} -DataRoot {ps_quote(data_root)}\n"
        "\"NONE=$($null -eq $none)\"\n"
        f"New-Item -ItemType Directory -Force {ps_quote(install_root)} | Out-Null; Set-Content {ps_quote(install_root / 'SortViewCollector.exe')} 'x'\n"
        f"$p = Get-ExistingInstallProblem -InstallRoot {ps_quote(install_root)} -DataRoot {ps_quote(data_root)}\n"
        "\"PARTIAL=$($p.Kind)|$($p.Found.Count)\"\n"
        "function Get-SortViewTask { [pscustomobject]@{ State = 'Ready' } }\n"
        f"$l = Get-ExistingInstallProblem -InstallRoot {ps_quote(install_root)} -DataRoot {ps_quote(data_root)}\n"
        "\"LIVE=$($l.Kind)\"\n"
        "function Get-SortViewTask { [pscustomobject]@{ State = 'Disabled' } }\n"
        f"$d = Get-ExistingInstallProblem -InstallRoot {ps_quote(install_root)} -DataRoot {ps_quote(data_root)}\n"
        "\"DISABLED=$($d.Kind)\""
    )
    result = _run_ps(script)

    assert result.stdout.split() == ["NONE=True", "PARTIAL=Partial|1", "LIVE=Live", "DISABLED=Partial"], result.stderr
    assert (install_root / "SortViewCollector.exe").read_text().strip() == "x"


_ELEVATION_CHECK = "if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {"


def _token_wrapper_without_the_elevation_check(tmp_path: Path) -> Path:
    """The REAL set-collector-api-token.ps1 with exactly one change: its elevation check is short-circuited, because the test
    process is not elevated. Asserted to be the only difference."""
    text = _read(TOKEN_SOURCE)
    assert text.count(_ELEVATION_CHECK) == 1
    copy = tmp_path / "set-api-token.ps1"
    copy.write_text(text.replace(_ELEVATION_CHECK, "if ($false) {"), encoding="utf-8")
    return copy


def _fake_collector(tmp_path: Path, exit_code: int = 0) -> Path:
    """A stand-in `SortViewCollector.exe` that records its arguments and everything it receives on STDIN."""
    fake = tmp_path / "FakeCollector.cmd"
    fake.write_text(
        "@echo off\r\n"
        f'echo %* > "{tmp_path / "args.txt"}"\r\n'
        f'findstr "^" > "{tmp_path / "stdin.txt"}"\r\n'
        "echo api token stored and protected (value not shown)\r\n"
        f"exit /b {exit_code}\r\n",
        encoding="ascii",
    )
    return fake


@needs_windows_powershell
@pytest.mark.parametrize("tenant", ["config", "explicit"])
def test_the_token_wrapper_hands_the_token_to_the_collector_on_stdin_and_never_as_an_argument(tmp_path, tenant):
    wrapper = _token_wrapper_without_the_elevation_check(tmp_path)
    fake = _fake_collector(tmp_path)
    config = tmp_path / "collector_config.json"
    config.write_text("{}", encoding="utf-8")
    tenant_args = (f"-ConfigPath {ps_quote(config)}" if tenant == "config"
                   else f"-CustomerId 7 -BranchId 3 -DataRoot {ps_quote(tmp_path / 'data')}")

    result = _run_ps(
        f"$t = ConvertTo-SecureString {ps_quote(SENTINEL_TOKEN)} -AsPlainText -Force\n"
        f"& {ps_quote(wrapper)} -ExePath {ps_quote(fake)} {tenant_args} -Token $t\n"
        "'WRAPPER_OK'"
    )

    assert "WRAPPER_OK" in result.stdout, result.stdout + result.stderr
    assert (tmp_path / "stdin.txt").read_text(encoding="ascii").strip() == SENTINEL_TOKEN  # the token went on STDIN...
    args = (tmp_path / "args.txt").read_text(encoding="ascii")
    assert SENTINEL_TOKEN not in args  # ...and never on the command line
    assert args.split()[:2] == ["api-token", "set"]
    expected = ["--config", str(config)] if tenant == "config" else ["--customer-id", "7", "--branch-id", "3", "--data-root"]
    assert args.split()[2:2 + len(expected)] == expected
    assert SENTINEL_TOKEN not in result.stdout + result.stderr
    assert "SetEnvironmentVariable" not in _read(TOKEN_SOURCE).split("#>", 1)[1]


@needs_windows_powershell
def test_the_token_wrapper_throws_when_the_collector_does_not_store_the_token(tmp_path):
    wrapper = _token_wrapper_without_the_elevation_check(tmp_path)
    fake = _fake_collector(tmp_path, exit_code=1)
    config = tmp_path / "collector_config.json"
    config.write_text("{}", encoding="utf-8")

    result = _run_ps(
        f"$t = ConvertTo-SecureString {ps_quote(SENTINEL_TOKEN)} -AsPlainText -Force\n"
        f"try {{ & {ps_quote(wrapper)} -ExePath {ps_quote(fake)} -ConfigPath {ps_quote(config)} -Token $t; 'NO_THROW' }} "
        "catch { 'THREW: ' + $_.Exception.Message }"
    )

    assert "THREW: The Collector did not store the API token (exit code 1)" in result.stdout, result.stdout + result.stderr
    assert "NO_THROW" not in result.stdout and SENTINEL_TOKEN not in result.stdout + result.stderr


@needs_windows_powershell
@pytest.mark.parametrize("partial", ["-CustomerId 7", "-CustomerId 7 -BranchId 3", "-DataRoot 'C:\\x'"])
def test_the_token_wrapper_refuses_a_partial_explicit_tenant_before_asking_for_anything(tmp_path, partial):
    wrapper = _token_wrapper_without_the_elevation_check(tmp_path)
    fake = _fake_collector(tmp_path)

    result = _run_ps(f"try {{ & {ps_quote(wrapper)} -ExePath {ps_quote(fake)} {partial}; 'NO_THROW' }} catch {{ 'THREW: ' + $_.Exception.Message }}")

    assert "THREW: -CustomerId, -BranchId and -DataRoot must be given together." in result.stdout, result.stdout + result.stderr
    assert not (tmp_path / "args.txt").exists()


@needs_windows_powershell
def test_finish_install_token_step_decision_table():
    finish = _read(FINISH_SOURCE)
    function = _ps_function("Get-TokenStepAction", finish)
    # (UseExisting, a usable api_token.dpapi) -> action. Only a stored token counts as "a token exists" -- there is no
    # second input; a DAMAGED api_token.dpapi never reaches this table (the script stops first).
    assert "param([bool]$UseExisting, [bool]$StoredTokenPresent)" in function
    cases = [
        ("$true", "$true", "UseExisting"), ("$true", "$false", "MissingExisting"),
        ("$false", "$true", "AskKeepOrReplace"), ("$false", "$false", "PromptForToken"),
    ]
    script = function + "\n" + "\n".join(
        f"Get-TokenStepAction -UseExisting {use} -StoredTokenPresent {stored}" for use, stored, _ in cases
    )

    assert _run_ps(script).stdout.split() == [expected for *_inputs, expected in cases]


@needs_windows_powershell
def test_finish_install_token_step_ignores_a_claimed_machine_token():
    # The third input is gone. A caller that still claims "a Machine token is present" changes nothing (a plain PowerShell
    # function ignores an unknown argument): with no stored token the answer is still "no token" in both modes.
    function = _ps_function("Get-TokenStepAction", _read(FINISH_SOURCE))
    assert "MachineTokenPresent" not in function
    script = function + (
        "\nGet-TokenStepAction -UseExisting $true -StoredTokenPresent $false -MachineTokenPresent $true"
        "\nGet-TokenStepAction -UseExisting $false -StoredTokenPresent $false -MachineTokenPresent $true"
    )

    assert _run_ps(script).stdout.split() == ["MissingExisting", "PromptForToken"]


# =====================================================================================
# 3. ORCHESTRATION scenarios
# =====================================================================================

_SCENARIO_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$FakeDir = '@@FAKE@@'
. '@@SETUP@@'

function Add-Call { param($Record) Add-Content -LiteralPath "$FakeDir\calls.jsonl" -Value ($Record | ConvertTo-Json -Compress -Depth 6) }
function Test-IsAdministrator { @@ADMIN@@ }
function Get-LocalHostName { 'TEST-PC' }
function Get-RuntimeVersion { param($ExePath) Add-Call @{ tool = 'runtime-version' }; '@@RUNTIME_VERSION@@' }
function Test-ApiTokenStored { param($DataRoot) Test-Path "$FakeDir\machine_token.txt" }
function Invoke-ApiTokenCheck {
    param($ExePath, $CustomerId, $BranchId, $DataRoot)
    if ($env:SETUPTEST_CHECK_MODE -eq 'fail') { return 1 }
    if (Test-Path "$FakeDir\machine_token.txt") { return 0 } else { return 3 }
}
function Get-SortViewTask { if (Test-Path "$FakeDir\task.json") { Get-Content -LiteralPath "$FakeDir\task.json" -Raw | ConvertFrom-Json } else { $null } }
function Set-FakeTaskState { param($State) $t = Get-SortViewTask; $t.State = $State; $t | ConvertTo-Json | Set-Content -LiteralPath "$FakeDir\task.json" }
function Enable-SortViewTask { Add-Call @{ tool = 'enable-task' }; if ($env:SETUPTEST_ENABLE_FAILS) { throw 'enable failed' }; Set-FakeTaskState 'Ready' }
function Disable-SortViewTask { Add-Call @{ tool = 'disable-task' }; Set-FakeTaskState 'Disabled' }
function Protect-SetupRecoveryDirectory { param($Path) Add-Call @{ tool = 'protect-recovery'; path = $Path }; if ($env:SETUPTEST_PROTECT_FAILS) { throw 'could not restrict the folder' } }
function Read-SetupAnswer {
    param([string]$Prompt)
    # One answer per line ("<ENTER>" = the empty answer); an empty/absent queue = a host that cannot be asked.
    $lines = @(); if (Test-Path "$FakeDir\answers.txt") { $lines = @(Get-Content -LiteralPath "$FakeDir\answers.txt") }
    Add-Call @{ tool = 'prompt'; text = $Prompt }
    if ($lines.Count -eq 0) { return $null }
    $first = [string]$lines[0]
    $rest = @($lines | Select-Object -Skip 1)
    if ($rest.Count -gt 0) { Set-Content -LiteralPath "$FakeDir\answers.txt" -Value $rest } else { Remove-Item -LiteralPath "$FakeDir\answers.txt" }
    if ($first -eq '<ENTER>') { return '' }
    return $first
}
function Read-EnrollmentCodeSecure { Add-Call @{ tool = 'read-code' }; if ($env:SETUPTEST_NO_CODE) { return $null }; ConvertTo-SecureString '@@CODE@@' -AsPlainText -Force }
function Invoke-SetupHttp {
    param($Method, $Url, $Body)
    $parsed = $null; if ($Body) { $parsed = $Body | ConvertFrom-Json }
    Add-Call @{ tool = 'http'; method = $Method; url = $Url; body = $parsed }
    $key = "$Method " + ([Uri]$Url).AbsolutePath
    $script = Get-Content -LiteralPath "$FakeDir\http.json" -Raw | ConvertFrom-Json
    $entry = $script.PSObject.Properties[$key]
    if ($null -eq $entry) { return [pscustomobject]@{ StatusCode = 0; Content = $null; Failure = "no scripted response for $key" } }
    $e = $entry.Value
    if ($e -is [array]) {
        # A SEQUENCE of answers for one route: the 1st call gets the 1st, ... and the last one repeats.
        $counter = Join-Path $FakeDir ("http_count_" + ($key -replace '[^A-Za-z0-9]', '_') + ".txt")
        $seen = 0; if (Test-Path -LiteralPath $counter) { $seen = [int](Get-Content -LiteralPath $counter -Raw) }
        Set-Content -LiteralPath $counter -Value ($seen + 1)
        $e = $e[[Math]::Min($seen, $e.Count - 1)]
    }
    return [pscustomobject]@{ StatusCode = [int]$e.status; Content = [string]$e.content; Failure = $e.failure }
}
@@EXTRA@@

$r = @(Invoke-SetupMain @@ARGS@@)
"SETUP_EXIT_CODE=$([int]$r[-1])"
"""

_STUB_HEADER = "$rec = @{ tool = '%s'; params = @{} }\n" \
    "foreach ($k in $PSBoundParameters.Keys) { $v = $PSBoundParameters[$k]; if ($v -is [switch]) { $v = [bool]$v }; " \
    "if ($v -is [securestring]) { $v = '<securestring>' }; $rec.params[$k] = $v }\n"

_LOG_CALL = "Add-Content -LiteralPath \"$env:SETUPTEST_FAKE_DIR\\calls.jsonl\" -Value ($rec | ConvertTo-Json -Compress -Depth 6)\n"

_INSTALL_STUB_BODY = _STUB_HEADER % "install" + r"""
if ($VerifyBundleOnly) {
    $rec['tool'] = 'install-verify'
""" + _LOG_CALL + r"""
    if ($env:SETUPTEST_VERIFY_MODE -eq 'fail') { Write-Host 'STUB install.ps1: MANIFEST VERIFICATION FAILED'; exit 4 }
    Write-Host 'STUB install.ps1: bundle verified (read-only)'
    exit 0
}
""" + _LOG_CALL + r"""
if ($env:SETUPTEST_INSTALL_MODE -eq 'fail') { Write-Host 'STUB install.ps1: FAILED'; exit 3 }
New-Item -ItemType Directory -Force -Path $InstallRoot, (Join-Path $DataRoot 'config'), (Join-Path $DataRoot 'data') | Out-Null
Set-Content -LiteralPath (Join-Path $InstallRoot 'SortViewCollector.exe') -Value 'fake'
$config = [ordered]@{
    customer_id = [int]$CustomerId; branch_id = [int]$BranchId; installation_id = [int]$InstallationId; api_url = $ApiUrl
    sources = @(
        [ordered]@{ name = 'checkins'; path = $CheckinsPath }, [ordered]@{ name = 'rejects'; path = $RejectsPath },
        [ordered]@{ name = 'acs'; path = $AcsPath })
    state_path = (Join-Path $DataRoot 'data\state.json')
}
if ($ContractMode) { $config['contract_mode'] = $ContractMode; $config['v2'] = [ordered]@{ key_id = $KeyId; timezone = $Timezone } }
Set-Content -LiteralPath (Join-Path $DataRoot 'config\collector_config.json') -Value ($config | ConvertTo-Json -Depth 5)
Write-Host 'STUB install.ps1: installed'
exit 0
"""

_TOKEN_STUB_BODY = _STUB_HEADER % "set-api-token" + r"""
$rec['token_supplied'] = $PSBoundParameters.ContainsKey('Token')
$rec['token_type'] = if ($null -ne $Token) { $Token.GetType().Name } else { $null }
""" + _LOG_CALL + r"""
if ($env:SETUPTEST_TOKEN_MODE -eq 'fail') { throw 'STUB set-api-token: could not set the variable' }
if ($null -ne $Token) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Token)
    try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if ($env:SETUPTEST_TOKEN_MODE -eq 'wrong') { $plain = 'a-different-token-value-entirely-xyz' }
    Set-Content -LiteralPath (Join-Path $env:SETUPTEST_FAKE_DIR 'machine_token.txt') -Value $plain -NoNewline
}
Write-Host "STUB set-api-token: stored in api_token.dpapi (value not shown)."
"""

_FINISH_STUB_BODY = _STUB_HEADER % "finish-install" + _LOG_CALL + r"""
$mode = $env:SETUPTEST_FINISH_MODE
function Register-FakeTask { param($State) @{ State = $State } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $env:SETUPTEST_FAKE_DIR 'task.json') }
switch ($mode) {
    'fail_interactive_preflight' { Write-Host '[FAIL] source_paths_exist: STUB'; Write-Host 'SETUP STOPPED at step 2 of 5 (interactive preflight)'; exit 1 }
    'fail_system_preflight'      { Write-Host 'SYSTEM-context preflight FAILED (STUB)'; Write-Host 'SETUP STOPPED at step 3 of 5 (SYSTEM-context preflight)'; exit 1 }
    'fail_bootstrap'             { Write-Host 'SETUP STOPPED at step 4 of 5 (bootstrap)'; exit 1 }
    'incomplete_state'           { Write-Host 'source acs has no entry -- the file does not cover every configured source.'; Write-Host 'SETUP STOPPED at step 4 of 5 (bootstrap)'; exit 2 }
    'fail_task_registration'     { Write-Host 'SETUP STOPPED at step 5 of 5 (Scheduled Task)'; exit 1 }
    'rogue_enabled_task_then_fail' { Register-FakeTask 'Ready'; Write-Host 'SETUP STOPPED at step 5 of 5 (Scheduled Task)'; exit 1 }
    'ok_but_task_enabled'        { Register-FakeTask 'Ready'; Write-Host 'finished (STUB)'; exit 0 }
    'ok_but_no_task'             { Write-Host 'finished (STUB)'; exit 0 }
    default                      { Register-FakeTask 'Disabled'; Write-Host 'STUB finish-install.ps1: setup complete, task registered Disabled'; exit 0 }
}
"""


@dataclass
class Bundle:
    root: Path
    fake: Path
    install_root: Path
    data_root: Path
    source_dir: Path
    version: str = RELEASE_VERSION

    @property
    def config_path(self) -> Path:
        return self.data_root / "config" / "collector_config.json"


def make_bundle(tmp_path: Path, *, sources_present: bool = True, template_mutation=None, manifest_version: str | None = None,
                source_layout: bool = False, omit_tools: tuple[str, ...] = ()) -> Bundle:
    root = tmp_path / "bundle"
    fake = tmp_path / "fake"
    source_dir = tmp_path / "TechLogic"
    (root / "tools").mkdir(parents=True)
    (root / "runtime" / "_internal").mkdir(parents=True)
    fake.mkdir()
    source_dir.mkdir()

    shutil.copy2(SETUP_SOURCE, root / "setup.ps1")
    (root / "runtime" / "SortViewCollector.exe").write_bytes(b"fake-exe")
    (root / "MANIFEST.json").write_text(
        json.dumps({"product": "SortView Collector", "version": manifest_version or RELEASE_VERSION, "files": []}), encoding="utf-8"
    )

    # The bundle's standard locations point at the scratch Tech Logic folder.
    template = json.loads(_read(EXAMPLE_CONFIG))
    template["sources"] = [
        {"name": "checkins", "path": str(source_dir / "Checkins.txt")},
        {"name": "rejects", "path": str(source_dir / "Rejects.txt")},
        {"name": "acs", "path": str(source_dir / "ACS Log.txt")},
    ]
    if template_mutation:
        template_mutation(template)
    (root / "collector_config.example.json").write_text(json.dumps(template), encoding="utf-8")
    if sources_present:
        for name in ("Checkins.txt", "Rejects.txt", "ACS Log.txt"):
            (source_dir / name).write_text("data\n", encoding="utf-8")

    # Stub tools carrying the REAL parameter blocks: a parameter the real script lacks fails binding here.
    (root / "install.ps1").write_text(_param_block(INSTALL_SOURCE) + "\n" + _INSTALL_STUB_BODY, encoding="utf-8")
    stubs = {
        "set-api-token.ps1": _param_block(TOKEN_SOURCE) + "\n" + _TOKEN_STUB_BODY,
        "finish-install.ps1": _param_block(FINISH_SOURCE) + "\n" + _FINISH_STUB_BODY,
        "preflight-system.ps1": "# stub\n",
        "register-task.ps1": "# stub\n",
    }
    for name, content in stubs.items():
        if name not in omit_tools:
            (root / "tools" / name).write_text(content, encoding="utf-8")
    if source_layout:
        (root / "collector").mkdir()

    return Bundle(root=root, fake=fake, install_root=tmp_path / "install", data_root=tmp_path / "data", source_dir=source_dir)


def real_bundle(tmp_path: Path, *, version: str = RELEASE_VERSION) -> Bundle:
    """A genuine frozen release bundle built by build_release: the real setup.ps1, the real install.ps1 and tools,
    and a real MANIFEST.json. Used to prove that tampering is caught by the REAL manifest verification."""
    runtime = tmp_path / "rt" / "SortViewCollector"
    (runtime / "_internal").mkdir(parents=True)
    (runtime / "SortViewCollector.exe").write_bytes(b"fake-exe")
    (runtime / "_internal" / "python.dll").write_bytes(b"fake-dll")
    built = build_release.build_frozen_release(
        REPO_ROOT, tmp_path / "out", version, runtime, built_at="2026-01-01T00:00:00.000000Z", version_probe=lambda _p: version
    )
    fake = tmp_path / "fake"
    source_dir = tmp_path / "TechLogic"
    fake.mkdir()
    source_dir.mkdir()
    for name in ("Checkins.txt", "Rejects.txt", "ACS Log.txt"):
        (source_dir / name).write_text("data\n", encoding="utf-8")
    return Bundle(root=built.bundle_dir, fake=fake, install_root=tmp_path / "install", data_root=tmp_path / "data",
                  source_dir=source_dir, version=version)


def source_args(bundle: Bundle) -> str:
    return (f"-CheckinsPath {ps_quote(bundle.source_dir / 'Checkins.txt')} -RejectsPath {ps_quote(bundle.source_dir / 'Rejects.txt')} "
            f"-AcsPath {ps_quote(bundle.source_dir / 'ACS Log.txt')}")


def _valid_enroll_body(**overrides) -> str:
    return json.dumps({**IDS, "agent_token": SENTINEL_TOKEN, **overrides})


ENROLL = "Post /collector/enroll"
KEY_ID = "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"
TIMEZONE = "America/New_York"
# Synthetic account names and patterns: recognisable CANARIES, so a test can sweep every byte setup printed or wrote.
RULE_NAME_BRANCH = "CANARY BRANCH SERVICES ACCOUNT"
RULE_NAME_COLLECTION = "CANARY COLLECTION SERVICES ACCOUNT"
RULE_PATTERN = "DACANARY DEPARTMENT PATTERN"
RULE_DESTINATION = "CANARY ANNEX"
RULES_DOC = {
    "schema_version": 1,
    "destinations": [{"slug": "canary_annex", "contains": RULE_DESTINATION}],
    "branch_services_names": [RULE_NAME_BRANCH],
    "collection_services_names": [RULE_NAME_COLLECTION, "CANARY SECOND ACCOUNT"],
    "branch_services_da_patterns": [],
    "collection_services_da_patterns": [RULE_PATTERN],
}
RULE_CANARIES = (RULE_NAME_BRANCH, RULE_NAME_COLLECTION, "CANARY SECOND ACCOUNT", RULE_PATTERN, RULE_DESTINATION, "canary_annex")


def refusal(code: str) -> dict:
    """The API's 409 for a valid code it will not enroll (collector_enrollment_service.REFUSAL_DETAILS). Never consumes the code."""
    return {"status": 409, "content": json.dumps({"code": code, "detail": "A fixed sentence from the server."})}


def _v2_enroll_body(rules: dict | None = None, **overrides) -> str:
    return json.dumps({**IDS, "agent_token": SENTINEL_TOKEN, "contract_version": 2, "contract_mode": "v2", "key_id": KEY_ID,
                       "timezone": TIMEZONE, "classification_rules": RULES_DOC if rules is None else rules, **overrides})


def default_http(**overrides) -> dict:
    """A server holding a LEGACY (contract 1) code, as the real API answers this release: the first request asks for
    Contract v2 and is refused `contract_not_permitted` (the code is not used), the second -- the legacy request -- enrolls."""
    script = {
        "Get /": {"status": 200, "content": json.dumps({"status": "SortView API running"})},
        ENROLL: [refusal("contract_not_permitted"), {"status": 200, "content": _valid_enroll_body()}],
    }
    script.update(overrides)
    return script


def v2_http(rules: dict | None = None, **overrides) -> dict:
    """A server holding a CONTRACT 2 code: the first (and only) request enrolls on Contract v2."""
    script = {
        "Get /": {"status": 200, "content": json.dumps({"status": "SortView API running"})},
        ENROLL: {"status": 200, "content": _v2_enroll_body(rules)},
    }
    script.update(overrides)
    return script


@dataclass
class Result:
    exit_code: int | None
    output: str
    calls: list[dict] = field(default_factory=list)
    task: dict | None = None
    machine_token: str | None = None
    bundle: Bundle | None = None

    def tools(self) -> list[str]:
        """Ordered names of the calls that matter (prompts excluded)."""
        names = []
        for call in self.calls:
            tool = call["tool"]
            if tool == "http":
                names.append(f"http:{call['method'].upper()}")
            elif tool not in ("prompt", "protect-recovery"):
                names.append(tool)
        return names

    def calls_to(self, tool: str) -> list[dict]:
        return [c for c in self.calls if c["tool"] == tool]

    def prompts(self) -> list[str]:
        return [c["text"] for c in self.calls if c["tool"] == "prompt"]

    @property
    def recovery_path(self) -> Path:
        assert self.bundle is not None
        return self.bundle.data_root / "setup" / "enrollment-recovery.json"

    @property
    def recovery(self) -> dict | None:
        assert self.bundle is not None
        return json.loads(self.recovery_path.read_text(encoding="utf-8")) if self.recovery_path.exists() else None

    @property
    def everything_written(self) -> str:
        """All text a scenario left on disk under the bundle's fake state and the install roots (except the fake Machine store)."""
        assert self.bundle is not None
        parts = []
        for base in (self.bundle.data_root, self.bundle.install_root):
            if base.exists():
                for path in base.rglob("*"):
                    if path.is_file() and path.name != "SortViewCollector.exe":
                        parts.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts)


def run_scenario(bundle: Bundle, *, args: str = "", admin: bool = True, http: dict | None = None, answers: list | None = None,
                 extra_ps: str = "", env: dict | None = None, runtime_version: str | None = None,
                 machine_token: str | None = None, task_state: str | None = None) -> Result:
    (bundle.fake / "http.json").write_text(json.dumps(http if http is not None else default_http()), encoding="utf-8")
    for counter in bundle.fake.glob("http_count_*.txt"):  # each run starts a scripted SEQUENCE of answers from its first
        counter.unlink()
    if answers:
        (bundle.fake / "answers.txt").write_text(
            "\n".join("<ENTER>" if a == "" else a for a in answers) + "\n", encoding="utf-8"
        )
    if machine_token is not None:
        (bundle.fake / "machine_token.txt").write_text(machine_token, encoding="utf-8")
    if task_state is not None:
        (bundle.fake / "task.json").write_text(json.dumps({"State": task_state}), encoding="utf-8")

    common_args = f"-InstallRoot {ps_quote(bundle.install_root)} -DataRoot {ps_quote(bundle.data_root)}"
    script = (
        _SCENARIO_TEMPLATE
        .replace("@@FAKE@@", str(bundle.fake))
        .replace("@@SETUP@@", str(bundle.root / "setup.ps1"))
        .replace("@@ADMIN@@", "$true" if admin else "$false")
        .replace("@@RUNTIME_VERSION@@", runtime_version or bundle.version)
        .replace("@@CODE@@", SENTINEL_CODE)
        .replace("@@EXTRA@@", extra_ps)
        .replace("@@ARGS@@", f"{common_args} {args}")
    )
    script_path = bundle.fake / "scenario.ps1"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        capture_output=True, text=True, timeout=180, check=False,
        env={**os.environ, "SETUPTEST_FAKE_DIR": str(bundle.fake), **(env or {})},
    )
    output = completed.stdout + "\n" + completed.stderr
    match = re.search(r"SETUP_EXIT_CODE=(\d+)", completed.stdout)
    calls_file = bundle.fake / "calls.jsonl"
    calls = [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines() if line.strip()] if calls_file.exists() else []
    task_file = bundle.fake / "task.json"
    token_file = bundle.fake / "machine_token.txt"
    return Result(
        exit_code=int(match.group(1)) if match else None,
        output=output,
        calls=calls,
        task=json.loads(task_file.read_text(encoding="utf-8")) if task_file.exists() else None,
        machine_token=token_file.read_text(encoding="utf-8") if token_file.exists() else None,
        bundle=bundle,
    )


def assert_no_secret_anywhere_visible(result: Result) -> None:
    """The token and the enrollment code appear in NO console output and no file setup or its tools left
    behind. (The fake HTTP layer's call log records what the request carried -- that is test scaffolding,
    not output -- so the code is checked there only against the tool records, never the token.)"""
    written = result.output + chr(10) + result.everything_written
    assert SENTINEL_TOKEN not in written and SENTINEL_CODE not in written
    tool_records = json.dumps([c for c in result.calls if c["tool"] != "http"])
    assert SENTINEL_TOKEN not in tool_records and SENTINEL_CODE not in tool_records
    assert SENTINEL_TOKEN not in json.dumps(result.calls)  # not even the HTTP layer ever handed the token onward


def http_bodies(result: Result) -> list[dict]:
    return [c["body"] for c in result.calls if c["tool"] == "http" and c["body"]]


# ---- the happy path -------------------------------------------------------------------------

@needs_windows_powershell
def test_successful_frozen_setup_runs_every_step_in_order_and_leaves_the_task_disabled(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])  # accept the standard files; Enter at the enable prompt

    assert result.exit_code == 0, result.output
    assert result.tools() == [
        "install-verify", "runtime-version", "http:GET", "read-code", "http:POST", "http:POST", "set-api-token", "install", "finish-install",
    ]
    assert result.task == {"State": "Disabled"}
    assert result.calls_to("enable-task") == []
    assert "COMPLETE" in result.output and "DISABLED" in result.output
    assert "did not start a Collector run" in result.output
    assert_no_secret_anywhere_visible(result)


@needs_windows_powershell
def test_the_returned_ids_and_derived_settings_are_passed_to_the_installer(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])

    (install,) = result.calls_to("install")
    params = install["params"]
    template = json.loads(_read(EXAMPLE_CONFIG))
    assert (params["CustomerId"], params["BranchId"], params["InstallationId"]) == (7, 3, 41)
    assert params["ApiUrl"] == template["api_url"].rstrip("/")  # the bundle's canonical production URL
    assert params["CheckinsPath"] == str(bundle.source_dir / "Checkins.txt")
    assert params["RejectsPath"] == str(bundle.source_dir / "Rejects.txt")
    assert params["AcsPath"] == str(bundle.source_dir / "ACS Log.txt")
    assert params["InstallRoot"] == str(bundle.install_root) and params["DataRoot"] == str(bundle.data_root)
    assert params["SuppressNextSteps"] is True
    assert "Force" not in params  # never a casual -Force
    config = json.loads(bundle.config_path.read_text(encoding="utf-8"))
    assert (config["customer_id"], config["branch_id"], config["installation_id"]) == (7, 3, 41)


@needs_windows_powershell
def test_finish_install_is_run_against_the_installed_config_with_the_existing_token_switch(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])

    (finish,) = result.calls_to("finish-install")
    assert finish["params"] == {
        "InstallRoot": str(bundle.install_root), "ConfigPath": str(bundle.config_path), "UseExistingMachineToken": True,
    }


@needs_windows_powershell
def test_the_technician_is_never_asked_for_an_id_or_a_token(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])

    asked = " ".join(result.prompts()).lower()
    for word in ("customer", "branch", "installation", "token", "api url", "id"):
        assert word not in asked.replace("[y/n]", "").replace("enable the scheduled task", ""), word
    assert len(result.calls_to("read-code")) == 1  # exactly one secret is requested, and it is the enrollment code


# ---- secrets -------------------------------------------------------------------------------------

@needs_windows_powershell
def test_the_permanent_token_is_stored_by_the_token_tool_as_a_secure_string_and_never_printed(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])

    (stored,) = result.calls_to("set-api-token")
    assert stored["token_supplied"] is True and stored["token_type"] == "SecureString"
    assert stored["params"]["Token"] == "<securestring>"  # the record never held the value
    # Stored BEFORE install, with the bundle's own runtime, for exactly the tenant and data root install.ps1 then writes.
    assert stored["params"]["ExePath"] == str(bundle.root / "runtime" / "SortViewCollector.exe")
    assert (stored["params"]["CustomerId"], stored["params"]["BranchId"]) == (IDS["customer_id"], IDS["branch_id"])
    assert stored["params"]["DataRoot"] == str(bundle.data_root)
    assert result.machine_token == SENTINEL_TOKEN  # stored (in the fake api_token.dpapi store)
    assert SENTINEL_TOKEN not in result.output
    assert_no_secret_anywhere_visible(result)


@needs_windows_powershell
def test_neither_secret_is_in_the_config_or_any_installed_file_or_the_output(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])

    config_text = bundle.config_path.read_text(encoding="utf-8").lower()
    assert "token" not in config_text and SENTINEL_TOKEN.lower() not in config_text and SENTINEL_CODE.lower() not in config_text
    assert SENTINEL_TOKEN not in result.everything_written and SENTINEL_CODE not in result.everything_written
    assert SENTINEL_TOKEN not in result.output and SENTINEL_CODE not in result.output


@needs_windows_powershell
def test_the_enrollment_request_carries_the_code_the_host_name_the_canonical_version_and_the_contract_only(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])

    first, second = [c for c in result.calls_to("http") if c["method"] == "Post"]
    url = json.loads(_read(EXAMPLE_CONFIG))["api_url"].rstrip("/") + "/collector/enroll"
    assert first["url"] == second["url"] == url
    # The first request asks for Contract v2 ...
    assert first["body"] == {"enrollment_code": SENTINEL_CODE, "hostname": "TEST-PC", "collector_version": RELEASE_VERSION,
                             "contract_version": 2}
    # ... and the single retry for a legacy code is the LEGACY request, key for key: no contract_version at all.
    assert second["body"] == {"enrollment_code": SENTINEL_CODE, "hostname": "TEST-PC", "collector_version": RELEASE_VERSION}
    assert SENTINEL_CODE not in result.output  # entered once, sent, never echoed


@needs_windows_powershell
def test_a_programmatic_enrollment_code_is_accepted_only_as_a_secure_string(tmp_path):
    bundle = make_bundle(tmp_path)
    extra = "$secure = ConvertTo-SecureString 'SV-FROM-PARAMETER-CODE' -AsPlainText -Force\n"

    result = run_scenario(bundle, answers=["", ""], extra_ps=extra + "function Read-EnrollmentCodeSecure { throw 'must not prompt' }\n",
                          args="-EnrollmentCode $secure")

    assert result.exit_code == 0, result.output
    assert http_bodies(result)[0]["enrollment_code"] == "SV-FROM-PARAMETER-CODE"
    assert result.calls_to("read-code") == []
    assert "SV-FROM-PARAMETER-CODE" not in result.output


@needs_windows_powershell
def test_a_plain_text_enrollment_code_parameter_cannot_even_be_passed(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, args="-EnrollmentCode 'SV-PLAIN-TEXT-ATTEMPT'")

    assert result.exit_code is None  # parameter binding refused before anything ran
    assert result.calls == []
    assert "SV-PLAIN-TEXT-ATTEMPT" not in result.calls.__repr__()


# ---- the canonical release version ----------------------------------------------------------------

@needs_windows_powershell
def test_the_version_reported_during_enrollment_is_the_runtimes_canonical_version(tmp_path):
    bundle = make_bundle(tmp_path, manifest_version="1.4.7")

    result = run_scenario(bundle, answers=["", ""], runtime_version="1.4.7")

    assert http_bodies(result)[0]["collector_version"] == "1.4.7"
    assert result.exit_code == 0


@needs_windows_powershell
def test_a_runtime_that_disagrees_with_the_manifest_stops_before_any_network_or_code_request(tmp_path):
    bundle = make_bundle(tmp_path, manifest_version="1.4.7")

    result = run_scenario(bundle, runtime_version="1.0.3")

    assert result.exit_code == 1
    assert result.tools() == ["install-verify", "runtime-version"]  # no HTTP, no code prompt
    assert "reports version '1.0.3' but the release manifest says '1.4.7'" in result.output


@needs_windows_powershell
def test_a_runtime_that_will_not_report_its_version_stops_before_any_network_or_code_request(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, extra_ps="function Get-RuntimeVersion { throw 'exit code 3' }\n")

    assert result.exit_code == 1 and result.tools() == ["install-verify"]  # verified, then nothing else
    assert result.calls_to("http") == [] and result.calls_to("read-code") == []
    assert "would not report its version" in result.output


# ---- bundle checks ---------------------------------------------------------------------------------

@needs_windows_powershell
def test_a_source_bundle_is_refused_before_anything_happens(tmp_path):
    bundle = make_bundle(tmp_path, source_layout=True)

    result = run_scenario(bundle)

    assert result.exit_code == 1 and result.tools() == ["install-verify"]
    assert result.calls_to("http") == [] and result.calls_to("read-code") == []
    assert "frozen" in result.output.lower() and "enrollment code was not requested" in result.output


@needs_windows_powershell
@pytest.mark.parametrize("missing", ["set-api-token.ps1", "finish-install.ps1", "preflight-system.ps1", "register-task.ps1"])
def test_an_incomplete_bundle_is_refused_before_the_code_is_requested(tmp_path, missing):
    bundle = make_bundle(tmp_path, omit_tools=(missing,))

    result = run_scenario(bundle)

    assert result.exit_code == 1 and result.calls_to("read-code") == [] and result.calls_to("http") == []
    assert "incomplete" in result.output


@needs_windows_powershell
def test_a_missing_manifest_is_refused_before_the_code_is_requested(tmp_path):
    bundle = make_bundle(tmp_path)
    (bundle.root / "MANIFEST.json").unlink()

    result = run_scenario(bundle)

    assert result.exit_code == 1 and result.calls_to("http") == [] and "MANIFEST.json" in result.output


@needs_windows_powershell
def test_an_unusable_bundle_default_is_refused_before_the_code_is_requested(tmp_path):
    bundle = make_bundle(tmp_path, template_mutation=lambda t: t.update(api_url="http://insecure.example.org"))

    result = run_scenario(bundle)

    assert result.exit_code == 1 and result.calls_to("http") == []
    assert "https://" in result.output


# ---- the API URL ---------------------------------------------------------------------------------------

@needs_windows_powershell
def test_the_production_url_is_never_typed_and_comes_from_the_bundle(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])

    template_url = json.loads(_read(EXAMPLE_CONFIG))["api_url"].rstrip("/")
    assert {c["url"] for c in result.calls_to("http")} == {template_url + "/", template_url + "/collector/enroll"}
    assert not any("url" in p.lower() for p in result.prompts())


@needs_windows_powershell
def test_an_explicit_api_url_overrides_the_bundle_default_everywhere(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""], args="-ApiUrl 'https://dev.example.org/'")

    assert {c["url"] for c in result.calls_to("http")} == {"https://dev.example.org/", "https://dev.example.org/collector/enroll"}
    assert result.calls_to("install")[0]["params"]["ApiUrl"] == "https://dev.example.org"


@needs_windows_powershell
@pytest.mark.parametrize("url", ["http://dev.example.org", "https://user:pw@dev.example.org", "ftp://x", "dev.example.org"])
def test_an_unacceptable_api_url_override_is_refused_before_anything_else(tmp_path, url):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, args=f"-ApiUrl {ps_quote(url)}")

    assert result.exit_code == 1 and result.calls_to("http") == [] and result.calls_to("read-code") == []
    assert "-ApiUrl is not acceptable" in result.output


# ---- source files -------------------------------------------------------------------------------------------

@needs_windows_powershell
def test_the_standard_tech_logic_locations_are_offered_and_used_by_default(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])  # Enter = accept the offered files

    assert any("Use these files?" in p for p in result.prompts())
    assert "Found the standard Tech Logic files" in result.output
    params = result.calls_to("install")[0]["params"]
    assert params["CheckinsPath"].endswith("Checkins.txt") and params["AcsPath"].endswith("ACS Log.txt")


@needs_windows_powershell
def test_explicit_source_paths_are_used_and_the_standard_offer_is_skipped(tmp_path):
    bundle = make_bundle(tmp_path)
    custom = tmp_path / "Custom Dir"
    custom.mkdir()
    paths = {name: custom / name for name in ("c.txt", "r.txt", "a.txt")}
    for path in paths.values():
        path.write_text("x", encoding="utf-8")

    result = run_scenario(
        bundle, answers=[""],
        args=f"-CheckinsPath {ps_quote(paths['c.txt'])} -RejectsPath {ps_quote(paths['r.txt'])} -AcsPath {ps_quote(paths['a.txt'])}",
    )

    assert result.exit_code == 0, result.output
    params = result.calls_to("install")[0]["params"]
    assert (params["CheckinsPath"], params["RejectsPath"], params["AcsPath"]) == tuple(str(paths[k]) for k in ("c.txt", "r.txt", "a.txt"))
    assert not any("Use these files?" in p for p in result.prompts())


@needs_windows_powershell
def test_one_explicit_path_overrides_only_its_own_source(tmp_path):
    bundle = make_bundle(tmp_path)
    custom = tmp_path / "elsewhere.txt"
    custom.write_text("x", encoding="utf-8")

    result = run_scenario(bundle, answers=[""], args=f"-RejectsPath {ps_quote(custom)}")

    params = result.calls_to("install")[0]["params"]
    assert params["RejectsPath"] == str(custom)
    assert params["CheckinsPath"] == str(bundle.source_dir / "Checkins.txt")


@needs_windows_powershell
def test_declining_the_standard_files_asks_for_each_path(tmp_path):
    bundle = make_bundle(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    answers = ["n"]
    for name in ("c.txt", "r.txt", "a.txt"):
        (other / name).write_text("x", encoding="utf-8")
        answers.append(str(other / name))
    answers.append("")

    result = run_scenario(bundle, answers=answers)

    assert result.exit_code == 0, result.output
    assert result.calls_to("install")[0]["params"]["CheckinsPath"] == str(other / "c.txt")


@needs_windows_powershell
def test_missing_standard_files_are_never_silently_accepted_and_stop_before_the_code(tmp_path):
    bundle = make_bundle(tmp_path, sources_present=False)

    result = run_scenario(bundle, answers=[""])  # Enter alone = give up

    assert result.exit_code == 1
    assert result.calls_to("http") == [] and result.calls_to("read-code") == [] and result.calls_to("install") == []
    assert "was not found" in result.output and "enrollment code was not requested" in result.output


@needs_windows_powershell
def test_a_missing_file_can_be_corrected_interactively(tmp_path):
    bundle = make_bundle(tmp_path, sources_present=False)
    real = tmp_path / "found"
    real.mkdir()
    answers = []
    for name in ("c.txt", "r.txt", "a.txt"):
        (real / name).write_text("x", encoding="utf-8")
        answers.append(str(real / name))
    answers.append("")

    result = run_scenario(bundle, answers=answers)

    assert result.exit_code == 0, result.output
    assert result.calls_to("install")[0]["params"]["AcsPath"] == str(real / "a.txt")


@needs_windows_powershell
def test_a_missing_explicit_file_stops_the_run(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[], args=f"-AcsPath {ps_quote(tmp_path / 'nope.txt')}")

    assert result.exit_code == 1 and result.calls_to("http") == []
    assert "acs source file was not found" in result.output


@needs_windows_powershell
def test_a_relative_source_path_is_refused(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[], args="-CheckinsPath 'Checkins.txt'")

    assert result.exit_code == 1 and result.calls_to("http") == []
    assert "not an absolute path" in result.output


@needs_windows_powershell
def test_a_directory_is_not_accepted_as_a_source_file(tmp_path):
    bundle = make_bundle(tmp_path)
    (tmp_path / "adir").mkdir()

    result = run_scenario(bundle, answers=[], args=f"-RejectsPath {ps_quote(tmp_path / 'adir')}")

    assert result.exit_code == 1 and result.calls_to("http") == []


# ---- elevation -------------------------------------------------------------------------------------------------

@needs_windows_powershell
def test_a_non_administrator_is_refused_before_anything_at_all(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, admin=False)

    assert result.exit_code == 1
    assert result.calls == []  # not even the read-only checks, and no code prompt
    assert "elevated (Administrator)" in result.output and "nothing was touched" in result.output
    assert not bundle.install_root.exists() and not bundle.data_root.exists()


@needs_windows_powershell
def test_the_real_script_refuses_a_non_elevated_session_with_a_nonzero_exit(tmp_path):
    check = _run_ps("([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrator')")
    if check.stdout.strip().lower() == "true":
        pytest.skip("this test session is elevated; the refusal is covered by the stubbed non-admin test")
    bundle = make_bundle(tmp_path)

    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(bundle.root / "setup.ps1"),
         "-InstallRoot", str(bundle.install_root), "-DataRoot", str(bundle.data_root)],
        capture_output=True, text=True, timeout=120, check=False,
    )

    assert completed.returncode == 1
    assert "elevated (Administrator)" in completed.stdout
    assert not bundle.install_root.exists() and not bundle.data_root.exists()


# ---- HTTP / TLS failures ----------------------------------------------------------------------------------------

@needs_windows_powershell
def test_an_unreachable_api_stops_before_the_code_is_requested_or_consumed(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], http={"Get /": {"status": 0, "content": None, "failure": "no response (WebException)"}})

    assert result.exit_code == 1
    assert result.tools() == ["install-verify", "runtime-version", "http:GET"]  # no code prompt, no POST
    assert "enrollment code was NOT used" in result.output and "Could not reach" in result.output
    assert result.machine_token is None and not bundle.install_root.exists()


@needs_windows_powershell
def test_no_response_during_enrollment_is_reported_without_installing_anything(tmp_path):
    bundle = make_bundle(tmp_path)
    http = default_http(**{"Post /collector/enroll": {"status": 0, "content": None, "failure": "no response (WebException)"}})

    result = run_scenario(bundle, answers=[""], http=http)

    assert result.exit_code == 1
    assert result.calls_to("set-api-token") == [] and result.calls_to("install") == []
    assert "No response was received from SortView" in result.output and result.machine_token is None
    assert_no_secret_anywhere_visible(result)


@needs_windows_powershell
@pytest.mark.parametrize(
    ("status", "fragment"),
    [(400, "not accepted"), (429, "limiting requests"), (302, "redirected"), (500, "reported an error"),
     (503, "reported an error"), (404, "unexpected status"), (401, "unexpected status")],
)
def test_error_statuses_fail_closed_with_a_useful_message_and_install_nothing(tmp_path, status, fragment):
    bundle = make_bundle(tmp_path)
    http = default_http(**{"Post /collector/enroll": {"status": status, "content": json.dumps({"detail": "Invalid or expired enrollment code"}), "failure": f"HTTP {status}"}})

    result = run_scenario(bundle, answers=[""], http=http)

    assert result.exit_code == 1
    assert fragment in result.output
    assert result.calls_to("set-api-token") == [] and result.calls_to("install") == [] and result.calls_to("finish-install") == []
    assert result.machine_token is None and not bundle.install_root.exists()
    assert result.task is None


@needs_windows_powershell
def test_a_rejected_code_enrolled_nothing_and_a_new_code_is_advised_only_if_no_earlier_attempt_was_unknown(tmp_path):
    bundle = make_bundle(tmp_path)
    http = default_http(**{"Post /collector/enroll": {"status": 400, "content": "{}", "failure": "HTTP 400"}})

    result = run_scenario(bundle, answers=[""], http=http)

    assert result.exit_code == 1 and "no token was stored" in result.output
    # What is known: THIS request enrolled nothing. What is not claimed: that the code was never used -- a 400 is also
    # what a code already used by an EARLIER request (whose answer was lost) gets.
    assert "SortView did not accept the enrollment code; this request enrolled nothing." in result.output
    assert "The enrollment code was NOT used." not in result.output
    # A new code is still the right advice for a mistyped or expired code -- but only after the other case is ruled out.
    advice = result.output[result.output.index("What to do:"):]
    assert advice.index("If an earlier setup attempt with this code") < advice.index("ask your SortView administrator for a new enrollment code")
    assert "contact SortView support and do NOT ask for a new enrollment code" in advice


@needs_windows_powershell
@pytest.mark.parametrize("case", list(_MALFORMED_BODIES))
def test_a_malformed_successful_response_fails_closed_and_never_echoes_the_token(tmp_path, case):
    bundle = make_bundle(tmp_path)
    http = default_http(**{"Post /collector/enroll": {"status": 200, "content": _MALFORMED_BODIES[case], "failure": None}})

    result = run_scenario(bundle, answers=[""], http=http)

    assert result.exit_code == 1, (case, result.output)
    assert result.calls_to("set-api-token") == [] and result.calls_to("install") == [] and result.calls_to("finish-install") == []
    assert result.machine_token is None and not bundle.install_root.exists() and result.task is None
    assert "response was not valid" in result.output
    assert "may or may not have been used" in result.output  # honest: the server may have consumed it
    assert SENTINEL_TOKEN not in result.output  # even when the malformed body itself contained a token


@needs_windows_powershell
def test_no_code_available_stops_without_installing(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_NO_CODE": "1"})

    assert result.exit_code == 1 and result.calls_to("http")[-1]["method"] == "Get"
    assert "No enrollment code was entered" in result.output
    assert result.calls_to("install") == []


# ---- later local failures --------------------------------------------------------------------------------------------

@needs_windows_powershell
def test_a_token_storage_failure_stops_before_installing_and_says_the_code_is_used(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_TOKEN_MODE": "fail"})

    assert result.exit_code == 1
    assert result.calls_to("install") == [] and result.machine_token is None
    assert "HAS BEEN USED" in result.output and "was NOT stored" in result.output and "new enrollment code" in result.output
    assert SENTINEL_TOKEN not in result.output


@needs_windows_powershell
def test_a_stored_token_the_collector_cannot_verify_stops_before_installing(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_CHECK_MODE": "fail"})

    assert result.exit_code == 1 and result.calls_to("install") == []
    assert "could not verify the stored API token" in result.output


@needs_windows_powershell
def test_an_installer_failure_stops_the_run_and_never_registers_or_enables_a_task(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_INSTALL_MODE": "fail"})

    assert result.exit_code == 3  # install.ps1's own exit code is passed through
    assert result.calls_to("finish-install") == [] and result.task is None and result.calls_to("enable-task") == []
    assert "HAS BEEN USED" in result.output and "IS stored on this machine" in result.output
    assert "RESUME" in result.output and "no new enrollment code is needed" in result.output  # the saved enrollment
    assert result.recovery is not None
    assert not bundle.config_path.exists()
    assert_no_secret_anywhere_visible(result)


_FINISH_FAILURES = {
    "fail_interactive_preflight": 1,
    "fail_system_preflight": 1,
    "fail_bootstrap": 1,
    "incomplete_state": 2,
    "fail_task_registration": 1,
}


@needs_windows_powershell
@pytest.mark.parametrize("mode", list(_FINISH_FAILURES))
def test_every_verification_failure_stops_setup_and_never_enables_the_task(tmp_path, mode):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_FINISH_MODE": mode})

    assert result.exit_code == _FINISH_FAILURES[mode]  # the tool's own exit code is passed through
    assert result.calls_to("enable-task") == []
    assert result.task is None or result.task["State"] == "Disabled"
    assert "SETUP STOPPED at step 8 (verification)" in result.output
    assert "tools\\finish-install.ps1" in result.output  # the resume instruction
    assert "no new enrollment code is needed" in result.output
    assert "IS stored on this machine" in result.output and "has not been enabled" in result.output
    assert bundle.config_path.exists()  # nothing installed was deleted
    assert_no_secret_anywhere_visible(result)


@needs_windows_powershell
@pytest.mark.parametrize("mode", list(_FINISH_FAILURES))
def test_even_the_explicit_enable_switch_cannot_enable_after_a_failure(tmp_path, mode):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, args="-EnableTask", env={"SETUPTEST_FINISH_MODE": mode})

    assert result.exit_code != 0
    assert result.calls_to("enable-task") == []
    assert result.task is None or result.task["State"] == "Disabled"
    assert not any("Enable the scheduled task" in p for p in result.prompts())


@needs_windows_powershell
def test_a_task_left_enabled_by_a_failed_step_is_disabled_by_setup(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_FINISH_MODE": "rogue_enabled_task_then_fail"})

    assert result.exit_code == 1
    assert len(result.calls_to("disable-task")) == 1 and result.calls_to("enable-task") == []
    assert result.task == {"State": "Disabled"}
    assert "has been DISABLED" in result.output


@needs_windows_powershell
def test_a_task_that_is_not_disabled_after_a_reportedly_successful_finish_is_refused_and_disabled(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_FINISH_MODE": "ok_but_task_enabled"})

    assert result.exit_code == 2
    assert result.task == {"State": "Disabled"} and len(result.calls_to("disable-task")) == 1
    assert result.calls_to("enable-task") == []


@needs_windows_powershell
def test_a_finish_that_registered_no_task_is_a_failure_not_a_success(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_FINISH_MODE": "ok_but_no_task"})

    assert result.exit_code == 1 and "does not exist" in result.output
    assert result.calls_to("enable-task") == [] and not any("Enable the scheduled task" in p for p in result.prompts())


@needs_windows_powershell
def test_a_pre_existing_live_task_is_refused_and_never_touched(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, task_state="Ready")  # a running customer install

    assert result.exit_code == 2
    assert result.task == {"State": "Ready"}  # NOT disabled by the cleanup: it was never ours
    assert result.calls_to("disable-task") == [] and result.calls_to("enable-task") == []
    assert result.calls_to("http") == [] and result.calls_to("read-code") == []
    assert "tools\\update.ps1" in result.output


# ---- completion and the enable decision ---------------------------------------------------------------------------

@needs_windows_powershell
@pytest.mark.parametrize("answer", ["", "n", "no", "N", "maybe", "yes please", "  ", "enable"])
def test_only_an_explicit_yes_enables_and_the_default_never_does(tmp_path, answer):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", answer])

    assert result.exit_code == 0, result.output
    assert result.calls_to("enable-task") == []
    assert result.task == {"State": "Disabled"}
    assert "left DISABLED" in result.output


@needs_windows_powershell
def test_pressing_enter_and_a_noninteractive_host_both_leave_the_task_disabled(tmp_path):
    # Enter at the enable prompt / no answer at all (a host that cannot be asked): both mean "leave it disabled".
    for name, answers in (("enter", ["", ""]), ("cannot_ask", [""])):
        bundle = make_bundle(tmp_path / name)
        result = run_scenario(bundle, answers=answers)
        assert result.exit_code == 0, (name, result.output)
        assert result.calls_to("enable-task") == [], name
        assert result.task == {"State": "Disabled"}, name


@needs_windows_powershell
@pytest.mark.parametrize("answer", ["y", "Y", "yes", " YES "])
def test_an_explicit_yes_enables_only_after_every_step_succeeded(tmp_path, answer):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", answer])

    assert result.exit_code == 0, result.output
    assert result.tools()[-2:] == ["finish-install", "enable-task"]  # strictly after verification
    assert result.task == {"State": "Ready"}
    assert "now ENABLED" in result.output
    assert not any(c["tool"] == "http" and c["method"] == "Post" for c in result.calls[result.calls.index(result.calls_to("enable-task")[0]):])


@needs_windows_powershell
def test_the_enable_switch_enables_without_asking_but_only_after_success(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], args="-EnableTask")

    assert result.exit_code == 0
    assert result.tools()[-1] == "enable-task" and len(result.calls_to("enable-task")) == 1
    assert not any("Enable the scheduled task" in p for p in result.prompts())
    assert result.task == {"State": "Ready"}


@needs_windows_powershell
def test_a_failed_enable_leaves_the_task_disabled_and_reports_failure(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", "y"], env={"SETUPTEST_ENABLE_FAILS": "1"})

    assert result.exit_code == 1
    assert result.task == {"State": "Disabled"}
    assert "Enabling the Scheduled Task failed" in result.output and "Enable-ScheduledTask" in result.output


@needs_windows_powershell
def test_even_when_enabling_setup_starts_no_collector_run(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", "y"])

    assert result.tools() == ["install-verify", "runtime-version", "http:GET", "read-code", "http:POST", "http:POST", "set-api-token",
                              "install", "finish-install", "enable-task"]
    assert "Start-ScheduledTask" in result.output  # only ever printed as optional guidance


# ---- re-runs and partial installs ------------------------------------------------------------------------------------------

def _partial_install(bundle: Bundle, *, exe=True, config=False):
    if exe:
        bundle.install_root.mkdir(parents=True, exist_ok=True)
        (bundle.install_root / "SortViewCollector.exe").write_bytes(b"existing")
    if config:
        (bundle.data_root / "config").mkdir(parents=True, exist_ok=True)
        (bundle.data_root / "config" / "collector_config.json").write_text('{"customer_id": 1}', encoding="utf-8")
        (bundle.data_root / "data").mkdir(exist_ok=True)
        (bundle.data_root / "data" / "state.json").write_text('{"keep": "me"}', encoding="utf-8")


@needs_windows_powershell
@pytest.mark.parametrize(("exe", "config"), [(True, False), (False, True), (True, True)])
def test_an_existing_install_is_refused_with_recovery_steps_and_never_modified(tmp_path, exe, config):
    bundle = make_bundle(tmp_path)
    _partial_install(bundle, exe=exe, config=config)
    before = {p: p.read_bytes() for base in (bundle.install_root, bundle.data_root) if base.exists() for p in base.rglob("*") if p.is_file()}

    result = run_scenario(bundle, machine_token="existing-token-value")

    assert result.exit_code == 2
    assert result.tools() == ["install-verify", "runtime-version"]  # nothing else: no HTTP, no code prompt, no installer
    assert "tools\\finish-install.ps1" in result.output and "tools\\uninstall.ps1" in result.output
    assert "NEW enrollment code" in result.output and "enrollment code was not requested" in result.output
    assert {p: p.read_bytes() for p in before} == before  # not one byte changed or deleted
    assert result.machine_token == "existing-token-value"


@needs_windows_powershell
def test_rerunning_after_a_finished_setup_is_refused_and_leaves_the_disabled_task_alone(tmp_path):
    bundle = make_bundle(tmp_path)
    first = run_scenario(bundle, answers=["", ""])
    assert first.exit_code == 0
    calls_before = len(first.calls)

    second = run_scenario(bundle, answers=[])

    assert second.exit_code == 2
    assert second.task == {"State": "Disabled"}
    # The fake call log accumulates across runs: the rerun added no installer call, no HTTP call and no code prompt.
    assert [c["tool"] for c in second.calls[calls_before:] if c["tool"] != "prompt"] == ["install-verify", "runtime-version"]
    assert len(second.calls_to("install")) == 1 and len(second.calls_to("read-code")) == 1


@needs_windows_powershell
def test_an_unfinished_setup_with_a_disabled_task_is_partial_not_live(tmp_path):
    bundle = make_bundle(tmp_path)
    _partial_install(bundle, config=True)

    result = run_scenario(bundle, task_state="Disabled")

    assert result.exit_code == 2
    assert "An earlier setup may be unfinished" in result.output and "tools\\finish-install.ps1" in result.output
    assert result.task == {"State": "Disabled"}


@needs_windows_powershell
def test_an_unreadable_machine_state_is_refused_not_assumed_clean(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, extra_ps="function Get-SortViewTask { throw 'Task Scheduler unavailable' }\n")

    assert result.exit_code == 2 and result.calls_to("http") == []
    assert "Could not inspect this machine" in result.output


@needs_windows_powershell
def test_an_existing_api_token_is_refused_on_a_fresh_run_and_never_offered_for_replacement(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["y", "y", "y"], machine_token="another-installations-token")  # "y" to anything asked

    assert result.exit_code == 2
    assert result.machine_token == "another-installations-token"  # untouched
    assert result.calls_to("http") == [] and result.calls_to("read-code") == []  # before the network and the code
    assert not any("eplace" in prompt for prompt in result.prompts())  # replacing it is not even offered
    assert "an API token at" in result.output and "the enrollment code was not requested" in result.output
    assert "another-installations-token" not in result.output  # the existing value is never shown
    assert result.calls_to("set-api-token") == [] and not bundle.install_root.exists()


@needs_windows_powershell
def test_there_is_no_switch_that_makes_a_fresh_run_replace_a_stored_token(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""], machine_token="leftover", args="-ReplaceExistingToken")

    assert result.exit_code != 0 and result.machine_token == "leftover"
    assert result.calls_to("read-code") == [] and result.calls_to("set-api-token") == []





@needs_windows_powershell
def test_a_noninteractive_host_never_replaces_an_existing_token(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=None, machine_token="existing")

    assert result.exit_code == 2 and result.machine_token == "existing"


# ---- contracts with the real tools -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("script", "names"),
    [
        (INSTALL_SOURCE, {"CustomerId", "BranchId", "InstallationId", "ApiUrl", "CheckinsPath", "RejectsPath", "AcsPath",
                          "InstallRoot", "DataRoot", "SuppressNextSteps"}),
        (FINISH_SOURCE, {"InstallRoot", "ConfigPath", "UseExistingMachineToken"}),
        (TOKEN_SOURCE, {"Token"}),
    ],
)
def test_every_parameter_setup_passes_exists_on_the_real_tool(script, names):
    declared = set(re.findall(r"\]\$([A-Za-z]+)", _param_block(script)))
    assert names <= declared


def test_install_parameters_are_typed_so_the_returned_ids_bind_as_integers():
    block = _param_block(INSTALL_SOURCE)
    for name in ("CustomerId", "BranchId", "InstallationId"):
        assert f"[Nullable[int]]${name}" in block


def test_stub_param_blocks_are_the_real_ones():
    assert "$SuppressNextSteps" in _param_block(INSTALL_SOURCE)
    assert "[SecureString]$Token" in _param_block(TOKEN_SOURCE)
    assert "$UseExistingMachineToken" in _param_block(FINISH_SOURCE)


@needs_windows_powershell
def test_every_shipped_script_still_parses_without_errors():
    for path in (SETUP_SOURCE, INSTALL_SOURCE, TOKEN_SOURCE, FINISH_SOURCE):
        script = (
            "$errs = $null; $tokens = $null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile({ps_quote(path)}, [ref]$tokens, [ref]$errs); "
            "$errs.Count"
        )
        assert _run_ps(script).stdout.strip() == "0", path.name


@needs_windows_powershell
def test_the_real_installers_config_generator_writes_the_returned_ids_and_never_a_token():
    generate = _ps_function("New-CollectorConfigJson", _read(INSTALL_SOURCE))
    text = _run_ps(
        generate + "\nNew-CollectorConfigJson -CustomerId 7 -BranchId 3 -InstallationId 41 -ApiUrl 'https://api.example.org' "
        "-CheckinsPath 'C:\\a\\Checkins.txt' -RejectsPath 'C:\\a\\Rejects.txt' -AcsPath 'C:\\a\\ACS Log.txt' -DataRoot 'C:\\Data'"
    ).stdout
    config = json.loads(text)

    assert (config["customer_id"], config["branch_id"], config["installation_id"]) == (7, 3, 41)
    assert "token" not in text.lower()


def _session_is_elevated() -> bool:
    check = _run_ps("([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrator')")
    return check.stdout.strip().lower() == "true"


@needs_windows_powershell
@pytest.mark.parametrize(
    ("script", "arguments"),
    [
        (INSTALL_SOURCE, "-SuppressNextSteps"),
        (FINISH_SOURCE, "-UseExistingMachineToken"),
        (TOKEN_SOURCE, "-Token (ConvertTo-SecureString 'not-a-real-token-value' -AsPlainText -Force)"),
    ],
    ids=["install.ps1", "finish-install.ps1", "set-api-token.ps1"],
)
def test_the_real_scripts_accept_the_new_parameter_and_reach_their_own_elevation_refusal(
    script, arguments, tmp_path
):
    """The REAL scripts (not stubs), run non-elevated: if the new switch/parameter did not exist they would fail
    to bind it; instead each must reach its own elevation check -- with no prompt and no effect on this machine."""
    if _session_is_elevated():
        pytest.skip("this session is elevated: the real scripts would run for real; the stubbed scenarios cover them")

    run_script = script

    if script == INSTALL_SOURCE:
        bundle_root = tmp_path / "bundle"
        tools_dir = bundle_root / "tools"
        tools_dir.mkdir(parents=True)

        run_script = bundle_root / "install.ps1"
        run_script.write_bytes(INSTALL_SOURCE.read_bytes())

        (tools_dir / "CollectorAcl.ps1").write_bytes(
            (DEPLOY / "CollectorAcl.ps1").read_bytes()
        )
        (tools_dir / "CollectorManifest.ps1").write_bytes(
            (DEPLOY / "CollectorManifest.ps1").read_bytes()
        )

    completed = _run_ps(f"& {ps_quote(run_script)} {arguments}; exit $LASTEXITCODE")
    combined = completed.stdout + completed.stderr

    assert "A parameter cannot be found" not in combined and "Cannot process argument" not in combined, combined
    assert "elevated" in combined.lower() or "Administrator" in combined, combined
    assert "not-a-real-token-value" not in combined
    assert "Paste the SortView Collector API token" not in combined


# =====================================================================================
# 4. BUNDLE INTEGRITY FIRST: the release is verified before anything in it is trusted
# =====================================================================================
#
# install.ps1 -VerifyBundleOnly is the ONE implementation of the manifest check (setup.ps1
# has none of its own); setup runs it right after the elevation check. The stubbed scenarios
# prove the ORDER; the real-bundle tests below run the REAL verifier against a genuine bundle
# built by build_release, then tamper with it.

RECOVERY_KEYS = {"schema_version", "created_utc", "release_version", "api_url", "customer_id", "branch_id", "installation_id"}
TEMPLATE_URL = json.loads(_read(EXAMPLE_CONFIG))["api_url"].rstrip("/")


def test_setup_has_no_manifest_verification_of_its_own():
    assert "function Test-ReleaseManifest" not in SETUP_BODY
    # The one hash setup computes is of the classification rules FILE it staged (to tie that file to the saved
    # enrollment): a single wrapper, called only by the rules functions -- never on anything in the bundle.
    assert SETUP_BODY.count("Get-FileHash") == 1 and "Get-FileHash" in _ps_function("Get-FileSha256")
    hashed = re.findall(r"Get-FileSha256 -Path (\$\w+)", SETUP_BODY)
    assert hashed and set(hashed) <= {"$path", "$live", "$staged"}, hashed
    for name in ("Save-StagedRules", "Get-RulesResumeState", "Install-ClassificationRules"):
        assert "Get-FileSha256" in _ps_function(name)
    assert "bundleRoot" not in "".join(_ps_function(name) for name in ("Get-FileSha256", "Save-StagedRules", "Get-RulesResumeState",
                                                                        "Install-ClassificationRules"))
    # MANIFEST.json is read for one thing only -- the version -- and only AFTER the real verifier has passed.
    reads = [m.start() for m in re.finditer(r"MANIFEST\.json", SETUP_BODY)]
    code_reads = [i for i in reads if "Get-Content" in SETUP_BODY[max(0, i - 120): i + 20]]
    verify = SETUP_BODY.index("VerifyBundleOnly = $true")
    assert code_reads and all(i > verify for i in code_reads)


def test_the_bundle_is_verified_before_anything_in_it_is_read_and_before_any_network_or_code_step():
    run = SETUP_BODY[SETUP_BODY.index("function Invoke-SetupMain"):]
    order = [
        "Test-IsAdministrator",
        "VerifyBundleOnly = $true",
        "Get-RuntimeVersion",
        "Get-BundleDefaults",           # the API address is read only after verification
        "Get-ExistingInstallProblem",
        "Get-SourceSelection",
        "Test-ApiReachable",            # the first network request
        "Read-EnrollmentCodeSecure",    # the code is requested last
        "Invoke-Enrollment -ApiUrl",
    ]
    positions = [run.index(marker) for marker in order]
    assert positions == sorted(positions), dict(zip(order, positions))
    assert "Test-EnrollmentApiUrl -Url $ApiUrl" in run[positions[1]:]  # even the -ApiUrl override is judged after


@needs_windows_powershell
def test_the_bundle_is_verified_first_with_no_ids_and_nothing_else_happens_before_it(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])

    assert result.tools()[0] == "install-verify"
    verify = result.calls_to("install-verify")[0]
    assert verify["params"] == {"VerifyBundleOnly": True}  # the switch alone: no CustomerId/BranchId/InstallationId needed
    assert result.tools().index("install-verify") < result.tools().index("runtime-version") < result.tools().index("http:GET")


@needs_windows_powershell
def test_a_bundle_that_fails_verification_stops_everything_before_any_network_or_code_request(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_VERIFY_MODE": "fail"})

    assert result.exit_code == 1
    assert result.tools() == ["install-verify"]  # not even the runtime-version check, no HTTP, no code prompt
    assert "failed verification against MANIFEST.json" in result.output
    assert "no network request was made" in result.output and "enrollment code was not requested" in result.output
    assert not bundle.install_root.exists() and not bundle.data_root.exists() and result.machine_token is None


@needs_windows_powershell
def test_a_missing_install_script_is_refused_before_anything_else(tmp_path):
    bundle = make_bundle(tmp_path)
    (bundle.root / "install.ps1").unlink()

    result = run_scenario(bundle)

    assert result.exit_code == 1 and result.calls == []
    assert "cannot be verified" in result.output


# ---- the REAL verifier, against a genuine bundle --------------------------------------------------------

def _tamper_modify_a_tool(root: Path):
    (root / "tools" / "register-task.ps1").write_text("# tampered\n", encoding="utf-8")


def _tamper_modify_the_api_address(root: Path):
    # The exact trust gap: point the bundle's canonical API URL at somewhere else.
    config = root / "collector_config.example.json"
    document = json.loads(config.read_text(encoding="utf-8"))
    document["api_url"] = "https://evil.example.org"
    config.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


def _tamper_modify_the_runtime(root: Path):
    (root / "runtime" / "_internal" / "python.dll").write_bytes(b"swapped-dll")


def _tamper_add_an_unlisted_file(root: Path):
    (root / "runtime" / "_internal" / "unlisted.dll").write_bytes(b"not in the manifest")


def _tamper_add_an_unlisted_script(root: Path):
    (root / "tools" / "extra.ps1").write_text("Write-Host 'hi'\n", encoding="utf-8")


def _tamper_delete_a_listed_file(root: Path):
    (root / "tools" / "uninstall.ps1").unlink()


def _tamper_modify_the_installer(root: Path):
    with (root / "install.ps1").open("ab") as handle:
        handle.write(b"\n# tampered\n")


_TAMPERS = {
    "modified_tool": _tamper_modify_a_tool,
    "modified_api_address": _tamper_modify_the_api_address,
    "modified_runtime": _tamper_modify_the_runtime,
    "unlisted_runtime_file": _tamper_add_an_unlisted_file,
    "unlisted_script": _tamper_add_an_unlisted_script,
    "deleted_listed_file": _tamper_delete_a_listed_file,
    "modified_installer": _tamper_modify_the_installer,
}


@needs_windows_powershell
def test_an_untampered_real_bundle_passes_the_real_verification_and_setup_proceeds_to_the_connection_step(tmp_path):
    bundle = real_bundle(tmp_path)
    http = default_http(**{"Post /collector/enroll": {"status": 400, "content": "{}", "failure": "HTTP 400"}})

    result = run_scenario(bundle, args=source_args(bundle), http=http, answers=[])

    assert "Manifest verified:" in result.output and "every file in the bundle matches MANIFEST.json" in result.output
    # Verification passed, so the run went on to the connection test and the code (stopping at a scripted refusal).
    assert result.tools() == ["runtime-version", "http:GET", "read-code", "http:POST"]
    assert result.calls_to("set-api-token") == [] and result.machine_token is None


@needs_windows_powershell
@pytest.mark.parametrize("tamper", list(_TAMPERS))
def test_a_tampered_real_bundle_stops_setup_before_any_http_request_or_code(tmp_path, tamper):
    bundle = real_bundle(tmp_path)
    _TAMPERS[tamper](bundle.root)

    result = run_scenario(bundle, args=source_args(bundle), answers=[])

    assert result.exit_code == 1, (tamper, result.output)
    assert result.calls == []  # not one HTTP call, no code prompt, no runtime check, no token, no installer
    assert "MANIFEST VERIFICATION FAILED" in result.output  # the real verifier's own message
    assert "failed verification against MANIFEST.json" in result.output
    assert "no network request was made" in result.output and "enrollment code was not requested" in result.output
    assert result.machine_token is None and not bundle.install_root.exists() and not bundle.data_root.exists()
    assert "evil.example.org" not in json.dumps(result.calls)  # the swapped address was never contacted


@needs_windows_powershell
def test_a_tampered_api_address_is_never_contacted_even_though_the_code_prompt_would_follow(tmp_path):
    bundle = real_bundle(tmp_path)
    _tamper_modify_the_api_address(bundle.root)
    http = default_http()

    result = run_scenario(bundle, args=source_args(bundle), http=http, answers=[])

    assert result.calls_to("http") == [] and result.calls_to("read-code") == []
    assert SENTINEL_CODE not in result.output


@needs_windows_powershell
def test_the_real_verify_only_mode_needs_no_ids_changes_nothing_and_passes_an_intact_bundle(tmp_path):
    bundle = real_bundle(tmp_path)
    before = {p: p.read_bytes() for p in bundle.root.rglob("*") if p.is_file()}

    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(bundle.root / "install.ps1"),
         "-VerifyBundleOnly", "-InstallRoot", str(bundle.install_root), "-DataRoot", str(bundle.data_root)],
        capture_output=True, text=True, timeout=120, check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Manifest verified:" in completed.stdout and "nothing was installed or changed" in completed.stdout
    assert {p: p.read_bytes() for p in bundle.root.rglob("*") if p.is_file()} == before  # not a byte changed
    assert not bundle.install_root.exists() and not bundle.data_root.exists()


@needs_windows_powershell
@pytest.mark.parametrize("tamper", list(_TAMPERS))
def test_the_real_verify_only_mode_fails_a_tampered_bundle(tmp_path, tamper):
    bundle = real_bundle(tmp_path)
    _TAMPERS[tamper](bundle.root)

    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(bundle.root / "install.ps1"),
         "-VerifyBundleOnly"],
        capture_output=True, text=True, timeout=120, check=False,
    )

    assert completed.returncode != 0, tamper
    assert "MANIFEST VERIFICATION FAILED" in completed.stdout + completed.stderr or "Refusing to proceed" in completed.stdout + completed.stderr


def test_verify_only_mode_is_read_only_ends_right_after_the_manifest_check_and_skips_only_elevation_and_inputs():
    text = _read(INSTALL_SOURCE)

    assert "[switch]$VerifyBundleOnly" in text
    verify_call = text.index("Test-ReleaseManifest -BundleRoot $BundleRoot")
    exit_block = text.index("if ($VerifyBundleOnly) {", verify_call)
    assert exit_block - verify_call < 120  # immediately after the verification
    block = text[exit_block: text.index("$ConfigPath = Join-Path", exit_block)]
    assert "exit 0" in block
    # Nothing that changes the machine runs between the start of the script body and that exit.
    body_before_exit = text[text.index("$ErrorActionPreference"): exit_block]
    for mutating in ("New-Item", "Copy-Item", "Remove-Item", "Set-Content", "WriteAllText", "& $PythonExe", "Get-ScheduledTask -TaskName"):
        assert mutating not in body_before_exit, mutating
    # It skips exactly two things, and nothing else: its own elevation check and the ID validation.
    assert "if (-not $VerifyBundleOnly -and -not $currentPrincipal.IsInRole" in text
    assert "if (-not $VerifyBundleOnly) {\n    $inputProblems = @(Get-InstallInputProblems" in text.replace("\r\n", "\n")
    assert text.count("$VerifyBundleOnly") == 4  # the declaration, the elevation exemption, the validation skip, the exit


# =====================================================================================
# 5. RESUME: a used enrollment code never has to be replaced because of a local failure
# =====================================================================================

def write_recovery(bundle: Bundle, **overrides) -> Path:
    record = {"schema_version": 1, "created_utc": "2026-09-20T12:00:00Z", "release_version": RELEASE_VERSION,
              "api_url": TEMPLATE_URL, "customer_id": 7, "branch_id": 3, "installation_id": 41}
    record.update(overrides)
    for key in [k for k, v in record.items() if v is _DROP]:
        del record[key]
    path = bundle.data_root / "setup" / "enrollment-recovery.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


_DROP = object()


def write_installed(bundle: Bundle, *, exe: bool = True, config: bool = True, **overrides) -> None:
    """An install already on disk, as install.ps1 would have left it."""
    if exe:
        bundle.install_root.mkdir(parents=True, exist_ok=True)
        (bundle.install_root / "SortViewCollector.exe").write_bytes(b"existing-runtime")
    if config:
        document = {"customer_id": 7, "branch_id": 3, "installation_id": 41, "api_url": TEMPLATE_URL,
                    "sources": [{"name": "checkins", "path": str(bundle.source_dir / "Checkins.txt")}]}
        document.update(overrides)
        (bundle.data_root / "config").mkdir(parents=True, exist_ok=True)
        bundle.config_path.write_text(json.dumps(document), encoding="utf-8")


def _snapshot(*bases: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for base in bases if base.exists() for p in base.rglob("*") if p.is_file()}


def new_calls(result: Result, before: int) -> list[dict]:
    return [c for c in result.calls[before:] if c["tool"] != "prompt"]


@needs_windows_powershell
def test_the_record_is_saved_right_after_redemption_and_holds_only_non_secret_details(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_INSTALL_MODE": "fail"})  # a failure immediately afterwards

    assert result.exit_code == 3
    assert result.machine_token == SENTINEL_TOKEN  # the token was stored first
    record = result.recovery
    assert record is not None and set(record) == RECOVERY_KEYS
    assert record["schema_version"] == 1
    assert (record["customer_id"], record["branch_id"], record["installation_id"]) == (7, 3, 41)
    assert record["api_url"] == TEMPLATE_URL and record["release_version"] == RELEASE_VERSION
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", record["created_utc"])
    raw = result.recovery_path.read_text(encoding="utf-8")
    for secret in (SENTINEL_TOKEN, SENTINEL_CODE, SENTINEL_CODE.replace("-", ""), "agent_token", "enrollment_code"):
        assert secret not in raw, secret
    assert_no_secret_anywhere_visible(result)
    # It is in the ProgramData hierarchy, and its folder was locked down BEFORE anything was written into it.
    assert result.recovery_path.parent == bundle.data_root / "setup"
    # (twice: when the folder was prepared and probed BEFORE the code was requested, and again when the record was saved)
    protects = result.calls_to("protect-recovery")
    assert len(protects) == 2 and {p["path"] for p in protects} == {str(bundle.data_root / "setup")}
    assert result.tools().index("install-verify") < [c["tool"] for c in result.calls].index("protect-recovery")
    assert result.tools().index("set-api-token") < result.tools().index("install")  # (token first, then install)


@needs_windows_powershell
def test_the_stop_after_redemption_tells_the_technician_to_rerun_setup_not_to_get_a_new_code(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_INSTALL_MODE": "fail"})

    assert "HAS BEEN USED" in result.output and "IS stored on this machine" in result.output
    assert "run setup.ps1 again to RESUME" in result.output and "no new enrollment code is needed" in result.output
    assert str(result.recovery_path) in result.output
    assert "NEW enrollment code" not in result.output.replace("NEW enrollment code will be needed", "")


@needs_windows_powershell
def test_rerunning_setup_resumes_without_another_enrollment_request(tmp_path):
    bundle = make_bundle(tmp_path)
    first = run_scenario(bundle, answers=[""], env={"SETUPTEST_INSTALL_MODE": "fail"})
    assert first.exit_code == 3
    before = len(first.calls)

    second = run_scenario(bundle, answers=["", "", ""])  # resume, use the standard files, leave the task disabled

    assert second.exit_code == 0, second.output
    fresh = new_calls(second, before)
    assert [c["tool"] for c in fresh] == ["install-verify", "runtime-version", "http", "install", "finish-install"]
    (http,) = [c for c in fresh if c["tool"] == "http"]
    assert http["method"] == "Get"  # only the reachability check: NO enrollment request
    assert not any(c["tool"] in ("read-code", "set-api-token") for c in fresh)  # no code prompt, token untouched
    (install,) = [c for c in fresh if c["tool"] == "install"]
    assert (install["params"]["CustomerId"], install["params"]["BranchId"], install["params"]["InstallationId"]) == (7, 3, 41)
    assert install["params"]["ApiUrl"] == TEMPLATE_URL
    assert "Force" not in install["params"]  # never -Force
    assert second.machine_token == SENTINEL_TOKEN and second.task == {"State": "Disabled"}
    assert "resuming" in second.output.lower() and "no enrollment request will be made" in second.output
    assert_no_secret_anywhere_visible(second)


@needs_windows_powershell
def test_a_failure_after_the_install_resumes_at_verification_without_reinstalling(tmp_path):
    bundle = make_bundle(tmp_path)
    first = run_scenario(bundle, answers=[""], env={"SETUPTEST_FINISH_MODE": "fail_bootstrap"})
    assert first.exit_code == 1 and first.recovery is not None and bundle.config_path.exists()
    before = len(first.calls)

    second = run_scenario(bundle, answers=["", ""])  # resume, leave the task disabled

    assert second.exit_code == 0, second.output
    fresh = new_calls(second, before)
    assert [c["tool"] for c in fresh] == ["install-verify", "runtime-version", "http", "finish-install"]  # NO install
    assert fresh[2]["method"] == "Get"
    assert second.task == {"State": "Disabled"}
    assert "only verification remains" in second.output
    assert not second.recovery_path.exists()  # cleared on completion


@needs_windows_powershell
def test_resuming_records_the_installation_the_first_run_used_and_reuses_the_stored_token(tmp_path):
    bundle = make_bundle(tmp_path)
    first = run_scenario(bundle, answers=[""], env={"SETUPTEST_INSTALL_MODE": "fail"})
    token_before = first.machine_token

    second = run_scenario(bundle, answers=["", "", ""])

    assert second.machine_token == token_before == SENTINEL_TOKEN  # not replaced, not re-issued
    finish = [c for c in second.calls_to("finish-install")][-1]
    assert finish["params"]["UseExistingMachineToken"] is True


def _tampered_save(transform: str) -> str:
    """PowerShell replacing Save-SetupRecovery with a writer that leaves a wrong or damaged record on disk (as a bad
    disk or a truncated write would). $Json is the record setup built; `transform` computes what is written instead."""
    return (
        "function Save-SetupRecovery { param([string]$DataRoot, [string]$Json)\n"
        "  $dir = Join-Path $DataRoot 'setup'; [System.IO.Directory]::CreateDirectory($dir) | Out-Null\n"
        "  $path = Join-Path $dir 'enrollment-recovery.json'\n"
        f"  $text = {transform}\n"
        "  [System.IO.File]::WriteAllText($path, $text)\n"
        "  return $path }\n"
    )


_RECORD_STEP = "step 6 (saving the enrollment details)"
_TOKEN_STORED_ONLY = ["install-verify", "runtime-version", "http:GET", "read-code", "http:POST", "http:POST", "set-api-token"]


def _assert_stopped_at_the_record_gate(result: Result, *, problem: str) -> None:
    """Redeemed and token stored -- and then NOTHING else: no install, no finish, no task, no record left behind."""
    assert result.bundle is not None

    assert result.exit_code == 1, result.output
    assert result.tools() == _TOKEN_STORED_ONLY  # install.ps1 was never started
    assert result.calls_to("install") == [] and result.calls_to("finish-install") == [] and result.calls_to("enable-task") == []
    assert result.machine_token == SENTINEL_TOKEN  # the token is still stored, so it is not lost
    assert result.task is None  # no Scheduled Task was created or touched
    assert not result.bundle.install_root.exists() and not result.bundle.config_path.exists()  # nothing installed
    assert f"SETUP STOPPED at {_RECORD_STEP}" in result.output and problem in result.output
    assert "The one-time enrollment code HAS BEEN USED" in result.output
    assert "The permanent API token IS stored on this machine" in result.output
    assert "NEW enrollment code" in result.output and "the API token that was just stored is still stored" in result.output
    assert "WARNING" not in result.output and "Setup continues" not in result.output  # a stop, not a warning
    assert_no_secret_anywhere_visible(result)  # neither the token nor the code, on the console or in any file


@needs_windows_powershell
def test_a_failure_to_write_the_record_stops_setup_before_install_and_keeps_the_token(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], extra_ps="function Save-SetupRecovery { throw 'disk full' }\n")

    _assert_stopped_at_the_record_gate(result, problem="could not be saved (disk full)")
    assert not result.recovery_path.exists()


@needs_windows_powershell
def test_a_setup_folder_that_cannot_be_restricted_stops_setup_before_the_code_is_requested(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_PROTECT_FAILS": "1"})

    assert result.exit_code == 1 and "SETUP STOPPED at step 4 (setup folder)" in result.output
    assert "could not restrict the folder" in result.output and "the enrollment code was NOT requested" in result.output
    assert result.calls_to("read-code") == [] and [c for c in result.calls_to("http") if c["method"] == "Post"] == []
    assert len(result.calls_to("protect-recovery")) == 1
    assert result.machine_token is None and result.calls_to("install") == []
    assert not result.recovery_path.exists()  # nothing was written into a folder that is not locked down


@needs_windows_powershell
def test_a_real_filesystem_failure_creating_the_setup_folder_stops_before_the_code_and_touches_nothing_else(tmp_path):
    bundle = make_bundle(tmp_path)
    bundle.data_root.mkdir(parents=True, exist_ok=True)
    blocker = bundle.data_root / "setup"
    blocker.write_text("something else that lives here", encoding="utf-8")  # a FILE where the folder must go

    result = run_scenario(bundle, answers=[""])

    assert result.exit_code == 1 and "SETUP STOPPED at step 4 (setup folder)" in result.output
    assert result.calls_to("read-code") == [] and [c for c in result.calls_to("http") if c["method"] == "Post"] == []
    assert "The enrollment code was NOT used." in result.output
    assert result.machine_token is None and not bundle.install_root.exists()
    assert blocker.read_text(encoding="utf-8") == "something else that lives here"  # never deleted or overwritten


_RECORD_DAMAGE = {
    "wrong_installation_id": (r"""($Json -replace '"installation_id":\s*\d+', '"installation_id": 99')""", "installation ID differs"),
    "wrong_customer_id": (r"""($Json -replace '"customer_id":\s*\d+', '"customer_id": 99')""", "customer ID differs"),
    "wrong_branch_id": (r"""($Json -replace '"branch_id":\s*\d+', '"branch_id": 99')""", "branch ID differs"),
    "wrong_api_url": (r"""($Json -replace '"api_url":\s*"[^"]*"', '"api_url": "https://other.example.com"')""", "API address differs"),
    "wrong_release_version": (r"""($Json -replace '"release_version":\s*"[^"]*"', '"release_version": "9.9.9"')""", "release version differs"),
    "not_json": ("'this is not json'", "the record read back is not valid"),
    "empty_file": ("''", "the record read back is not valid"),
    "truncated": ("$Json.Substring(0, 40)", "the record read back is not valid"),
    "unexpected_extra_field": (r"""($Json.TrimEnd().TrimEnd('}') + ', "note": "x" }')""", "unexpected fields"),
}


@needs_windows_powershell
@pytest.mark.parametrize("damage", sorted(_RECORD_DAMAGE))
def test_a_record_that_does_not_verify_stops_setup_before_install_and_is_removed(tmp_path, damage):
    transform, explanation = _RECORD_DAMAGE[damage]
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], extra_ps=_tampered_save(transform))

    _assert_stopped_at_the_record_gate(result, problem="could not be verified")
    assert explanation in result.output
    assert not result.recovery_path.exists()  # an unverified record must not be left to steer a later resume


@needs_windows_powershell
def test_a_record_that_vanishes_after_the_write_does_not_verify(tmp_path):
    bundle = make_bundle(tmp_path)
    nothing_written = "function Save-SetupRecovery { param([string]$DataRoot, [string]$Json) return (Join-Path $DataRoot 'setup\\enrollment-recovery.json') }\n"

    result = run_scenario(bundle, answers=[""], extra_ps=nothing_written)

    _assert_stopped_at_the_record_gate(result, problem="could not be verified (the record is not there after it was written)")


@needs_windows_powershell
def test_a_verification_failure_whose_cleanup_also_fails_still_stops_and_names_the_file_to_delete(tmp_path):
    bundle = make_bundle(tmp_path)
    extra = _tampered_save("'this is not json'") + "function Remove-SetupRecovery { throw 'locked' }\n"

    result = run_scenario(bundle, answers=[""], extra_ps=extra)

    assert result.exit_code == 1 and result.tools() == _TOKEN_STORED_ONLY
    assert "the unverified record could not be removed (locked)" in result.output and "enrollment-recovery.json" in result.output
    assert result.machine_token == SENTINEL_TOKEN and result.task is None
    assert result.calls_to("install") == [] and result.calls_to("finish-install") == []
    # ...and because it is unusable, a rerun refuses it instead of resuming from it (before any network call).
    before = len(result.calls)
    again = run_scenario(bundle, answers=[""], extra_ps="")
    assert again.exit_code == 2 and "not valid" in again.output
    assert [c["tool"] for c in new_calls(again, before)] == ["install-verify", "runtime-version"]


@needs_windows_powershell
def test_after_a_legacy_record_gate_stop_the_stored_token_is_never_replaced_until_the_operator_removes_it(tmp_path):
    bundle = make_bundle(tmp_path)
    first = run_scenario(bundle, answers=[""], extra_ps="function Save-SetupRecovery { throw 'disk full' }\n")
    assert first.exit_code == 1 and first.machine_token == SENTINEL_TOKEN
    assert "setup will not replace it" in first.output and "api_token.dpapi" in first.output

    second = run_scenario(bundle, answers=["", ""])  # a new code in hand; the fault is gone -- but the old token is still there
    assert second.exit_code == 2 and second.machine_token == SENTINEL_TOKEN
    assert "an API token at" in second.output and "the enrollment code was not requested" in second.output

    (bundle.fake / "machine_token.txt").unlink()  # the operator removes the abandoned enrollment's token, deliberately
    before = len(second.calls)
    third = run_scenario(bundle, answers=["", ""])
    assert third.exit_code == 0, third.output
    later = [c["tool"] for c in new_calls(third, before) if c["tool"] != "protect-recovery"]
    assert later[-3:] == ["set-api-token", "install", "finish-install"]
    assert third.task == {"State": "Disabled"} and not third.recovery_path.exists()  # completed, so the record is cleared


@needs_windows_powershell
def test_a_saved_and_verified_record_lets_setup_proceed_to_install(tmp_path):
    bundle = make_bundle(tmp_path)

    stopped = run_scenario(bundle, answers=[""], env={"SETUPTEST_INSTALL_MODE": "fail"})  # fails only AFTER the gate

    assert stopped.tools() == ["install-verify", "runtime-version", "http:GET", "read-code", "http:POST", "http:POST", "set-api-token", "install"]
    assert "saved and verified" in stopped.output
    recovery = stopped.recovery
    assert recovery is not None

    assert recovery == {
        "schema_version": 1,
        "created_utc": recovery["created_utc"],
        "release_version": RELEASE_VERSION,
        "api_url": TEMPLATE_URL,
        "customer_id": 7,
        "branch_id": 3,
        "installation_id": 41,
    }
    assert SENTINEL_TOKEN not in stopped.recovery_path.read_text(encoding="utf-8")
    assert SENTINEL_CODE not in stopped.recovery_path.read_text(encoding="utf-8")
    assert stopped.machine_token == SENTINEL_TOKEN
    assert_no_secret_anywhere_visible(stopped)

    ok = run_scenario(make_bundle(tmp_path / "ok"), answers=["", ""])  # and the whole run, unchanged, still completes
    assert ok.exit_code == 0, ok.output
    assert ok.tools() == ["install-verify", "runtime-version", "http:GET", "read-code", "http:POST", "http:POST", "set-api-token", "install", "finish-install"]
    assert ok.task == {"State": "Disabled"}


@needs_windows_powershell
def test_no_record_is_saved_when_the_token_could_not_be_stored(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=[""], env={"SETUPTEST_TOKEN_MODE": "fail"})

    assert result.exit_code == 1
    assert result.recovery is None  # (legacy order: token first) a record without a token is useless
    assert len(result.calls_to("protect-recovery")) == 1  # only the folder check made BEFORE the code was requested
    assert "NEW enrollment code" in result.output


@needs_windows_powershell
def test_a_successful_setup_removes_the_recovery_record_and_its_folder(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])

    assert result.exit_code == 0
    assert not result.recovery_path.exists() and not result.recovery_path.parent.exists()
    assert bundle.config_path.exists()  # ...while the install itself, of course, stays


@needs_windows_powershell
def test_a_resumed_setup_that_completes_also_clears_the_record(tmp_path):
    bundle = make_bundle(tmp_path)
    write_recovery(bundle)

    result = run_scenario(bundle, answers=["", "", ""], machine_token=SENTINEL_TOKEN)

    assert result.exit_code == 0, result.output
    assert not result.recovery_path.exists() and not result.recovery_path.parent.exists()


@needs_windows_powershell
def test_the_record_is_kept_after_every_failure_so_a_later_run_can_resume(tmp_path):
    for mode, expected_exit in _FINISH_FAILURES.items():
        bundle = make_bundle(tmp_path / mode)
        result = run_scenario(bundle, answers=[""], env={"SETUPTEST_FINISH_MODE": mode})
        assert result.exit_code == expected_exit, mode
        assert result.recovery is not None and set(result.recovery) == RECOVERY_KEYS, mode


@needs_windows_powershell
def test_a_failure_to_remove_the_record_after_success_is_a_note_not_a_failure(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""], extra_ps="function Remove-SetupRecovery { throw 'locked' }\n")

    assert result.exit_code == 0 and "could not be removed: locked" in result.output
    assert result.task == {"State": "Disabled"}


@needs_windows_powershell
def test_the_real_acl_function_restricts_the_folder_to_administrators_and_system(tmp_path):
    folder = tmp_path / "setup"
    folder.mkdir()
    q = ps_quote(folder)
    script = (
        f". {ps_quote(SETUP_SOURCE)}\n"
        "try {\n"
        f"    Protect-SetupRecoveryDirectory -Path {q}\n"
        f"    $acl = Get-Acl -LiteralPath {q}\n"
        "    \"PROTECTED=$($acl.AreAccessRulesProtected)\"\n"
        "    foreach ($r in $acl.Access) { \"RULE=$($r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value)"
        "|$($r.FileSystemRights)|$($r.AccessControlType)|$($r.InheritanceFlags)|$($r.IsInherited)\" }\n"
        "} finally {\n"
        # This (non-elevated) test session is not an Administrator/SYSTEM: hand it back access so the scratch folder can be cleaned up.
        f"    icacls {q} /inheritance:e | Out-Null\n"
        "}\n"
    )

    result = _run_ps(script)

    lines = result.stdout.splitlines()
    assert "PROTECTED=True" in lines, result.stdout + result.stderr  # inheritance from the parent is cut
    rules = [line[len("RULE="):].split("|") for line in lines if line.startswith("RULE=")]
    assert {r[0] for r in rules} == {"S-1-5-18", "S-1-5-32-544"}  # SYSTEM and Administrators, nobody else
    for sid, rights, access, inheritance, inherited in rules:
        assert rights == "FullControl" and access == "Allow"
        assert inheritance == "ContainerInherit, ObjectInherit"  # whatever is created inside gets the same restriction
        assert inherited == "False"
    granted = {r[0] for r in rules}
    for everyone in ("S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-4", "S-1-2-0"):  # Everyone, Authenticated, Users, ...
        assert everyone not in granted


# ---- refusals: a saved enrollment never makes setup guess ---------------------------------------------------

@needs_windows_powershell
def test_a_saved_enrollment_without_the_token_fails_safely_and_touches_nothing(tmp_path):
    bundle = make_bundle(tmp_path)
    record_path = write_recovery(bundle)
    before = _snapshot(bundle.data_root)

    result = run_scenario(bundle, answers=[""])  # no stored token at all

    assert result.exit_code == 2
    assert result.tools() == ["install-verify", "runtime-version"]  # no HTTP, no code prompt, no installer
    assert "has no API token" in result.output and "NEW enrollment code" in result.output
    assert str(record_path) in result.output
    assert _snapshot(bundle.data_root) == before and result.machine_token is None
    assert not bundle.install_root.exists()


_MALFORMED_RECORDS = {
    "empty": lambda: "",
    "not_json": lambda: "{oops",
    "json_array": lambda: "[1, 2, 3]",
    "missing_field": lambda: {"branch_id": _DROP},
    "missing_all_ids": lambda: {"customer_id": _DROP, "branch_id": _DROP, "installation_id": _DROP},
    "extra_token_field": lambda: {"agent_token": SENTINEL_TOKEN},
    "extra_code_field": lambda: {"enrollment_code": SENTINEL_CODE},
    "extra_field": lambda: {"note": "hello"},
    "renamed_field": lambda: {"Customer_ID": 7, "customer_id": _DROP},
    "string_id": lambda: {"customer_id": "7"},
    "float_id": lambda: {"branch_id": 3.5},
    "zero_id": lambda: {"installation_id": 0},
    "negative_id": lambda: {"customer_id": -1},
    "id_too_big_for_the_installer": lambda: {"installation_id": 2**31},
    "null_id": lambda: {"branch_id": None},
    "http_url": lambda: {"api_url": "http://insecure.example.org"},
    "url_with_credentials": lambda: {"api_url": "https://user:pw@api.example.org"},
    "url_not_text": lambda: {"api_url": 5},
    "unsupported_schema": lambda: {"schema_version": 2},
    "not_a_version": lambda: {"release_version": "not a version!"},
    "not_a_timestamp": lambda: {"created_utc": "yesterday"},
}


@needs_windows_powershell
@pytest.mark.parametrize("case", list(_MALFORMED_RECORDS))
def test_a_malformed_record_with_the_token_present_fails_safely_and_touches_nothing(tmp_path, case):
    bundle = make_bundle(tmp_path)
    change = _MALFORMED_RECORDS[case]()
    if isinstance(change, str):
        path = bundle.data_root / "setup" / "enrollment-recovery.json"
        path.parent.mkdir(parents=True)
        path.write_text(change, encoding="utf-8")
    else:
        path = write_recovery(bundle, **change)
    before = _snapshot(bundle.data_root)

    result = run_scenario(bundle, answers=[""], machine_token=SENTINEL_TOKEN)

    assert result.exit_code == 2, (case, result.output)
    assert result.tools() == ["install-verify", "runtime-version"]  # no HTTP, no code prompt, no installer
    assert "is not valid" in result.output and str(path) in result.output
    assert _snapshot(bundle.data_root) == before  # the record is neither repaired nor deleted
    assert result.machine_token == SENTINEL_TOKEN and not bundle.install_root.exists()
    assert result.task is None
    assert SENTINEL_TOKEN not in result.output and SENTINEL_CODE not in result.output  # never echoes what is in the file


@needs_windows_powershell
def test_declining_the_offer_to_resume_stops_and_leaves_everything_in_place(tmp_path):
    bundle = make_bundle(tmp_path)
    record_path = write_recovery(bundle)

    result = run_scenario(bundle, answers=["n"], machine_token=SENTINEL_TOKEN)

    assert result.exit_code == 2
    assert result.tools() == ["install-verify", "runtime-version"]
    assert "Resuming the unfinished setup was declined" in result.output and "NEW enrollment code" in result.output
    assert record_path.exists() and result.machine_token == SENTINEL_TOKEN


@needs_windows_powershell
def test_resume_is_the_default_and_a_host_that_cannot_be_asked_resumes(tmp_path):
    for name, answers in (("enter", ["", "", ""]), ("cannot_ask", None)):
        bundle = make_bundle(tmp_path / name)
        write_recovery(bundle)

        result = run_scenario(bundle, answers=answers, machine_token=SENTINEL_TOKEN)

        assert result.exit_code == 0, (name, result.output)
        assert result.calls_to("read-code") == [], name
        assert result.task == {"State": "Disabled"}, name


@needs_windows_powershell
@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"installation_id": 99}, "different installation_id"),
        ({"customer_id": 8}, "different customer_id"),
        ({"branch_id": 4}, "different branch_id"),
        ({"api_url": "https://elsewhere.example.org"}, "different api_url"),
    ],
)
def test_an_unrelated_existing_install_is_never_adopted_or_overwritten(tmp_path, override, expected):
    bundle = make_bundle(tmp_path)
    write_recovery(bundle)
    write_installed(bundle, **override)
    before = _snapshot(bundle.install_root, bundle.data_root)

    result = run_scenario(bundle, answers=[""], machine_token=SENTINEL_TOKEN)

    assert result.exit_code == 2, result.output
    assert expected in result.output and "will not overwrite or adopt it" in result.output
    assert [c for c in result.tools() if c in ("install", "finish-install", "http:GET", "http:POST", "read-code")] == []
    assert _snapshot(bundle.install_root, bundle.data_root) == before
    assert "tools\\uninstall.ps1" in result.output and "no new enrollment code is needed" in result.output


@needs_windows_powershell
@pytest.mark.parametrize(("exe", "config", "expected"), [(True, False, "no collector_config.json"), (False, True, "runtime is not there")])
def test_a_partial_install_left_by_the_interrupted_setup_is_refused_with_uninstall_instructions(tmp_path, exe, config, expected):
    bundle = make_bundle(tmp_path)
    write_recovery(bundle)
    write_installed(bundle, exe=exe, config=config)
    before = _snapshot(bundle.install_root, bundle.data_root)

    result = run_scenario(bundle, answers=[""], machine_token=SENTINEL_TOKEN)

    assert result.exit_code == 2 and expected in result.output
    assert result.calls_to("install") == [] and _snapshot(bundle.install_root, bundle.data_root) == before
    assert "tools\\uninstall.ps1" in result.output and "saved enrollment and the stored token are kept" in result.output


@needs_windows_powershell
def test_a_running_or_enabled_collector_is_never_touched_even_with_a_saved_enrollment(tmp_path):
    bundle = make_bundle(tmp_path)
    write_recovery(bundle)
    write_installed(bundle)
    before = _snapshot(bundle.install_root, bundle.data_root)

    result = run_scenario(bundle, answers=[""], machine_token=SENTINEL_TOKEN, task_state="Ready")

    assert result.exit_code == 2 and "tools\\update.ps1" in result.output
    assert result.task == {"State": "Ready"}  # not disabled: it was never this run's
    assert result.calls_to("disable-task") == [] and result.calls_to("install") == []
    assert _snapshot(bundle.install_root, bundle.data_root) == before


@needs_windows_powershell
def test_resume_uses_the_api_address_the_token_was_issued_for_not_the_bundles_current_default(tmp_path):
    bundle = make_bundle(tmp_path, template_mutation=lambda t: t.update(api_url="https://new-default.example.org"))
    write_recovery(bundle, api_url="https://recorded.example.org")

    result = run_scenario(bundle, answers=["", "", ""], machine_token=SENTINEL_TOKEN)

    assert result.exit_code == 0, result.output
    assert {c["url"] for c in result.calls_to("http")} == {"https://recorded.example.org/"}
    assert result.calls_to("install")[-1]["params"]["ApiUrl"] == "https://recorded.example.org"


@needs_windows_powershell
def test_an_api_url_override_that_differs_from_the_enrollment_is_refused_on_resume(tmp_path):
    bundle = make_bundle(tmp_path)
    write_recovery(bundle)

    result = run_scenario(bundle, answers=[""], machine_token=SENTINEL_TOKEN, args="-ApiUrl 'https://other.example.org'")

    assert result.exit_code == 2 and "differs from the API address this enrollment was made with" in result.output
    assert result.calls_to("http") == []


@needs_windows_powershell
@pytest.mark.parametrize("mode", list(_FINISH_FAILURES))
def test_every_failure_while_resuming_still_leaves_the_task_disabled_and_keeps_the_record(tmp_path, mode):
    bundle = make_bundle(tmp_path)
    write_recovery(bundle)

    result = run_scenario(bundle, answers=["", ""], machine_token=SENTINEL_TOKEN, env={"SETUPTEST_FINISH_MODE": mode})

    assert result.exit_code == _FINISH_FAILURES[mode]
    assert result.calls_to("enable-task") == [] and (result.task is None or result.task["State"] == "Disabled")
    assert result.recovery is not None  # still resumable
    assert "read-code" not in result.tools() and "http:POST" not in result.tools()


@needs_windows_powershell
def test_a_task_left_enabled_by_a_failed_resume_is_disabled(tmp_path):
    bundle = make_bundle(tmp_path)
    write_recovery(bundle)

    result = run_scenario(bundle, answers=["", ""], machine_token=SENTINEL_TOKEN,
                          env={"SETUPTEST_FINISH_MODE": "rogue_enabled_task_then_fail"})

    assert result.exit_code == 1 and result.task == {"State": "Disabled"} and len(result.calls_to("disable-task")) == 1


@needs_windows_powershell
def test_an_installer_failure_while_resuming_keeps_the_record_and_reports_the_same_recovery_steps(tmp_path):
    bundle = make_bundle(tmp_path)
    write_recovery(bundle)

    result = run_scenario(bundle, answers=["", ""], machine_token=SENTINEL_TOKEN, env={"SETUPTEST_INSTALL_MODE": "fail"})

    assert result.exit_code == 3 and result.recovery is not None
    assert "no new enrollment code is needed" in result.output and result.calls_to("finish-install") == []
    assert result.calls_to("enable-task") == []


@needs_windows_powershell
def test_the_enable_switch_still_enables_only_after_a_successful_resume(tmp_path):
    bundle = make_bundle(tmp_path)
    write_recovery(bundle)

    ok = run_scenario(bundle, answers=["", ""], machine_token=SENTINEL_TOKEN, args="-EnableTask")

    assert ok.exit_code == 0 and ok.tools()[-1] == "enable-task" and ok.task == {"State": "Ready"}

    failing_bundle = make_bundle(tmp_path / "failing")
    write_recovery(failing_bundle)
    failed = run_scenario(failing_bundle, answers=["", ""], machine_token=SENTINEL_TOKEN, args="-EnableTask",
                          env={"SETUPTEST_FINISH_MODE": "fail_bootstrap"})
    assert failed.exit_code == 1 and failed.calls_to("enable-task") == []


@needs_windows_powershell
def test_resume_never_asks_for_a_code_and_never_calls_the_token_tool(tmp_path):
    bundle = make_bundle(tmp_path)
    write_recovery(bundle)

    result = run_scenario(bundle, answers=["", "", ""], machine_token=SENTINEL_TOKEN)

    assert result.calls_to("read-code") == [] and result.calls_to("set-api-token") == []
    assert not any(c["method"] == "Post" for c in result.calls_to("http"))


@needs_windows_powershell
def test_an_existing_token_prompt_does_not_appear_when_resuming(tmp_path):
    bundle = make_bundle(tmp_path)
    write_recovery(bundle)

    result = run_scenario(bundle, answers=["", "", ""], machine_token=SENTINEL_TOKEN)

    assert not any("Replace the existing token?" in p for p in result.prompts())  # the token is THIS enrollment's own


# ---- pure helpers -----------------------------------------------------------------------------------------------------------

@needs_windows_powershell
def test_recovery_content_validation_accepts_exactly_the_documented_record():
    record = {"schema_version": 1, "created_utc": "2026-09-20T12:00:00Z", "release_version": "1.0.3",
              "api_url": "https://api.example.org/", "customer_id": 7, "branch_id": 3, "installation_id": 41}

    result = _dot_source_and_run(
        f"$r = Test-SetupRecoveryContent -Content {ps_quote(json.dumps(record))}\n"
        "\"$($r.Valid)|$($r.Record.CustomerId)|$($r.Record.BranchId)|$($r.Record.InstallationId)|$($r.Record.ApiUrl)|$($r.Record.ReleaseVersion)\""
    )

    assert result.stdout.strip() == "True|7|3|41|https://api.example.org|1.0.3"


@needs_windows_powershell
def test_the_recovery_json_builder_produces_exactly_the_allowed_fields():
    result = _dot_source_and_run(
        "ConvertTo-SetupRecoveryJson -CustomerId 7 -BranchId 3 -InstallationId 41 -ApiUrl 'https://api.example.org' -ReleaseVersion '1.0.3'"
    )

    document = json.loads(result.stdout)
    assert set(document) == RECOVERY_KEYS
    assert (document["schema_version"], document["customer_id"], document["branch_id"], document["installation_id"]) == (1, 7, 3, 41)


@needs_windows_powershell
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("match", "True"), ("trailing_slash", "True"),
        ("different_customer", "False"), ("different_branch", "False"), ("different_installation", "False"),
        ("different_url", "False"), ("no_exe", "False"), ("no_config", "False"), ("unreadable_config", "False"),
        ("string_ids", "False"),
    ],
)
def test_install_matching_compares_the_ids_and_api_address_of_the_installed_config(tmp_path, case, expected):
    install_root, config_path = tmp_path / "install", tmp_path / "config" / "collector_config.json"
    install_root.mkdir()
    config_path.parent.mkdir()
    (install_root / "SortViewCollector.exe").write_bytes(b"x")
    document = {"customer_id": 7, "branch_id": 3, "installation_id": 41, "api_url": "https://api.example.org"}
    if case == "trailing_slash":
        document["api_url"] += "/"
    elif case == "different_customer":
        document["customer_id"] = 8
    elif case == "different_branch":
        document["branch_id"] = 4
    elif case == "different_installation":
        document["installation_id"] = 42
    elif case == "different_url":
        document["api_url"] = "https://other.example.org"
    elif case == "string_ids":
        document["customer_id"] = "7"
    if case == "no_exe":
        (install_root / "SortViewCollector.exe").unlink()
    if case == "unreadable_config":
        config_path.write_text("{not json", encoding="utf-8")
    elif case != "no_config":
        config_path.write_text(json.dumps(document), encoding="utf-8")

    result = _dot_source_and_run(
        "$r = [pscustomobject]@{ CustomerId = 7; BranchId = 3; InstallationId = 41; ApiUrl = 'https://api.example.org' }\n"
        f"(Test-InstallMatchesRecovery -InstallRoot {ps_quote(install_root)} -ConfigPath {ps_quote(config_path)} -Record $r).Matches"
    )

    assert result.stdout.strip() == expected


# =====================================================================================
# CONTRACT V2 (stage A3a): the fresh-install path, and the legacy fallback
#
# This release asks for Contract v2 first. `default_http()` above is a server holding a LEGACY
# code, so every scenario before this point already runs through the fallback; the scenarios
# below use `v2_http()`, a server holding a CONTRACT 2 code.
# =====================================================================================

import hashlib

RECOVERY_KEYS_V2 = RECOVERY_KEYS | {"contract_version", "contract_mode", "key_id", "timezone", "rules_sha256"}
V2_HAPPY_TOOLS = ["install-verify", "runtime-version", "http:GET", "read-code", "http:POST", "set-api-token", "install",
                  "finish-install", "enable-task"]


def enroll_posts(result: Result) -> list[dict]:
    return [c for c in result.calls_to("http") if c["method"] == "Post"]


def staged_rules_path(bundle: Bundle) -> Path:
    return bundle.data_root / "setup" / "classification_rules.json"


def live_rules_path(bundle: Bundle) -> Path:
    return bundle.data_root / "config" / "classification_rules.json"


def assert_nothing_sensitive_was_printed(result: Result, rules: dict | None = None) -> None:
    """Console output holds neither secret, nor the key id, nor a single account name, pattern or destination."""
    values = [SENTINEL_TOKEN, SENTINEL_CODE, KEY_ID]
    document = RULES_DOC if rules is None else rules
    for entries in document.values():
        if isinstance(entries, list):
            for entry in entries:
                values.extend(entry.values() if isinstance(entry, dict) else [entry])
    for value in values:
        assert str(value) not in result.output, "a sensitive value was printed"
    # The tools were handed the token only as a SecureString, and never a rule.
    tool_records = json.dumps([c for c in result.calls if c["tool"] != "http"])
    assert SENTINEL_TOKEN not in tool_records and SENTINEL_CODE not in tool_records
    for canary in RULE_CANARIES:
        assert canary not in tool_records


# ---- response validation (pure) ------------------------------------------------------------

def _parse_v2(body: str, requested: int = 2) -> dict:
    script = (
        f"$r = ConvertTo-EnrollmentResult -Content {ps_quote(body)} -RequestedContract {requested}\n"
        "[pscustomobject]@{ Valid = $r.Valid; Problem = $r.Problem; Contract = $r.Contract; KeyId = $r.KeyId; Timezone = $r.Timezone; "
        "RulesIsObject = ($r.Rules -is [System.Management.Automation.PSCustomObject]); Customer = $r.CustomerId } | ConvertTo-Json -Compress"
    )
    completed = _dot_source_and_run(script)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@needs_windows_powershell
def test_v2_response_validation_accepts_exactly_the_nine_field_object():
    parsed = _parse_v2(_v2_enroll_body())

    assert parsed == {"Valid": True, "Problem": None, "Contract": 2, "KeyId": KEY_ID, "Timezone": TIMEZONE, "RulesIsObject": True,
                      "Customer": 7}


_BAD_V2_BODIES = {
    "four_fields_for_a_v2_request": _valid_enroll_body(),
    "missing_contract_version": json.dumps({k: v for k, v in json.loads(_v2_enroll_body()).items() if k != "contract_version"}),
    "missing_contract_mode": json.dumps({k: v for k, v in json.loads(_v2_enroll_body()).items() if k != "contract_mode"}),
    "missing_key_id": json.dumps({k: v for k, v in json.loads(_v2_enroll_body()).items() if k != "key_id"}),
    "missing_timezone": json.dumps({k: v for k, v in json.loads(_v2_enroll_body()).items() if k != "timezone"}),
    "missing_rules": json.dumps({k: v for k, v in json.loads(_v2_enroll_body()).items() if k != "classification_rules"}),
    "an_extra_field": _v2_enroll_body(hmac_secret="CANARY-EXTRA-FIELD-VALUE"),
    "contract_version_1": _v2_enroll_body(contract_version=1),
    "contract_version_as_text": _v2_enroll_body(contract_version="2"),
    "contract_version_true": _v2_enroll_body(contract_version=True),
    "contract_mode_v1": _v2_enroll_body(contract_mode="v1"),
    "contract_mode_upper_case": _v2_enroll_body(contract_mode="V2"),
    "key_id_upper_case": _v2_enroll_body(key_id=KEY_ID.upper()),
    "key_id_not_a_uuid": _v2_enroll_body(key_id="CANARY-NOT-A-KEY-ID"),
    "key_id_a_number": _v2_enroll_body(key_id=12345),
    "timezone_empty": _v2_enroll_body(timezone=""),
    "timezone_with_spaces": _v2_enroll_body(timezone="New York"),
    "timezone_a_number": _v2_enroll_body(timezone=5),
    "timezone_far_too_long": _v2_enroll_body(timezone="A" * 80),
    "rules_a_list": _v2_enroll_body(classification_rules=[]),
    "rules_a_string": _v2_enroll_body(classification_rules="CANARY-RULES-AS-TEXT"),
    "rules_null": _v2_enroll_body(classification_rules=None),
    "a_bad_token": _v2_enroll_body(agent_token="short"),
    "a_zero_id": _v2_enroll_body(customer_id=0),
}


@needs_windows_powershell
@pytest.mark.parametrize("case", sorted(_BAD_V2_BODIES))
def test_v2_response_validation_rejects_every_malformed_body_without_repeating_a_value(case):
    parsed = _parse_v2(_BAD_V2_BODIES[case])

    assert parsed["Valid"] is False and parsed["Problem"]
    assert (parsed["KeyId"], parsed["Timezone"], parsed["RulesIsObject"], parsed["Customer"]) == (None, None, False, None)
    for value in (SENTINEL_TOKEN, KEY_ID, KEY_ID.upper(), "CANARY", *RULE_CANARIES):
        assert value not in parsed["Problem"], parsed["Problem"]


@needs_windows_powershell
def test_the_two_response_shapes_are_never_mixed():
    assert _parse_v2(_v2_enroll_body(), requested=1)["Valid"] is False  # nine fields answering a LEGACY request
    assert _parse_v2(_valid_enroll_body(), requested=2)["Valid"] is False  # four fields answering a V2 request
    assert _parse_v2(_valid_enroll_body(), requested=1)["Valid"] is True


@needs_windows_powershell
def test_the_refusal_code_reader_accepts_only_the_four_fixed_codes():
    cases = {
        json.dumps({"code": "collector_upgrade_required", "detail": "x"}): "collector_upgrade_required",
        json.dumps({"code": "contract_not_permitted", "detail": "x"}): "contract_not_permitted",
        json.dumps({"code": "unsupported_contract_version"}): "unsupported_contract_version",
        json.dumps({"code": "enrollment_not_ready", "detail": "x", "more": 1}): "enrollment_not_ready",
        json.dumps({"code": "Contract_Not_Permitted"}): "",          # exact, case-sensitive
        json.dumps({"code": "something_else"}): "",
        json.dumps({"code": ["contract_not_permitted"]}): "",
        json.dumps({"detail": "contract_not_permitted"}): "",          # the code is read from `code` and nowhere else
        json.dumps(["contract_not_permitted"]): "",
        "contract_not_permitted": "",
        "{ not json": "",
        "": "",
    }
    script = "\n".join(f"'[' + (Get-EnrollmentRefusalCode -Content {ps_quote(body)}) + ']'" for body in cases)

    assert _dot_source_and_run(script).stdout.split() == [f"[{expected}]" for expected in cases.values()]


# ---- the happy path ------------------------------------------------------------------------

@needs_windows_powershell
def test_a_fresh_v2_setup_enrolls_once_installs_the_servers_rules_and_enables_the_task_itself(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=v2_http(), answers=[""])  # accept the standard files; nothing else is asked

    assert result.exit_code == 0, result.output
    assert result.tools() == V2_HAPPY_TOOLS
    # ONE enrollment request, asking for Contract v2. No fallback, no second request.
    (post,) = enroll_posts(result)
    assert post["body"] == {"enrollment_code": SENTINEL_CODE, "hostname": "TEST-PC", "collector_version": RELEASE_VERSION,
                            "contract_version": 2}
    # The task was registered Disabled by finish-install and enabled by setup as its LAST action, without a question.
    assert result.task == {"State": "Ready"}
    assert not any("Enable the scheduled task" in prompt for prompt in result.prompts())
    assert result.tools()[-1] == "enable-task" and result.tools().index("finish-install") < result.tools().index("enable-task")
    assert "did not start a Collector run" in result.output
    # The server's rules are where the Collector reads them, exactly as sent, and the staging area is gone.
    assert json.loads(live_rules_path(bundle).read_text(encoding="utf-8")) == RULES_DOC
    assert not staged_rules_path(bundle).exists() and not result.recovery_path.exists()
    assert not (bundle.data_root / "setup").exists()
    assert_nothing_sensitive_was_printed(result)


@needs_windows_powershell
def test_a_v2_setup_passes_the_contract_the_key_id_and_the_time_zone_to_the_installer_and_nothing_else_new(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=v2_http(), answers=[""])

    (install,) = result.calls_to("install")
    params = install["params"]
    assert (params["ContractMode"], params["KeyId"], params["Timezone"]) == ("v2", KEY_ID, TIMEZONE)
    assert (params["CustomerId"], params["BranchId"], params["InstallationId"]) == (7, 3, 41)
    assert set(params) == {"InstallRoot", "DataRoot", "CustomerId", "BranchId", "InstallationId", "ApiUrl", "CheckinsPath",
                           "RejectsPath", "AcsPath", "SuppressNextSteps", "ContractMode", "KeyId", "Timezone"}
    config = json.loads(bundle.config_path.read_text(encoding="utf-8-sig"))
    assert config["contract_mode"] == "v2" and config["v2"] == {"key_id": KEY_ID, "timezone": TIMEZONE}
    assert SENTINEL_TOKEN not in bundle.config_path.read_text(encoding="utf-8-sig")
    for canary in RULE_CANARIES:
        assert canary not in bundle.config_path.read_text(encoding="utf-8-sig")  # no rule is ever in the config


@needs_windows_powershell
def test_a_legacy_setup_passes_no_contract_setting_to_the_installer(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])

    (install,) = result.calls_to("install")
    assert not {"ContractMode", "KeyId", "Timezone"} & set(install["params"])
    assert "contract_mode" not in json.loads(bundle.config_path.read_text(encoding="utf-8-sig"))
    assert not live_rules_path(bundle).exists() and not staged_rules_path(bundle).exists()  # a legacy install has no rules file


@needs_windows_powershell
def test_the_enable_switch_is_not_needed_for_v2_and_changes_nothing(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=v2_http(), answers=[""], args="-EnableTask")

    assert result.exit_code == 0 and result.tools() == V2_HAPPY_TOOLS and result.task == {"State": "Ready"}


# ---- the legacy fallback, and what is never retried ------------------------------------------

@needs_windows_powershell
def test_a_legacy_code_is_retried_exactly_once_without_a_contract_version_and_takes_the_legacy_path(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, answers=["", ""])  # default_http: refused contract_not_permitted, then enrolled

    assert result.exit_code == 0, result.output
    first, second = enroll_posts(result)
    assert first["body"]["contract_version"] == 2 and "contract_version" not in second["body"]
    assert result.calls_to("read-code") and len(result.calls_to("read-code")) == 1  # the code is asked for ONCE
    # The legacy path, unchanged: its own enable question, whose default leaves the task disabled.
    assert any("Enable the scheduled task now?" in prompt for prompt in result.prompts())
    assert result.task == {"State": "Disabled"} and result.calls_to("enable-task") == []
    assert "legacy contract" in result.output


@needs_windows_powershell
def test_the_fallback_never_loops_when_the_server_keeps_refusing(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=default_http(**{ENROLL: refusal("contract_not_permitted")}), answers=[""])

    assert result.exit_code == 1 and len(enroll_posts(result)) == 2  # the v2 request and ONE legacy retry, never a third
    assert "The enrollment code was NOT used." in result.output and "may or may not" not in result.output
    assert result.machine_token is None and result.calls_to("install") == []


@needs_windows_powershell
@pytest.mark.parametrize(("code", "advice"), [
    ("collector_upgrade_required", "current SortView Collector release bundle"),
    ("unsupported_contract_version", "do not agree on a contract"),
    ("enrollment_not_ready", "finish preparing this installation"),
])
def test_every_other_fixed_refusal_stops_at_once_and_says_the_code_was_not_used(tmp_path, code, advice):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=default_http(**{ENROLL: refusal(code)}), answers=[""])

    assert result.exit_code == 1 and "SETUP STOPPED at step 5 (enrollment)" in result.output
    assert len(enroll_posts(result)) == 1  # NO fallback to the legacy contract for any of these
    assert "The enrollment code was NOT used." in result.output and advice in result.output
    assert "may or may not" not in result.output and "HAS BEEN USED" not in result.output
    assert "still valid until it expires" in result.output  # the same code can be used again
    assert result.machine_token is None and result.calls_to("install") == [] and result.task is None
    assert not (bundle.data_root / "setup").exists()  # the folder prepared for the enrollment is taken away again
    assert "A fixed sentence from the server." not in result.output  # the response body is never shown
    assert_no_secret_anywhere_visible(result)


@needs_windows_powershell
@pytest.mark.parametrize("content", [
    json.dumps({"code": "some_future_refusal", "detail": "CANARY-SERVER-TEXT"}),
    json.dumps({"detail": "CANARY-SERVER-TEXT"}),
    "CANARY-SERVER-TEXT not json",
    "",
])
def test_an_unknown_409_is_never_retried_and_stays_conservative_about_the_code(tmp_path, content):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=default_http(**{ENROLL: {"status": 409, "content": content}}), answers=[""])

    assert result.exit_code == 1 and len(enroll_posts(result)) == 1
    assert_ambiguous_outcome_guidance(result)  # not known to be a non-consuming refusal
    assert "CANARY-SERVER-TEXT" not in result.output and "some_future_refusal" not in result.output
    assert result.machine_token is None and result.calls_to("install") == []


@needs_windows_powershell
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 500, 503])
def test_no_other_status_ever_triggers_the_legacy_retry(tmp_path, status):
    bundle = make_bundle(tmp_path)
    answer = {"status": status, "content": json.dumps({"code": "contract_not_permitted"})}  # the code only counts on a 409

    result = run_scenario(bundle, http=default_http(**{ENROLL: answer}), answers=[""])

    assert result.exit_code == 1 and len(enroll_posts(result)) == 1
    assert result.machine_token is None and result.calls_to("install") == []


@needs_windows_powershell
def test_a_malformed_v2_success_fails_closed_and_shows_nothing_from_the_response(tmp_path):
    bundle = make_bundle(tmp_path)
    body = _v2_enroll_body(key_id="CANARY-NOT-A-KEY-ID")

    result = run_scenario(bundle, http=v2_http(**{ENROLL: {"status": 200, "content": body}}), answers=[""])

    assert result.exit_code == 1 and len(enroll_posts(result)) == 1
    assert "the response was not valid" in result.output
    assert result.machine_token is None and result.calls_to("install") == [] and result.recovery is None
    assert not staged_rules_path(bundle).exists()
    assert "CANARY-NOT-A-KEY-ID" not in result.output
    assert_nothing_sensitive_was_printed(result)


# ---- what is saved, and in what order ---------------------------------------------------------

@needs_windows_powershell
def test_the_v2_record_and_rules_are_saved_and_verified_before_the_token_is_stored(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=v2_http(), answers=[""], env={"SETUPTEST_TOKEN_MODE": "fail"})

    # The token could not be stored -- and everything a resume needs was ALREADY on disk when that happened.
    assert result.exit_code == 1 and "SETUP STOPPED at step 6b (API token)" in result.output
    assert result.machine_token is None
    record = result.recovery
    assert record is not None and set(record) == RECOVERY_KEYS_V2
    assert json.loads(staged_rules_path(bundle).read_text(encoding="utf-8")) == RULES_DOC
    assert result.calls_to("install") == [] and result.calls_to("finish-install") == [] and result.task is None
    # The advice: support issues a replacement token and setup resumes. A new code is never the advice.
    assert "Do NOT ask for another enrollment code" in result.output
    assert "replacement API token must be issued for installation 41" in result.output
    assert "NEW enrollment code" not in result.output and "new enrollment code" not in result.output.replace("Do NOT ask for another enrollment code", "")
    assert "The one-time enrollment code HAS BEEN USED" in result.output
    assert "the permanent API token is NOT stored" in result.output
    assert_nothing_sensitive_was_printed(result)


@needs_windows_powershell
def test_the_v2_record_holds_only_the_approved_non_secret_fields_and_no_rule(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=v2_http(), answers=[""], env={"SETUPTEST_INSTALL_MODE": "fail"})

    assert result.exit_code == 3
    record = result.recovery
    assert record is not None
    staged = staged_rules_path(bundle).read_bytes()
    assert record == {
        "schema_version": 2,
        "created_utc": record["created_utc"],
        "release_version": RELEASE_VERSION,
        "api_url": TEMPLATE_URL,
        "customer_id": 7,
        "branch_id": 3,
        "installation_id": 41,
        "contract_version": 2,
        "contract_mode": "v2",
        "key_id": KEY_ID,
        "timezone": TIMEZONE,
        "rules_sha256": hashlib.sha256(staged).hexdigest(),  # the staged FILE's hash: ties it to this enrollment
    }
    raw = result.recovery_path.read_text(encoding="utf-8")
    for forbidden in (SENTINEL_TOKEN, SENTINEL_CODE, "agent_token", "enrollment_code", "classification_rules", "secret",
                      *RULE_CANARIES):
        assert forbidden not in raw, forbidden
    assert result.machine_token == SENTINEL_TOKEN  # (and by now the token is stored too)
    assert result.tools().index("set-api-token") < result.tools().index("install")


@needs_windows_powershell
def test_a_v2_record_that_cannot_be_saved_stops_before_the_token_and_points_to_support(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=v2_http(), answers=[""], extra_ps="function Save-SetupRecovery { throw 'disk full' }\n")

    assert result.exit_code == 1 and "SETUP STOPPED at step 6 (saving the enrollment)" in result.output
    assert "could not be saved (disk full)" in result.output
    assert result.calls_to("set-api-token") == [] and result.machine_token is None and result.calls_to("install") == []
    assert not result.recovery_path.exists() and not staged_rules_path(bundle).exists()  # nothing unverified is left to steer a resume
    assert "Do NOT ask for another enrollment code" in result.output and "installation number 41" in result.output
    assert "NEW enrollment code" not in result.output


@needs_windows_powershell
def test_staged_rules_that_do_not_read_back_as_the_servers_stop_setup_before_the_token(tmp_path):
    bundle = make_bundle(tmp_path)
    # A writer that silently drops a list entry, as a truncating disk or a serialization bug would.
    lossy = ("function ConvertTo-ClassificationRulesJson { param($Rules, [switch]$Compress)\n"
             "  if ($Compress) { return (ConvertTo-Json -InputObject $Rules -Depth 20 -Compress) }\n"
             "  return '{\"schema_version\":1,\"destinations\":[],\"branch_services_names\":[],\"collection_services_names\":[],"
             "\"branch_services_da_patterns\":[],\"collection_services_da_patterns\":[]}' }\n")

    result = run_scenario(bundle, http=v2_http(), answers=[""], extra_ps=lossy)

    assert result.exit_code == 1 and "SETUP STOPPED at step 6 (saving the enrollment)" in result.output
    assert "do not have the structure the server sent" in result.output
    # The problem is described by SHAPE (list sizes), never by content.
    assert "branch_services_names=1" in result.output and "branch_services_names=0" in result.output
    assert result.calls_to("set-api-token") == [] and not staged_rules_path(bundle).exists()
    assert_nothing_sensitive_was_printed(result)


# ---- the rules document reaches disk exactly as the server sent it -----------------------------

_RULES_DOCUMENTS = {
    "reviewed_and_empty": {"schema_version": 1, "destinations": [], "branch_services_names": [], "collection_services_names": [],
                           "branch_services_da_patterns": [], "collection_services_da_patterns": []},
    "one_entry_per_list": {"schema_version": 1, "destinations": [{"slug": "annex", "contains": "ANNEX"}],
                           "branch_services_names": ["ONLY ONE"], "collection_services_names": ["ONLY ONE TOO"],
                           "branch_services_da_patterns": ["DAONE"], "collection_services_da_patterns": ["DATWO"]},
    "non_ascii_and_awkward_characters": {
        "schema_version": 1,
        "destinations": [{"slug": "sodra", "contains": "SÖDRA FILIALEN"}, {"slug": "west", "contains": "WEST <A&B>"}],
        "branch_services_names": ["ÅSA ÖBERG-ÇELIK", "O'BRIEN \"QUOTED\" \\ SLASH", "名前 テスト"],
        "collection_services_names": ["JOSÉ NUÑEZ", "MÜLLER, STRAßE"],
        "branch_services_da_patterns": ["DA ÉTÉ"],
        "collection_services_da_patterns": [],
    },
    "many_entries": {"schema_version": 1, "destinations": [{"slug": f"d{i}", "contains": f"DEST {i}"} for i in range(40)],
                     "branch_services_names": [f"ACCOUNT {i}" for i in range(300)], "collection_services_names": [],
                     "branch_services_da_patterns": [], "collection_services_da_patterns": [f"DA{i}" for i in range(50)]},
}


@needs_windows_powershell
@pytest.mark.parametrize("name", sorted(_RULES_DOCUMENTS))
def test_the_servers_rules_are_installed_exactly_as_sent_utf8_without_a_bom(tmp_path, name):
    document = _RULES_DOCUMENTS[name]
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=v2_http(document), answers=[""])

    assert result.exit_code == 0, result.output
    raw = live_rules_path(bundle).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # no BOM: the Collector reads strict UTF-8
    assert json.loads(raw.decode("utf-8")) == document  # nothing added, dropped, merged, reordered or re-encoded
    # ... and the Collector's own validator accepts the installed file unchanged.
    from collector import v2_rules
    assert v2_rules.normalize_document(json.loads(raw.decode("utf-8"))) is not None
    assert not [p for p in live_rules_path(bundle).parent.iterdir() if p.suffix == ".tmp"]  # no temporary file left behind
    assert_nothing_sensitive_was_printed(result, document)


@needs_windows_powershell
def test_the_rules_shape_describes_structure_and_never_content():
    shape = _dot_source_and_run(
        f"Get-ClassificationRulesShape -Rules ({ps_quote(json.dumps(RULES_DOC))} | ConvertFrom-Json)\n"
        "'[' + (Get-ClassificationRulesShape -Rules 'text') + ']'\n"
        "Get-ClassificationRulesShape -Rules ('{\"schema_version\":1,\"destinations\":\"x\"}' | ConvertFrom-Json)"
    ).stdout.splitlines()

    assert shape[0] == ("fields=branch_services_da_patterns,branch_services_names,collection_services_da_patterns,"
                        "collection_services_names,destinations,schema_version; schema_version=1; destinations=1; "
                        "branch_services_names=1; collection_services_names=2; branch_services_da_patterns=0; "
                        "collection_services_da_patterns=1")
    assert shape[1] == "[]"  # not an object: no shape
    assert "destinations=not-a-list" in shape[2] and "branch_services_names=absent" in shape[2]
    for canary in RULE_CANARIES:
        assert canary not in "\n".join(shape)


# ---- a first install starts from a clean machine -----------------------------------------------

_LEFTOVERS = {
    "a_v2_secret": ("secrets/v2_key.dpapi", "a Contract v2 secret"),
    "a_legacy_state_file": ("data/state.json", "a Collector state file (the legacy cursor)"),
    "a_v2_state_file": ("data/v2/state_v2.json", "a Contract v2 state file (cursor)"),
    "a_live_rules_file": ("config/classification_rules.json", "a classification rules file"),
    "staged_rules_with_no_record": ("setup/classification_rules.json", "classification rules staged by an earlier setup"),
}


@needs_windows_powershell
@pytest.mark.parametrize("leftover", sorted(_LEFTOVERS))
def test_a_fresh_run_refuses_collector_data_left_by_an_earlier_install_before_the_code(tmp_path, leftover):
    relative, description = _LEFTOVERS[leftover]
    bundle = make_bundle(tmp_path)
    path = bundle.data_root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"left here by an earlier install")

    result = run_scenario(bundle, http=v2_http(), answers=["y", "y", "y"])

    assert result.exit_code == 2 and "SETUP STOPPED at step 2 (machine)" in result.output
    assert description in result.output and "the enrollment code was not requested" in result.output
    assert result.calls_to("read-code") == [] and result.calls_to("http") == []  # before the code AND before any network request
    assert path.read_bytes() == b"left here by an earlier install"  # never adopted, replaced or deleted
    assert result.machine_token is None and not bundle.install_root.exists() and result.task is None
    assert [c["tool"] for c in result.calls if c["tool"] not in ("prompt",)] == ["install-verify", "runtime-version"]


@needs_windows_powershell
def test_every_leftover_is_listed_at_once(tmp_path):
    bundle = make_bundle(tmp_path)
    for relative, _description in _LEFTOVERS.values():
        path = bundle.data_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    result = run_scenario(bundle, http=v2_http(), answers=[""], machine_token="an-earlier-installations-token")

    assert result.exit_code == 2
    for _relative, description in _LEFTOVERS.values():
        assert description in result.output
    assert "an API token at" in result.output and "an-earlier-installations-token" not in result.output


@needs_windows_powershell
def test_a_clean_machine_reports_no_leftovers():
    out = _dot_source_and_run(
        f"'[' + (@(Get-LeftoverArtifacts -DataRoot {ps_quote(REPO_ROOT / 'no-such-data-root')}).Count) + ']'"
    ).stdout.strip()

    assert out == "[0]"


# ---- resume: an interrupted v2 setup is finished from disk, with no enrollment request -------

def _interrupt_v2(tmp_path: Path, *, env: dict) -> tuple[Bundle, Result]:
    bundle = make_bundle(tmp_path)
    first = run_scenario(bundle, http=v2_http(), answers=[""], env=env)
    assert first.exit_code not in (0, None), first.output
    assert first.recovery is not None and first.recovery["schema_version"] == 2
    assert first.machine_token == SENTINEL_TOKEN
    assert first.task is None or first.task == {"State": "Disabled"}  # an interrupted setup never leaves an active task
    return bundle, first


#: After the first run the server would REFUSE this code (the branch's v2 identity exists). Any enrollment request a
#: resume made would be answered with this -- so a resume that completes proves it made none.
_ALREADY_ENROLLED = {
    "Get /": {"status": 200, "content": json.dumps({"status": "SortView API running"})},
    ENROLL: refusal("enrollment_not_ready"),
}


@needs_windows_powershell
def test_a_v2_setup_interrupted_before_the_install_resumes_without_an_enrollment_request(tmp_path):
    bundle, first = _interrupt_v2(tmp_path, env={"SETUPTEST_INSTALL_MODE": "fail"})
    assert first.exit_code == 3 and not live_rules_path(bundle).exists() and staged_rules_path(bundle).exists()
    assert "run setup.ps1 again to RESUME -- no enrollment code is needed" in first.output
    assert "Do NOT ask for another enrollment code" in first.output
    before = len(first.calls)

    second = run_scenario(bundle, http=_ALREADY_ENROLLED, answers=["", ""])  # Enter = resume; Enter = use the standard files

    assert second.exit_code == 0, second.output
    later = new_calls(second, before)
    names = [c["tool"] for c in later if c["tool"] != "protect-recovery"]
    assert names == ["install-verify", "runtime-version", "http", "install", "finish-install", "enable-task"]
    assert [c for c in later if c["tool"] == "http" and c["method"] == "Post"] == []  # NO enrollment request
    assert [c for c in later if c["tool"] in ("read-code", "set-api-token")] == []  # no code asked for; the stored token is reused
    install = next(c for c in later if c["tool"] == "install")["params"]
    assert (install["ContractMode"], install["KeyId"], install["Timezone"]) == ("v2", KEY_ID, TIMEZONE)  # from the saved record
    assert (install["CustomerId"], install["BranchId"], install["InstallationId"]) == (7, 3, 41)
    assert json.loads(live_rules_path(bundle).read_text(encoding="utf-8")) == RULES_DOC
    assert second.task == {"State": "Ready"}  # completed: enabled, as a fresh v2 setup is
    assert not second.recovery_path.exists() and not staged_rules_path(bundle).exists()
    assert second.machine_token == SENTINEL_TOKEN
    assert "no enrollment request will be made" in second.output
    assert_nothing_sensitive_was_printed(second)


@needs_windows_powershell
@pytest.mark.parametrize("mode", ["fail_interactive_preflight", "fail_system_preflight", "fail_bootstrap", "fail_task_registration"])
def test_a_v2_setup_interrupted_after_the_install_resumes_at_verification_without_reinstalling(tmp_path, mode):
    bundle, first = _interrupt_v2(tmp_path, env={"SETUPTEST_FINISH_MODE": mode})
    assert first.calls_to("enable-task") == [] and first.task is None
    assert json.loads(live_rules_path(bundle).read_text(encoding="utf-8")) == RULES_DOC  # already installed by the first run
    rules_before = live_rules_path(bundle).read_bytes()
    config_before = bundle.config_path.read_bytes()
    before = len(first.calls)

    second = run_scenario(bundle, http=_ALREADY_ENROLLED, answers=[""])  # Enter = resume

    assert second.exit_code == 0, second.output
    names = [c["tool"] for c in new_calls(second, before) if c["tool"] != "protect-recovery"]
    assert names == ["install-verify", "runtime-version", "http", "finish-install", "enable-task"]  # no install, no enrollment
    assert live_rules_path(bundle).read_bytes() == rules_before and bundle.config_path.read_bytes() == config_before  # reused, not rewritten
    assert second.task == {"State": "Ready"} and not second.recovery_path.exists()
    assert "the classification rules from SortView are already installed" in second.output


@needs_windows_powershell
@pytest.mark.parametrize("tamper", ["staged_rules_changed", "staged_rules_missing", "live_rules_changed"])
def test_a_resume_whose_rules_are_not_the_saved_enrollments_refuses_and_points_to_support(tmp_path, tamper):
    env = {"SETUPTEST_FINISH_MODE": "fail_bootstrap"} if tamper == "live_rules_changed" else {"SETUPTEST_INSTALL_MODE": "fail"}
    bundle, first = _interrupt_v2(tmp_path, env=env)
    if tamper == "staged_rules_changed":
        staged_rules_path(bundle).write_text(json.dumps({**RULES_DOC, "branch_services_names": ["SOMEONE ELSE"]}), encoding="utf-8")
    elif tamper == "staged_rules_missing":
        staged_rules_path(bundle).unlink()
    else:
        live_rules_path(bundle).write_text(json.dumps({**RULES_DOC, "branch_services_names": ["SOMEONE ELSE"]}), encoding="utf-8")
    record_before = first.recovery_path.read_bytes()
    before = len(first.calls)

    second = run_scenario(bundle, http=_ALREADY_ENROLLED, answers=["", ""])

    assert second.exit_code == 2 and "cannot be resumed safely" in second.output
    assert "Do NOT ask for another enrollment code" in second.output and "Contact SortView support" in second.output
    names = [c["tool"] for c in new_calls(second, before)]
    assert names == ["install-verify", "runtime-version"]  # stopped before the network, the code, the install and the task
    assert first.recovery_path.read_bytes() == record_before and second.machine_token == SENTINEL_TOKEN  # nothing deleted or replaced
    assert "SOMEONE ELSE" not in second.output
    assert second.task is None or second.task == {"State": "Disabled"}


@needs_windows_powershell
def test_a_saved_v2_enrollment_with_no_token_keeps_the_record_and_never_suggests_a_new_code(tmp_path):
    bundle, first = _interrupt_v2(tmp_path, env={"SETUPTEST_INSTALL_MODE": "fail"})
    (bundle.fake / "machine_token.txt").unlink()  # the token is gone
    record_before = first.recovery_path.read_bytes()
    before = len(first.calls)

    second = run_scenario(bundle, http=_ALREADY_ENROLLED, answers=["", ""])

    assert second.exit_code == 2
    assert "no API token" in second.output and "replacement API token for installation 41" in second.output
    assert "Do NOT ask for another enrollment code" in second.output and "do NOT delete the saved record" in second.output
    assert "NEW enrollment code" not in second.output
    assert first.recovery_path.read_bytes() == record_before and staged_rules_path(bundle).exists()
    assert [c["tool"] for c in new_calls(second, before)] == ["install-verify", "runtime-version"]


@needs_windows_powershell
def test_a_legacy_saved_enrollment_with_no_token_still_gets_the_legacy_advice(tmp_path):
    bundle = make_bundle(tmp_path)
    first = run_scenario(bundle, answers=[""], env={"SETUPTEST_INSTALL_MODE": "fail"})
    assert first.recovery is not None and first.recovery["schema_version"] == 1
    (bundle.fake / "machine_token.txt").unlink()

    second = run_scenario(bundle, answers=["", ""])

    assert second.exit_code == 2 and "NEW enrollment code" in second.output  # unchanged: a legacy code CAN simply be reissued


@needs_windows_powershell
def test_a_declined_v2_resume_does_not_suggest_starting_over_with_a_new_code(tmp_path):
    bundle, _first = _interrupt_v2(tmp_path, env={"SETUPTEST_INSTALL_MODE": "fail"})

    second = run_scenario(bundle, http=_ALREADY_ENROLLED, answers=["n"])

    assert second.exit_code == 2 and "Resuming the unfinished setup was declined" in second.output
    assert "cannot be started over with a new enrollment code" in second.output and "get a NEW enrollment code" not in second.output
    assert second.recovery is not None and staged_rules_path(bundle).exists()


@needs_windows_powershell
def test_the_recovery_reader_accepts_a_v2_record_and_rejects_every_mix_of_the_two_shapes():
    v1 = {"schema_version": 1, "created_utc": "2026-01-01T00:00:00Z", "release_version": "1.0.14", "api_url": "https://api.example.org",
          "customer_id": 7, "branch_id": 3, "installation_id": 41}
    v2 = {**v1, "schema_version": 2, "contract_version": 2, "contract_mode": "v2", "key_id": KEY_ID, "timezone": TIMEZONE,
          "rules_sha256": "a" * 64}
    cases = {
        "v1": (v1, True), "v2": (v2, True),
        "v1_claiming_schema_2": ({**v1, "schema_version": 2}, False),
        "v2_fields_on_a_schema_1_record": ({**v2, "schema_version": 1}, False),
        "v2_without_the_rules_hash": ({k: v for k, v in v2.items() if k != "rules_sha256"}, False),
        "v2_with_an_extra_field": ({**v2, "classification_rules": {"branch_services_names": ["X"]}}, False),
        "v2_with_a_token_field": ({**v2, "agent_token": "x" * 30}, False),
        "v2_bad_key_id": ({**v2, "key_id": "NOT-A-KEY"}, False),
        "v2_bad_hash": ({**v2, "rules_sha256": "zz"}, False),
        "v2_bad_mode": ({**v2, "contract_mode": "v1"}, False),
        "v2_bad_version": ({**v2, "contract_version": 1}, False),
        "v2_bad_timezone": ({**v2, "timezone": "not a zone"}, False),
        "schema_3": ({**v1, "schema_version": 3}, False),
    }
    script = "\n".join(
        f"$c = Test-SetupRecoveryContent -Content {ps_quote(json.dumps(doc))}; '{name}=' + $c.Valid + '/' + $(if ($c.Valid) {{ $c.Record.Contract }} else {{ 0 }})"
        for name, (doc, _valid) in cases.items()
    )

    lines = dict(line.split("=") for line in _dot_source_and_run(script).stdout.split())
    for name, (doc, valid) in cases.items():
        expected = f"True/{2 if doc.get('schema_version') == 2 else 1}" if valid else "False/0"
        assert lines[name] == expected, name


@needs_windows_powershell
def test_an_installed_config_matches_a_saved_enrollment_only_on_the_same_contract_key_and_zone(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "SortViewCollector.exe").write_bytes(b"fake")
    base = {"customer_id": 7, "branch_id": 3, "installation_id": 41, "api_url": "https://api.example.org"}
    configs = {
        "legacy": base,
        "v2_same": {**base, "contract_mode": "v2", "v2": {"key_id": KEY_ID, "timezone": TIMEZONE}},
        "v2_other_key": {**base, "contract_mode": "v2", "v2": {"key_id": KEY_ID.replace("3f", "4a"), "timezone": TIMEZONE}},
        "v2_other_zone": {**base, "contract_mode": "v2", "v2": {"key_id": KEY_ID, "timezone": "America/Chicago"}},
        "v2_no_section": {**base, "contract_mode": "v2"},
    }
    record = ("[pscustomobject]@{{ CustomerId = 7; BranchId = 3; InstallationId = 41; ApiUrl = 'https://api.example.org'; "
              "Contract = {contract}; KeyId = {key}; Timezone = {zone} }}")
    lines = []
    for name, config in configs.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        for label, rec in (("v2", record.format(contract=2, key=ps_quote(KEY_ID), zone=ps_quote(TIMEZONE))),
                           ("v1", record.format(contract=1, key="$null", zone="$null"))):
            lines.append(f"'{name}/{label}=' + (Test-InstallMatchesRecovery -InstallRoot {ps_quote(install_root)} "
                         f"-ConfigPath {ps_quote(path)} -Record ({rec})).Matches")

    results = dict(line.split("=") for line in _dot_source_and_run("\n".join(lines)).stdout.split())

    assert results == {
        "legacy/v2": "False", "legacy/v1": "True",
        "v2_same/v2": "True", "v2_same/v1": "False",
        "v2_other_key/v2": "False", "v2_other_key/v1": "False",
        "v2_other_zone/v2": "False", "v2_other_zone/v1": "False",
        "v2_no_section/v2": "False", "v2_no_section/v1": "False",
    }


# ---- the task: enabled only at the very end, and never by a failed v2 setup --------------------

@needs_windows_powershell
@pytest.mark.parametrize("mode", sorted(_FINISH_FAILURES))
def test_no_v2_verification_failure_ever_enables_the_task(tmp_path, mode):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=v2_http(), answers=[""], env={"SETUPTEST_FINISH_MODE": mode})

    assert result.exit_code == _FINISH_FAILURES[mode]
    assert result.calls_to("enable-task") == [] and result.task is None  # never enabled; none was even registered
    assert "SETUP STOPPED at step 8 (verification)" in result.output
    assert result.recovery is not None  # resumable
    assert "Do NOT ask for another enrollment code" in result.output


@needs_windows_powershell
@pytest.mark.parametrize("mode", ["rogue_enabled_task_then_fail", "ok_but_task_enabled"])
def test_a_task_found_enabled_before_the_final_step_of_a_v2_setup_is_disabled_and_setup_fails(tmp_path, mode):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=v2_http(), answers=[""], env={"SETUPTEST_FINISH_MODE": mode})

    assert result.exit_code != 0 and result.task == {"State": "Disabled"}
    assert result.calls_to("enable-task") == [] and len(result.calls_to("disable-task")) == 1


@needs_windows_powershell
def test_a_v2_setup_whose_final_enable_fails_leaves_the_task_disabled_and_reports_failure(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=v2_http(), answers=[""], env={"SETUPTEST_ENABLE_FAILS": "1"})

    assert result.exit_code == 1 and "SETUP STOPPED at step 9 (enable)" in result.output
    assert result.task == {"State": "Disabled"}
    assert "Enable-ScheduledTask" in result.output  # how to enable it by hand


@needs_windows_powershell
def test_a_v2_setup_that_finishes_registers_no_task_is_a_failure_and_enables_nothing(tmp_path):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=v2_http(), answers=[""], env={"SETUPTEST_FINISH_MODE": "ok_but_no_task"})

    assert result.exit_code == 1 and result.calls_to("enable-task") == [] and result.task is None


def test_the_v2_automatic_enable_is_the_same_single_guarded_call_site():
    run = SETUP_BODY[SETUP_BODY.index("function Invoke-SetupMain"):]
    assert run.count("Enable-SortViewTask") == 1  # one call site for both contracts
    decision = run.index("$enableNow = ([bool]$EnableTask -or $isV2Install)")
    # It is decided only after the verification succeeded AND the task was confirmed Disabled.
    assert run.index("Invoke-SetupTool -Path $finishScript") < run.index('if ([string]$task.State -ne "Disabled")') < decision
    assert decision < run.index("Enable-SortViewTask")
    # Nothing starts a run: the cmdlet's name appears only inside text that is printed as guidance.
    assert all("Write-Host" in line for line in _code_lines(run) if "Start-ScheduledTask" in line)


def test_setup_never_prints_a_rule_a_key_id_or_the_response():
    printing = re.compile(r"(Write-(Host|Output|Verbose|Debug|Warning|Information|Error)\b[^\n]*|Stop-Setup\b[^\n]*(\n\s+-[^\n]*)*)")
    for match in printing.finditer(SETUP_BODY):
        statement = match.group(0)
        for sensitive in ("$keyId", "$KeyId", ".KeyId", "$enrollment.Rules", "$Rules", "$rulesJson", "$http.Content", "$Content",
                          "$readBack", "$timezone", "$record.Timezone"):
            assert sensitive not in statement, (sensitive, statement[:120])
    # The rules object is dropped from memory as soon as it has been staged, whatever happened.
    main = SETUP_BODY[SETUP_BODY.index("function Invoke-SetupMain"):]
    assert "} finally {\n                $enrollment.Rules = $null" in main


@needs_windows_powershell
def test_a_staged_rules_file_that_is_not_json_is_reported_without_quoting_it(tmp_path):
    """A JSON parser's own error message quotes the text it could not read -- here, that would be a piece of the rules."""
    bundle = make_bundle(tmp_path)
    broken = ("function ConvertTo-ClassificationRulesJson { param($Rules, [switch]$Compress)\n"
              "  if ($Compress) { return (ConvertTo-Json -InputObject $Rules -Depth 20 -Compress) }\n"
              "  return '{ \"branch_services_names\": [ CANARY-UNQUOTED-ACCOUNT-NAME ' }\n")

    result = run_scenario(bundle, http=v2_http(), answers=[""], extra_ps=broken)

    assert result.exit_code == 1 and "SETUP STOPPED at step 6 (saving the enrollment)" in result.output
    assert "could not be read back as JSON" in result.output
    assert "CANARY-UNQUOTED-ACCOUNT-NAME" not in result.output
    assert result.calls_to("set-api-token") == [] and not staged_rules_path(bundle).exists()
    assert_nothing_sensitive_was_printed(result)


# =====================================================================================
# AMBIGUOUS ENROLLMENT OUTCOMES: what setup may and may not say about the single-use code
#
# A Contract v2 enrollment creates the branch's v2 identity on the server exactly once; a second code is refused. So
# "ask for a new enrollment code" is safe advice ONLY when the server is known not to have enrolled this computer.
#
#   known A1 refusals (409 + fixed code), 429, a redirect   the code was NOT used -- said plainly
#   400                                                     this request enrolled nothing; nothing is claimed about
#                                                           an earlier one
#   no response, 5xx, an unexpected status, an unreadable   UNKNOWN: never "not used", never "get a new code" --
#   200                                                     retry with the SAME code; if that is refused, support
# =====================================================================================

_NEW_CODE_ADVICE = re.compile(r"ask (your SortView administrator )?for a new enrollment code", re.IGNORECASE)


def assert_ambiguous_outcome_guidance(result: Result) -> None:
    """The conservative guidance, and none of the unsafe claims."""
    out = result.output
    assert result.exit_code == 1 and "SETUP STOPPED at step 5 (enrollment)" in out
    assert "The enrollment code may or may not have been used: the result of the enrollment request is UNKNOWN." in out
    assert "Do NOT ask for a new enrollment code yet. Run setup.ps1 again with the SAME code." in out
    assert "If the same code is then refused as invalid or already used, the first request may have completed: contact SortView support" in out
    # Never the two unsafe statements:
    assert "The enrollment code was NOT used" not in out and "HAS BEEN USED" not in out
    # "new enrollment code" appears only inside "Do NOT ask for a new enrollment code": nothing ADVISES getting one.
    assert _NEW_CODE_ADVICE.sub("", out.replace("Do NOT ask for a new enrollment code", "")) == out.replace("Do NOT ask for a new enrollment code", "")
    assert "NEW enrollment code" not in out
    # And setup did not retry by itself, stored nothing and installed nothing.
    assert len(enroll_posts(result)) == 1
    assert result.machine_token is None and result.calls_to("install") == [] and result.task is None
    assert result.recovery is None


@needs_windows_powershell
@pytest.mark.parametrize("failure", [
    "no response (WebException)",          # a timeout while waiting for the answer
    "no response (IOException)",           # the connection was reset / closed after the request was sent
    "no response (TaskCanceledException)", # PowerShell 7's timeout
    "no response (HttpRequestException)",
])
def test_no_response_never_says_the_code_was_unused_and_says_to_retry_the_same_code(tmp_path, failure):
    bundle = make_bundle(tmp_path)
    http = v2_http(**{ENROLL: {"status": 0, "content": None, "failure": failure}})

    result = run_scenario(bundle, http=http, answers=[""])

    assert_ambiguous_outcome_guidance(result)
    assert "No response was received from SortView" in result.output
    assert "the result of the enrollment request is unknown" in result.output
    assert "Could not complete a secure connection" not in result.output  # no claim that the request failed to arrive
    assert_nothing_sensitive_was_printed(result)


@needs_windows_powershell
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_never_suggests_a_new_code(tmp_path, status):
    bundle = make_bundle(tmp_path)
    http = v2_http(**{ENROLL: {"status": status, "content": json.dumps({"detail": "CANARY-SERVER-TEXT"}), "failure": f"HTTP {status}"}})

    result = run_scenario(bundle, http=http, answers=[""])

    assert_ambiguous_outcome_guidance(result)
    assert "CANARY-SERVER-TEXT" not in result.output


@needs_windows_powershell
@pytest.mark.parametrize("status", [401, 403, 404, 418, 422])
def test_an_unknown_status_never_suggests_a_new_code(tmp_path, status):
    bundle = make_bundle(tmp_path)
    http = v2_http(**{ENROLL: {"status": status, "content": json.dumps({"detail": "CANARY-SERVER-TEXT"}), "failure": f"HTTP {status}"}})

    result = run_scenario(bundle, http=http, answers=[""])

    assert_ambiguous_outcome_guidance(result)
    assert "unexpected status" in result.output and "CANARY-SERVER-TEXT" not in result.output


@needs_windows_powershell
@pytest.mark.parametrize("body", [
    _v2_enroll_body(key_id="CANARY-NOT-A-KEY-ID"),   # the server enrolled; the answer cannot be used
    _valid_enroll_body(),                             # a legacy answer to the Contract v2 request
    "CANARY-SERVER-TEXT not json at all",
    "",
])
def test_a_malformed_200_never_suggests_a_new_code(tmp_path, body):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=v2_http(**{ENROLL: {"status": 200, "content": body}}), answers=[""])

    assert_ambiguous_outcome_guidance(result)
    assert "the response was not valid" in result.output
    assert "CANARY" not in result.output and SENTINEL_TOKEN not in result.output and KEY_ID not in result.output


@needs_windows_powershell
@pytest.mark.parametrize("code", ["collector_upgrade_required", "unsupported_contract_version", "enrollment_not_ready"])
def test_a_known_refusal_still_says_plainly_that_the_code_was_not_used(tmp_path, code):
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=default_http(**{ENROLL: refusal(code)}), answers=[""])

    assert result.exit_code == 1
    assert "The enrollment code was NOT used." in result.output and "still valid until it expires" in result.output
    assert "UNKNOWN" not in result.output and "may or may not" not in result.output


@needs_windows_powershell
def test_a_rate_limit_and_a_redirect_still_say_the_code_was_not_used(tmp_path):
    for status in (429, 302):
        bundle = make_bundle(tmp_path / str(status))
        http = default_http(**{ENROLL: {"status": status, "content": "", "failure": f"HTTP {status}"}})

        result = run_scenario(bundle, http=http, answers=[""])

        assert result.exit_code == 1 and "The enrollment code was NOT used." in result.output, status
        assert "UNKNOWN" not in result.output


@needs_windows_powershell
def test_a_retry_refused_as_already_used_after_an_unknown_result_directs_to_support(tmp_path):
    """The whole lost-response story: the first request gets no answer (the server may have enrolled), the operator
    retries with the same code as told, and the server now refuses it as already used."""
    bundle = make_bundle(tmp_path)
    first = run_scenario(bundle, http=v2_http(**{ENROLL: {"status": 0, "content": None, "failure": "no response (WebException)"}}),
                         answers=[""])
    assert_ambiguous_outcome_guidance(first)

    already_used = {"status": 400, "content": json.dumps({"detail": "Invalid or expired enrollment code"}), "failure": "HTTP 400"}
    second = run_scenario(bundle, http=v2_http(**{ENROLL: already_used}), answers=[""])

    assert second.exit_code == 1 and len(enroll_posts(second)) == 2  # one request per run: no automatic retry in either
    advice = second.output[second.output.rindex("What to do:"):]
    # The FIRST thing the operator is told covers exactly this case, and it is support -- not a new code.
    assert advice.strip().startswith("What to do:       If an earlier setup attempt with this code on this computer ended with an unknown result")
    assert "that attempt may have completed: contact SortView support and do NOT ask for a new enrollment code" in advice
    assert "The enrollment code was NOT used." not in second.output
    assert second.machine_token is None and second.calls_to("install") == [] and second.task is None
    assert "Invalid or expired enrollment code" not in second.output  # the server's text is not shown


@needs_windows_powershell
def test_a_new_code_refused_as_not_ready_after_an_unknown_result_directs_to_support(tmp_path):
    """If a new code was obtained anyway, the server refuses it (the branch's v2 identity exists): support comes first."""
    bundle = make_bundle(tmp_path)

    result = run_scenario(bundle, http=default_http(**{ENROLL: refusal("enrollment_not_ready")}), answers=[""])

    advice = result.output[result.output.rindex("What to do:"):]
    assert advice.index("If an earlier setup attempt for this installation ended with an unknown result") < advice.index("Otherwise ask your SortView administrator")
    assert "contact SortView support and do NOT ask for another enrollment code: the enrollment may already exist" in advice
    assert "The enrollment code was NOT used." in result.output  # (this request's code was not used -- that much is known)


def test_only_proven_non_consuming_outcomes_are_classified_as_not_used():
    run = SETUP_BODY[SETUP_BODY.index("function Invoke-SetupMain"):]
    assert '$definitelyNotUsed = @("RateLimited", "Redirected") -contains $enrollment.Outcome' in run
    assert '"Unreachable"' not in run[run.index("$definitelyNotUsed = @("):run.index("-contains $enrollment.Outcome")]
    # The ambiguous outcomes share ONE piece of advice, and it never contains the unsafe one.
    same_code = run[run.index("$sameCode = "):run.index("$fix = switch ($enrollment.Outcome) {")]
    assert "Run setup.ps1 again with the SAME code" in same_code and "contact SortView support" in same_code
    switch = run[run.index("$fix = switch ($enrollment.Outcome) {"):run.index("if ($definitelyNotUsed) { $script:State.CodeAttempted = $false }")]
    assert 'default { $sameCode }' in switch and '"Unreachable" { "Check this computer\'s network/proxy. $sameCode" }' in switch
    # No automatic retry was added: still exactly two enrollment call sites (the v2 request and the one legacy fallback).
    assert run.count("Invoke-Enrollment -ApiUrl") == 2
    assert "while (" not in run[run.index("=== STEP 5: redeem"):run.index("$script:State.CodeConsumed = $true")]


# ---- a damaged recovery record ---------------------------------------------------------------

def _damage_record(bundle: Bundle, text: str) -> None:
    (bundle.data_root / "setup" / "enrollment-recovery.json").write_text(text, encoding="utf-8")


@needs_windows_powershell
@pytest.mark.parametrize("damage", ["truncated", "extra_field", "rules_missing_too_but_text_says_v2", "not_json_with_staged_rules"])
def test_a_malformed_v2_recovery_record_directs_to_support_and_never_to_a_new_code(tmp_path, damage):
    bundle, first = _interrupt_v2(tmp_path, env={"SETUPTEST_INSTALL_MODE": "fail"})
    raw = first.recovery_path.read_text(encoding="utf-8")
    if damage == "truncated":
        _damage_record(bundle, raw[: len(raw) // 2])                      # a write cut short; the staged rules are beside it
    elif damage == "extra_field":
        _damage_record(bundle, json.dumps({**json.loads(raw), "unexpected": 1}))
    elif damage == "rules_missing_too_but_text_says_v2":
        staged_rules_path(bundle).unlink()
        _damage_record(bundle, raw.replace('"customer_id"', '"customer"'))
    else:
        _damage_record(bundle, "this is not json")
    record_before = first.recovery_path.read_bytes()
    before = len(first.calls)

    second = run_scenario(bundle, http=_ALREADY_ENROLLED, answers=["", ""])

    assert second.exit_code == 2 and "SETUP STOPPED at step 2 (machine)" in second.output and "not valid" in second.output
    assert "This is the record of a Contract v2 enrollment" in second.output
    assert "Do NOT ask for another enrollment code (it would be refused)" in second.output and "Contact SortView support." in second.output
    assert "NEW enrollment code" not in second.output and "If you are sure it is stale" not in second.output
    # Stopped safely: nothing read from the network, nothing asked for, nothing deleted or rewritten.
    assert [c["tool"] for c in new_calls(second, before)] == ["install-verify", "runtime-version"]
    assert first.recovery_path.read_bytes() == record_before and second.machine_token == SENTINEL_TOKEN
    assert_nothing_sensitive_was_printed(second)


@needs_windows_powershell
def test_a_malformed_legacy_recovery_record_keeps_the_legacy_advice(tmp_path):
    bundle = make_bundle(tmp_path)
    first = run_scenario(bundle, answers=[""], env={"SETUPTEST_INSTALL_MODE": "fail"})
    assert first.recovery is not None and first.recovery["schema_version"] == 1
    raw = first.recovery_path.read_text(encoding="utf-8")
    _damage_record(bundle, raw[: len(raw) // 2])

    second = run_scenario(bundle, answers=["", ""])

    assert second.exit_code == 2 and "not valid" in second.output
    # Unchanged: a legacy enrollment CAN simply be redone with a new code.
    assert "If you are sure it is stale, delete that one file and run setup.ps1 again (a NEW enrollment code will then be needed)" in second.output
    assert "Contract v2" not in second.output[second.output.index("SETUP STOPPED"):]


@needs_windows_powershell
def test_the_v2_record_detector_reads_only_and_never_throws(tmp_path):
    cases = {}
    for name, (record, staged) in {
        "legacy_record": ('{"schema_version": 1, "customer_id": 7', False),
        "v2_by_text": ('{"schema_version": 2, "customer_id": 7', False),
        "v2_by_field": ('{"key_id": "x"', False),
        "v2_by_staged_rules": ("garbage", True),
        "no_record_at_all": (None, False),
    }.items():
        root = tmp_path / name
        (root / "setup").mkdir(parents=True)
        if record is not None:
            (root / "setup" / "enrollment-recovery.json").write_text(record, encoding="utf-8")
        if staged:
            (root / "setup" / "classification_rules.json").write_text("{}", encoding="utf-8")
        cases[name] = root
    script = "\n".join(f"'{name}=' + (Test-RecoveryLooksContractV2 -DataRoot {ps_quote(root)})" for name, root in cases.items())

    results = dict(line.split("=") for line in _dot_source_and_run(script).stdout.split())

    assert results == {"legacy_record": "False", "v2_by_text": "True", "v2_by_field": "True", "v2_by_staged_rules": "True",
                       "no_record_at_all": "False"}
