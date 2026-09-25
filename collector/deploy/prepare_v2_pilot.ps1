<#
.SYNOPSIS
    SortView Collector -- Contract v2 NBPL pilot preparation. Installs
    the release-packaged classification rules, generates a DEDICATED
    dry-run-only config, runs `SortViewCollector.exe run --v2-dry-run`
    against it, and evaluates the onsite acceptance gate
    (docs/collector-v2.md's "Onsite dry-run acceptance check") -- which now
    includes running `SortViewCollector.exe identity-collision-diag` against
    the SAME dry-run config and requiring its bounded, source/category-aware
    identity_collision_gate=pass (collector/identity_collision_diag.py; NOT a
    flat identical_identity_events == 0 requirement -- see that module's own
    docstring for why). Ships at tools\prepare_v2_pilot.ps1 in the release
    bundle (see collector/build_release.py's DEPLOY_TOOL_FILES).

.DESCRIPTION
    This tool STOPS AT DRY-RUN. It never sets `contract_mode: "v2"`, never
    creates or touches a v2 secret/key, never enables the Scheduled Task,
    never runs live v2 ingestion, and never modifies the v1 state cursor.
    Server/key/cutover preparation is a deliberately separate, later step.

    Fails closed on every prerequisite problem -- nothing after a failed
    check runs, and the Scheduled Task is left exactly as found (disabled)
    whether this tool succeeds or fails.

    Sequence:
      1. Elevation check.
      2. The installed runtime at -InstallRoot must exist and report the
         SAME version as this bundle's own MANIFEST.json (never a
         hardcoded literal here -- collector.__version__, via the bundle
         that ships this script, is the one version authority; see
         collector/__init__.py and docs/release-process.md).
      3. The Scheduled Task (-TaskName) must not be ENABLED. Not
         registered at all is fine (nothing to fire); ENABLED refuses.
      4. The packaged pilot rules artifact (this bundle's own
         pilot\classification_rules.json -- generated at BUILD TIME from
         src/branch_settings.json by collector/build_release.py, see its
         own docstring) is validated as JSON and copied to
         -RulesDestPath, overwriting only that pilot-owned destination
         file.
      5. The three source paths (acs, checkins, rejects) are read from the
         EXISTING production -ProductionConfigPath -- never hardcoded --
         and written into a NEW, separate, dry-run-only config at
         -DryRunConfigPath with this NBPL pilot's fixed -Timezone and
         `v2.rules_path` pointed at -RulesDestPath. `contract_mode` is
         never written (so it is absent, the v1 default) and no `v2.key_id`
         or secret path is written -- the dry run needs neither. The
         production config itself is opened read-only; this tool verifies
         its content is byte-for-byte unchanged before reporting success.
      6. Runs the dry run against -DryRunConfigPath and confirms every exact
         safety counter (source_*_present, throwaway_key,
         persistent_secret_used, network_calls, dry_run_complete). Any
         missing or wrong counter fails closed, reports FAILED, and exits
         nonzero without suggesting any further action.
      7. Runs `identity-collision-diag` against the SAME -DryRunConfigPath
         and requires its printed `identity_collision_gate=pass`. This is a
         BOUNDED, source/category-aware rule (collector/identity_collision_diag.py),
         not a flat "must be zero" check: it tolerates only the two
         collapse patterns already documented and tested as intentional --
         an ACS hold read twice, unchanged, in the same second, and a
         small, spread-out, low-rate cluster of barcode-less
         `ils_acs_failure`/`rfid_collision` reject duplicates -- and fails
         closed on anything else (an item-keyed checkin/reject/non-hold-ACS
         collision, an oversized group, an unexpected reject class, a rate
         or clustering breach). `identical_identity_events` is still always
         printed by that tool, never hidden. On failure, its full per-source
         detail (already printed above the gate verdict) is what to
         investigate next -- see docs/collector-v2.md's "Onsite dry-run
         acceptance check".

.EXAMPLE
    .\prepare_v2_pilot.ps1 `
        -InstallRoot "C:\SortView\Collector" `
        -ProductionConfigPath "C:\ProgramData\SortViewCollector\config\collector_config.json"
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\SortView\Collector",
    [string]$ProductionConfigPath = "C:\ProgramData\SortViewCollector\config\collector_config.json",
    [string]$DryRunConfigPath = "C:\ProgramData\SortViewCollector\config\collector_config.v2-dry-run.json",
    [string]$RulesDestPath = "C:\ProgramData\SortViewCollector\config\classification_rules.json",
    [string]$TaskName = "SortView Collector",
    [string]$Timezone = "America/Chicago"
)

