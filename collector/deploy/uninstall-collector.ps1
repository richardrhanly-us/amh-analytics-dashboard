<#
.SYNOPSIS
    Uninstalls the SortView Collector: unregisters the Scheduled Task and
    removes the application runtime (-InstallRoot, including its venv).
    By default, config/state/status/logs under -DataRoot are PRESERVED --
    exactly like the continuous-agent tooling's own uninstall behavior,
    never auto-deleted. Never touches C:\SortViewAgent, its Scheduled
    Task, or any Python installation outside -InstallRoot\.venv (which
    is entirely self-contained and safe to remove on its own).

.DESCRIPTION
    1. Stops and unregisters the "SortView Collector" Scheduled Task, if
       registered. Never touches any other task.
    2. Removes -InstallRoot entirely -- the venv, collector\*.py, and the
       canonical parser runtime (agent\*, per collector/deploy_manifest.py)
       installed alongside it.
    3. Leaves -DataRoot (config\, data\, logs\) untouched UNLESS
       -PurgeData is passed, in which case it is removed too -- but only
       after an interactive confirmation prompt (bypassed only by also
       passing -Confirm:$false, an explicit double opt-in for a
       destructive action).
    4. Prints whether the Machine-scope SORTVIEW_API_TOKEN environment
       variable still exists and exactly how to remove it -- this script
       never removes it automatically, since it may be a shared secret
       intentionally left in place (e.g. if reinstalling shortly after).

.EXAMPLE
    # Keep config/state/status/logs (the default, recommended way):
    .\uninstall-collector.ps1

.EXAMPLE
    # Also permanently delete all data:
    .\uninstall-collector.ps1 -PurgeData
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$InstallRoot = "C:\SortView\Collector",
    [string]$DataRoot = "C:\ProgramData\SortViewCollector",
    [string]$TaskName = "SortView Collector",
    [switch]$PurgeData
)

$ErrorActionPreference = "Stop"

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell session."
}

Write-Host "=== 1. Scheduled Task ===" -ForegroundColor Cyan
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered '$TaskName'."
} else {
    Write-Host "'$TaskName' is not registered -- nothing to unregister."
}

Write-Host "=== 2. Application runtime ===" -ForegroundColor Cyan
if (Test-Path $InstallRoot) {
    Remove-Item -Recurse -Force $InstallRoot
    Write-Host "Removed $InstallRoot (venv + collector\*.py + canonical parser runtime)."
} else {
    Write-Host "$InstallRoot does not exist -- nothing to remove."
}

Write-Host "=== 3. Data (config/state/status/logs) ===" -ForegroundColor Cyan
if (Test-Path $DataRoot) {
    if ($PurgeData) {
        if ($PSCmdlet.ShouldProcess($DataRoot, "Permanently delete config/state/status/logs")) {
            Remove-Item -Recurse -Force $DataRoot
            Write-Host "Removed $DataRoot (config/state/status/logs) -- PurgeData was requested and confirmed." -ForegroundColor Yellow
        } else {
            Write-Host "Purge cancelled -- $DataRoot was left in place." -ForegroundColor Yellow
        }
    } else {
        Write-Host "$DataRoot (config/state/status/logs) was left in place -- pass -PurgeData to also remove it." -ForegroundColor Green
    }
} else {
    Write-Host "$DataRoot does not exist."
}

Write-Host ""
Write-Host "=== 4. API token ===" -ForegroundColor Cyan
$tokenStillSet = [Environment]::GetEnvironmentVariable("SORTVIEW_API_TOKEN", "Machine")
if ($tokenStillSet) {
    Write-Host "SORTVIEW_API_TOKEN is STILL SET as a Machine-scope environment variable." -ForegroundColor Yellow
    Write-Host "This script does not remove it automatically (it may be intentionally shared" -ForegroundColor Yellow
    Write-Host "or about to be reused for a reinstall). To remove it manually:"
    Write-Host '  [Environment]::SetEnvironmentVariable("SORTVIEW_API_TOKEN", $null, "Machine")'
} else {
    Write-Host "SORTVIEW_API_TOKEN is not set (or was already removed)."
}

Write-Host ""
Write-Host "Uninstall complete." -ForegroundColor Green
