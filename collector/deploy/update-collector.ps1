<#
.SYNOPSIS
    Updates an existing SortView Collector install in place, preserving
    config/state/status/logs/token, keeping a rollback copy of the prior
    runtime, and verifying the new runtime with preflight BEFORE
    declaring success or touching the Scheduled Task.

.DESCRIPTION
    1. Stops the production task if it's currently running (so an update
       never races a scheduled run) -- does not unregister it.
    2. ALWAYS backs up the current collector\*.py files (fast, small) to
       a timestamped backup folder alongside -InstallRoot. NEVER touches
       -DataRoot (config\, data\, logs\) -- those are not part of what
       this script replaces.
    3. Compares collector/deploy/requirements.txt's hash against the
       hash recorded at the last install/update
       (-InstallRoot\.deps-hash). If unchanged, only the .py files are
       replaced (fast path, existing venv kept as-is). If changed, the
       ENTIRE existing -InstallRoot is renamed aside (not deleted) to the
       same timestamped backup location, and a completely fresh
       install (venv + dependencies + code) is built -- see
       install-collector.ps1's own logic, reused here for that path.
    4. Runs preflight (interactively) against the NEW runtime. If it
       fails, the update stops here: the new runtime is left in place
       for inspection, the OLD runtime remains fully intact at the
       backup path, and the task is NOT restarted -- exact rollback
       instructions are printed. Nothing is auto-reverted; this is a
       deliberate operator decision, not something this script decides
       silently.
    5. If preflight passes: the task is restarted (only if it was
       already registered before this update began) and this is
       reported as a successful update.

    Does not run the SYSTEM-context preflight automatically (that
    requires registering a temporary task, a heavier operation) -- for
    anything beyond a trivial code fix, re-run
    run-preflight-as-system.ps1 manually afterward too before trusting
    the update fully, per the same standing the initial install already
    requires.

.EXAMPLE
    .\update-collector.ps1 -InstallRoot "C:\SortView\Collector" `
        -ConfigPath "C:\ProgramData\SortViewCollector\config\collector_config.json"
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\SortView\Collector",
    [string]$ConfigPath = "C:\ProgramData\SortViewCollector\config\collector_config.json",
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$TaskName = "SortView Collector"

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell session."
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$SourceCollectorDir = Join-Path $RepoRoot "collector"
$SourceRequirements = Join-Path $RepoRoot "collector\deploy\requirements.txt"

if (-not (Test-Path (Join-Path $InstallRoot ".venv"))) {
    throw "No existing install found at '$InstallRoot' -- use install-collector.ps1 for a fresh install."
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupRoot = "$InstallRoot.backup-$timestamp"

Write-Host "=== 1. Stop the task if running ===" -ForegroundColor Cyan
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$wasRegistered = $null -ne $existingTask
if ($existingTask -and $existingTask.State -eq "Running") {
    Stop-ScheduledTask -TaskName $TaskName
    Write-Host "Stopped '$TaskName'."
} else {
    Write-Host "Task not currently running (or not registered yet)."
}

Write-Host "=== 2. Back up current .py files ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Path (Join-Path $BackupRoot "collector") -Force | Out-Null
Copy-Item (Join-Path $InstallRoot "collector\*.py") -Destination (Join-Path $BackupRoot "collector") -Force
Write-Host "Backed up current collector\*.py to $BackupRoot\collector"

Write-Host "=== 3. Dependency check ===" -ForegroundColor Cyan
$newHash = (Get-FileHash $SourceRequirements -Algorithm SHA256).Hash
$hashMarkerPath = Join-Path $InstallRoot ".deps-hash"
$oldHash = if (Test-Path $hashMarkerPath) { (Get-Content $hashMarkerPath -Raw).Trim() } else { $null }

if ($newHash -eq $oldHash) {
    Write-Host "requirements.txt unchanged -- replacing .py files only, keeping the existing venv."
    Copy-Item (Join-Path $SourceCollectorDir "*.py") -Destination (Join-Path $InstallRoot "collector") -Force
} else {
    Write-Host "requirements.txt changed (or no prior record) -- rebuilding the runtime." -ForegroundColor Yellow
    Write-Host "Renaming the entire current install aside to $BackupRoot (not deleting)..."
    # The .py-only backup from step 2 is now redundant with this full
    # rename, but is harmless to leave in place -- kept simple rather
    # than conditionally skipping it.
    Move-Item -Path $InstallRoot -Destination "$BackupRoot-full" -Force

    New-Item -ItemType Directory -Path (Join-Path $InstallRoot "collector") -Force | Out-Null
    Copy-Item (Join-Path $SourceCollectorDir "*.py") -Destination (Join-Path $InstallRoot "collector") -Force

    & $PythonExe -m venv (Join-Path $InstallRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
    $VenvPython = Join-Path $InstallRoot ".venv\Scripts\python.exe"
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install -r $SourceRequirements --quiet
    if ($LASTEXITCODE -ne 0) { throw "dependency install failed (exit $LASTEXITCODE)" }
}
Set-Content -Path $hashMarkerPath -Value $newHash -Encoding utf8

Write-Host "=== 4. Verify the new runtime ===" -ForegroundColor Cyan
$VenvPython = Join-Path $InstallRoot ".venv\Scripts\python.exe"
& $VenvPython -m collector.preflight --config $ConfigPath
$preflightExitCode = $LASTEXITCODE

if ($preflightExitCode -ne 0) {
    Write-Host ""
    Write-Host "UPDATE FAILED VERIFICATION (preflight exit code $preflightExitCode)." -ForegroundColor Red
    Write-Host "The task was NOT restarted. The new runtime is left in place at $InstallRoot for" -ForegroundColor Red
    Write-Host "inspection. The prior runtime is fully intact -- to roll back manually:" -ForegroundColor Red
    if (Test-Path "$BackupRoot-full") {
        Write-Host "  Remove-Item -Recurse -Force `"$InstallRoot`""
        Write-Host "  Move-Item `"$BackupRoot-full`" `"$InstallRoot`""
    } else {
        Write-Host "  Copy-Item `"$BackupRoot\collector\*.py`" `"$InstallRoot\collector`" -Force"
    }
    if ($wasRegistered) {
        Write-Host "  Start-ScheduledTask -TaskName '$TaskName'   # once you're ready to resume"
    }
    exit 1
}

Write-Host ""
Write-Host "Preflight passed against the new runtime." -ForegroundColor Green
if ($wasRegistered) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Restarted '$TaskName'." -ForegroundColor Green
} else {
    Write-Host "Task was not previously registered -- nothing to restart. Use register-collector-task.ps1 if needed."
}

Write-Host ""
Write-Host "UPDATE COMPLETE." -ForegroundColor Green
Write-Host "Prior runtime preserved at: $(if (Test-Path "$BackupRoot-full") { "$BackupRoot-full" } else { "$BackupRoot\collector (code only)" })"
Write-Host "Consider re-running run-preflight-as-system.ps1 too before fully trusting this update," -ForegroundColor Yellow
Write-Host "especially if dependencies changed." -ForegroundColor Yellow
