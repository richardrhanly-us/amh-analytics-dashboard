<#
.SYNOPSIS
    Installs the SortView Collector v1 runtime -- FRESH installs only.
    For updating an existing install, use update-collector.ps1 instead.

.DESCRIPTION
    1. Creates the install root (-InstallRoot) and copies collector/*.py
       PLUS the narrow canonical-parser runtime slice of agent/ that
       collector/parsers.py actually imports (agent/__init__.py,
       agent/logger_config.py, agent/parser/*.py -- see
       collector/deploy_manifest.py, the single source of truth for
       exactly this list). Never tests/, never this deploy/ folder
       itself, never any other part of agent/ (no agent/runtime/*, no
       agent/main.py, no agent/config.py, no legacy agent/run_pipeline.py
       mirror, no archived AMH snapshot), never anything else from the
       source checkout -- a deliberately narrow copy, not a mirror of
       the whole repository or of agent/ itself.
    2. Creates -DataRoot's subdirectories: config\, data\, logs\.
       data\processed\ is created here too even though nothing in the
       current design writes to it -- collector/parsers.py maps parsed
       rows straight into upload payloads in memory, it never writes a
       cleaned CSV the way the legacy pipeline did -- kept only so the
       layout is visible up front; harmless if it stays empty.
    3. Creates a fresh Python virtual environment under
       <InstallRoot>\.venv using -PythonExe, and installs the pinned
       dependencies from collector/deploy/requirements.txt (NOT this
       repository's own requirements.txt, which is for the unrelated
       Streamlit/FastAPI backend and must never end up in this venv --
       see that file's own comment for why that specifically matters).
    4. Writes collector_config.json from the -CustomerId/-BranchId/
       -ApiUrl/-SourcePaths parameters (or copies the example template
       verbatim if none are given, for manual editing afterward). NEVER
       writes a token into this file -- SORTVIEW_API_TOKEN is handled
       entirely separately; see TOKEN SETUP below.
    5. Prints exact next steps (token, preflight, task registration) --
       does NOT register the Scheduled Task or start anything itself.

    SAFE TO RE-RUN: if -DataRoot\config\collector_config.json OR
    -InstallRoot\.venv already exist, this script refuses to proceed
    (no partial/silent overwrite of an existing install) unless -Force
    is passed -- and even with -Force, state.json/status.json/logs under
    -DataRoot\data and -DataRoot\logs are NEVER touched, only the
    application runtime (-InstallRoot) and config template are replaced.
    For a real in-place update of an already-running install, use
    update-collector.ps1, which additionally preserves a rollback copy
    and re-validates before declaring success.

.PARAMETER TOKEN SETUP
    This script does not set SORTVIEW_API_TOKEN. That is handled by
    agent/deploy/set-sortview-api-token.ps1 -- reused AS-IS (unmodified)
    from the continuous-agent deployment tooling, since the mechanism
    (a Machine-scope Windows environment variable, SecureString prompt,
    never written to a file or log) is entirely generic and not specific
    to which process reads it. There is no collector-specific fork of
    that script, deliberately, to avoid two copies of the same logic
    drifting apart over time.

.EXAMPLE
    .\install-collector.ps1 `
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

# This script lives at <repo>\collector\deploy\install-collector.ps1 --
# the repo root is two levels up. Used only to locate the SOURCE files to
# copy; never written to.
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$SourceCollectorDir = Join-Path $RepoRoot "collector"
$SourceRequirements = Join-Path $RepoRoot "collector\deploy\requirements.txt"
$SourceExampleConfig = Join-Path $RepoRoot "collector\deploy\collector_config.example.json"

if (-not (Test-Path $SourceCollectorDir)) {
    throw "Could not find the collector\ source directory at '$SourceCollectorDir' -- run this script from a real checkout, not a copied-out deploy\ folder."
}

$ConfigPath = Join-Path $DataRoot "config\collector_config.json"
$VenvPath = Join-Path $InstallRoot ".venv"

$alreadyInstalled = (Test-Path $VenvPath) -or (Test-Path $ConfigPath)
if ($alreadyInstalled -and -not $Force) {
    Write-Host "An existing install was detected:" -ForegroundColor Yellow
    if (Test-Path $VenvPath) { Write-Host "  venv:   $VenvPath" }
    if (Test-Path $ConfigPath) { Write-Host "  config: $ConfigPath" }
    Write-Host ""
    Write-Host "install-collector.ps1 is for FRESH installs only. To update an existing" -ForegroundColor Yellow
    Write-Host "install, use update-collector.ps1 instead -- it preserves config/state/logs" -ForegroundColor Yellow
    Write-Host "and validates the new runtime before declaring success. Pass -Force here" -ForegroundColor Yellow
    Write-Host "only if you specifically intend to replace the runtime/config from scratch" -ForegroundColor Yellow
    Write-Host "(state.json/status.json/logs are still never touched)."
    return
}

Write-Host "=== 1. Application runtime ===" -ForegroundColor Cyan
$TargetCollectorDir = Join-Path $InstallRoot "collector"
New-Item -ItemType Directory -Path $TargetCollectorDir -Force | Out-Null
# Copy ONLY the collector package's own .py files -- never __pycache__,
# never deploy\ (these scripts aren't needed at runtime), never any other
# part of the repository.
Get-ChildItem -Path $SourceCollectorDir -Filter "*.py" -File | ForEach-Object {
    Copy-Item $_.FullName -Destination $TargetCollectorDir -Force
}
Write-Host "Copied collector\*.py to $TargetCollectorDir"

Write-Host "=== 1b. Canonical parser runtime (agent.parser.*) ===" -ForegroundColor Cyan
# collector/parsers.py imports agent.parser.{checkins,rejects,acs} (and,
# transitively, agent/logger_config.py) -- collector/deploy_manifest.py
# is the single, unit-tested source of truth for exactly which files that
# requires, so this list can never silently drift from what
# collector/parsers.py actually imports. Deliberately NOT all of agent/
# -- see that module's own docstring for the full excluded list and why.
# Run via the bare -PythonExe, not a venv python -- the venv doesn't
# exist yet at this point in the script.
Push-Location $RepoRoot
try {
    $parserRuntimeFiles = & $PythonExe -m collector.deploy_manifest
    if ($LASTEXITCODE -ne 0) { throw "collector.deploy_manifest failed to list required parser runtime files (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}
foreach ($relativePath in $parserRuntimeFiles) {
    $sourceFile = Join-Path $RepoRoot $relativePath
    $destFile = Join-Path $InstallRoot $relativePath
    New-Item -ItemType Directory -Path (Split-Path $destFile -Parent) -Force | Out-Null
    Copy-Item $sourceFile -Destination $destFile -Force
}
Write-Host "Copied $($parserRuntimeFiles.Count) canonical parser runtime file(s) to $InstallRoot\agent"

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
# Recorded so update-collector.ps1 can tell whether requirements.txt has
# changed since this install without re-hashing/re-installing blindly.
(Get-FileHash $SourceRequirements -Algorithm SHA256).Hash | Set-Content -Path (Join-Path $InstallRoot ".deps-hash") -Encoding utf8
Write-Host "Created venv at $VenvPath and installed pinned dependencies from collector\deploy\requirements.txt"

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
    $config | ConvertTo-Json -Depth 5 | Set-Content -Path $ConfigPath -Encoding utf8
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
Write-Host "     <repo>\agent\deploy\set-sortview-api-token.ps1"
Write-Host "   (reused as-is from the continuous-agent tooling -- same mechanism, same env var name)"
Write-Host "3. Run preflight interactively, then as SYSTEM:"
Write-Host "     $VenvPython -m collector.preflight --config `"$ConfigPath`""
Write-Host "     .\run-preflight-as-system.ps1 -ConfigPath `"$ConfigPath`" -InstallRoot `"$InstallRoot`""
Write-Host "4. Register the Scheduled Task:"
Write-Host "     .\register-collector-task.ps1 -InstallRoot `"$InstallRoot`" -ConfigPath `"$ConfigPath`""
Write-Host ""
Write-Host "NOTE: the production Tech Logic parser (checkins/rejects/acs) is wired in -- once" -ForegroundColor Yellow
Write-Host "the task is registered and started, an ordinary run parses and uploads real data" -ForegroundColor Yellow
Write-Host "for those three sources. The fail-closed gate (exit code 2, 'no parser configured')" -ForegroundColor Yellow
Write-Host "still applies, but only as a safety net for a source name outside those three (e.g." -ForegroundColor Yellow
Write-Host "a config typo, or a not-yet-supported source) -- it is not expected in normal use." -ForegroundColor Yellow
