<#
.SYNOPSIS
    Registers the canonical SortView agent as an unattended Windows
    Scheduled Task. Never touches C:\SortViewAgent or its own Scheduled
    Task.

.DESCRIPTION
    Creates (or, with -Force, replaces) a Scheduled Task that runs
    `python -m agent.main --config <ConfigPath>` from -InstallRoot,
    configured to:
      - start automatically at machine boot (no interactive login needed)
      - run as SYSTEM by default (no separate password to manage/rotate;
        override -RunAsUser/-RunAsPassword for a dedicated low-privilege
        service account instead, if that's this site's policy)
      - restart automatically if the process exits unexpectedly, up to
        RestartCount times, RestartIntervalMinutes apart
      - run with NO execution time limit -- Task Scheduler's default
        3-day limit would otherwise silently kill this long-running
        process; this script disables it explicitly (a well-known
        Scheduled Task gotcha for anything meant to run indefinitely)
      - keep running even if the machine is on battery / user logs off

    Does NOT set SORTVIEW_API_TOKEN -- run set-sortview-api-token.ps1
    separately (and BEFORE starting the task) for that. Does NOT create
    -InstallRoot, -ConfigPath, or any C:\ProgramData\SortView
    directories -- see docs/amh-production-cutover-runbook.md for the
    full staged procedure this script is one step of.

    Idempotent: re-running without -Force reports the existing task and
    makes no changes. With -Force, unregisters and re-registers it (the
    task's own history is lost, but this machine's SortView
    state/spool/logs are untouched -- those live under -ConfigPath's
    referenced directories, not in the task definition).

.PARAMETER InstallRoot
    Directory containing the agent/ package and its own virtual
    environment (…\.venv\Scripts\python.exe), e.g. C:\SortView\CanonicalAgent.
    This becomes the task's working directory.

.PARAMETER ConfigPath
    Path to the runtime_config.json this run should use. Not created by
    this script -- see agent/deploy/runtime_config.production.example.json.

.PARAMETER TaskName
    Distinct from anything used by C:\SortViewAgent's own Scheduled Task.
    Defaults to "SortView Canonical Agent".

.PARAMETER RunAsUser / RunAsPassword
    Optional: run as a specific account instead of SYSTEM. If provided,
    both must be provided together.

.EXAMPLE
    # From an elevated PowerShell prompt, after the token has already
    # been set via set-sortview-api-token.ps1:
    .\register-sortview-task.ps1 `
        -InstallRoot "C:\SortView\CanonicalAgent" `
        -ConfigPath "C:\ProgramData\SortView\config\agent_runtime_config.json"
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\SortView\CanonicalAgent",
    [string]$ConfigPath = "C:\ProgramData\SortView\config\agent_runtime_config.json",
    [string]$TaskName = "SortView Canonical Agent",
    [int]$RestartCount = 999,
    [int]$RestartIntervalMinutes = 2,
    [string]$RunAsUser,
    [securestring]$RunAsPassword,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

if ($TaskName -like "*SortViewAgent*") {
    throw "TaskName must not resemble the legacy agent's own task name -- refusing to risk touching C:\SortViewAgent's Scheduled Task."
}

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell session."
}

$pythonExe = Join-Path $InstallRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found at '$pythonExe' -- is -InstallRoot correct, and has the .venv been created there?"
}
if (-not (Test-Path $ConfigPath)) {
    Write-Warning "Config file '$ConfigPath' does not exist yet -- the task will fail to start until it does. Continuing to register anyway (this is expected if you're registering ahead of the config being written)."
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
    Write-Host "Task '$TaskName' already exists. Pass -Force to unregister and re-register it. No changes made." -ForegroundColor Yellow
    return
}
if ($existing -and $Force) {
    Write-Host "Removing existing task '$TaskName' before re-registering (per -Force)..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $pythonExe `
    -Argument "-m agent.main --config `"$ConfigPath`"" `
    -WorkingDirectory $InstallRoot

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount $RestartCount `
    -RestartInterval (New-TimeSpan -Minutes $RestartIntervalMinutes) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
    # ExecutionTimeLimit = Zero means "no limit" -- WITHOUT this, Task
    # Scheduler's default 3-day limit would silently terminate this
    # long-running process.

if ($RunAsUser) {
    if (-not $RunAsPassword) {
        throw "-RunAsUser requires -RunAsPassword."
    }
    $credPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($RunAsPassword))
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
            -User $RunAsUser -Password $credPlain -RunLevel Highest `
            -Description "SortView canonical continuous agent (production). Registered by agent/deploy/register-sortview-task.ps1. Does not touch C:\SortViewAgent." | Out-Null
    } finally {
        $credPlain = $null
    }
} else {
    # SYSTEM: no password to manage/rotate, runs without any interactive
    # login, simplest choice for a first production cutover on this
    # single-branch machine. See this script's header for the
    # dedicated-service-account alternative.
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -User "SYSTEM" -RunLevel Highest `
        -Description "SortView canonical continuous agent (production). Registered by agent/deploy/register-sortview-task.ps1. Does not touch C:\SortViewAgent." | Out-Null
}

Write-Host ""
Write-Host "Registered Scheduled Task '$TaskName'." -ForegroundColor Green
Write-Host "  Working directory: $InstallRoot"
Write-Host "  Config:            $ConfigPath"
Write-Host "  Runs as:           $(if ($RunAsUser) { $RunAsUser } else { 'SYSTEM' })"
Write-Host ""
Write-Host "The task has NOT been started yet. Confirm SORTVIEW_API_TOKEN is already set" -ForegroundColor Yellow
Write-Host "(set-sortview-api-token.ps1), then start it explicitly:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
