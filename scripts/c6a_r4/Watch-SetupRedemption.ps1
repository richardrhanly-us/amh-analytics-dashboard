<#
.SYNOPSIS
    C6a R4: cut this VM's network the moment SortView Setup saves its enrollment record. Qualification VMs only.

.DESCRIPTION
    Produces an "interrupted after redemption" Setup without changing Setup, the Collector or the bundle.

    Start it in an elevated PowerShell window BEFORE starting SortView Setup. It polls for
    <DataRoot>\setup\enrollment-recovery.json, which Setup writes in step 6, right after SortView accepted the
    enrollment code. As soon as the file appears, it disables every network adapter that was up when the watcher
    started. Disabling the adapters, rather than adding a firewall rule, also cuts connections that are already open.

    Setup then cannot reach SortView in its verification step (step 8) and stops. The code is used, the installation
    exists on the server, and the computer holds the token and the resume record: run Setup again and choose
    Resume setup. For the lost-response case, revert the VM to its clean snapshot instead, so the computer holds
    nothing while the server keeps the redeemed, still-Provisioning installation.

    Restore the network with -Restore. It re-enables exactly the adapters this script disabled, as listed in
    -StatePath. A VMware snapshot revert also restores them.

    It never reads the record's contents, never contacts a server and never changes a SortView file.

.PARAMETER DataRoot
    Setup's data folder. Default: C:\ProgramData\SortViewCollector.

.PARAMETER TimeoutMinutes
    Stop watching, without changing anything, after this long.

.PARAMETER Restore
    Re-enable the adapters disabled by an earlier run.

.PARAMETER StatePath
    Where the names of the disabled adapters are kept between the watch and -Restore.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Watch-SetupRedemption.ps1
    powershell -ExecutionPolicy Bypass -File .\Watch-SetupRedemption.ps1 -Restore
#>
[CmdletBinding(DefaultParameterSetName = "Watch")]
param(
    [Parameter(ParameterSetName = "Watch")][string]$DataRoot = "C:\ProgramData\SortViewCollector",
    [Parameter(ParameterSetName = "Watch")][ValidateRange(1, 240)][int]$TimeoutMinutes = 30,
    [Parameter(ParameterSetName = "Restore", Mandatory = $true)][switch]$Restore,
    [string]$StatePath = "C:\SortView-R4\disabled-adapters.json"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated (Administrator) PowerShell window."
}

if ($Restore) {
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { throw "No '$StatePath': nothing was disabled by this script." }
    $names = @(Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json)
    Enable-NetAdapter -Name $names -Confirm:$false
    Remove-Item -LiteralPath $StatePath
    Write-Host "Re-enabled: $($names -join ', '). Check the connection before going on."
    exit 0
}

$recordPath = Join-Path (Join-Path $DataRoot "setup") "enrollment-recovery.json"
if (Test-Path -LiteralPath $recordPath) {
    throw "'$recordPath' already exists: this computer already has a saved enrollment. Use a clean snapshot."
}
if (Test-Path -LiteralPath $StatePath) {
    throw "'$StatePath' exists: an earlier run disabled adapters. Run with -Restore first."
}
$names = @(Get-NetAdapter | Where-Object { $_.Status -eq "Up" } | ForEach-Object { $_.Name })
if ($names.Count -eq 0) { throw "No network adapter is up; Setup could not reach SortView anyway." }

Write-Host "Watching for $recordPath (up to $TimeoutMinutes minutes)."
Write-Host "Adapters that will be disabled: $($names -join ', '). Start SortView Setup now."

$clock = [Diagnostics.Stopwatch]::StartNew()
while (-not (Test-Path -LiteralPath $recordPath)) {
    if ($clock.Elapsed.TotalMinutes -ge $TimeoutMinutes) {
        Write-Host "Timed out. Nothing was changed."
        exit 1
    }
    Start-Sleep -Milliseconds 50
}

$stateFolder = Split-Path -Parent $StatePath
if (-not (Test-Path -LiteralPath $stateFolder)) { New-Item -ItemType Directory -Path $stateFolder | Out-Null }
ConvertTo-Json -InputObject $names | Set-Content -LiteralPath $StatePath -Encoding UTF8
Disable-NetAdapter -Name $names -Confirm:$false
$stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
Write-Host "$stamp UTC: the enrollment record appeared; disabled $($names -join ', ')."
Write-Host "Record where SortView Setup stops and what it says. Restore with -Restore (or revert the snapshot)."
exit 0
