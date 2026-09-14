<#
.SYNOPSIS
    Stops and removes the canonical SortView agent's Scheduled Task
    (rollback / maintenance). Never touches C:\SortViewAgent or its own
    Scheduled Task. Never deletes state/spool/logs/diagnostics.

.DESCRIPTION
    Part of the rollback procedure in
    docs/amh-production-cutover-runbook.md: stops the canonical agent
    cleanly (Stop-ScheduledTask sends a normal process termination --
    the agent's own signal handler / AgentRunner.stop() path is not
    invoked by this, since Task Scheduler does not send SIGTERM on
    Windows; see the runbook for the preferred CLEAN stop method before
    resorting to this for an unresponsive process) and removes the task
    registration so it cannot restart on the next reboot.

    This script deliberately does NOT delete anything under
    C:\ProgramData\SortView (or wherever -ConfigPath's referenced
    directories point) -- state, spool, logs, and diagnostics are left
    completely untouched for later investigation, exactly as the
    rollback procedure requires.

.PARAMETER TaskName
    Must match whatever register-sortview-task.ps1 was given. Defaults
    to "SortView Canonical Agent".

.PARAMETER RemoveTask
    If set, unregisters the task after stopping it (use for a real
    rollback to legacy). Without it, the task is only STOPPED, not
    removed -- use this for routine maintenance where the canonical
    agent should come back after the maintenance window (it will not
    auto-restart on its own once stopped this way; start it again
    explicitly with Start-ScheduledTask).

.EXAMPLE
    # Stop only, for a maintenance window:
    .\unregister-sortview-task.ps1

.EXAMPLE
    # Full rollback -- stop and remove the task registration:
    .\unregister-sortview-task.ps1 -RemoveTask
#>

[CmdletBinding()]
param(
    [string]$TaskName = "SortView Canonical Agent",
    [switch]$RemoveTask
)

$ErrorActionPreference = "Stop"

if ($TaskName -like "*SortViewAgent*") {
    throw "TaskName must not resemble the legacy agent's own task name -- refusing to risk touching C:\SortViewAgent's Scheduled Task."
}

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell session."
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Task '$TaskName' does not exist. Nothing to stop or remove." -ForegroundColor Yellow
    return
}

$state = (Get-ScheduledTask -TaskName $TaskName).State
if ($state -eq "Running") {
    Write-Host "Stopping task '$TaskName'..."
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
} else {
    Write-Host "Task '$TaskName' is not currently running (state: $state)."
}

if ($RemoveTask) {
    Write-Host "Removing task registration '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed. It will NOT restart on the next reboot." -ForegroundColor Green
} else {
    Write-Host "Task registration left in place (not removed) -- it will run again at the next reboot" -ForegroundColor Yellow
    Write-Host "unless you also disable it (Disable-ScheduledTask -TaskName '$TaskName') or pass -RemoveTask."
}

Write-Host ""
Write-Host "State/spool/logs/diagnostics directories were NOT touched by this script." -ForegroundColor Green