$ErrorActionPreference = "Stop"

# === side-effect wrappers ====================================================
# Everything that touches the machine or runs the installed executable goes
# through one of these small functions -- the orchestration below never calls
# the underlying cmdlet/process directly, so tests can replace exactly the
# effect they are exercising (tests/test_v2_pilot_release_prep.py dot-sources
# this file and redefines them) while a real run uses these.

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
    # Shared PS-5.1-safe subprocess wrapper: runs `<exe> <arguments...>`, merges stdout and
    # stderr into one text blob, and returns the exit code -- used by every subcommand
    # invocation below (the dry run and the identity-collision acceptance gate) so the fix
    # below can never accidentally apply to only one of them.
    #
    # Windows PowerShell 5.1 turns each merged native stderr line into an ErrorRecord, and
    # under the script-wide "Stop" that aborts here before the exit code can be read. Relax
    # the preference for this one native call only, and always restore it. Only the child's
    # own stderr lines are tolerated; any other PowerShell error the relaxed preference let
    # through (e.g. the exe could not be started) is re-raised.
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

    $resolvedExitCode = if ($null -ne $code) {
        [int]$code
    } else {
        0
    }
    return [pscustomobject]@{
        ExitCode = $resolvedExitCode
        Output   = $text
    }
}

function Invoke-CollectorDryRun {
    # Runs `<exe> run --config <config> --v2-dry-run`. stdout carries the "name=integer"
    # counters; stderr carries a "Configuration error: ..." line on a config problem --
    # merged here so an operator sees both either way; counter parsing below only matches
    # "name=integer" lines, so a stray error line is ignored by the parser, not mistaken
    # for a counter.
    param([string]$ExePath, [string]$ConfigPath)
    return Invoke-CollectorExeCommand -ExePath $ExePath -Arguments @("run", "--config", $ConfigPath, "--v2-dry-run")
}

function Invoke-IdentityCollisionDiag {
    # Runs `<exe> identity-collision-diag --config <config>` against the SAME dry-run config
    # -- same throwaway-key/no-secret/no-network/no-state-write contract as the dry run
    # itself (collector/identity_collision_diag.py). Its output ends with a `=== gate ===`
    # block whose `identity_collision_gate=pass|fail` line is the bounded acceptance verdict
    # (see Test-IdentityCollisionGatePassed) -- the threshold logic itself lives once, in
    # Python, never duplicated here.
    param([string]$ExePath, [string]$ConfigPath)
    return Invoke-CollectorExeCommand -ExePath $ExePath -Arguments @("identity-collision-diag", "--config", $ConfigPath)
}

# === pure helpers (touch nothing; exercised directly by tests) ===============

function Stop-PilotPrep {
    # One place for every fail-closed stop: thrown (not `exit`) so the caller's
    # catch always runs and tests can drive the whole flow in-process.
    param([Parameter(Mandatory)][string]$Problem, [int]$ExitCode = 1)
    $ex = New-Object System.Management.Automation.RuntimeException($Problem)
    $ex.Data["SortViewPilotPrepExitCode"] = $ExitCode
    throw $ex
}

function Get-ProductionDryRunSources {
    # Reads the EXISTING production collector_config.json and returns the three source
    # paths (acs, checkins, rejects) it already has -- never hardcoded, never invented.
    # Throws a plain error (the caller decides the exit code via Stop-PilotPrep) if any
    # of the three is absent or empty.
    param([string]$ConfigPath)
    $raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
    $doc = $raw | ConvertFrom-Json
    if ($null -eq $doc.sources) { throw "the production config has no 'sources' array" }
    $byName = @{}
    foreach ($entry in @($doc.sources)) {
        if ($null -ne $entry.name -and $null -ne $entry.path -and [string]$entry.path) {
            $byName[[string]$entry.name] = [string]$entry.path
        }
    }
    $required = @("acs", "checkins", "rejects")
    $missing = @($required | Where-Object { -not $byName.ContainsKey($_) })
    if ($missing.Count -gt 0) {
        throw "the production config's 'sources' is missing (or has an empty path for): $($missing -join ', ')"
    }
    return $byName
}

