<#
.SYNOPSIS
    Updates an existing SortView Collector install in place from a
    standalone RELEASE BUNDLE, preserving config/state/status/logs/token,
    keeping a rollback copy of the prior runtime, and verifying the new
    runtime with preflight BEFORE declaring success or touching the
    Scheduled Task. Supports BOTH bundle/install kinds (SOURCE and
    FROZEN) -- see install.ps1's own docstring for what distinguishes
    them; auto-detected here the same way, from both the BUNDLE's own
    contents and the INSTALLED runtime's contents, never a user-facing
    switch.

.DESCRIPTION
    This is the release-bundle-facing updater: it resolves every source
    file relative to the bundle it ships in (this script lives at
    <bundle>\tools\update.ps1; the payload is at ..\collector and ..\agent
    for a source bundle, or ..\runtime for a frozen one, relative to this
    script), never a Git repository. See collector/deploy/update-collector.ps1
    for the repo-checkout-based equivalent used for developer/QA updates.

    MISMATCH SAFETY: refuses (fails closed, touches nothing) if the
    bundle's kind does not match the currently-installed runtime's kind --
    e.g. a source-mode bundle pointed at a frozen install, or vice versa.
    Updating across kinds is not supported by this script; reinstall with
    install.ps1 instead if you genuinely want to switch kinds.

    MANIFEST VERIFICATION: the bundle's actual file set is checked against
    MANIFEST.json EXACTLY -- every listed file present with the correct
    SHA-256, and no unlisted extra file -- BEFORE anything is touched; see
    Test-ReleaseManifest, identical to install.ps1's own copy of this
    function. This is an INTEGRITY check, not an authenticity/signature
    one -- see that function's own comment for the precise distinction.

    AMBIGUOUS INSTALL SAFETY: also refuses if the INSTALLED runtime itself
    is ambiguous -- both a source marker (.venv\Scripts\python.exe) and a
    frozen marker (SortViewCollector.exe) present under -InstallRoot at
    once (this should not happen with a correctly-guarded install.ps1, but
    is checked explicitly rather than assumed). Checked before any task or
    runtime mutation.

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
      4. Performs the update, then preflight (frozen: SortViewCollector.exe
         preflight --config ...; source: python -m collector.preflight).
      5. Restores the PRIOR enabled/disabled state only after preflight
         passes -- via Enable-ScheduledTask, never Start-ScheduledTask (an
         immediate ad hoc run is never triggered implicitly; pass
         -StartNow to explicitly request one after a successful update).
      6. On preflight failure, the task is left DISABLED regardless of its
         prior state, with exact rollback instructions printed -- nothing
         is auto-reverted, and nothing is left able to fire on a
         half-updated runtime.

    RUNTIME REPLACEMENT: for a SOURCE install, uses the existing
    dependency-hash fast path (requirements.txt unchanged -> code-only
    deterministic replacement, keeping the venv; changed -> full rebuild).
    For a FROZEN install, there is no dependency-hash concept at all --
    every frozen update deterministically replaces the COMPLETE
    -InstallRoot (not just SortViewCollector.exe/_internal specifically --
    since -InstallRoot for a frozen install IS the whole application
    runtime, with -DataRoot entirely separate, treating all of it as the
    replaceable unit is what actually guarantees no stale top-level
    file/folder from an older frozen release can ever survive, even one a
    future PyInstaller build adds that this script doesn't know about by
    name). Removed entirely then recreated from the bundle's runtime\
    directory, never overlaid. No pip/venv/requirements logic ever
    executes for a frozen update.

    Never touches -DataRoot (config\, data\, logs\) or the Machine-scope
    SORTVIEW_API_TOKEN -- only the application runtime (-InstallRoot) is
    replaced, for either kind.

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

