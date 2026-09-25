<#
.SYNOPSIS
    SortView Collector -- converts an existing PRODUCTION collector_config.json
    from Contract v1 to Contract v2 ("contract_mode": "v2") in place, safely.
    Ships at tools\configure_v2.ps1 in the release bundle (see
    collector/build_release.py's DEPLOY_TOOL_FILES).

.DESCRIPTION
    This tool does EXACTLY ONE THING: it edits collector_config.json so the
    Collector CAN run Contract v2 the next time something runs it. It never
    creates or touches the v2 secret (see tools\v2-key or
    `SortViewCollector.exe v2-key init|check`), never enables the Scheduled
    Task, never sets SORTVIEW_V2_INGEST_ENABLED, and never runs live v2
    ingestion. Key creation, server feature-flag activation, cutover
    insertion and task enabling are DELIBERATELY separate, explicit, later
    operator steps -- never combined into this one.

    Fails closed on every prerequisite problem, and -- critically -- never
    touches the production config file until a CANDIDATE copy of the finished
    result has already validated successfully against the real collector
    config loader. A failure at any step before that leaves the production
    config completely untouched, not merely restored from a backup.

    Sequence:
      1. Elevation check.
      2. -KeyId must be a well-formed lower-case UUID4 (the server-issued
         key_id shape) -- refused before anything is read or written.
      3. The installed runtime at -InstallRoot must exist and report the
         SAME version as this bundle's own MANIFEST.json (never a hardcoded
         literal here -- collector.__version__, via the bundle that ships
         this script, is the one version authority).
      4. The Scheduled Task (-TaskName) must not be ENABLED. Not registered
         at all is fine; ENABLED refuses.
      5. The existing production -ConfigPath must exist and parse as JSON.
         It is read, never modified in place until step 9.
      6. -RulesPath (default: the release-packaged classification rules
         tools\prepare_v2_pilot.ps1 already installs) must already exist and
         be valid JSON. This tool does not generate or copy it -- only
         references an already-installed file.
      7. Builds a CANDIDATE document: the existing production config,
         UNCHANGED in every field (customer_id, branch_id, api_url, sources,
         state_path, status_path, log_path, installation_id, and anything
         else present -- this tool never enumerates or assumes a fixed field
         list), with only `contract_mode: "v2"` and a `v2` section
         (`key_id`, `timezone`, `rules_path`) set or overwritten. No
         `v2.secret_path`, `v2.state_path`, `v2.status_path`,
         `v2.patron_cache_path` or `v2.quarantine_path` is written -- the
         supported defaults (derived from the existing top-level
         `state_path`, see collector/v2_config.py's `load_v2_config`) are
         used instead of inventing incompatible paths.
      8. VALIDATES the candidate as a SEPARATE temporary file, using the
         REAL collector config loader via the installed runtime itself --
         `SortViewCollector.exe support-info` (collector.config.load_config,
         the same loader collector/run.py calls unconditionally) and
         `SortViewCollector.exe run --v2-dry-run` (collector.v2_config's
         load_dry_run_settings, which validates `v2.timezone`, `v2.rules_path`
         and `sources`). Both are network-free and side-effect-free. Neither
         requires (or checks) the identity-collision acceptance gate --
         that is tools\prepare_v2_pilot.ps1's job, a separate step, run
         again against this config once contract_mode is v2.
      9. Only once BOTH validations pass: backs up the current production
         config to a UTC-timestamped copy alongside it, then atomically
         replaces the production config with the validated candidate.
     10. Re-confirms the Scheduled Task is still exactly as found.

    On any failure at steps 2-8, the production config is untouched --
    nothing was ever written to it. This is a stronger guarantee than
    "back up then restore on failure": there is nothing to restore, because
    validation happens on a throwaway copy first.

.EXAMPLE
    .\configure_v2.ps1 `
        -InstallRoot "C:\SortView\Collector" `
        -ConfigPath "C:\ProgramData\SortViewCollector\config\collector_config.json" `
        -KeyId "b04c3dc1-7651-4803-a593-12272dd3cfc3"
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\SortView\Collector",
    [string]$ConfigPath = "C:\ProgramData\SortViewCollector\config\collector_config.json",
    # NOT [Parameter(Mandatory)] here (unlike Invoke-ConfigureV2Main's own $KeyId below) --
    # a top-level Mandatory parameter is evaluated the instant this file is DOT-SOURCED, before
    # the "run only when executed as a script" guard at the bottom ever runs, which would break
    # every dot-sourced test (tests/test_configure_v2.py) even when it never calls this script's
    # own top-level scope. Invoke-ConfigureV2Main's Mandatory declaration is what actually
    # enforces -KeyId being required for a real run; $PSBoundParameters only carries -KeyId
    # through to it when the operator explicitly passed one.
    [string]$KeyId = "",
    [string]$RulesPath = "C:\ProgramData\SortViewCollector\config\classification_rules.json",
    [string]$Timezone = "America/Chicago",
    [string]$TaskName = "SortView Collector"
)

$ErrorActionPreference = "Stop"

# === side-effect wrappers (touch the machine or run the installed executable; tests replace
# exactly the effect they are exercising, same convention as prepare_v2_pilot.ps1) ==============

function Test-IsAdministrator {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-SortViewTaskInfo {
    # $null when there is no such task. A failed lookup is thrown, never read as "absent".
    param([string]$TaskName)
    return (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
}

function Get-CollectorRuntimeVersion {
    # Asks the installed runtime what version it actually is (its config-free
    # `version` command). Throws unless it exits 0 and prints exactly one version.
    param([string]$ExePath)
    $lines = $null
    $global:LASTEXITCODE = $null
    try {
        $lines = @(& $ExePath version)
    } catch {
        throw "could not run '$ExePath version': $($_.Exception.Message)"
    }
    if ($null -ne $global:LASTEXITCODE -and $global:LASTEXITCODE -ne 0) {
        throw "'$ExePath version' exited with code $($global:LASTEXITCODE)"
    }
    $text = (($lines | ForEach-Object { [string]$_ }) -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($text)) { throw "'$ExePath version' printed no version" }
    if ($text -notmatch '^[0-9A-Za-z][0-9A-Za-z.+\-]*$') { throw "'$ExePath version' printed something that is not a version: '$text'" }
    return $text
}

function Invoke-CollectorExeCommand {
    # Same PS-5.1-safe subprocess wrapper as prepare_v2_pilot.ps1's (see that file's own comment
    # for the full rationale): Windows PowerShell 5.1 turns each merged native stderr line into
    # an ErrorRecord, which aborts here under the script-wide "Stop" before the exit code can be
    # read, unless the preference is relaxed for this one call and always restored.
    param([string]$ExePath, [string[]]$Arguments)
    $global:LASTEXITCODE = $null
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $lines = @(& $ExePath @Arguments 2>&1)
        $code = $global:LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedPreference
    }
    foreach ($line in $lines) {
        if ($line -is [System.Management.Automation.ErrorRecord] -and
            $line.FullyQualifiedErrorId -notlike "NativeCommandError*") {
            throw $line
        }
    }
    $text = (($lines | ForEach-Object { [string]$_ }) -join "`n")
    $resolvedExitCode = if ($null -ne $code) { [int]$code } else { 0 }
    return [pscustomobject]@{ ExitCode = $resolvedExitCode; Output = $text }
}