function New-DryRunConfigDocument {
    # Everything `run --v2-dry-run` needs and NOTHING else (docs/collector-v2.md's Dry
    # run section): the three source paths, the fixed NBPL pilot timezone, and rules_path.
    # No contract_mode, no v2.key_id, no secret/state/status/log path.
    param([hashtable]$SourcePaths, [string]$Timezone, [string]$RulesPath)
    $sources = @(
        foreach ($name in @("acs", "checkins", "rejects")) {
            [ordered]@{ name = $name; path = $SourcePaths[$name] }
        }
    )
    return [ordered]@{
        sources = $sources
        v2      = [ordered]@{
            timezone   = $Timezone
            rules_path = $RulesPath
        }
    }
}

$script:RequiredExactCounters = [ordered]@{
    source_acs_present        = 1
    source_checkins_present   = 1
    source_rejects_present    = 1
    throwaway_key              = 1
    persistent_secret_used     = 0
    network_calls               = 0
    dry_run_complete            = 1
}

function ConvertTo-DryRunCounters {
    # Parses "name=integer" lines out of the dry run's combined output into a hashtable.
    # Any other line (blank, or a stray "Configuration error: ..." on stderr) is ignored here,
    # not fatal by itself -- a missing/wrong counter is what Test-DryRunAcceptance reports.
    param([string]$Text)
    $counters = @{}
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match '^([a-z][a-z0-9_]*)=(-?\d+)$') {
            $counters[$Matches[1]] = [int]$Matches[2]
        }
    }
    return $counters
}

function Test-DryRunAcceptance {
    # Every problem found (missing counter, or one whose value differs from the required exact
    # value), or an empty array when every required counter is present and correct.
    # `identical_identity_events` is deliberately NOT in $script:RequiredExactCounters -- it is
    # evaluated by the bounded gate instead (Test-IdentityCollisionGatePassed, below), never a
    # flat "must be zero" check here.
    param([hashtable]$Counters)
    $problems = @()
    foreach ($name in $script:RequiredExactCounters.Keys) {
        if (-not $Counters.ContainsKey($name)) {
            $problems += "missing required counter: $name"
            continue
        }
        $expected = $script:RequiredExactCounters[$name]
        $actual = $Counters[$name]
        if ($actual -ne $expected) {
            $problems += "$name=$actual (required: $expected)"
        }
    }
    return $problems
}

function Test-IdentityCollisionGatePassed {
    # True only if `identity-collision-diag`'s own output explicitly reports
    # identity_collision_gate=pass. The bounded-collision rule itself (max group size, the
    # permitted ACS-hold-duplicate pattern, the permitted keyless reject classes, the collision
    # rate and time-spread ceilings) is evaluated ONCE, in Python
    # (collector/identity_collision_diag.py's _evaluate_gate), never re-implemented here -- that
    # keeps the two thresholds from ever silently drifting apart. A missing line (an older
    # runtime, or truncated/garbled output) is treated as NOT passing -- fail closed, same as
    # every other check in this tool. See tests/test_collector_identity_collision_diag.py for
    # that rule's own test coverage.
    param([string]$Text)
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match '^identity_collision_gate=(pass|fail)$') {
            return $Matches[1] -eq "pass"
        }
    }
    return $false
}

# === orchestration ============================================================

