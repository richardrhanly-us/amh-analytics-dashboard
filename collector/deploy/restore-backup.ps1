<#
.SYNOPSIS
    Restores a frozen SortView Collector runtime backup made by tools\update.ps1
    -- only after proving the backup is restricted, not redirected, and
    identical to a release the operator trusts. (1.0.15)

.DESCRIPTION
    Replaces the old printed rollback (a bare Copy-Item of the backup into
    -InstallRoot), which copied whatever the backup held into the folder a
    SYSTEM task runs. On NBPL's production sorter a backup was found
    inheriting Authenticated Users Modify from C:\SortView (2026-11-16), so a
    backup is never trusted for what it says about itself.

    TRUST ANCHOR -- exactly one is required, and it must come from OUTSIDE the
    backup:
      -ExpectedManifestSha256   the release record's "Bundle MANIFEST.json
                                SHA-256" for the release the backup holds
                                (docs/releases/collector-<version>.md), or
      -TrustedManifestPath      the MANIFEST.json of that release's canonical
                                ZIP, extracted after the ZIP was verified
                                against its release record. Refused if it is
                                inside the backup or reached through a
                                junction or link.

    REFUSES, CHANGING NOTHING, when: the session is not elevated; the trust
    anchor is missing, doubled or malformed; the backup or install path is
    reached through a junction or link, or the backup holds one; the backup
    is not restricted to SYSTEM+Administrators on every item, or any item is
    owned by another account (an insecure backup -- e.g. one made before
    1.0.15: reinstall that release from its verified canonical ZIP instead);
    the backup's MANIFEST.json is not the trusted one; any release file is
    missing or differs from it (an altered backup); the backup is not a
    frozen release; or the task does not stop in time.

    ORDER:
      1. Verify the backup (above). Files in the backup that no release placed
         (e.g. old parser logs) are listed and NEVER restored.
      2. Disable the task, and stop and wait out a run in progress.
      3. Move the current -InstallRoot aside, intact, to
         <InstallRoot>.replaced-<timestamp>.
      4. Create -InstallRoot ALREADY restricted, copy in only the verified
         release files, write its manifests, restrict every item, and verify
         the result (Test-InstalledManifest).
      5. Preflight the restored runtime.
      6. Re-enable the task only if it was enabled before -- never an
         immediate run.
    Any failure from step 3 on leaves the task DISABLED and prints how to
    return to the installation that was replaced.

    Never touches the data root -- config, secrets (API token, Contract v2
    key), classification rules and cursors stay exactly as they are; the
    configuration path is used only to preflight. Frozen installs only: a
    developer (source) backup is restored by hand.

