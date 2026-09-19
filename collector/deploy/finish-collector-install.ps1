<#
.SYNOPSIS
    Guided finish of a first-time SortView Collector install: API token,
    interactive preflight, SYSTEM-context preflight, starting-cursor
    bootstrap, and Scheduled Task registration -- in that order, stopping
    at the first problem. The task is left DISABLED; this script never
    enables or starts it. FROZEN installs only.

.DESCRIPTION
    Shipped in the release bundle as tools\finish-install.ps1. Run it from
    an elevated, INTERACTIVE PowerShell session AFTER install.ps1 has
    written the runtime and collector_config.json:

        .\tools\finish-install.ps1

    It adds no logic of its own to any step -- it calls the tools this
    bundle already ships, by their shipped names, from its own folder:

        1. tools\set-api-token.ps1
        2. <InstallRoot>\SortViewCollector.exe preflight --config <ConfigPath>
        3. tools\preflight-system.ps1 -InstallRoot ... -ConfigPath ...
        4. <InstallRoot>\SortViewCollector.exe bootstrap --config <ConfigPath>
        5. tools\register-task.ps1 -InstallRoot ... -ConfigPath ...

    Nothing in SortViewCollector.exe is changed by, or needed for, this
    script.

    STEP 0 (read-only, before any prompt or change): elevation; the frozen
    runtime is present (a source/.venv install is refused); the config
    exists and is complete; the Scheduled Task "SortView Collector" is
    inspected. A task that is Enabled, Running, or does not point at THIS
    install's executable and config is refused -- this script is for a
    first install, not for an install that is already live.

    TOKEN: if a Machine-scope SORTVIEW_API_TOKEN already exists you are
    asked whether to keep or replace it; the value is never displayed. A
    Machine-scope variable set during this PowerShell session does NOT
    reach this process's own environment, and the Collector's preflight and
    bootstrap read the token from THEIR process environment -- so the
    Machine value is copied into this process's SORTVIEW_API_TOKEN for the
    duration of the run and the previous process value (if any) is
    restored, or the variable removed, in a top-level finally. The token is
    never written to a file, a log, a command line, or the console.

    BOOTSTRAP: bootstrap's exit code 2 means BOTH "bad config" and "state
    already exists", so it is not trusted alone. state.json is inspected
    first: absent -> bootstrap runs and its result is verified; present
    and valid for every configured source -> bootstrap is skipped; present
    but invalid or incomplete -> refused. state.json is never deleted or
    overwritten, and bootstrap is never forced.

    TASK: register-task.ps1 reports "already exists" with a bare return
    (no exit code), so the task is verified afterwards: it must exist, be
    Disabled, and run this install's executable against this config.
    register-task.ps1 is never passed -Enabled or -Force.

    SAFE TO RE-RUN: the token is kept unless you choose to replace it, both
    preflights run again, a valid state.json skips bootstrap, a matching
    Disabled task skips registration, and the task stays Disabled.

    EXIT CODES: 0 = setup complete. 1 = a step failed (or not elevated, or
    a required file is missing/invalid). 2 = refused because the existing
    state is unsafe (source install, Enabled/Running/mismatching task,
    invalid or incomplete state.json). Every stop names the step, what was
    left unchanged, and what to correct before re-running.

.EXAMPLE
    .\finish-install.ps1