function Test-ReleaseManifest {
    <# See install.ps1's identical copy of this function for the full
    rationale -- duplicated deliberately, not shared via a module, per
    this codebase's established preference for small independent
    primitives over cross-script coupling in release-bundle tooling.
    Verifies the bundle's ACTUAL file set matches MANIFEST.json EXACTLY
    (safe/unique manifest paths, every listed file present with the
    correct SHA-256, and no unlisted extra file -- MANIFEST.json itself
    excluded from that comparison). ACCURACY NOTE: this is an INTEGRITY
    check (corruption, incomplete copy, accidental modification), NOT an
    authenticated signature -- MANIFEST.json itself is not signed.
    Code signing (not yet implemented) is the intended future
    authenticity mechanism. #>
    param([Parameter(Mandatory)][string]$BundleRoot)

    $manifestPath = Join-Path $BundleRoot "MANIFEST.json"
    if (-not (Test-Path $manifestPath -PathType Leaf)) {
        throw "This release bundle has no MANIFEST.json -- refusing to install/update from an unverifiable bundle."
    }

    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    $problems = @()

    # --- manifest paths themselves: no traversal/rooted paths, no duplicates ---
    $manifestKeys = @{}
    foreach ($entry in $manifest.files) {
        $relPath = $entry.path
        if ($relPath -match '(^|[\\/])\.\.([\\/]|$)' -or $relPath -match '^[\\/]' -or $relPath -match '^[A-Za-z]:') {
            $problems += "UNSAFE MANIFEST PATH: $relPath (traversal or rooted/absolute path)"
            continue
        }
        $key = ($relPath -replace '/', '\').ToLowerInvariant()
        if ($manifestKeys.ContainsKey($key)) {
            $problems += "DUPLICATE MANIFEST PATH: $relPath"
            continue
        }
        $manifestKeys[$key] = $entry
    }

    if ($problems.Count -eq 0) {
        # --- every safe, unique manifest entry: exists, hash matches ---
        foreach ($key in $manifestKeys.Keys) {
            $entry = $manifestKeys[$key]
            $filePath = Join-Path $BundleRoot $key
            if (-not (Test-Path $filePath -PathType Leaf)) {
                $problems += "MISSING: $($entry.path)"
                continue
            }
            $actualHash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash
            if ($actualHash -ne $entry.sha256.ToUpper()) {
                $problems += "HASH MISMATCH: $($entry.path) (expected $($entry.sha256), got $($actualHash.ToLower()))"
            }
        }

        # --- no file present that the manifest doesn't know about ---
        $actualFiles = Get-ChildItem -Path $BundleRoot -Recurse -File
        foreach ($f in $actualFiles) {
            $relative = $f.FullName.Substring($BundleRoot.Length).TrimStart('\')
            if ($relative -eq "MANIFEST.json") { continue }
            $key = $relative.ToLowerInvariant()
            if (-not $manifestKeys.ContainsKey($key)) {
                $problems += "UNEXPECTED FILE (not listed in manifest): $relative"
            }
        }
    }

    if ($problems.Count -gt 0) {
        Write-Host "MANIFEST VERIFICATION FAILED -- this bundle may be corrupted, incompletely" -ForegroundColor Red
        Write-Host "copied, accidentally modified, or otherwise does not match its release manifest:" -ForegroundColor Red
        foreach ($p in $problems) { Write-Host "  $p" -ForegroundColor Red }
        throw "Refusing to proceed -- $($problems.Count) manifest problem(s). Re-download/re-copy the bundle rather than editing it by hand."
    }

    Write-Host "Manifest verified: $($manifest.files.Count) file(s) match their recorded SHA-256, no unlisted files present." -ForegroundColor Green
}

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell session."
}

# This script lives at <bundle>\tools\update.ps1 -- the bundle root is
# one level up.
$BundleRoot = Split-Path $PSScriptRoot -Parent
$SourceCollectorDir = Join-Path $BundleRoot "collector"
$SourceAgentDir = Join-Path $BundleRoot "agent"
$SourceRequirements = Join-Path $BundleRoot "requirements.txt"
$FrozenRuntimeDir = Join-Path $BundleRoot "runtime"
$FrozenExeSource = Join-Path $FrozenRuntimeDir "SortViewCollector.exe"

$bundleIsSource = (Test-Path $SourceCollectorDir) -and (Test-Path $SourceAgentDir)
$bundleIsFrozen = Test-Path $FrozenExeSource

if ($bundleIsSource -and $bundleIsFrozen) {
    throw "This release bundle contains BOTH a source runtime (collector\, agent\) and a frozen " +
          "runtime (runtime\SortViewCollector.exe) -- ambiguous, refusing to guess. This bundle is malformed."
}
if (-not $bundleIsSource -and -not $bundleIsFrozen) {
    throw "This release bundle is incomplete or damaged -- neither a source runtime nor a frozen " +
          "runtime was found under '$BundleRoot'. Re-download/re-copy the bundle rather than editing it by hand."
}
if ($bundleIsSource -and -not (Test-Path $SourceRequirements)) {
    throw "This release bundle is incomplete or damaged -- source bundle missing requirements.txt."
}

Test-ReleaseManifest -BundleRoot $BundleRoot

$InstalledVenvPython = Join-Path $InstallRoot ".venv\Scripts\python.exe"
$InstalledFrozenExe = Join-Path $InstallRoot "SortViewCollector.exe"
$installIsSource = Test-Path $InstalledVenvPython
$installIsFrozen = Test-Path $InstalledFrozenExe

if ($installIsSource -and $installIsFrozen) {
    throw "The installed runtime at '$InstallRoot' is ambiguous/malformed -- BOTH a source install " +
          "marker (.venv\Scripts\python.exe) and a frozen install marker (SortViewCollector.exe) were " +
          "found. Refusing to update an install that mixes both kinds -- this should never happen from " +
          "a correctly-guarded install.ps1 run; investigate and clean up '$InstallRoot' manually " +
          "before retrying. Checked before any task or runtime mutation."
}
if (-not $installIsSource -and -not $installIsFrozen) {
    throw "No existing install found at '$InstallRoot' -- use install.ps1 for a fresh install."
}
if ($bundleIsFrozen -and -not $installIsFrozen) {
    throw "This bundle is a FROZEN release, but the install at '$InstallRoot' is a SOURCE install " +
          "(no SortViewCollector.exe found -- has a .venv instead). Refusing to update across kinds. " +
          "Reinstall with install.ps1 if you genuinely want to switch from source to frozen."
}
if ($bundleIsSource -and -not $installIsSource) {
    throw "This bundle is a SOURCE release, but the install at '$InstallRoot' is a FROZEN install " +
          "(SortViewCollector.exe found, no .venv). Refusing to update across kinds. Reinstall with " +
          "install.ps1 if you genuinely want to switch from frozen to source."
}

$isFrozen = $bundleIsFrozen
Write-Host "Update kind: $(if ($isFrozen) { 'FROZEN (no Python required)' } else { 'SOURCE (Python + venv)' })" -ForegroundColor Cyan

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
if ($isFrozen) {
    # The WHOLE InstallRoot IS the frozen application runtime (DataRoot is
    # entirely separate) -- back up its COMPLETE current contents, not
    # just the two paths today's build happens to produce
    # (SortViewCollector.exe + _internal\), so any future frozen release
    # that adds a new top-level file/folder is still fully covered
    # without this script needing to be updated in lockstep.
    #
    # $BackupRoot must NOT already exist before this call (it's a fresh
    # timestamped path, so it never does in practice) -- verified
    # empirically, same finding as elsewhere in this file: Copy-Item onto
    # an EXISTING destination nests the source folder inside it instead
    # of copying its contents in place; onto a destination that does not
    # yet exist, it copies the contents flat, which is what's wanted here.
    Copy-Item $InstallRoot -Destination $BackupRoot -Recurse -Force
    Write-Host "Backed up complete current frozen runtime ($InstallRoot) to $BackupRoot"
} else {
    New-Item -ItemType Directory -Path (Join-Path $BackupRoot "collector") -Force | Out-Null
    Copy-Item (Join-Path $InstallRoot "collector\*.py") -Destination (Join-Path $BackupRoot "collector") -Force
    Write-Host "Backed up current collector\*.py to $BackupRoot\collector"

    if (Test-Path (Join-Path $InstallRoot "agent")) {
        New-Item -ItemType Directory -Path (Join-Path $BackupRoot "agent") -Force | Out-Null
        Copy-Item (Join-Path $InstallRoot "agent\*") -Destination (Join-Path $BackupRoot "agent") -Recurse -Force
        Write-Host "Backed up current canonical parser runtime to $BackupRoot\agent"
    }
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

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

if ($isFrozen) {
    Write-Host "=== 3. Replace frozen runtime (deterministic, no dependency-hash fast path) ===" -ForegroundColor Cyan
    # No pip/venv/requirements concept for a frozen install -- every
    # update replaces the WHOLE InstallRoot the same way, always: remove
    # it entirely then recreate it fresh from the bundle's runtime\
    # directory, never overlay. Since -InstallRoot IS the complete frozen
    # application runtime (DataRoot is entirely separate), this is exact
    # by construction -- no top-level file/folder from an older release
    # (not just SortViewCollector.exe/_internal specifically) can ever
    # survive as a stale leftover, regardless of what a future release
    # happens to add.
    Remove-Item -Recurse -Force $InstallRoot
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    Copy-Item (Join-Path $FrozenRuntimeDir "*") -Destination $InstallRoot -Recurse -Force
    Write-Host "Replaced frozen runtime from $FrozenRuntimeDir"
} else {
    Write-Host "=== 3. Dependency check ===" -ForegroundColor Cyan
    $newHash = (Get-FileHash $SourceRequirements -Algorithm SHA256).Hash
    $hashMarkerPath = Join-Path $InstallRoot ".deps-hash"
    $oldHash = if (Test-Path $hashMarkerPath) { (Get-Content $hashMarkerPath -Raw).Trim() } else { $null }

    if ($newHash -eq $oldHash) {
        Write-Host "requirements.txt unchanged -- replacing runtime files only, keeping the existing venv."
        # DETERMINISTIC REPLACEMENT, not an overlay -- see FIX 2's own
        # comment history: deleting each runtime directory entirely before
        # recreating it from the bundle guarantees installed
        # collector\/agent\ == exactly what the bundle contains. The task
        # is already disabled (step 1, above); .venv and everything under
        # -DataRoot are untouched.
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
}

Write-Host "=== 4. Verify the new runtime ===" -ForegroundColor Cyan
if ($isFrozen) {
    & $InstalledFrozenExe preflight --config $ConfigPath
    $preflightExitCode = $LASTEXITCODE
} else {
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
}

if ($preflightExitCode -ne 0) {
    Write-Host ""
    Write-Host "UPDATE FAILED VERIFICATION (preflight exit code $preflightExitCode)." -ForegroundColor Red
    Write-Host "The new runtime is left in place at $InstallRoot for inspection. The prior" -ForegroundColor Red
    Write-Host "runtime is fully intact -- to roll back manually:" -ForegroundColor Red
    if (Test-Path "$BackupRoot-full") {
        Write-Host "  Remove-Item -Recurse -Force `"$InstallRoot`""
        Write-Host "  Move-Item `"$BackupRoot-full`" `"$InstallRoot`""
    } elseif ($isFrozen) {
        # The COMPLETE prior frozen InstallRoot, not just
        # SortViewCollector.exe/_internal specifically -- $BackupRoot IS a
        # full copy of the whole prior InstallRoot (see step 2's backup
        # above), so restoring it in full is what actually guarantees
        # nothing from the failed update (any file the new release added,
        # anywhere under InstallRoot) can survive. Remove first, then copy
        # the backup in -- same exact-restoration property as the
        # source-mode branch below.
        Write-Host "  Remove-Item -Recurse -Force `"$InstallRoot`""
        Write-Host "  Copy-Item `"$BackupRoot`" `"$InstallRoot`" -Recurse -Force"
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
if ($isFrozen) {
    Write-Host "Prior runtime preserved at: $BackupRoot"
} else {
    Write-Host "Prior runtime preserved at: $(if (Test-Path "$BackupRoot-full") { "$BackupRoot-full" } else { "$BackupRoot\collector (code only)" })"
}
Write-Host "Consider re-running tools\preflight-system.ps1 too before fully trusting this update," -ForegroundColor Yellow
Write-Host "especially if dependencies changed." -ForegroundColor Yellow
