<#
.SYNOPSIS
    Updates an existing SortView Collector install in place from a
    standalone RELEASE BUNDLE, preserving config/state/status/logs/token,
    keeping a rollback copy of the prior runtime, and verifying the new
    runtime with preflight BEFORE declaring success or touching the
    Scheduled Task.

.DESCRIPTION
    This is the release-bundle-facing updater: it resolves every source
    file relative to the bundle it ships in (this script lives at
    <bundle>\tools\update.ps1; the payload is at ..\collector and ..\agent
    relative to this script), never a Git repository. See
    collector/deploy/update-collector.ps1 for the repo-checkout-based
    equivalent used for developer/QA updates.

    TASK SAFETY (real production finding, fixed here): the repo-checkout
    equivalent of this script used to only stop the task if it was
    already Running -- if the task was Ready-and-ENABLED, its normal
    15-minute trigger could fire mid-update, racing a partially-replaced
    runtime. This script instead:
      1. Records whether the task exists and whether it was enabled.
      2. Disables it FIRST, before touching any runtime file, if it was
         enabled -- so no trigger can fire during the update regardless of
         timing.
      3. If it happens to be Running at that moment (a run already in
         flight), stops it and waits (bounded by -StopTimeoutSeconds) for
         it to actually exit -- aborting the update (without touching the
         runtime) rather than proceeding on a guess if it doesn't stop in
         time.
      4. Performs the update, then preflight.
      5. Restores the PRIOR enabled/disabled state only after preflight
         passes -- via Enable-ScheduledTask, never Start-ScheduledTask (an
         immediate ad hoc run is never triggered implicitly; pass
         -StartNow to explicitly request one after a successful update).
      6. On preflight failure, the task is left DISABLED regardless of its
         prior state, with exact rollback instructions printed -- nothing
         is auto-reverted, and nothing is left able to fire on a
         half-updated runtime.

    Never touches -DataRoot (config\, data\, logs\) -- only the
    application runtime (-InstallRoot) is replaced.