function Invoke-SupportInfo {
    # `<exe> support-info --config <config>` -- exercises collector.config.load_config (the SAME
    # loader collector/run.py calls unconditionally, v1 or v2), network-free, side-effect-free.
    # Exit 0 iff the config loaded.
    param([string]$ExePath, [string]$ConfigPath)
    return Invoke-CollectorExeCommand -ExePath $ExePath -Arguments @("support-info", "--config", $ConfigPath)
}

function Invoke-V2DryRun {
    # `<exe> run --config <config> --v2-dry-run` -- exercises collector.v2_config.load_dry_run_settings
    # (validates v2.timezone, v2.rules_path, sources). Throwaway key, no persistent secret, no
    # network call, no state/cursor write, no live ingestion. Exit 0 iff the v2 section validates.
    param([string]$ExePath, [string]$ConfigPath)
    return Invoke-CollectorExeCommand -ExePath $ExePath -Arguments @("run", "--config", $ConfigPath, "--v2-dry-run")
}

# === pure helpers (touch nothing; exercised directly by tests) =================================

function Stop-ConfigureV2 {
    # One place for every fail-closed stop: thrown (not `exit`) so the caller's catch always
    # runs and tests can drive the whole flow in-process.
    param([Parameter(Mandatory)][string]$Problem, [int]$ExitCode = 1)
    $ex = New-Object System.Management.Automation.RuntimeException($Problem)
    $ex.Data["SortViewConfigureV2ExitCode"] = $ExitCode
    throw $ex
}

