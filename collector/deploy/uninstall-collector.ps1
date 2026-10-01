<#
.SYNOPSIS
    Uninstalls the SortView Collector: unregisters the Scheduled Task and
    removes the application runtime (-InstallRoot entirely) -- works
    identically for BOTH a SOURCE install (venv + collector\*.py + agent\*)
    and a FROZEN install (SortViewCollector.exe + _internal\), since it
    simply removes everything under -InstallRoot regardless of which kind
    is there; no mode detection is needed here. By default,
    config/state/status/logs under -DataRoot are PRESERVED -- exactly
    like the continuous-agent tooling's own uninstall behavior, never
    auto-deleted. Never touches C:\SortViewAgent, its Scheduled Task, or
    any Python installation outside -InstallRoot\.venv (which is entirely
    self-contained and safe to remove on its own; not applicable at all
    for a frozen install, which has no venv).

.DESCRIPTION
    1. Stops and unregisters the "SortView Collector" Scheduled Task, if
       registered. Never touches any other task.
    2. Removes -InstallRoot entirely -- for a source install: the venv,
       collector\*.py, and the canonical parser runtime (agent\*, per
       collector/deploy_manifest.py) installed alongside it. For a frozen
       install: SortViewCollector.exe and its _internal\ payload. Both
       simply fall out of one unconditional Remove-Item -Recurse -Force
       against -InstallRoot below -- no per-file-kind logic needed.
    3. Leaves -DataRoot (config\, data\, logs\) untouched UNLESS
       -PurgeData is passed, in which case it is removed too -- but only
       after an interactive confirmation prompt (bypassed only by also
       passing -Confirm:$false, an explicit double opt-in for a
       destructive action).
    4. Prints whether the API token file -DataRoot\secrets\api_token.dpapi
       exists (never its contents; it is kept with -DataRoot unless
       -PurgeData), and whether the legacy Machine-scope SORTVIEW_API_TOKEN
       environment variable still exists and exactly how to remove it. The
       Collector itself never reads that variable (its only token source is
       api_token.dpapi); this script reports it but never removes it
       automatically -- cleaning it up is an explicit operator action.

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
    # Detected only for an accurate printed message below -- the actual
    # removal itself (Remove-Item -Recurse) needs no mode distinction at all.
    $wasFrozen = Test-Path (Join-Path $InstallRoot "SortViewCollector.exe")
    Remove-Item -Recurse -Force $InstallRoot
    if ($wasFrozen) {
        Write-Host "Removed $InstallRoot (frozen runtime: SortViewCollector.exe + _internal\)."
    } else {
        Write-Host "Removed $InstallRoot (venv + collector\*.py + canonical parser runtime)."
    }
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
# Existence only -- the file is never opened here. It lives under -DataRoot, so it is
# removed exactly when -PurgeData removed -DataRoot above, and kept otherwise (a
# reinstall, or setup.ps1 resuming an interrupted install, reuses it).
$tokenFile = Join-Path $DataRoot "secrets\api_token.dpapi"
if (Test-Path -LiteralPath $tokenFile) {
    Write-Host "The API token file $tokenFile is still in place (DPAPI, Administrators + SYSTEM only)." -ForegroundColor Yellow
    Write-Host "It was kept with the rest of -DataRoot; pass -PurgeData to remove it, or delete that one file."
} else {
    Write-Host "No API token file ($tokenFile)."
}
$tokenStillSet = [Environment]::GetEnvironmentVariable("SORTVIEW_API_TOKEN", "Machine")
if ($tokenStillSet) {
    Write-Host "SORTVIEW_API_TOKEN is STILL SET as a Machine-scope environment variable." -ForegroundColor Yellow
    Write-Host "The Collector does not read it (its token is api_token.dpapi only). This script does" -ForegroundColor Yellow
    Write-Host "not remove it automatically (something else on this machine may use it). To remove it manually:"
    Write-Host '  [Environment]::SetEnvironmentVariable("SORTVIEW_API_TOKEN", $null, "Machine")'
} else {
    Write-Host "SORTVIEW_API_TOKEN is not set (or was already removed)."
}

Write-Host ""
Write-Host "Uninstall complete." -ForegroundColor Green