.EXAMPLE
    .\restore-backup.ps1 -BackupRoot "C:\SortView\Collector.backup-20261120-091500" `
        -ExpectedManifestSha256 <Bundle MANIFEST.json SHA-256 from the prior release's record>
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$BackupRoot,
    [string]$ExpectedManifestSha256,
    [string]$TrustedManifestPath,
    [string]$InstallRoot = "C:\SortView\Collector",
    [string]$ConfigPath = "C:\ProgramData\SortViewCollector\config\collector_config.json",
    [string]$TaskName = "SortView Collector",
    [int]$StopTimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "CollectorAcl.ps1")
. (Join-Path $PSScriptRoot "CollectorManifest.ps1")
. (Join-Path $PSScriptRoot "CollectorBackup.ps1")

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell session. Nothing was changed."
}

$BackupRoot = [System.IO.Path]::GetFullPath($BackupRoot).TrimEnd('\', '/')
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\', '/')

function Test-PathInside([string]$Root, [string]$Path) {
    try { [void](Get-CollectorRelativePath -Root $Root -FullPath $Path); return $true } catch { return $false }
}

if ($BackupRoot -ieq $InstallRoot -or (Test-PathInside $BackupRoot $InstallRoot) -or (Test-PathInside $InstallRoot $BackupRoot)) {
    throw "The backup '$BackupRoot' and the install '$InstallRoot' must be separate folders. Nothing was changed."
}

# --- the trust anchor: exactly one, never from the backup ---
$hasHash = -not [string]::IsNullOrWhiteSpace($ExpectedManifestSha256)
$hasPath = -not [string]::IsNullOrWhiteSpace($TrustedManifestPath)
if ($hasHash -eq $hasPath) {
    throw "Give exactly one trust anchor: -ExpectedManifestSha256 (from the release record) or -TrustedManifestPath " +
          "(the MANIFEST.json of the verified canonical release ZIP). Nothing was changed."
}
if ($hasPath) {
    $TrustedManifestPath = [System.IO.Path]::GetFullPath($TrustedManifestPath)
    Assert-CollectorPathNotRedirected -Path $TrustedManifestPath
    if (Test-PathInside $BackupRoot $TrustedManifestPath) {
        throw "-TrustedManifestPath is inside the backup -- a backup cannot vouch for itself. Nothing was changed."
    }
    if (-not (Test-Path -LiteralPath $TrustedManifestPath -PathType Leaf)) {
        throw "-TrustedManifestPath '$TrustedManifestPath' is not a file. Nothing was changed."
    }
    $trustedSha = Get-CollectorFileSha256 $TrustedManifestPath
    Write-Host "Trust anchor: $TrustedManifestPath (SHA-256 $trustedSha)"
} else {
    $trustedSha = $ExpectedManifestSha256.Trim()
    Write-Host "Trust anchor: expected MANIFEST.json SHA-256 $trustedSha"
}

Write-Host "=== 1. Verify the backup (nothing is changed yet) ===" -ForegroundColor Cyan
Assert-CollectorPathNotRedirected -Path $BackupRoot
Assert-CollectorPathNotRedirected -Path $InstallRoot
if (-not (Test-Path -LiteralPath $BackupRoot -PathType Container)) {
    throw "No backup folder at '$BackupRoot'. Nothing was changed."
}
$tree = Get-CollectorTreeFiles -Root $BackupRoot
if (@($tree.Problems).Count -gt 0) {
    throw "REDIRECTED BACKUP: $(@($tree.Problems) -join '; ') -- refusing to restore. Nothing was changed."
}
if (-not (Test-CollectorPathProtected -Path $BackupRoot -Recurse)) {
    throw "INSECURE BACKUP: '$BackupRoot' is not restricted to SYSTEM and Administrators on every item, so it could " +
          "have been changed by another account. Do not restore it; reinstall that release from its verified " +
          "canonical ZIP instead. Nothing was changed."
}
if (-not (Test-CollectorOwnerTrusted -Path $BackupRoot -Recurse)) {
    throw "INSECURE BACKUP: an item under '$BackupRoot' is owned by an account other than Administrators or SYSTEM, " +
          "which keeps the right to reopen it. Do not restore it. Nothing was changed."
}
$trusted = Get-CollectorTrustedBackup -BackupRoot $BackupRoot -TrustedManifestSha256 $trustedSha
if (@($trusted.Problems).Count -gt 0) {
    Write-Host "BACKUP VERIFICATION FAILED:" -ForegroundColor Red
    foreach ($problem in @($trusted.Problems)) { Write-Host "  $problem" -ForegroundColor Red }
    throw "ALTERED OR UNTRUSTED BACKUP: $(@($trusted.Problems).Count) problem(s) -- refusing to restore. Nothing was changed."
}
Write-Host "Backup verified: $(@($trusted.Files).Count) release file(s) of $($trusted.Manifest.product) $($trusted.Manifest.version) match the trusted manifest." -ForegroundColor Green
foreach ($unlisted in @($trusted.Unlisted)) {
    Write-Host "  Not a release file, will NOT be restored: $unlisted" -ForegroundColor Yellow
}

Write-Host "=== 2. Stop collection ===" -ForegroundColor Cyan
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$wasRegistered = $null -ne $existingTask
$wasEnabled = $false
if ($wasRegistered) {
    $wasEnabled = [bool]$existingTask.Settings.Enabled
    if ($wasEnabled) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Host "Disabled '$TaskName'."
    }
    if ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running") {
        Write-Host "Task is running -- stopping and waiting (timeout: ${StopTimeoutSeconds}s)..."
        Stop-ScheduledTask -TaskName $TaskName
        $deadline = (Get-Date).AddSeconds($StopTimeoutSeconds)
        while ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running" -and (Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 1
        }
        if ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running") {
            throw "Task '$TaskName' did not stop within ${StopTimeoutSeconds}s -- nothing was restored, and the task is " +
                  "left DISABLED. Investigate the stuck run, then re-enable it with Enable-ScheduledTask -TaskName '$TaskName'."
        }
    }
} else {
    Write-Host "'$TaskName' is not registered -- nothing to stop."
}

$ReplacedRoot = "$InstallRoot.replaced-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$movedAside = $false

function Stop-RestoreWithCollectionDisabled([string]$Message) {
    Write-Host ""
    Write-Host "RESTORE FAILED: $Message" -ForegroundColor Red
    Write-Host "Collection is left DISABLED." -ForegroundColor Red
    if ($movedAside) {
        Write-Host "The installation that was in place before this restore is preserved, unchanged, at:" -ForegroundColor Red
        Write-Host "  $ReplacedRoot"
        Write-Host "To return to it:"
        Write-Host "  if (Test-Path `"$InstallRoot`") { Remove-Item -Recurse -Force `"$InstallRoot`" }"
        Write-Host "  Move-Item `"$ReplacedRoot`" `"$InstallRoot`""
        Write-Host "  & `"$PSScriptRoot\verify-install.ps1`" -InstallRoot `"$InstallRoot`""
    } else {
        Write-Host "The installed runtime at '$InstallRoot' was not changed." -ForegroundColor Red
    }
    if ($wasRegistered) {
        Write-Host "Re-enable collection only once the runtime passes preflight:"
        Write-Host "  Enable-ScheduledTask -TaskName '$TaskName'"
    }
    exit 1
}