.EXAMPLE
    .\update.ps1 -InstallRoot "C:\SortView\Collector" `
        -ConfigPath "C:\ProgramData\SortViewCollector\config\collector_config.json"
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\SortView\Collector",
    [string]$ConfigPath = "C:\ProgramData\SortViewCollector\config\collector_config.json",
    [string]$TaskName = "SortView Collector",
    [string]$PythonExe = "python",
    [int]$StopTimeoutSeconds = 60,
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell session."
}

# This script lives at <bundle>\tools\update.ps1 -- the bundle root (with
# collector\, agent\, requirements.txt) is one level up.
$BundleRoot = Split-Path $PSScriptRoot -Parent
$SourceCollectorDir = Join-Path $BundleRoot "collector"
$SourceAgentDir = Join-Path $BundleRoot "agent"
$SourceRequirements = Join-Path $BundleRoot "requirements.txt"

foreach ($required in @($SourceCollectorDir, $SourceAgentDir, $SourceRequirements)) {
    if (-not (Test-Path $required)) {
        throw "This release bundle is incomplete or damaged -- missing '$required'. Re-download/re-copy the bundle rather than editing it by hand."
    }
}

if (-not (Test-Path (Join-Path $InstallRoot ".venv"))) {
    throw "No existing install found at '$InstallRoot' -- use install.ps1 for a fresh install."
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupRoot = "$InstallRoot.backup-$timestamp"

Write-Host "=== 1. Snapshot and disable the task before touching anything ===" -ForegroundColor Cyan
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$wasRegistered = $null -ne $existingTask
$wasEnabled = $false

if ($wasRegistered) {
    $wasEnabled = [bool]$existingTask.Settings.Enabled
    if ($wasEnabled) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Host "Disabled '$TaskName' (was enabled) -- its recurring trigger cannot fire during this update, regardless of timing." -ForegroundColor Green
    } else {
        Write-Host "'$TaskName' was already disabled -- nothing to disable."
    }

    # Disabling does not stop an ALREADY-RUNNING instance -- only future,
    # trigger-driven starts. Check separately and wait it out if so.
    $currentState = (Get-ScheduledTask -TaskName $TaskName).State
    if ($currentState -eq "Running") {
        Write-Host "Task is currently running -- stopping and waiting (timeout: ${StopTimeoutSeconds}s)..."
        Stop-ScheduledTask -TaskName $TaskName
        $deadline = (Get-Date).AddSeconds($StopTimeoutSeconds)
        do {
            Start-Sleep -Seconds 1
            $currentState = (Get-ScheduledTask -TaskName $TaskName).State
        } while ($currentState -eq "Running" -and (Get-Date) -lt $deadline)

        if ($currentState -eq "Running") {
            throw "Task '$TaskName' did not stop within ${StopTimeoutSeconds}s -- aborting update WITHOUT touching the runtime. " +
                  "The task is left DISABLED. Investigate the stuck run manually, then re-enable with Enable-ScheduledTask once resolved, and retry this update."
        }
        Write-Host "Task stopped."
    }
} else {
    Write-Host "'$TaskName' is not registered -- nothing to disable/stop."
}

Write-Host "=== 2. Back up current runtime files ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Path (Join-Path $BackupRoot "collector") -Force | Out-Null
Copy-Item (Join-Path $InstallRoot "collector\*.py") -Destination (Join-Path $BackupRoot "collector") -Force
Write-Host "Backed up current collector\*.py to $BackupRoot\collector"

if (Test-Path (Join-Path $InstallRoot "agent")) {
    New-Item -ItemType Directory -Path (Join-Path $BackupRoot "agent") -Force | Out-Null
    Copy-Item (Join-Path $InstallRoot "agent\*") -Destination (Join-Path $BackupRoot "agent") -Recurse -Force
    Write-Host "Backed up current canonical parser runtime to $BackupRoot\agent"
}

function Restore-DisabledTaskAndFail([string]$Message) {
    Write-Host ""
    Write-Host $Message -ForegroundColor Red
    if ($wasRegistered) {
        Write-Host "The task is left DISABLED (regardless of its prior state) -- re-enable manually" -ForegroundColor Red
        Write-Host "only once you're satisfied the runtime is in a good state:" -ForegroundColor Red
        Write-Host "  Enable-ScheduledTask -TaskName '$TaskName'"
    }
    exit 1
}

Write-Host "=== 3. Dependency check ===" -ForegroundColor Cyan
$newHash = (Get-FileHash $SourceRequirements -Algorithm SHA256).Hash
$hashMarkerPath = Join-Path $InstallRoot ".deps-hash"
$oldHash = if (Test-Path $hashMarkerPath) { (Get-Content $hashMarkerPath -Raw).Trim() } else { $null }
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

if ($newHash -eq $oldHash) {
    Write-Host "requirements.txt unchanged -- replacing runtime files only, keeping the existing venv."
    # DETERMINISTIC REPLACEMENT, not an overlay: the previous Copy-Item-only
    # approach copied the new release's files IN but never removed a file
    # that existed in the OLD install and is simply absent from the new
    # release (a module removed or renamed between versions) -- that stale
    # file would silently survive as dead code in the installed runtime.
    # Deleting each runtime directory entirely before recreating it from
    # the bundle guarantees installed collector\/agent\ == exactly what
    # the bundle contains, with no growing list of delete-exceptions to
    # maintain. The task is already disabled (step 1, above) before this
    # runs; .venv and everything under -DataRoot are untouched -- only
    # these two sibling directories under -InstallRoot are replaced.
    if (Test-Path (Join-Path $InstallRoot "collector")) {
        Remove-Item -Recurse -Force (Join-Path $InstallRoot "collector")
    }
    New-Item -ItemType Directory -Path (Join-Path $InstallRoot "collector") -Force | Out-Null
    Copy-Item (Join-Path $SourceCollectorDir "*.py") -Destination (Join-Path $InstallRoot "collector") -Force

    if (Test-Path (Join-Path $InstallRoot "agent")) {
        Remove-Item -Recurse -Force (Join-Path $InstallRoot "agent")
    }
    New-Item -ItemType Directory -Path (Join-Path $InstallRoot "agent") -Force | Out-Null
    Copy-Item (Join-Path $SourceAgentDir "*") -Destination (Join-Path $InstallRoot "agent") -Recurse -Force
} else {
    Write-Host "requirements.txt changed (or no prior record) -- rebuilding the runtime." -ForegroundColor Yellow
    Write-Host "Renaming the entire current install aside to $BackupRoot (not deleting)..."
    Move-Item -Path $InstallRoot -Destination "$BackupRoot-full" -Force

    New-Item -ItemType Directory -Path (Join-Path $InstallRoot "collector") -Force | Out-Null
    Copy-Item (Join-Path $SourceCollectorDir "*.py") -Destination (Join-Path $InstallRoot "collector") -Force
    New-Item -ItemType Directory -Path (Join-Path $InstallRoot "agent") -Force | Out-Null
    Copy-Item (Join-Path $SourceAgentDir "*") -Destination (Join-Path $InstallRoot "agent") -Recurse -Force

    & $PythonExe -m venv (Join-Path $InstallRoot ".venv")
    if ($LASTEXITCODE -ne 0) { Restore-DisabledTaskAndFail "venv creation failed (exit $LASTEXITCODE)." }
    $VenvPython = Join-Path $InstallRoot ".venv\Scripts\python.exe"
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install -r $SourceRequirements --quiet
    if ($LASTEXITCODE -ne 0) { Restore-DisabledTaskAndFail "dependency install failed (exit $LASTEXITCODE)." }
}
[System.IO.File]::WriteAllText($hashMarkerPath, $newHash, $utf8NoBom)

Write-Host "=== 4. Verify the new runtime ===" -ForegroundColor Cyan
$VenvPython = Join-Path $InstallRoot ".venv\Scripts\python.exe"
# -m collector.preflight resolves the `collector` package via the
# PROCESS'S OWN working directory, which must be -InstallRoot.
Push-Location $InstallRoot
try {
    & $VenvPython -m collector.preflight --config $ConfigPath
    $preflightExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($preflightExitCode -ne 0) {
    Write-Host ""
    Write-Host "UPDATE FAILED VERIFICATION (preflight exit code $preflightExitCode)." -ForegroundColor Red
    Write-Host "The new runtime is left in place at $InstallRoot for inspection. The prior" -ForegroundColor Red
    Write-Host "runtime is fully intact -- to roll back manually:" -ForegroundColor Red
    if (Test-Path "$BackupRoot-full") {
        Write-Host "  Remove-Item -Recurse -Force `"$InstallRoot`""
        Write-Host "  Move-Item `"$BackupRoot-full`" `"$InstallRoot`""
    } else {
        # EXACT restoration, not an overlay: the failed new runtime may
        # have introduced a file that does not exist in the backup (a
        # module added in this release) -- copying the backup ON TOP of
        # it would leave that new-only file behind, silently mixing old
        # and new code. Removing each runtime directory first, THEN
        # copying the backup in, makes the backup the sole source of
        # truth for what ends up installed -- nothing from the failed
        # update can survive. (Deleting the destination first also means
        # the source path does NOT need a trailing \* here -- Copy-Item
        # -Recurse onto a destination that does not yet exist copies the
        # source directory itself, no nesting; the trailing-\* concern
        # only applies when the destination already exists, as in the
        # -Force-only overlay this replaces.)
        Write-Host "  Remove-Item -Recurse -Force `"$InstallRoot\collector`""
        Write-Host "  Copy-Item `"$BackupRoot\collector`" `"$InstallRoot\collector`" -Recurse -Force"
        Write-Host "  Remove-Item -Recurse -Force `"$InstallRoot\agent`""
        Write-Host "  Copy-Item `"$BackupRoot\agent`" `"$InstallRoot\agent`" -Recurse -Force   # canonical parser runtime"
    }
    Restore-DisabledTaskAndFail "Task was NOT restarted or re-enabled."
}