.EXAMPLE
    .\finish-install.ps1 -InstallRoot "C:\SortView\Collector" `
        -ConfigPath "C:\ProgramData\SortViewCollector\config\collector_config.json"
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\SortView\Collector",
    [string]$ConfigPath = "C:\ProgramData\SortViewCollector\config\collector_config.json"
)

$ErrorActionPreference = "Stop"

# The Scheduled Task registered by tools\register-task.ps1. This script only
# LOOKS FOR it (Step 0 and the post-registration check) -- it never enables,
# starts, disables, changes, or removes it.
$TaskName = "SortView Collector"

# The three source names the Collector's parser recognizes -- exactly what
# install.ps1 writes. Anything else in the config is a hand-edit or a typo.
$RequiredSourceNames = @("checkins", "rejects", "acs")

# collector/state.py's STATE_SCHEMA_VERSION. A test pins these two together,
# so a future schema change cannot silently leave this script accepting a
# state file the Collector would treat as corrupt.
$ExpectedStateSchemaVersion = 1

function Stop-Setup {
    # One place for every stop: names the step, what was left unchanged, and
    # what to correct -- then exits non-zero. `exit` (not a bare return)
    # because a bare return leaves the exit code 0, which automation reads
    # as success. Exiting from here still runs the top-level finally.
    param(
        [Parameter(Mandatory)][string]$Step,
        [Parameter(Mandatory)][string]$Problem,
        [Parameter(Mandatory)][string]$Unchanged,
        [Parameter(Mandatory)][string]$Fix,
        [int]$ExitCode = 1
    )
    Write-Host ""
    Write-Host "SETUP STOPPED at $Step" -ForegroundColor Red
    Write-Host "  What went wrong:  $Problem" -ForegroundColor Red
    Write-Host "  Left unchanged:   $Unchanged"
    Write-Host "  What to do:       $Fix"
    Write-Host "  Then re-run this script -- it is safe to re-run."
    exit $ExitCode
}

# --- pure helpers (touch nothing on the machine; run by the tests) ---------

function ConvertTo-ComparablePath {
    # Case-insensitive, slash-agnostic, trailing-backslash-agnostic. String
    # logic only -- no filesystem access, so it behaves the same everywhere.
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return "" }
    return $Path.Trim().Replace('/', '\').TrimEnd('\').ToLowerInvariant()
}

function Get-FinishConfigProblems {
    # Validates the parsed collector_config.json. Returns a list of problems
    # (empty = valid).
    param($Config, [string[]]$RequiredSourceNames)

    if ($null -eq $Config -or $Config -isnot [System.Management.Automation.PSCustomObject]) {
        return @("the config document root must be a JSON object")
    }

    $problems = @()

    # installation_id: required for a freshly installed (commercial) Collector,
    # so its heartbeat links to its installation record. (collector/config.py
    # itself still loads a config without it -- an already-deployed 1.0.2
    # config keeps running -- but a new install must never finish without it.)
    foreach ($idName in @("customer_id", "branch_id", "installation_id")) {
        $property = $Config.PSObject.Properties[$idName]
        $parsed = 0
        if ($null -eq $property -or -not [int]::TryParse([string]$property.Value, [ref]$parsed) -or $parsed -lt 1) {
            $problems += "'$idName' is required and must be a positive integer."
        }
    }

    $apiUrl = $Config.PSObject.Properties["api_url"]
    if ($null -eq $apiUrl -or [string]::IsNullOrWhiteSpace([string]$apiUrl.Value)) {
        $problems += "'api_url' is required."
    } elseif (([string]$apiUrl.Value).Trim() -notmatch '^https://[^/\s]+') {
        $problems += "'api_url' must begin with https:// (got '$($apiUrl.Value)')."
    }

    foreach ($pathName in @("state_path", "status_path", "log_path")) {
        $property = $Config.PSObject.Properties[$pathName]
        if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
            $problems += "'$pathName' is required."
        }
    }

    $sourcesProperty = $Config.PSObject.Properties["sources"]
    if ($null -eq $sourcesProperty -or $null -eq $sourcesProperty.Value) {
        $problems += "'sources' is required."
        return $problems
    }

    $names = @()
    foreach ($entry in @($sourcesProperty.Value)) {
        $entryName = $null
        $entryPath = $null
        if ($entry -is [System.Management.Automation.PSCustomObject]) {
            $nameProperty = $entry.PSObject.Properties["name"]
            $pathProperty = $entry.PSObject.Properties["path"]
            if ($null -ne $nameProperty) { $entryName = [string]$nameProperty.Value }
            if ($null -ne $pathProperty) { $entryPath = [string]$pathProperty.Value }
        }
        if ([string]::IsNullOrWhiteSpace($entryName) -or [string]::IsNullOrWhiteSpace($entryPath)) {
            $problems += "every entry in 'sources' needs a non-empty 'name' and 'path'."
            continue
        }
        $names += $entryName
    }

    $duplicates = @($names | Group-Object | Where-Object { $_.Count -gt 1 } | ForEach-Object { $_.Name })
    if ($duplicates.Count -gt 0) {
        $problems += "'sources' has duplicate name(s): $($duplicates -join ', ')."
    }
    $unknown = @($names | Where-Object { $RequiredSourceNames -cnotcontains $_ })
    if ($unknown.Count -gt 0) {
        $problems += "'sources' has unrecognized name(s): $($unknown -join ', ') (expected exactly: $($RequiredSourceNames -join ', '))."
    }
    $missing = @($RequiredSourceNames | Where-Object { $names -cnotcontains $_ })
    if ($missing.Count -gt 0) {
        $problems += "'sources' is missing: $($missing -join ', ') (expected exactly: $($RequiredSourceNames -join ', '))."
    }

    return $problems
}

function Get-FinishStateProblems {
    # Validates an EXISTING state.json against what collector/state.py
    # accepts, for the configured source names. Returns a list of problems
    # (empty = valid and covers every source).
    param([string]$StateText, [string[]]$SourceNames, [int]$ExpectedSchemaVersion)

    if ([string]::IsNullOrWhiteSpace($StateText)) {
        return @("the state file is empty.")
    }

    $document = $null
    try {
        $document = $StateText | ConvertFrom-Json
    } catch {
        return @("the state file is not valid JSON ($($_.Exception.Message)).")
    }
    if ($document -isnot [System.Management.Automation.PSCustomObject]) {
        return @("the state document root must be a JSON object.")
    }

    $problems = @()

    $versionProperty = $document.PSObject.Properties["schema_version"]
    # int OR long, like the offset and identity checks below: Windows PowerShell 5.1 parses a JSON
    # integer as Int32 but PowerShell 7 (pwsh, Linux CI) parses it as Int64. A string "1" is neither.
    if ($null -eq $versionProperty -or ($versionProperty.Value -isnot [int] -and $versionProperty.Value -isnot [long]) -or $versionProperty.Value -ne $ExpectedSchemaVersion) {
        $problems += "'schema_version' must be $ExpectedSchemaVersion (the only version this Collector supports)."
    }

    $sourcesProperty = $document.PSObject.Properties["sources"]
    if ($null -eq $sourcesProperty -or $sourcesProperty.Value -isnot [System.Management.Automation.PSCustomObject]) {
        $problems += "'sources' must be a JSON object."
        return $problems
    }

    foreach ($name in $SourceNames) {
        $entryProperty = $sourcesProperty.Value.PSObject.Properties[$name]
        # Case-exact, like the Collector: PowerShell's own property lookup is not.
        if ($null -eq $entryProperty -or $entryProperty.Name -cne $name) {
            $problems += "source '$name' has no entry -- the file does not cover every configured source."
            continue
        }
        $entry = $entryProperty.Value
        if ($entry -isnot [System.Management.Automation.PSCustomObject]) {
            $problems += "source '$name': the entry must be a JSON object."
            continue
        }

        $offsetProperty = $entry.PSObject.Properties["offset"]
        $offset = if ($null -ne $offsetProperty) { $offsetProperty.Value } else { $null }
        if (($offset -isnot [int] -and $offset -isnot [long]) -or $offset -lt 0 -or $offset -gt 1000000000000000000) {
            $problems += "source '$name': 'offset' must be a non-negative integer byte count."
        }

        $identityProperty = $entry.PSObject.Properties["identity"]
        if ($null -ne $identityProperty -and $null -ne $identityProperty.Value) {
            $identity = @($identityProperty.Value)
            $wellFormed = ($identity.Count -eq 2)
            foreach ($part in $identity) {
                if ($part -isnot [int] -and $part -isnot [long]) { $wellFormed = $false }
            }
            if (-not $wellFormed) {
                $problems += "source '$name': 'identity' must be a 2-element integer list or null."
            }
        }
    }

    return $problems
}

function Get-ExistingTaskDecision {
    # Decides what an existing task means for a FIRST install. $TaskState is
    # empty/null when no task exists. $Actions are the task's action objects
    # (each with .Execute and .Arguments).
    #   None              -> no task: register one.
    #   AlreadyRegistered -> Disabled AND running exactly this install's
    #                        executable against exactly this config: done.
    #   Refuse            -> anything else (Enabled, Running, or pointing
    #                        somewhere else): never touched, never adopted.
    param([string]$TaskState, $Actions, [string]$ExpectedExe, [string]$ExpectedConfigPath)

    if ([string]::IsNullOrEmpty($TaskState)) {
        return [pscustomobject]@{ Decision = "None"; Reason = "no task is registered" }
    }
    if ($TaskState -ne "Disabled") {
        return [pscustomobject]@{ Decision = "Refuse"; Reason = "the task exists and its state is '$TaskState' (it can run, or is running, on its own)" }
    }

    $actionList = @($Actions)
    if ($actionList.Count -ne 1) {
        return [pscustomobject]@{ Decision = "Refuse"; Reason = "the task has $($actionList.Count) action(s); this install's task has exactly one" }
    }

    $expectedArguments = 'run --config "' + $ExpectedConfigPath + '"'
    if ((ConvertTo-ComparablePath $actionList[0].Execute) -ne (ConvertTo-ComparablePath $ExpectedExe)) {
        return [pscustomobject]@{ Decision = "Refuse"; Reason = "the task runs '$($actionList[0].Execute)', not this install's '$ExpectedExe'" }
    }
    if ((ConvertTo-ComparablePath $actionList[0].Arguments) -ne (ConvertTo-ComparablePath $expectedArguments)) {
        return [pscustomobject]@{ Decision = "Refuse"; Reason = "the task's arguments are '$($actionList[0].Arguments)', not '$expectedArguments'" }
    }

    return [pscustomobject]@{ Decision = "AlreadyRegistered"; Reason = "a matching Disabled task is already registered" }
}

function Get-TokenChoice {
    # Interprets the answer to "keep or replace?". Enter = keep.
    param([string]$Answer)
    $normalized = if ($null -eq $Answer) { "" } else { $Answer.Trim().ToLowerInvariant() }
    if (@("", "k", "keep") -contains $normalized) { return "Keep" }
    if (@("r", "replace") -contains $normalized) { return "Replace" }
    return "Invalid"
}

# === STEP 0: read-only safety checks -- before any prompt or change =========

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Stop-Setup -Step "step 0 (checks)" -Problem "This script must be run from an elevated (Administrator) PowerShell session." `
        -Unchanged "nothing was touched" -Fix "Open PowerShell with 'Run as administrator' and run this script again." -ExitCode 1
}

