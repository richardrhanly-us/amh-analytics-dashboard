<#
.SYNOPSIS
    Installs the SortView Collector v1 runtime from a standalone RELEASE
    BUNDLE -- FRESH installs only. For updating an existing install, use
    tools\update.ps1 (in the same bundle) instead. Supports BOTH bundle
    kinds this repo's collector/build_release.py can produce -- SOURCE
    (collector\*.py + agent\* + a Python venv, `python -m collector.run`)
    and FROZEN (a PyInstaller onedir runtime, no Python/pip/venv at all,
    SortViewCollector.exe run --config ...) -- auto-detected from the
    bundle's own contents (collector\+agent\ vs. runtime\SortViewCollector.exe),
    never a user-facing switch, per this integration phase's own design
    preference.

.DESCRIPTION
    This is the release-bundle-facing installer: it resolves every source
    file relative to ITS OWN location (the bundle root -- see $BundleRoot
    below), never a Git repository. It is meant to be run directly from a
    copied-out or downloaded SortViewCollector-<version>\ folder, on a
    machine with no Git, no GitHub access, no repository checkout, and no
    development environment -- for a FROZEN bundle, no Python either.

    0. Detects the bundle kind from its own contents, verifies it is not
       ambiguous (both collector\/agent\ AND runtime\SortViewCollector.exe
       present) or incomplete (neither present), then verifies the
       bundle's actual file set matches MANIFEST.json EXACTLY (every
       listed file present with the correct SHA-256, and no unlisted
       extra file) -- refusing to proceed at all if the bundle is
       corrupted, incompletely copied, or has been accidentally modified.
       This is an INTEGRITY check, not an authenticity/signature one --
       see Test-ReleaseManifest's own comment for the precise distinction.
       Both checks run BEFORE anything under -InstallRoot/-DataRoot is touched.
    1. SOURCE: copies collector\*.py and agent\* (the canonical parser
       runtime) from the bundle. FROZEN: copies the bundle's entire
       runtime\ directory (SortViewCollector.exe + _internal\) directly
       into -InstallRoot (so -InstallRoot\SortViewCollector.exe is the
       Task Scheduler command -- see tools\register-task.ps1).
    2. Creates -DataRoot's subdirectories: config\, data\, logs\ (and
       data\processed\, kept for layout visibility -- see
       install-collector.ps1's own comment). Identical for both kinds.
    3. SOURCE ONLY: creates a fresh Python virtual environment under
       <InstallRoot>\.venv using -PythonExe, and installs the pinned
       dependencies from the bundle's requirements.txt. FROZEN: this step
       is skipped entirely -- no venv is created, pip is never invoked,
       -PythonExe is never used, and a frozen bundle does not even contain
       a requirements.txt to install from.
    4. Writes collector_config.json from the -CustomerId/-BranchId/
       -ApiUrl parameters (or copies the bundle's own
       collector_config.example.json verbatim if none are given, for
       manual editing afterward). NEVER writes a token into this file --
       SORTVIEW_API_TOKEN is handled entirely separately; see TOKEN SETUP
       below. Written UTF-8 with NO byte-order mark (see the BOM comment
       inline below -- a real production finding, identical for both
       bundle kinds).
    5. Prints exact next steps (token, preflight, bootstrap, task
       registration), using the correct invocation form for whichever
       bundle kind was detected -- does NOT register the Scheduled Task,
       bootstrap state, or start anything itself.

    SAFE TO RE-RUN: if -DataRoot\config\collector_config.json OR an
    existing runtime is detected under -InstallRoot (.venv for a source
    install, SortViewCollector.exe for a frozen one) already exist, this
    script refuses to proceed (no partial/silent overwrite of an existing
    install) unless -Force is passed -- and even with -Force,
    state.json/status.json/logs under -DataRoot\data and -DataRoot\logs
    are NEVER touched, only the application runtime (-InstallRoot) and
    config template are replaced. For a real in-place update of an
    already-running install, use tools\update.ps1, which additionally
    preserves a rollback copy, disables the Scheduled Task before
    touching anything, and re-validates before declaring success.