try {
    Write-Host "=== 3. Move the current installation aside ===" -ForegroundColor Cyan
    if (Test-CollectorEntryExists -Path $InstallRoot) {
        Assert-CollectorBackupPathAvailable -Path $ReplacedRoot
        Move-Item -LiteralPath $InstallRoot -Destination $ReplacedRoot
        $movedAside = $true
        Write-Host "Moved '$InstallRoot' to '$ReplacedRoot' (kept, not deleted)."
    } else {
        Write-Host "No installation at '$InstallRoot' -- nothing to move aside."
    }

    Write-Host "=== 4. Restore the verified release files into a restricted folder ===" -ForegroundColor Cyan
    New-ProtectedCollectorDirectory -Path $InstallRoot
    foreach ($entry in @($trusted.Files)) {
        $relative = ConvertTo-CollectorCanonicalRelativePath $entry.installed_path
        $destination = Join-Path $InstallRoot $relative
        $destinationDir = Split-Path -Path $destination -Parent
        if (-not (Test-Path -LiteralPath $destinationDir -PathType Container)) {
            New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
        }
        Copy-Item -LiteralPath (Join-Path $BackupRoot $relative) -Destination $destination
    }
    New-InstalledManifest -BundleRoot $BackupRoot -InstallRoot $InstallRoot -Manifest $trusted.Manifest -BundleKind "frozen"
    # The release's own bytes, so the restored MANIFEST.json still has the release record's SHA-256.
    Copy-Item -LiteralPath (Join-Path $BackupRoot "MANIFEST.json") -Destination (Join-Path $InstallRoot "MANIFEST.json") -Force
    Protect-CollectorPath -Path $InstallRoot -Recurse
    Test-InstalledManifest -InstallRoot $InstallRoot
    if ((Get-CollectorFileSha256 (Join-Path $InstallRoot "MANIFEST.json")) -ne $trustedSha.ToUpperInvariant()) {
        throw "the restored MANIFEST.json does not have the trusted SHA-256."
    }
    Write-Host "Restored $(@($trusted.Files).Count) release file(s); restricted to SYSTEM + Administrators and verified." -ForegroundColor Green

    Write-Host "=== 5. Preflight the restored runtime ===" -ForegroundColor Cyan
    & (Join-Path $InstallRoot "SortViewCollector.exe") preflight --config $ConfigPath
    if ($LASTEXITCODE -ne 0) {
        throw "preflight of the restored runtime failed (exit code $LASTEXITCODE)."
    }
} catch {
    Stop-RestoreWithCollectionDisabled $_.Exception.Message
}

Write-Host "=== 6. Resume collection ===" -ForegroundColor Cyan
if ($wasRegistered -and $wasEnabled) {
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "Re-enabled '$TaskName' -- it resumes on its normal schedule, not immediately." -ForegroundColor Green
} elseif ($wasRegistered) {
    Write-Host "'$TaskName' was disabled before this restore -- left disabled." -ForegroundColor Yellow
} else {
    Write-Host "No task to resume. Use tools\register-task.ps1 if needed."
}

Write-Host ""
Write-Host "RESTORE COMPLETE: $($trusted.Manifest.product) $($trusted.Manifest.version) is installed at $InstallRoot." -ForegroundColor Green
if ($movedAside) {
    Write-Host "The installation it replaced is kept at $ReplacedRoot -- remove it once you are satisfied."
}
Write-Host "Confirm the next scheduled run reaches SortView (heartbeat, pipeline health), not only its exit code." -ForegroundColor Yellow