Write-Host ""
Write-Host "Preflight passed against the new runtime." -ForegroundColor Green

if ($wasRegistered -and $wasEnabled) {
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "Re-enabled '$TaskName' (restored its prior enabled state) -- it will resume on" -ForegroundColor Green
    Write-Host "its normal recurring schedule, not immediately." -ForegroundColor Green
    if ($StartNow) {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Also triggered an immediate run now (-StartNow was passed)." -ForegroundColor Green
    }
} elseif ($wasRegistered -and -not $wasEnabled) {
    Write-Host "'$TaskName' was already disabled before this update -- left disabled (its prior" -ForegroundColor Yellow
    Write-Host "state); nothing to restore. Enable-ScheduledTask when you're ready." -ForegroundColor Yellow
} else {
    Write-Host "Task was not previously registered -- nothing to restore. Use tools\register-task.ps1 if needed."
}

Write-Host ""
Write-Host "UPDATE COMPLETE." -ForegroundColor Green
Write-Host "Prior runtime preserved at: $(if (Test-Path "$BackupRoot-full") { "$BackupRoot-full" } else { "$BackupRoot\collector (code only)" })"
Write-Host "Consider re-running tools\preflight-system.ps1 too before fully trusting this update," -ForegroundColor Yellow
Write-Host "especially if dependencies changed." -ForegroundColor Yellow