.PARAMETER TOKEN SETUP
    This script does not set SORTVIEW_API_TOKEN. Run tools\set-api-token.ps1
    (in this same bundle) -- a Machine-scope Windows environment variable,
    SecureString prompt, never written to a file or log. Identical for
    both bundle kinds.

.EXAMPLE
    .\install.ps1 `
        -CustomerId 1 -BranchId 1 `
        -ApiUrl "https://sortview-app-2p336.ondigitalocean.app"
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\SortView\Collector",
    [string]$DataRoot = "C:\ProgramData\SortViewCollector",
    [string]$PythonExe = "python",
    [Nullable[int]]$CustomerId,
    [Nullable[int]]$BranchId,
    [string]$ApiUrl = "https://sortview-app-2p336.ondigitalocean.app",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Test-ReleaseManifest {
    <#
    Verifies the bundle's ACTUAL file set matches MANIFEST.json EXACTLY --
    BEFORE any install/update mutation. Checks, all collected into one
    report rather than stopping at the first problem:
      - every manifest-listed path is safe (no ".." traversal segment, not
        rooted/absolute) and not a duplicate of another listed path;
      - every manifest-listed file exists and its SHA-256 matches;
      - the bundle contains NO file absent from the manifest (enumerated
        recursively, MANIFEST.json itself excluded from that comparison --
        it is the list, not a member of the list it describes).

    ACCURACY NOTE (do not overclaim): this detects corruption, an
    incomplete copy, accidental modification, or a file that no longer
    matches the release manifest. It is NOT an authenticated signature --
    MANIFEST.json itself is not signed, so a deliberate tamperer who
    controls the whole bundle could edit a payload file and its manifest
    entry consistently, or add/remove entries to match. Code signing
    (not yet implemented -- see collector/freeze/build_frozen.ps1) is the
    intended future authenticity mechanism against that threat; this
    check's job is integrity against accidental damage and incomplete
    transfer, which is what "copied to a machine with no Git/package-
    manager signature" actually needs day to day.

    Duplicated (not shared via a module) in both install.ps1 and
    update.ps1, matching this codebase's established preference for small
    independent primitives over cross-script coupling for release-bundle
    tooling.
    #>
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

    # A manifest with unsafe or duplicate paths is malformed on its own
    # terms -- stop here rather than also trying to resolve/hash a path
    # that might not even be safely containable under $BundleRoot.
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

# This script lives at the RELEASE BUNDLE ROOT (e.g.
# SortViewCollector-1.0.0\install.ps1) -- everything it needs is a direct
# sibling, already placed here by collector/build_release.py. Never a
# repo-root resolution, never a `python -m collector.deploy_manifest`
# subprocess -- the file set is fixed and already verified at build time.
$BundleRoot = $PSScriptRoot
$SourceCollectorDir = Join-Path $BundleRoot "collector"
$SourceAgentDir = Join-Path $BundleRoot "agent"
$SourceRequirements = Join-Path $BundleRoot "requirements.txt"
$SourceExampleConfig = Join-Path $BundleRoot "collector_config.example.json"
$FrozenRuntimeDir = Join-Path $BundleRoot "runtime"
$FrozenExeSource = Join-Path $FrozenRuntimeDir "SortViewCollector.exe"

if (-not (Test-Path $SourceExampleConfig)) {
    throw "This release bundle is incomplete or damaged -- missing '$SourceExampleConfig'. Re-download/re-copy the bundle rather than editing it by hand."
}

# --- bundle kind auto-detection -- never a user-facing switch ----------
$isSourceBundle = (Test-Path $SourceCollectorDir) -and (Test-Path $SourceAgentDir)
$isFrozenBundle = Test-Path $FrozenExeSource

if ($isSourceBundle -and $isFrozenBundle) {
    throw "This release bundle contains BOTH a source runtime (collector\, agent\) and a frozen " +
          "runtime (runtime\SortViewCollector.exe) -- ambiguous, refusing to guess which one to " +
          "install. This bundle is malformed; rebuild it with either build_release.py's source mode " +
          "or --frozen-runtime, never both into the same output."
}
if (-not $isSourceBundle -and -not $isFrozenBundle) {
    throw "This release bundle is incomplete or damaged -- neither a source runtime (collector\, " +
          "agent\) nor a frozen runtime (runtime\SortViewCollector.exe) was found under '$BundleRoot'. " +
          "Re-download/re-copy the bundle rather than editing it by hand."
}
if ($isSourceBundle -and -not (Test-Path $SourceRequirements)) {
    throw "This release bundle is incomplete or damaged -- source bundle missing requirements.txt."
}

Write-Host "Bundle kind: $(if ($isFrozenBundle) { 'FROZEN (no Python required)' } else { 'SOURCE (Python + venv required)' })" -ForegroundColor Cyan
Test-ReleaseManifest -BundleRoot $BundleRoot

$ConfigPath = Join-Path $DataRoot "config\collector_config.json"
$VenvPath = Join-Path $InstallRoot ".venv"
$FrozenExeInstalled = Join-Path $InstallRoot "SortViewCollector.exe"

$alreadyInstalled = (Test-Path $VenvPath) -or (Test-Path $FrozenExeInstalled) -or (Test-Path $ConfigPath)
if ($alreadyInstalled -and -not $Force) {
    Write-Host "An existing install was detected:" -ForegroundColor Yellow
    if (Test-Path $VenvPath) { Write-Host "  venv:   $VenvPath" }
    if (Test-Path $FrozenExeInstalled) { Write-Host "  frozen runtime: $FrozenExeInstalled" }
    if (Test-Path $ConfigPath) { Write-Host "  config: $ConfigPath" }
    Write-Host ""
    Write-Host "install.ps1 is for FRESH installs only. To update an existing install, use" -ForegroundColor Yellow
    Write-Host "tools\update.ps1 instead -- it preserves config/state/logs and validates the" -ForegroundColor Yellow
    Write-Host "new runtime before declaring success. Pass -Force here only if you specifically" -ForegroundColor Yellow
    Write-Host "intend to replace the runtime/config from scratch (state.json/status.json/logs" -ForegroundColor Yellow
    Write-Host "are still never touched)."
    return
}

if ($Force -and (Test-Path $InstallRoot)) {
    # A FORCED fresh install must never OVERLAY whatever is already under
    # -InstallRoot -- reaching here means either $alreadyInstalled was
    # true and -Force was passed (the normal forced-reinstall case), or
    # -InstallRoot exists without tripping that check (e.g. debris from an
    # interrupted prior install that died before writing .venv/
    # SortViewCollector.exe/config). Either way, something (a prior
    # source install: collector\, agent\, .venv\; a prior frozen install:
    # SortViewCollector.exe, _internal\; or partial debris) may already be
    # sitting there. Removing -InstallRoot entirely before recreating it
    # from this bundle is what actually guarantees: no stale file from an
    # older release can survive, and switching bundle kinds (source ->
    # frozen or frozen -> source) on the same -InstallRoot can never leave
    # a hybrid mix of both. Gated strictly on -Force -- never runs on a
    # plain (non-forced) install, which the $alreadyInstalled check above
    # already protects. -DataRoot is a completely separate path and is
    # never touched by this.
    Write-Host "=== 0. Removing existing InstallRoot before forced reinstall ===" -ForegroundColor Cyan
    Remove-Item -Recurse -Force $InstallRoot
    Write-Host "Removed $InstallRoot"
}

Write-Host "=== 1. Application runtime ===" -ForegroundColor Cyan
if ($isFrozenBundle) {
    # Deterministic: -InstallRoot doesn't exist yet at this point on a
    # fresh install (the already-installed check above already refused
    # otherwise, absent -Force), so a plain copy of the bundle's runtime\
    # contents can never overlay stale files -- there is nothing there yet.
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    Copy-Item (Join-Path $FrozenRuntimeDir "*") -Destination $InstallRoot -Recurse -Force
    $frozenFileCount = (Get-ChildItem -Path $FrozenRuntimeDir -Recurse -File).Count
    Write-Host "Copied frozen runtime ($frozenFileCount file(s)) to $InstallRoot"
} else {
    $TargetCollectorDir = Join-Path $InstallRoot "collector"
    New-Item -ItemType Directory -Path $TargetCollectorDir -Force | Out-Null
    Get-ChildItem -Path $SourceCollectorDir -Filter "*.py" -File | ForEach-Object {
        Copy-Item $_.FullName -Destination $TargetCollectorDir -Force
    }
    Write-Host "Copied collector\*.py to $TargetCollectorDir"

    Write-Host "=== 1b. Canonical parser runtime (agent.parser.*) ===" -ForegroundColor Cyan
    # Copied directly from the bundle's own agent\ folder -- already curated
    # and verified (nothing beyond agent/__init__.py, agent/logger_config.py,
    # agent/parser/*.py) by collector/build_release.py at build time. A
    # trailing \* on the source is required: copying a bare "...\agent" with
    # -Recurse into an EXISTING destination nests the source folder inside it
    # instead of overwriting its contents in place (verified empirically --
    # see update-collector.ps1's own rollback-instructions comment for the
    # same finding).
    $TargetAgentDir = Join-Path $InstallRoot "agent"
    New-Item -ItemType Directory -Path $TargetAgentDir -Force | Out-Null
    Copy-Item (Join-Path $SourceAgentDir "*") -Destination $TargetAgentDir -Recurse -Force
    $parserFileCount = (Get-ChildItem -Path $SourceAgentDir -Filter "*.py" -Recurse -File).Count
    Write-Host "Copied $parserFileCount canonical parser runtime file(s) to $TargetAgentDir"
}

Write-Host "=== 2. Data directories ===" -ForegroundColor Cyan
foreach ($sub in @("config", "data", "data\processed", "logs")) {
    New-Item -ItemType Directory -Path (Join-Path $DataRoot $sub) -Force | Out-Null
}
Write-Host "Created $DataRoot\{config,data,data\processed,logs}"

$RunnerExe = $FrozenExeInstalled
if (-not $isFrozenBundle) {
    Write-Host "=== 3. Python virtual environment ===" -ForegroundColor Cyan
    & $PythonExe -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
    $VenvPython = Join-Path $VenvPath "Scripts\python.exe"
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install -r $SourceRequirements --quiet
    if ($LASTEXITCODE -ne 0) { throw "dependency install failed (exit $LASTEXITCODE)" }
    # Recorded so tools\update.ps1 can tell whether requirements.txt has
    # changed since this install without re-hashing/re-installing blindly.
    # Frozen installs have no dependency-hash concept at all -- every
    # frozen update replaces the whole runtime deterministically instead.
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText((Join-Path $InstallRoot ".deps-hash"), (Get-FileHash $SourceRequirements -Algorithm SHA256).Hash, $utf8NoBom)
    Write-Host "Created venv at $VenvPath and installed pinned dependencies from requirements.txt"
    $RunnerExe = $VenvPython
} else {
    Write-Host "=== 3. Python virtual environment: SKIPPED (frozen bundle -- no Python/pip/venv required) ===" -ForegroundColor Cyan
}

Write-Host "=== 4. Configuration ===" -ForegroundColor Cyan
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
if (Test-Path $ConfigPath -PathType Leaf) {
    Write-Host "Config already exists at $ConfigPath -- left untouched." -ForegroundColor Yellow
} elseif ($CustomerId -and $BranchId) {
    $config = [ordered]@{
        customer_id = $CustomerId
        branch_id   = $BranchId
        api_url     = $ApiUrl
        sources     = @(
            @{ name = "checkins"; path = "C:\TLCFinalDlls\Checkins.txt" }
            @{ name = "rejects"; path = "C:\TLCFinalDlls\Rejects.txt" }
            @{ name = "acs"; path = "C:\TLCFinalDlls\ACS Log.txt" }
        )
        state_path  = (Join-Path $DataRoot "data\state.json")
        status_path = (Join-Path $DataRoot "data\status.json")
        log_path    = (Join-Path $DataRoot "logs\collector.log")
    }
    # Real production finding (unchanged from install-collector.ps1):
    # `Set-Content -Encoding utf8` on Windows PowerShell 5.1 writes a
    # UTF-8 BOM -- collector/config.py reads with strict `encoding="utf-8"`
    # (deliberately not "utf-8-sig"), so a BOM makes json.loads fail
    # immediately. [System.Text.UTF8Encoding($false)] writes UTF-8 with NO
    # BOM. Identical for both bundle kinds.
    $jsonText = $config | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($ConfigPath, $jsonText, $utf8NoBom)
    Write-Host "Wrote $ConfigPath from -CustomerId/-BranchId/-ApiUrl. Review the 'sources' paths -- " -ForegroundColor Green
    Write-Host "they default to the standard Tech Logic locations and may need editing per-site."
} else {
    Copy-Item $SourceExampleConfig -Destination $ConfigPath -Force
    Write-Host "No -CustomerId/-BranchId given -- copied the example template to $ConfigPath." -ForegroundColor Yellow
    Write-Host "Edit it by hand before continuing (customer_id, branch_id, source paths)." -ForegroundColor Yellow
}
Write-Host "Config never contains the API token -- see TOKEN SETUP below."

Write-Host ""
Write-Host "=== Install complete. Next steps: ===" -ForegroundColor Green
Write-Host "1. Review/edit $ConfigPath if it was copied from the template."
Write-Host "2. Set the API token (Machine-scope env var, not stored in any file):"
Write-Host "     $(Join-Path $BundleRoot 'tools\set-api-token.ps1')"
Write-Host "3. Run preflight interactively, then as SYSTEM:"
if ($isFrozenBundle) {
    Write-Host "     `"$RunnerExe`" preflight --config `"$ConfigPath`""
} else {
    Write-Host "     $RunnerExe -m collector.preflight --config `"$ConfigPath`""
}
Write-Host "     $(Join-Path $BundleRoot 'tools\preflight-system.ps1') -ConfigPath `"$ConfigPath`" -InstallRoot `"$InstallRoot`""
Write-Host "4. Bootstrap the starting cursor BEFORE the first run -- REQUIRED, not optional:" -ForegroundColor Yellow
Write-Host "   if the source files already contain historical data (the normal case on a" -ForegroundColor Yellow
Write-Host "   real Tech Logic machine) and this step is skipped, the first run will replay" -ForegroundColor Yellow
Write-Host "   and upload ALL of it:" -ForegroundColor Yellow
if ($isFrozenBundle) {
    Write-Host "     `"$RunnerExe`" bootstrap --config `"$ConfigPath`""
} else {
    Write-Host "     $RunnerExe -m collector.bootstrap_state --config `"$ConfigPath`""
}
Write-Host "5. Register the Scheduled Task (registers DISABLED by default -- see its own"
Write-Host "   printed output for the register/enable/start distinction):"
Write-Host "     $(Join-Path $BundleRoot 'tools\register-task.ps1') -InstallRoot `"$InstallRoot`" -ConfigPath `"$ConfigPath`""
Write-Host "6. Inspect: confirm both preflight checks passed, bootstrap completed" -ForegroundColor Yellow
Write-Host "   successfully, and state.json now has an entry for EVERY configured source at" -ForegroundColor Yellow
Write-Host "   its safe current cursor -- offset 0 is a VALID seed for an empty/new source" -ForegroundColor Yellow
Write-Host "   file, not a sign of failure. Confirm before proceeding to step 7." -ForegroundColor Yellow
Write-Host "7. Only once satisfied: Enable-ScheduledTask -TaskName 'SortView Collector'"
Write-Host "8. Optionally trigger one run immediately (Start-ScheduledTask), or simply wait" -ForegroundColor Yellow
Write-Host "   for the next 15-minute trigger -- both are safe once enabled." -ForegroundColor Yellow
Write-Host ""
Write-Host "NOTE: the production Tech Logic parser (checkins/rejects/acs) is wired in -- once" -ForegroundColor Yellow
Write-Host "the task is registered, bootstrapped, and ENABLED (step 7), an ordinary run parses" -ForegroundColor Yellow
Write-Host "and uploads real data for those three sources. The fail-closed gate (exit code 2," -ForegroundColor Yellow
Write-Host "'no parser configured') still applies, but only as a safety net for a source name" -ForegroundColor Yellow
Write-Host "outside those three (e.g. a config typo, or a not-yet-supported source)." -ForegroundColor Yellow
