<#
.SYNOPSIS
    Builds the frozen (PyInstaller onedir) SortView Collector runtime
    proof. PACKAGING-ONLY tooling -- never installed on, or run as part
    of, a real production Collector install.

.DESCRIPTION
    Invokes PyInstaller from the ISOLATED .pyinstaller-venv (built from
    collector/deploy/requirements.txt PLUS PyInstaller as a build-only
    dependency -- never the normal project .venv, which carries different,
    development-only package versions). Refuses to run if that venv is
    missing, rather than silently falling back to any other Python found
    on PATH.

    Produces dist\SortViewCollector\SortViewCollector.exe (onedir --
    SortViewCollector.exe plus its _internal\ payload), covering all four
    CLI surfaces via subcommand (see dispatcher.py):

        SortViewCollector.exe run --config <path>
        SortViewCollector.exe preflight --config <path>
        SortViewCollector.exe bootstrap --config <path>
        SortViewCollector.exe support-info --config <path>

.EXAMPLE
    .\collector\freeze\build_frozen.ps1
#>

[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")),
    [string]$PackagingVenv = (Join-Path $RepoRoot ".pyinstaller-venv"),
    [string]$DistPath = (Join-Path $RepoRoot "dist"),
    [string]$WorkPath = (Join-Path $RepoRoot "build")
)

$ErrorActionPreference = "Stop"

$VenvPython = Join-Path $PackagingVenv "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Isolated packaging venv not found at '$PackagingVenv'. Create it first, e.g.:`n" +
          "  python -m venv `"$PackagingVenv`"`n" +
          "  `"$VenvPython`" -m pip install -r `"$RepoRoot\collector\deploy\requirements.txt`" pyinstaller`n" +
          "Never build the frozen runtime from the normal project .venv -- its package versions do not " +
          "represent the pinned Collector release requirements."
}

$SpecPath = Join-Path $PSScriptRoot "sortview_collector.spec"
if (-not (Test-Path $SpecPath)) {
    throw "Spec file not found at '$SpecPath'."
}

Write-Host "Building frozen runtime with $VenvPython ..." -ForegroundColor Cyan
& $VenvPython -m PyInstaller $SpecPath --distpath $DistPath --workpath $WorkPath --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed (exit $LASTEXITCODE)" }

$ExePath = Join-Path $DistPath "SortViewCollector\SortViewCollector.exe"
if (-not (Test-Path $ExePath)) { throw "Build reported success but $ExePath was not found." }

Write-Host ""
Write-Host "Built frozen runtime: $ExePath" -ForegroundColor Green
