<#
.SYNOPSIS
    Read-only: does the currently INSTALLED collector actually match the
    release it was installed/updated from?

.DESCRIPTION
    Recomputes the SHA-256 of every file MANIFEST.installed.json records
    as having been placed at -InstallRoot by install.ps1/tools\update.ps1
    and compares against the hash recorded there (itself copied from the
    release bundle's own MANIFEST.json at install/update time -- see
    tools\CollectorManifest.ps1's New-InstalledManifest for exactly which
    bundle-relative paths become which installed-relative paths, and why
    a raw MANIFEST.json comparison against the live install would be
    wrong for both a frozen bundle's "runtime\" prefix and a source
    bundle's bundle-only tooling files).

    Reports, per file: PASS, MISSING, or HASH MISMATCH; also reports an
    UNEXPECTED FILE for anything present in a release-managed part of
    -InstallRoot (collector\ non-recursively, agent\ recursively for a
    source install; the whole tree for a frozen install) that
    MANIFEST.installed.json does not know about. Never touches
    -InstallRoot -DataRoot, config, state, logs or the v2 secret -- this
    only reads and hashes.

    Exit code 0 = every recorded file matched and nothing unexpected was
    found. Exit code 1 = at least one problem (or the manifest itself is
    missing/malformed) -- MANIFEST MISSING most commonly means this
    install predates release-integrity persistence (installed before this
    tooling existed); that is reported, not silently treated as success.

.EXAMPLE
    .\verify-install.ps1 -InstallRoot "C:\SortView\Collector"
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\SortView\Collector"
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "CollectorManifest.ps1")

if (-not (Test-Path -LiteralPath $InstallRoot)) {
    Write-Host "VERIFY FAILED: '$InstallRoot' does not exist." -ForegroundColor Red
    exit 1
}

try {
    Test-InstalledManifest -InstallRoot $InstallRoot
    Write-Host ""
    Write-Host "VERIFIED: $InstallRoot matches its recorded release manifest." -ForegroundColor Green
    exit 0
} catch {
    Write-Host ""
    Write-Host "VERIFY FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