function Test-KeyIdFormat {
    # Mirrors collector/v2_events.py's UUID4_PATTERN exactly (a lower-case UUID4) -- a format
    # sanity check on OPERATOR INPUT before anything is read or written, not a substitute for
    # the real validation that happens later (load_v2_config's own validate_key_id, exercised
    # via the installed runtime in Invoke-V2DryRun). tests/test_configure_v2.py compares this
    # pattern against the Python source directly, so the two can never silently drift apart.
    param([string]$KeyId)
    return $KeyId -cmatch '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
}

function New-V2ConfigDocument {
    # The existing production document, UNCHANGED in every field, with only `contract_mode` and
    # `v2` set or overwritten. Never enumerates a fixed field list -- clones the whole object (a
    # shallow copy; this function never mutates $ExistingDocument) and touches only the two keys
    # this tool's job is to set, so customer_id/branch_id/api_url/sources/state_path/status_path/
    # log_path/installation_id/anything-else-present all pass through exactly as they were.
    param(
        [Parameter(Mandatory)]$ExistingDocument,
        [Parameter(Mandatory)][string]$KeyId,
        [Parameter(Mandatory)][string]$Timezone,
        [Parameter(Mandatory)][string]$RulesPath
    )
    $document = $ExistingDocument.PSObject.Copy()
    $v2Section = [ordered]@{ key_id = $KeyId; timezone = $Timezone; rules_path = $RulesPath }

    if ($document.PSObject.Properties.Name -contains "contract_mode") {
        $document.contract_mode = "v2"
    } else {
        $document | Add-Member -NotePropertyName "contract_mode" -NotePropertyValue "v2"
    }
    if ($document.PSObject.Properties.Name -contains "v2") {
        $document.v2 = $v2Section
    } else {
        $document | Add-Member -NotePropertyName "v2" -NotePropertyValue $v2Section
    }
    return $document
}

# === orchestration ===============================================================================

