<#
.SYNOPSIS
    One-time ACL remediation for an ALREADY-INSTALLED SortView Collector
    whose permissions predate this hardening work -- run this once onsite
    against an existing production install; it is not part of the normal
    install/update flow (install.ps1 and tools\update.ps1 already apply
    the same protection themselves, going forward, at the points where
    they create or replace these paths).

.DESCRIPTION
    Confirmed onsite finding this fixes: C:\SortView\Collector (and the
    .exe and logs\ underneath it, as inherited children of an unprotected
    parent) had "Authenticated Users: Modify", and
    C:\ProgramData\SortViewCollector and its config\ subdirectory allowed
    "BUILTIN\Users: Write" at the directory level -- while the Scheduled
    Task that runs SortViewCollector.exe runs as SYSTEM every 15 minutes.
    Under those ACLs, any local authenticated user (or process running as
    one) could already replace the executable or rewrite
    collector_config.json/classification_rules.json.

    Restricts, using tools\CollectorAcl.ps1's Protect-CollectorPath (the
    SAME primitive install.ps1/tools\update.ps1 now use, and the same
    SYSTEM+Administrators-only, inheritance-cut design as the v2 secret
    folder's own ACL logic in collector\v2_keys.py):

        -InstallRoot                          (recursively -- covers the
                                                .exe and its own logs\)
        -DataRoot                             (its own entry only)
        -DataRoot\config
        -DataRoot\data
        -DataRoot\logs

    NEVER touches -DataRoot\secrets (the v2 DPAPI master secret's folder)
    -- that remains governed exclusively by collector\v2_keys.py's own,
    separately fail-closed ACL logic; this script does not even look at
    whether it exists.

    SAFETY: refuses to run at all unless the "-TaskName" Scheduled Task is
    already Disabled, or is genuinely NOT REGISTERED (a real, explicitly
    distinguished case -- not assumed from any query failure; see below),
    so an ACL change can never race a live SYSTEM-context run reading/
    writing under these paths. Never disables, enables, or otherwise
    touches the task itself -- that is the operator's decision, made
    separately, before running this. The Scheduled Task query itself
    fails closed: a genuine Task Scheduler/CIM/permission failure refuses
    the run exactly like an Enabled task does, rather than being treated
    as "not registered" -- those are NOT the same thing, and only a
    query that actually succeeds and returns nothing is "not registered."

    FAIL CLOSED ON A MISSING TARGET: this is a security remediation, not
    a best-effort cleanup -- every one of the five target paths below is
    REQUIRED to exist. A missing target is reported as a problem and
    makes the whole run exit nonzero; "REPAIR COMPLETE" is never printed
    when any required target was absent. (A genuinely absent target most
    likely means -InstallRoot/-DataRoot were given wrong, or this install
    is not what it was expected to be -- either way, silently treating
    "I couldn't find it" as "nothing to do here" is exactly the kind of
    gap that would let a real misconfiguration go unnoticed.)

    Deletes nothing, writes nothing to collector_config.json or any other
    file's CONTENTS, and touches no PATH beyond the five listed above.
    Safe to run twice: Protect-CollectorPath's end state (SYSTEM and
    Administrators only, inheritance cut) is the same the second time.

    EXIT CODES: 0 = every required path exists and verified protected. 1
    = not elevated, the Scheduled Task query failed (for any reason,
    including but not limited to the task being Enabled), a required
    target path does not exist, or an ACL could not be applied/verified
    -- in every case, whichever paths were already fixed before the
    failure remain fixed (each path is applied and verified
    independently); this script does not attempt to undo a partial run.

.EXAMPLE
    .\repair-collector-permissions.ps1
    .\repair-collector-permissions.ps1 -InstallRoot "C:\SortView\Collector" -DataRoot "C:\ProgramData\SortViewCollector"
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\SortView\Collector",
    [string]$DataRoot = "C:\ProgramData\SortViewCollector",
    [string]$TaskName = "SortView Collector"
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "CollectorAcl.ps1")

function Stop-Repair {
    param([Parameter(Mandatory)][string]$Message, [int]$ExitCode = 1)
    Write-Host ""
    Write-Host "REPAIR REFUSED: $Message" -ForegroundColor Red
    exit $ExitCode
}