$ExePath = Join-Path $InstallRoot "SortViewCollector.exe"
$VenvPython = Join-Path $InstallRoot ".venv\Scripts\python.exe"
$SetTokenScript = Join-Path $PSScriptRoot "set-api-token.ps1"
$PreflightSystemScript = Join-Path $PSScriptRoot "preflight-system.ps1"
$RegisterTaskScript = Join-Path $PSScriptRoot "register-task.ps1"

Write-Host "=== SortView Collector: guided setup ===" -ForegroundColor Cyan
Write-Host "Install root: $InstallRoot"
Write-Host "Config:       $ConfigPath"
Write-Host ""
Write-Host "=== Step 0: read-only checks ===" -ForegroundColor Cyan

if (Test-Path $VenvPython) {
    Stop-Setup -Step "step 0 (checks)" -Problem "A source (Python venv) install was found at '$InstallRoot'. This script supports FROZEN installs only." `
        -Unchanged "nothing was touched" -Fix "Use the documented source-install steps instead, or reinstall from a frozen release bundle." -ExitCode 2
}
if (-not (Test-Path $ExePath -PathType Leaf)) {
    Stop-Setup -Step "step 0 (checks)" -Problem "The frozen runtime was not found at '$ExePath'." `
        -Unchanged "nothing was touched" -Fix "Run install.ps1 from this release bundle first (or pass the correct -InstallRoot)." -ExitCode 1
}
foreach ($required in @($SetTokenScript, $PreflightSystemScript, $RegisterTaskScript)) {
    if (-not (Test-Path $required -PathType Leaf)) {
        Stop-Setup -Step "step 0 (checks)" -Problem "This script's sibling tool is missing: '$required'." `
            -Unchanged "nothing was touched" -Fix "Run this script from the tools\ folder of an intact release bundle (re-copy the bundle if it is incomplete)." -ExitCode 1
    }
}
if (-not (Test-Path $ConfigPath -PathType Leaf)) {
    Stop-Setup -Step "step 0 (checks)" -Problem "The config file was not found at '$ConfigPath'." `
        -Unchanged "nothing was touched" -Fix "Run install.ps1 first (it writes the config), or pass the correct -ConfigPath." -ExitCode 1
}

$config = $null
try {
    $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Stop-Setup -Step "step 0 (checks)" -Problem "The config file is not valid JSON: $($_.Exception.Message)" `
        -Unchanged "nothing was touched" -Fix "Correct '$ConfigPath' (it is plain JSON), or regenerate it by re-running install.ps1 from this bundle." -ExitCode 1
}
$configProblems = @(Get-FinishConfigProblems -Config $config -RequiredSourceNames $RequiredSourceNames)
if ($configProblems.Count -gt 0) {
    foreach ($problem in $configProblems) { Write-Host "  $problem" -ForegroundColor Red }
    Stop-Setup -Step "step 0 (checks)" -Problem "$($configProblems.Count) problem(s) in '$ConfigPath' (listed above)." `
        -Unchanged "nothing was touched" -Fix "Correct the config file, then re-run." -ExitCode 1
}
$SourceNames = @($config.sources | ForEach-Object { [string]$_.name })
$StatePath = [string]$config.state_path

# Read-only lookup of the Scheduled Task. Failing to look it up is "cannot
# prove it is safe", so it is refused rather than assumed absent.
$existingTask = $null
try {
    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
} catch {
    Stop-Setup -Step "step 0 (checks)" -Problem "Could not check whether the '$TaskName' Scheduled Task exists: $($_.Exception.Message)" `
        -Unchanged "nothing was touched" -Fix "Resolve the error above, then re-run." -ExitCode 2
}
$existingTaskState = if ($existingTask) { [string]$existingTask.State } else { $null }
$existingTaskActions = if ($existingTask) { @($existingTask.Actions) } else { @() }
$taskDecision = Get-ExistingTaskDecision -TaskState $existingTaskState -Actions $existingTaskActions `
    -ExpectedExe $ExePath -ExpectedConfigPath $ConfigPath
if ($taskDecision.Decision -eq "Refuse") {
    Stop-Setup -Step "step 0 (checks)" -Problem "The '$TaskName' Scheduled Task is not safe to adopt: $($taskDecision.Reason)." `
        -Unchanged "the task was not changed and nothing else was touched" `
        -Fix "This script is for a first install. To update a live install use tools\update.ps1; to start over, use tools\uninstall.ps1 first." -ExitCode 2
}
Write-Host "  OK: frozen runtime, config, and sibling tools are present; task check: $($taskDecision.Reason)." -ForegroundColor Green

# The token is copied into THIS process's environment for the child
# processes below and put back exactly as it was in the finally at the end.
$hadProcessToken = Test-Path -Path "Env:SORTVIEW_API_TOKEN"
$previousProcessToken = $env:SORTVIEW_API_TOKEN
$seededNow = $false
$registeredNow = $false

try {
    # === STEP 1: API token ==================================================
    Write-Host ""
    Write-Host "=== Step 1 of 5: API token ===" -ForegroundColor Cyan

    $machineToken = [Environment]::GetEnvironmentVariable("SORTVIEW_API_TOKEN", "Machine")
    $runTokenTool = $true
    if (-not [string]::IsNullOrWhiteSpace($machineToken)) {
        Write-Host "An API token is already set on this machine (Machine scope; value not shown)."
        $choice = "Invalid"
        while ($choice -eq "Invalid") {
            $answer = $null
            try {
                $answer = Read-Host "Keep the existing token, or replace it? [K]eep / [R]eplace (Enter = keep)"
            } catch {
                Stop-Setup -Step "step 1 of 5 (API token)" -Problem "Could not prompt: this script must run in an interactive PowerShell session." `
                    -Unchanged "the existing token was not changed" -Fix "Run this script from an interactive, elevated PowerShell window." -ExitCode 1
            }
            $choice = Get-TokenChoice -Answer $answer
            if ($choice -eq "Invalid") { Write-Host "Please answer K (keep) or R (replace)." -ForegroundColor Yellow }
        }
        $runTokenTool = ($choice -eq "Replace")
    }

    if ($runTokenTool) {
        try {
            & $SetTokenScript
        } catch {
            Stop-Setup -Step "step 1 of 5 (API token)" -Problem "Token setup failed: $($_.Exception.Message)" `
                -Unchanged "no preflight, bootstrap, or Scheduled Task step was run" -Fix "Re-run and enter the token again." -ExitCode 1
        }
    } else {
        Write-Host "Keeping the existing token."
    }

    $machineToken = [Environment]::GetEnvironmentVariable("SORTVIEW_API_TOKEN", "Machine")
    if ([string]::IsNullOrWhiteSpace($machineToken)) {
        Stop-Setup -Step "step 1 of 5 (API token)" -Problem "No Machine-scope SORTVIEW_API_TOKEN is set after token setup." `
            -Unchanged "no preflight, bootstrap, or Scheduled Task step was run" -Fix "Re-run and enter the token when prompted." -ExitCode 1
    }
    # Machine scope does not reach this process on its own -- see .DESCRIPTION.
    $env:SORTVIEW_API_TOKEN = $machineToken
    $machineToken = $null
    Write-Host "  OK: token present (Machine scope) and made available to this run's child processes." -ForegroundColor Green

    # === STEP 2: interactive preflight ======================================
    Write-Host ""
    Write-Host "=== Step 2 of 5: interactive preflight ===" -ForegroundColor Cyan
    & $ExePath preflight --config $ConfigPath
    if ($LASTEXITCODE -ne 0) {
        Stop-Setup -Step "step 2 of 5 (interactive preflight)" -Problem "Preflight reported FAIL (exit code $LASTEXITCODE) -- see the [FAIL] lines above." `
            -Unchanged "the token stays set; no state file, and no Scheduled Task, was created by this run" `
            -Fix "Correct what the [FAIL] lines name (source files, network/proxy, token, config), then re-run." -ExitCode 1
    }
    Write-Host "  OK: interactive preflight passed." -ForegroundColor Green

    # === STEP 3: SYSTEM-context preflight ===================================
    Write-Host ""
    Write-Host "=== Step 3 of 5: SYSTEM-context preflight ===" -ForegroundColor Cyan
    # Sentinel: a child that ends without setting an exit code must not be
    # mistaken for success by a stale $LASTEXITCODE from an earlier command.
    $global:LASTEXITCODE = 99
    try {
        & $PreflightSystemScript -InstallRoot $InstallRoot -ConfigPath $ConfigPath
    } catch {
        Stop-Setup -Step "step 3 of 5 (SYSTEM-context preflight)" -Problem "The SYSTEM preflight tool failed to run: $($_.Exception.Message)" `
            -Unchanged "the token stays set; no state file, and no Scheduled Task, was created by this run" -Fix "Resolve the error above, then re-run." -ExitCode 1
    }
    if ($LASTEXITCODE -ne 0) {
        Stop-Setup -Step "step 3 of 5 (SYSTEM-context preflight)" -Problem "SYSTEM-context preflight did not pass (exit code $LASTEXITCODE) -- see the result lines above." `
            -Unchanged "the token stays set; no state file, and no Scheduled Task, was created by this run" `
            -Fix "Fix the SYSTEM-specific problem named above (file permissions, proxy, Machine-scope token), then re-run." -ExitCode 1
    }
    Write-Host "  OK: SYSTEM-context preflight passed." -ForegroundColor Green

    # === STEP 4: bootstrap the starting cursor ==============================
    Write-Host ""
    Write-Host "=== Step 4 of 5: starting cursor (bootstrap) ===" -ForegroundColor Cyan
    if (Test-Path -LiteralPath $StatePath) {
        # Bootstrap's exit code 2 is ambiguous (bad config / state exists),
        # so an existing state file is judged here, never handed to bootstrap.
        if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
            Stop-Setup -Step "step 4 of 5 (bootstrap)" -Problem "'$StatePath' exists but is not a file." `
                -Unchanged "nothing was written or deleted" -Fix "Investigate '$StatePath'; it must be the Collector's state.json." -ExitCode 2
        }
        $stateText = $null
        try {
            $stateText = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8
        } catch {
            Stop-Setup -Step "step 4 of 5 (bootstrap)" -Problem "The existing state file could not be read: $($_.Exception.Message)" `
                -Unchanged "state.json was not modified or deleted" -Fix "Investigate '$StatePath' yourself; this script never overwrites it." -ExitCode 2
        }
        $stateProblems = @(Get-FinishStateProblems -StateText $stateText -SourceNames $SourceNames -ExpectedSchemaVersion $ExpectedStateSchemaVersion)
        if ($stateProblems.Count -gt 0) {
            foreach ($problem in $stateProblems) { Write-Host "  $problem" -ForegroundColor Red }
            Stop-Setup -Step "step 4 of 5 (bootstrap)" -Problem "An existing state file is invalid or incomplete (listed above)." `
                -Unchanged "state.json was not modified or deleted" `
                -Fix "Investigate and recover '$StatePath' yourself as a deliberate action; this script never overwrites or deletes it." -ExitCode 2
        }
        Write-Host "  OK: state file already exists and covers every configured source -- bootstrap skipped." -ForegroundColor Green
    } else {
        & $ExePath bootstrap --config $ConfigPath
        if ($LASTEXITCODE -ne 0) {
            Stop-Setup -Step "step 4 of 5 (bootstrap)" -Problem "Bootstrap failed (exit code $LASTEXITCODE) -- see the message above." `
                -Unchanged "no Scheduled Task was created by this run" -Fix "Correct what the message names (usually a missing/unreadable source file), then re-run." -ExitCode 1
        }
        $verifyText = $null
        if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
            $verifyText = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8
        }
        $verifyProblems = @(Get-FinishStateProblems -StateText $verifyText -SourceNames $SourceNames -ExpectedSchemaVersion $ExpectedStateSchemaVersion)
        if ($verifyProblems.Count -gt 0) {
            foreach ($problem in $verifyProblems) { Write-Host "  $problem" -ForegroundColor Red }
            Stop-Setup -Step "step 4 of 5 (bootstrap)" -Problem "Bootstrap exited 0 but the state file is not valid for every configured source (listed above)." `
                -Unchanged "state.json was not modified or deleted by this script; no Scheduled Task was created" `
                -Fix "Investigate '$StatePath'; do not delete it blindly." -ExitCode 1
        }
        $seededNow = $true
        Write-Host "  OK: starting cursor seeded for: $($SourceNames -join ', ')." -ForegroundColor Green
    }

    # === STEP 5: register the Scheduled Task (DISABLED) =====================
    Write-Host ""
    Write-Host "=== Step 5 of 5: Scheduled Task ===" -ForegroundColor Cyan
    if ($taskDecision.Decision -eq "AlreadyRegistered") {
        Write-Host "  OK: '$TaskName' is already registered and Disabled, and matches this install -- registration skipped." -ForegroundColor Green
    } else {
        # Never -Enabled, never -Force: the task is registered DISABLED.
        try {
            & $RegisterTaskScript -InstallRoot $InstallRoot -ConfigPath $ConfigPath
        } catch {
            Stop-Setup -Step "step 5 of 5 (Scheduled Task)" -Problem "Task registration failed: $($_.Exception.Message)" `
                -Unchanged "the token and state.json stay in place; the task was not enabled" -Fix "Resolve the error above, then re-run." -ExitCode 1
        }

        # register-task.ps1 does not give a reliable exit code, so the task
        # itself is verified.
        $verifyTask = $null
        try {
            $verifyTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        } catch {
            Stop-Setup -Step "step 5 of 5 (Scheduled Task)" -Problem "Could not verify the task after registration: $($_.Exception.Message)" `
                -Unchanged "the token and state.json stay in place; the task was not enabled" -Fix "Check the task by hand, then re-run." -ExitCode 1
        }
        if ($null -eq $verifyTask) {
            Stop-Setup -Step "step 5 of 5 (Scheduled Task)" -Problem "Registration finished but '$TaskName' does not exist." `
                -Unchanged "the token and state.json stay in place" -Fix "Read the registration output above, resolve it, then re-run." -ExitCode 1
        }
        $verifyDecision = Get-ExistingTaskDecision -TaskState ([string]$verifyTask.State) -Actions @($verifyTask.Actions) `
            -ExpectedExe $ExePath -ExpectedConfigPath $ConfigPath
        if ($verifyDecision.Decision -ne "AlreadyRegistered") {
            Stop-Setup -Step "step 5 of 5 (Scheduled Task)" -Problem "The registered task is not the expected Disabled task: $($verifyDecision.Reason)." `
                -Unchanged "the task was not changed by this script" `
                -Fix "Inspect '$TaskName' in Task Scheduler. It must be Disabled and run '$ExePath' with: run --config `"$ConfigPath`"." -ExitCode 2
        }
        $registeredNow = $true
        Write-Host "  OK: '$TaskName' registered -- State: Disabled." -ForegroundColor Green
    }

    # === STEP 6: summary ====================================================
    Write-Host ""
    Write-Host "=== SortView Collector setup COMPLETE. The task is registered DISABLED. ===" -ForegroundColor Green
    Write-Host ""
    Write-Host "  [OK] API token set (Machine scope; value never shown)"
    Write-Host "  [OK] Interactive preflight passed"
    Write-Host "  [OK] SYSTEM-context preflight passed"
    if ($seededNow) {
        Write-Host "  [OK] Starting cursor seeded for: $($SourceNames -join ', ')"
    } else {
        Write-Host "  [OK] Starting cursor already seeded"
    }
    if ($registeredNow) {
        Write-Host "  [OK] Scheduled Task '$TaskName' registered -- State: Disabled"
    } else {
        Write-Host "  [OK] Scheduled Task '$TaskName' already registered -- State: Disabled"
    }
    Write-Host ""
    Write-Host "NOTHING WILL RUN until you enable the task." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "When you are ready, enable it:"
    Write-Host '    Enable-ScheduledTask -TaskName "SortView Collector"'
    Write-Host ""
    Write-Host "Optional immediate run after enabling (otherwise it runs at its next 15-minute trigger):"
    Write-Host '    Start-ScheduledTask -TaskName "SortView Collector"'
    Write-Host ""
    Write-Host "This script does not run either command."
    exit 0
} finally {
    # Put this process's SORTVIEW_API_TOKEN back exactly as it was found.
    if ($hadProcessToken) {
        $env:SORTVIEW_API_TOKEN = $previousProcessToken
    } else {
        Remove-Item -Path "Env:SORTVIEW_API_TOKEN" -ErrorAction SilentlyContinue
    }
    $previousProcessToken = $null
}
