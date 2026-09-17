<#
.SYNOPSIS
    Installs the SortView Collector v1 runtime from a standalone RELEASE
    BUNDLE -- FRESH installs only. For updating an existing install, use
    tools\update.ps1 (in the same bundle) instead.

.DESCRIPTION
    This is the release-bundle-facing installer: it resolves every source
    file relative to ITS OWN location (the bundle root -- see $BundleRoot
    below), never a Git repository. It is meant to be run directly from a
    copied-out or downloaded SortViewCollector-<version>\ folder, on a
    machine with no Git, no GitHub access, no repository checkout, and no
    development environment -- only Python (see -PythonExe) is required.
    See collector/build_release.py for how this bundle is produced, and
    collector/deploy/install-collector.ps1 for the repo-checkout-based
    equivalent used for developer/QA installs (a different script,
    deliberately not reused here -- see that script and
    collector/build_release.py's module docstring for why).

    1. Copies collector\*.py and agent\* (the canonical parser runtime)
       from THIS BUNDLE -- both were already curated and verified at build
       time (see MANIFEST.json alongside this script) -- never by shelling
       out to Python or resolving a repository. Never tests, never this
       bundle's own tools\ folder, nothing beyond exactly what the bundle
       already contains.
    2. Creates -DataRoot's subdirectories: config\, data\, logs\ (and
       data\processed\, kept for layout visibility even though nothing
       currently writes to it -- see install-collector.ps1's own comment).
    3. Creates a fresh Python virtual environment under
       <InstallRoot>\.venv using -PythonExe, and installs the pinned
       dependencies from THIS BUNDLE's requirements.txt.
    4. Writes collector_config.json from the -CustomerId/-BranchId/
       -ApiUrl parameters (or copies the bundle's own
       collector_config.example.json verbatim if none are given, for
       manual editing afterward). NEVER writes a token into this file --
       SORTVIEW_API_TOKEN is handled entirely separately; see TOKEN SETUP
       below. Written UTF-8 with NO byte-order mark (see the BOM comment
       inline below -- a real production finding, unchanged from
       install-collector.ps1).
    5. Prints exact next steps (token, preflight, bootstrap, task
       registration) -- does NOT register the Scheduled Task, bootstrap
       state, or start anything itself.

    SAFE TO RE-RUN: if -DataRoot\config\collector_config.json OR
    -InstallRoot\.venv already exist, this script refuses to proceed (no
    partial/silent overwrite of an existing install) unless -Force is
    passed -- and even with -Force, state.json/status.json/logs under
    -DataRoot\data and -DataRoot\logs are NEVER touched, only the
    application runtime (-InstallRoot) and config template are replaced.
    For a real in-place update of an already-running install, use
    tools\update.ps1, which additionally preserves a rollback copy,
    disables the Scheduled Task before touching anything, and
    re-validates before declaring success.

.PARAMETER TOKEN SETUP
    This script does not set SORTVIEW_API_TOKEN. Run tools\set-api-token.ps1
    (in this same bundle) -- a Machine-scope Windows environment variable,
    SecureString prompt, never written to a file or log.

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

foreach ($required in @($SourceCollectorDir, $SourceAgentDir, $SourceRequirements, $SourceExampleConfig)) {
    if (-not (Test-Path $required)) {
        throw "This release bundle is incomplete or damaged -- missing '$required'. Re-download/re-copy the bundle rather than editing it by hand."
    }
}

$ConfigPath = Join-Path $DataRoot "config\collector_config.json"
$VenvPath = Join-Path $InstallRoot ".venv"

$alreadyInstalled = (Test-Path $VenvPath) -or (Test-Path $ConfigPath)
if ($alreadyInstalled -and -not $Force) {
    Write-Host "An existing install was detected:" -ForegroundColor Yellow
    if (Test-Path $VenvPath) { Write-Host "  venv:   $VenvPath" }
    if (Test-Path $ConfigPath) { Write-Host "  config: $ConfigPath" }
    Write-Host ""
    Write-Host "install.ps1 is for FRESH installs only. To update an existing install, use" -ForegroundColor Yellow
    Write-Host "tools\update.ps1 instead -- it preserves config/state/logs and validates the" -ForegroundColor Yellow
    Write-Host "new runtime before declaring success. Pass -Force here only if you specifically" -ForegroundColor Yellow
    Write-Host "intend to replace the runtime/config from scratch (state.json/status.json/logs" -ForegroundColor Yellow
    Write-Host "are still never touched)."
    return
}

Write-Host "=== 1. Application runtime ===" -ForegroundColor Cyan
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

Write-Host "=== 2. Data directories ===" -ForegroundColor Cyan
foreach ($sub in @("config", "data", "data\processed", "logs")) {
    New-Item -ItemType Directory -Path (Join-Path $DataRoot $sub) -Force | Out-Null
}
Write-Host "Created $DataRoot\{config,data,data\processed,logs}"

Write-Host "=== 3. Python virtual environment ===" -ForegroundColor Cyan
& $PythonExe -m venv $VenvPath
if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip --quiet
& $VenvPython -m pip install -r $SourceRequirements --quiet
if ($LASTEXITCODE -ne 0) { throw "dependency install failed (exit $LASTEXITCODE)" }
# Recorded so tools\update.ps1 can tell whether requirements.txt has
# changed since this install without re-hashing/re-installing blindly.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $InstallRoot ".deps-hash"), (Get-FileHash $SourceRequirements -Algorithm SHA256).Hash, $utf8NoBom)
Write-Host "Created venv at $VenvPath and installed pinned dependencies from requirements.txt"

Write-Host "=== 4. Configuration ===" -ForegroundColor Cyan
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
    # BOM.
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
Write-Host "     $VenvPython -m collector.preflight --config `"$ConfigPath`""
Write-Host "     $(Join-Path $BundleRoot 'tools\preflight-system.ps1') -ConfigPath `"$ConfigPath`" -InstallRoot `"$InstallRoot`""
Write-Host "4. Bootstrap the starting cursor BEFORE the first run -- REQUIRED, not optional:" -ForegroundColor Yellow
Write-Host "   if the source files already contain historical data (the normal case on a" -ForegroundColor Yellow
Write-Host "   real Tech Logic machine) and this step is skipped, the first run will replay" -ForegroundColor Yellow
Write-Host "   and upload ALL of it:" -ForegroundColor Yellow
Write-Host "     $VenvPython -m collector.bootstrap_state --config `"$ConfigPath`""
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