# --- elevation -----------------------------------------------------------
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Stop-Repair "This script must be run from an elevated (Administrator) PowerShell session."
}

# --- task-disabled gate: never mutated by this script, only checked ------
# Get-ScheduledTask -ErrorAction Stop (never SilentlyContinue): SilentlyContinue would make a genuine query/
# CIM/permission failure indistinguishable from "the task simply isn't registered" -- both would leave $task
# $null and this script would proceed on a machine state it never actually verified. "Not registered" is a
# real, intentionally supported case, but it is confirmed by inspecting the SPECIFIC error the ScheduledTasks
# module raises for it -- not assumed from any failure.
$task = $null
$taskIsRegistered = $true
try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch {
    $isNotRegistered = ($_.CategoryInfo.Category -eq "ObjectNotFound") -or
                        ($_.Exception.Message -like "*No MSFT_ScheduledTask objects found*")
    if ($isNotRegistered) {
        $taskIsRegistered = $false
    } else {
        Stop-Repair ("Could not query the '$TaskName' Scheduled Task ($($_.Exception.Message)). A query/CIM/" +
            "permission failure is NOT the same as 'not registered' and cannot be treated as safe to proceed " +
            "on. Nothing was changed.")
    }
}
if ($taskIsRegistered -and [bool]$task.Settings.Enabled) {
    Stop-Repair ("The '$TaskName' Scheduled Task is currently ENABLED (state: $($task.State)). Disable it first " +
        "so an ACL change here cannot race a live SYSTEM-context run:`n  Disable-ScheduledTask -TaskName '$TaskName'`n" +
        "This script never disables the task itself. Nothing was changed.")
}
if (-not $taskIsRegistered) {
    Write-Host "'$TaskName' is not registered -- proceeding (nothing to race)." -ForegroundColor Yellow
} else {
    Write-Host "'$TaskName' is Disabled -- safe to proceed." -ForegroundColor Green
}

# --- targets: NEVER include -DataRoot\secrets -----------------------------
$targets = @(
    [ordered]@{ Path = $InstallRoot; Recurse = $true }
    [ordered]@{ Path = $DataRoot; Recurse = $false }
    [ordered]@{ Path = (Join-Path $DataRoot "config"); Recurse = $true }
    [ordered]@{ Path = (Join-Path $DataRoot "data"); Recurse = $true }
    [ordered]@{ Path = (Join-Path $DataRoot "logs"); Recurse = $true }
)

Write-Host ""
Write-Host "=== Restricting to SYSTEM + Administrators only (everyone else denied) ===" -ForegroundColor Cyan

$problems = @()
foreach ($target in $targets) {
    if (-not (Test-Path -LiteralPath $target.Path)) {
        # FAIL CLOSED: a required target that does not exist is a problem, not something to skip past -- see
        # the docstring's "FAIL CLOSED ON A MISSING TARGET" section. This must reach $problems so the run
        # exits nonzero and "REPAIR COMPLETE" is never printed.
        Write-Host "MISSING (required): $($target.Path)" -ForegroundColor Red
        $problems += $target.Path
        continue
    }
    Write-Host "Protecting: $($target.Path)$(if ($target.Recurse) { ' (and everything already inside it)' })"
    try {
        Protect-CollectorPath -Path $target.Path -Recurse:$target.Recurse
        Write-Host "  VERIFIED: SYSTEM + Administrators only, inheritance cut." -ForegroundColor Green
    } catch {
        Write-Host "  FAILED: $($_.Exception.Message)" -ForegroundColor Red
        $problems += $target.Path
    }
}

Write-Host ""
if ($problems.Count -gt 0) {
    Write-Host "REPAIR INCOMPLETE -- $($problems.Count) path(s) are missing or could not be protected/verified:" -ForegroundColor Red
    foreach ($p in $problems) { Write-Host "  $p" -ForegroundColor Red }
    Write-Host "Paths not listed above (if any) were already fixed and remain fixed." -ForegroundColor Yellow
    exit 1
}

Write-Host "REPAIR COMPLETE -- every existing target path is protected and verified." -ForegroundColor Green
Write-Host "The v2 secrets directory ($((Join-Path $DataRoot 'secrets'))), if present, was not touched -- it is governed separately." -ForegroundColor Cyan
exit 0