function Invoke-ConfigureV2Main {
    [CmdletBinding()]
    param(
        [string]$InstallRoot = "C:\SortView\Collector",
        [string]$ConfigPath = "C:\ProgramData\SortViewCollector\config\collector_config.json",
        [Parameter(Mandatory)][string]$KeyId,
        [string]$RulesPath = "C:\ProgramData\SortViewCollector\config\classification_rules.json",
        [string]$Timezone = "America/Chicago",
        [string]$TaskName = "SortView Collector"
    )
    $exitCode = 1
    $backupPath = $null
    try {
        Write-Host "=== SortView Collector -- Contract v2 production config conversion ===" -ForegroundColor Cyan

        # --- Step 0: elevation, before anything else -----------------------------------
        if (-not (Test-IsAdministrator)) {
            Stop-ConfigureV2 -Problem "This script must be run from an elevated (Administrator) PowerShell session." -ExitCode 1
        }

        # --- Step 1: key_id format, before anything is read or written -------------------
        Write-Host "=== Step 1: key_id format ===" -ForegroundColor Cyan
        if (-not (Test-KeyIdFormat -KeyId $KeyId)) {
            Stop-ConfigureV2 -Problem "-KeyId '$KeyId' is not a lower-case UUID4 (server-issued key_id format) -- refusing before touching anything." -ExitCode 1
        }
        Write-Host "  OK: -KeyId is a well-formed UUID4." -ForegroundColor Green

        # --- Step 2: installed runtime, and its version against THIS bundle's own manifest ---
        Write-Host "=== Step 2: installed runtime and version ===" -ForegroundColor Cyan
        $exePath = Join-Path $InstallRoot "SortViewCollector.exe"
        if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
            Stop-ConfigureV2 -Problem "No installed Collector runtime found at '$exePath'. Install/update the release first." -ExitCode 1
        }
        $bundleRoot = Split-Path $PSScriptRoot -Parent
        $manifestPath = Join-Path $bundleRoot "MANIFEST.json"
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
            Stop-ConfigureV2 -Problem "This bundle has no MANIFEST.json -- cannot verify the installed runtime's version against it." -ExitCode 1
        }
        $bundleVersion = $null
        try {
            $bundleVersion = [string]((Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).version)
        } catch {
            Stop-ConfigureV2 -Problem "MANIFEST.json could not be read: $($_.Exception.Message)" -ExitCode 1
        }
        $installedVersion = $null
        try {
            $installedVersion = Get-CollectorRuntimeVersion -ExePath $exePath
        } catch {
            Stop-ConfigureV2 -Problem "The installed runtime would not report its version: $($_.Exception.Message)" -ExitCode 1
        }
        if ($installedVersion -cne $bundleVersion) {
            Stop-ConfigureV2 -Problem "Installed Collector reports version '$installedVersion' but this release bundle is '$bundleVersion' -- refusing to run against a mismatched runtime." -ExitCode 1
        }
        Write-Host "  OK: installed Collector is $installedVersion." -ForegroundColor Green

        # --- Step 3: the Scheduled Task must not be enabled -----------------------------
        Write-Host "=== Step 3: Scheduled Task disabled ===" -ForegroundColor Cyan
        $task = Get-SortViewTaskInfo -TaskName $TaskName
        if ($null -ne $task -and [bool]$task.Settings.Enabled) {
            Stop-ConfigureV2 -Problem "Scheduled Task '$TaskName' is ENABLED. Refusing to proceed -- disable it first (Disable-ScheduledTask -TaskName '$TaskName'), confirm it stays disabled, and re-run this tool." -ExitCode 1
        }
        if ($null -eq $task) {
            Write-Host "  NOTE: Scheduled Task '$TaskName' is not registered -- nothing to check as disabled." -ForegroundColor Yellow
        } else {
            Write-Host "  OK: Scheduled Task '$TaskName' is present and disabled." -ForegroundColor Green
        }

        # --- Step 4: the existing production config must exist and parse as JSON --------
        Write-Host "=== Step 4: existing production config ===" -ForegroundColor Cyan
        if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
            Stop-ConfigureV2 -Problem "Production config not found: '$ConfigPath'." -ExitCode 1
        }
        $existing = $null
        try {
            $existing = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            Stop-ConfigureV2 -Problem "Production config is not valid JSON: $($_.Exception.Message)" -ExitCode 1
        }
        Write-Host "  OK: read '$ConfigPath'." -ForegroundColor Green

        # --- Step 5: the rules file must already exist and be valid JSON ----------------
        Write-Host "=== Step 5: classification rules ===" -ForegroundColor Cyan
        if (-not (Test-Path -LiteralPath $RulesPath -PathType Leaf)) {
            Stop-ConfigureV2 -Problem "-RulesPath '$RulesPath' does not exist -- install the release-packaged classification rules first (tools\prepare_v2_pilot.ps1's Step 4), or pass the correct -RulesPath." -ExitCode 1
        }
        try {
            $null = Get-Content -LiteralPath $RulesPath -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            Stop-ConfigureV2 -Problem "-RulesPath '$RulesPath' is not valid JSON: $($_.Exception.Message)" -ExitCode 1
        }
        Write-Host "  OK: '$RulesPath' exists and is valid JSON." -ForegroundColor Green

        # --- Step 6: build the candidate v2 config ---------------------------------------
        Write-Host "=== Step 6: candidate v2 config ===" -ForegroundColor Cyan
        $candidate = New-V2ConfigDocument -ExistingDocument $existing -KeyId $KeyId -Timezone $Timezone -RulesPath $RulesPath
        $json = $candidate | ConvertTo-Json -Depth 10
        try {
            $null = $json | ConvertFrom-Json
        } catch {
            Stop-ConfigureV2 -Problem "The generated v2 config failed to validate as JSON: $($_.Exception.Message)" -ExitCode 1
        }
        Write-Host "  OK: candidate document built (contract_mode: v2, v2.key_id/.timezone/.rules_path set)." -ForegroundColor Green

        # --- Step 7: validate the candidate with the REAL collector config loader, as a
        #             SEPARATE file -- production is not touched until this passes ---------
        Write-Host "=== Step 7: validating the candidate (production untouched so far) ===" -ForegroundColor Cyan
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        $candidatePath = "$ConfigPath.v2-candidate-$([guid]::NewGuid().ToString('N')).tmp"
        [System.IO.File]::WriteAllText($candidatePath, $json, $utf8NoBom)
        try {
            $supportInfo = Invoke-SupportInfo -ExePath $exePath -ConfigPath $candidatePath
            Write-Host $supportInfo.Output
            if ($supportInfo.ExitCode -ne 0) {
                Stop-ConfigureV2 -Problem "The candidate config did not load with the real collector config loader (support-info exited $($supportInfo.ExitCode)) -- production config left unchanged. If this is unexpected, confirm SORTVIEW_API_TOKEN is visible to THIS process (a Machine-scope variable set after this shell started needs a fresh elevated session)." -ExitCode 1
            }
            $dryRun = Invoke-V2DryRun -ExePath $exePath -ConfigPath $candidatePath
            Write-Host $dryRun.Output
            if ($dryRun.ExitCode -ne 0) {
                Stop-ConfigureV2 -Problem "The candidate config's v2 section did not validate (run --v2-dry-run exited $($dryRun.ExitCode)) -- production config left unchanged." -ExitCode 1
            }
        } finally {
            Remove-Item -LiteralPath $candidatePath -Force -ErrorAction SilentlyContinue
        }
        Write-Host "  OK: candidate config loads, and its v2 section validates." -ForegroundColor Green

        # --- Step 8: only now -- back up the current production config ------------------
        Write-Host "=== Step 8: backup ===" -ForegroundColor Cyan
        $timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmss") + "Z"
        $backupPath = "$ConfigPath.bak-$timestamp"
        Copy-Item -LiteralPath $ConfigPath -Destination $backupPath
        Write-Host "  OK: backed up to '$backupPath'." -ForegroundColor Green

        # --- Step 9: atomically replace the production config with the validated candidate ---
        Write-Host "=== Step 9: writing the v2 config ===" -ForegroundColor Cyan
        $finalTmp = "$ConfigPath.v2-final-$([guid]::NewGuid().ToString('N')).tmp"
        [System.IO.File]::WriteAllText($finalTmp, $json, $utf8NoBom)
        Move-Item -LiteralPath $finalTmp -Destination $ConfigPath -Force
        Write-Host "  OK: wrote contract_mode: v2 to '$ConfigPath'." -ForegroundColor Green

        # --- Final safety re-check: the Scheduled Task is still exactly as found --------
        $taskAfter = Get-SortViewTaskInfo -TaskName $TaskName
        if ($null -ne $taskAfter -and [bool]$taskAfter.Settings.Enabled) {
            Stop-ConfigureV2 -Problem "SAFETY VIOLATION: Scheduled Task '$TaskName' became ENABLED during this run. Do not trust this result; investigate before doing anything else." -ExitCode 1
        }

        Write-Host ""
        Write-Host "V2 CONFIG CONVERSION: PASS" -ForegroundColor Green
        Write-Host "contract_mode is now 'v2' in '$ConfigPath'."
        Write-Host "Backup of the previous config: '$backupPath'."
        Write-Host "Scheduled Task remains disabled."
        Write-Host "No live v2 ingestion has run. SORTVIEW_V2_INGEST_ENABLED was not touched."
        Write-Host ""
        Write-Host "Next steps (explicit, separate, NOT performed by this tool):"
        Write-Host "  1. SortViewCollector.exe v2-key init --config `"$ConfigPath`"   (creates and protects the local secret)"
        Write-Host "  2. SortViewCollector.exe v2-key check --config `"$ConfigPath`"  (verifies it)"
        Write-Host "  3. Re-run tools\prepare_v2_pilot.ps1 (or run --v2-dry-run / identity-collision-diag directly) against this config"
        Write-Host "  4. Only when ready: enable the Scheduled Task"
        $exitCode = 0
    } catch {
        $stopCode = $null
        if ($_.Exception.Data.Contains("SortViewConfigureV2ExitCode")) { $stopCode = [int]$_.Exception.Data["SortViewConfigureV2ExitCode"] }
        Write-Host ""
        Write-Host "V2 CONFIG CONVERSION: FAILED" -ForegroundColor Red
        Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
        $exitCode = if ($null -ne $stopCode) { $stopCode } else { 1 }
    }
    return $exitCode
}

# Run only when executed as a script. Dot-sourcing (. .\configure_v2.ps1) just defines the
# functions above -- how tests drive the flow with side effects replaced -- and runs nothing.
if ($MyInvocation.InvocationName -ne ".") {
    $result = @(Invoke-ConfigureV2Main @PSBoundParameters)
    exit ([int]$result[-1])
}
