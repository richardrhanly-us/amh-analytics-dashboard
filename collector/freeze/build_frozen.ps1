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
        SortViewCollector.exe version

    RELEASE READINESS (before anything is built): scripts\check_release_readiness.py
    must pass -- collector.__version__ is still the only place the version is
    written, the packaging tooling derives from it, and no current-state test
    carries a stale release literal or depends on today's date.

    VERSION CHECK (fails the build on any drift): collector.__version__ is
    the single authoritative Collector version. After PyInstaller succeeds
    and the executable exists, this script runs `SortViewCollector.exe
    version` and requires exit code 0 and output exactly equal to
    collector.__version__ as imported by the packaging Python from THIS
    repository (isolated mode, this repo's root first on sys.path, and the
    imported package's location verified -- an installed `collector`
    package elsewhere can never supply the expected value). A mismatch
    throws with both the expected and the actual version and never
    continues. This script never rewrites the source version.

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

function Get-SourceCollectorVersion {
    # The expected version: collector.__version__ imported by a normal Python
    # import, not parsed out of the file. -I (isolated) ignores PYTHONPATH,
    # the user site and the current directory, and this repository's root is
    # then put FIRST on sys.path explicitly -- and the imported package's
    # location is checked -- so it can only ever be THIS repository's
    # Collector. Only single quotes inside the code: it is passed as one
    # command-line argument.
    param(
        [Parameter(Mandatory)][string]$PythonExe,
        [Parameter(Mandatory)][string]$RepoRoot
    )

    $code = "import sys, pathlib; repo = pathlib.Path(sys.argv[1]).resolve(); sys.path.insert(0, str(repo)); import collector; loc = pathlib.Path(collector.__file__).resolve().parent.parent; loc == repo or sys.exit('collector was imported from ' + str(loc) + ', not from ' + str(repo)); print(collector.__version__)"
    $output = @(& $PythonExe -I -c $code $RepoRoot)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read collector.__version__ from '$RepoRoot' using '$PythonExe' (exit $LASTEXITCODE)."
    }
    $version = (($output | ForEach-Object { [string]$_ }) -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($version) -or $version -match '\s') {
        throw "collector.__version__ read from '$RepoRoot' is blank or malformed: '$version'"
    }
    return $version
}

function Assert-FrozenRuntimeVersion {
    # Pure check of what `SortViewCollector.exe version` did -- throws unless
    # it exited 0 and printed exactly the expected version.
    param(
        [Parameter(Mandatory)][string]$ExpectedVersion,
        [AllowNull()]$ActualOutput,
        [Parameter(Mandatory)][int]$ExitCode
    )

    if ([string]::IsNullOrWhiteSpace($ExpectedVersion)) {
        throw "VERSION CHECK FAILED: the expected collector.__version__ is blank."
    }
    if ($ExitCode -ne 0) {
        throw "VERSION CHECK FAILED: the built executable's 'version' command exited $ExitCode (expected 0). Expected version: '$ExpectedVersion'."
    }
    $actual = (@($ActualOutput | ForEach-Object { [string]$_ }) -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($actual)) {
        throw "VERSION CHECK FAILED: the built executable's 'version' command printed nothing. Expected version: '$ExpectedVersion'."
    }
    if ($actual -match '\s') {
        throw "VERSION CHECK FAILED: the built executable's 'version' output is malformed (expected exactly a version). Expected: '$ExpectedVersion'. Actual output: '$actual'."
    }
    if ($actual -cne $ExpectedVersion) {
        throw "VERSION MISMATCH: expected '$ExpectedVersion' (collector.__version__ in this repository) but the built executable reports '$actual'. Refusing to continue with a mismatched runtime."
    }
}

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

# --- release readiness: BEFORE anything is built ---------------------------
# collector.__version__ is the single version authority; this proves the tooling still derives from it and that
# no current-state test carries a stale release literal or depends on today's date -- and fails fast, before the
# ~25s PyInstaller build, if not. (collector/build_release.py runs the same check before packaging.)
$ReadinessScript = Join-Path $RepoRoot "scripts\check_release_readiness.py"
if (-not (Test-Path $ReadinessScript)) {
    throw "Release readiness check not found at '$ReadinessScript'."
}
Write-Host "Checking release readiness ..." -ForegroundColor Cyan
& $VenvPython $ReadinessScript --repo-root $RepoRoot
if ($LASTEXITCODE -ne 0) { throw "Release readiness check failed (exit $LASTEXITCODE): fix what it lists, then build again." }

Write-Host "Building frozen runtime with $VenvPython ..." -ForegroundColor Cyan
& $VenvPython -m PyInstaller $SpecPath --distpath $DistPath --workpath $WorkPath --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed (exit $LASTEXITCODE)" }

$ExePath = Join-Path $DistPath "SortViewCollector\SortViewCollector.exe"
if (-not (Test-Path $ExePath)) { throw "Build reported success but $ExePath was not found." }

# --- version check: the executable must report collector.__version__ ------
$ExpectedVersion = Get-SourceCollectorVersion -PythonExe $VenvPython -RepoRoot $RepoRoot
Write-Host "Checking the built executable's version against collector.__version__ '$ExpectedVersion' ..." -ForegroundColor Cyan
$ActualOutput = @(& $ExePath version)
$VersionExit = $LASTEXITCODE
Assert-FrozenRuntimeVersion -ExpectedVersion $ExpectedVersion -ActualOutput $ActualOutput -ExitCode $VersionExit

Write-Host ""
Write-Host "Built frozen runtime: $ExePath (version $ExpectedVersion, verified)" -ForegroundColor Green