function Invoke-PrepareV2PilotMain {
    [CmdletBinding()]
    param(
        [string]$InstallRoot = "C:\SortView\Collector",
        [string]$ProductionConfigPath = "C:\ProgramData\SortViewCollector\config\collector_config.json",
        [string]$DryRunConfigPath = "C:\ProgramData\SortViewCollector\config\collector_config.v2-dry-run.json",
        [string]$RulesDestPath = "C:\ProgramData\SortViewCollector\config\classification_rules.json",
        [string]$TaskName = "SortView Collector",
        [string]$Timezone = "America/Chicago"
    )
    $exitCode = 1
    try {
        Write-Host "=== SortView Collector -- Contract v2 NBPL pilot preparation (dry-run only) ===" -ForegroundColor Cyan

        # --- Step 0: elevation, before anything else -----------------------------------
        if (-not (Test-IsAdministrator)) {
            Stop-PilotPrep -Problem "This script must be run from an elevated (Administrator) PowerShell session." -ExitCode 1
        }

        # --- Step 1: installed runtime, and its version against THIS bundle's own manifest ---
        Write-Host "=== Step 1: installed runtime and version ===" -ForegroundColor Cyan
        $exePath = Join-Path $InstallRoot "SortViewCollector.exe"
        if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
            Stop-PilotPrep -Problem "No installed Collector runtime found at '$exePath'. Install/update the release first." -ExitCode 1
        }

        $bundleRoot = Split-Path $PSScriptRoot -Parent
        $manifestPath = Join-Path $bundleRoot "MANIFEST.json"
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
            Stop-PilotPrep -Problem "This bundle has no MANIFEST.json -- cannot verify the installed runtime's version against it." -ExitCode 1
        }
        $bundleVersion = $null
        try {
            $bundleVersion = [string]((Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).version)
        } catch {
            Stop-PilotPrep -Problem "MANIFEST.json could not be read: $($_.Exception.Message)" -ExitCode 1
        }

        $installedVersion = $null
        try {
            $installedVersion = Get-CollectorRuntimeVersion -ExePath $exePath
        } catch {
            Stop-PilotPrep -Problem "The installed runtime would not report its version: $($_.Exception.Message)" -ExitCode 1
        }
        if ($installedVersion -cne $bundleVersion) {
            Stop-PilotPrep -Problem "Installed Collector reports version '$installedVersion' but this release bundle is '$bundleVersion' -- refusing to run a pilot preparation tool against a mismatched runtime." -ExitCode 1
        }
        Write-Host "  OK: installed Collector is $installedVersion." -ForegroundColor Green

        # --- Step 2: the Scheduled Task must not be enabled -----------------------------
        Write-Host "=== Step 2: Scheduled Task disabled ===" -ForegroundColor Cyan
        $task = Get-SortViewTaskInfo -TaskName $TaskName
        if ($null -ne $task -and [bool]$task.Settings.Enabled) {
            Stop-PilotPrep -Problem "Scheduled Task '$TaskName' is ENABLED. Refusing to proceed -- disable it first (Disable-ScheduledTask -TaskName '$TaskName'), confirm it stays disabled, and re-run this tool." -ExitCode 1
        }
        if ($null -eq $task) {
            Write-Host "  NOTE: Scheduled Task '$TaskName' is not registered -- nothing to check as disabled." -ForegroundColor Yellow
        } else {
            Write-Host "  OK: Scheduled Task '$TaskName' is present and disabled." -ForegroundColor Green
        }

        # --- Step 3: the existing production config, read-only ---------------------------
        Write-Host "=== Step 3: production config (read-only) ===" -ForegroundColor Cyan
        if (-not (Test-Path -LiteralPath $ProductionConfigPath -PathType Leaf)) {
            Stop-PilotPrep -Problem "Production config not found: '$ProductionConfigPath'." -ExitCode 1
        }
        $configHashBefore = (Get-FileHash -LiteralPath $ProductionConfigPath -Algorithm SHA256).Hash
        $sourcePaths = $null
        try {
            $sourcePaths = Get-ProductionDryRunSources -ConfigPath $ProductionConfigPath
        } catch {
            Stop-PilotPrep -Problem "Could not derive source paths from the production config: $($_.Exception.Message)" -ExitCode 1
        }
        Write-Host "  OK: derived source paths for acs, checkins, rejects from '$ProductionConfigPath'." -ForegroundColor Green

        # --- Step 4: install the packaged pilot classification rules --------------------
        Write-Host "=== Step 4: classification rules ===" -ForegroundColor Cyan
        $rulesSource = Join-Path $bundleRoot "pilot\classification_rules.json"
        if (-not (Test-Path -LiteralPath $rulesSource -PathType Leaf)) {
            Stop-PilotPrep -Problem "This release bundle is missing its packaged pilot rules artifact ('pilot\classification_rules.json')." -ExitCode 1
        }
        try {
            $null = Get-Content -LiteralPath $rulesSource -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            Stop-PilotPrep -Problem "The bundled pilot rules artifact is not valid JSON: $($_.Exception.Message)" -ExitCode 1
        }
        New-Item -ItemType Directory -Force -Path (Split-Path $RulesDestPath -Parent) | Out-Null
        Copy-Item -LiteralPath $rulesSource -Destination $RulesDestPath -Force
        try {
            $null = Get-Content -LiteralPath $RulesDestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            Stop-PilotPrep -Problem "The installed classification rules file is not valid JSON after copying: $($_.Exception.Message)" -ExitCode 1
        }
        Write-Host "  OK: classification rules installed at '$RulesDestPath'." -ForegroundColor Green

        # --- Step 5: generate the dedicated dry-run config -------------------------------
        Write-Host "=== Step 5: dedicated dry-run config ===" -ForegroundColor Cyan
        $document = New-DryRunConfigDocument -SourcePaths $sourcePaths -Timezone $Timezone -RulesPath $RulesDestPath
        $json = $document | ConvertTo-Json -Depth 6
        try {
            $null = $json | ConvertFrom-Json
        } catch {
            Stop-PilotPrep -Problem "The generated dry-run config failed to validate as JSON: $($_.Exception.Message)" -ExitCode 1
        }
        New-Item -ItemType Directory -Force -Path (Split-Path $DryRunConfigPath -Parent) | Out-Null
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($DryRunConfigPath, $json, $utf8NoBom)
        Write-Host "  OK: wrote '$DryRunConfigPath' (contract_mode absent, no key_id, no secret)." -ForegroundColor Green

        # --- Step 6: run the dry run and evaluate the acceptance gate --------------------
        Write-Host "=== Step 6: v2 dry run ===" -ForegroundColor Cyan
        Write-Host "  $exePath run --config `"$DryRunConfigPath`" --v2-dry-run"
        $dryRun = Invoke-CollectorDryRun -ExePath $exePath -ConfigPath $DryRunConfigPath
        Write-Host $dryRun.Output
        if ($dryRun.ExitCode -ne 0) {
            Stop-PilotPrep -Problem "The dry run exited with code $($dryRun.ExitCode) -- see output above." -ExitCode 1
        }
        $counters = ConvertTo-DryRunCounters -Text $dryRun.Output
        $problems = @(Test-DryRunAcceptance -Counters $counters)
        if ($problems.Count -gt 0) {
            $joined = ($problems -join "`n  ")
            Stop-PilotPrep -Problem "Dry-run acceptance check failed:`n  $joined`nNot proceeding to any live-v2 action." -ExitCode 1
        }

        # --- Step 6b: bounded identity-collision acceptance gate -------------------------
        Write-Host "=== Step 6b: identity-collision acceptance gate ===" -ForegroundColor Cyan
        Write-Host "  $exePath identity-collision-diag --config `"$DryRunConfigPath`""
        $collisionDiag = Invoke-IdentityCollisionDiag -ExePath $exePath -ConfigPath $DryRunConfigPath
        Write-Host $collisionDiag.Output
        if ($collisionDiag.ExitCode -ne 0) {
            Stop-PilotPrep -Problem "identity-collision-diag exited with code $($collisionDiag.ExitCode) -- see output above." -ExitCode 1
        }
        if (-not (Test-IdentityCollisionGatePassed -Text $collisionDiag.Output)) {
            Stop-PilotPrep -Problem "Identity-collision acceptance gate failed (identity_collision_gate != pass) -- see the per-source detail above for investigation. Not proceeding to any live-v2 action." -ExitCode 1
        }

        # --- Final safety re-check: the production config is still byte-for-byte identical ---
        $configHashAfter = (Get-FileHash -LiteralPath $ProductionConfigPath -Algorithm SHA256).Hash
        if ($configHashAfter -ne $configHashBefore) {
            Stop-PilotPrep -Problem "SAFETY VIOLATION: production config '$ProductionConfigPath' changed during this run. Do not trust this result; investigate before doing anything else." -ExitCode 1
        }

        Write-Host ""
        Write-Host "V2 PILOT DRY RUN: PASS" -ForegroundColor Green
        Write-Host "No network calls made."
        Write-Host "Production v1 config unchanged."
        Write-Host "Scheduled Task remains disabled."
        Write-Host "Ready for separate server/key/cutover preparation."
        $exitCode = 0
    } catch {
        $stopCode = $null
        if ($_.Exception.Data.Contains("SortViewPilotPrepExitCode")) { $stopCode = [int]$_.Exception.Data["SortViewPilotPrepExitCode"] }
        Write-Host ""
        Write-Host "V2 PILOT DRY RUN: FAILED" -ForegroundColor Red
        Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
        $exitCode = if ($null -ne $stopCode) { $stopCode } else { 1 }
    }
    return $exitCode
}

# Run only when executed as a script. Dot-sourcing (. .\prepare_v2_pilot.ps1) just
# defines the functions above -- how tests drive the flow with side effects
# replaced -- and runs nothing.
if ($MyInvocation.InvocationName -ne ".") {
    $result = @(Invoke-PrepareV2PilotMain @PSBoundParameters)
    exit ([int]$result[-1])
}
