<#
.SYNOPSIS
    Sets SORTVIEW_API_TOKEN as a MACHINE-level Windows environment variable
    for the SortView Collector, without ever writing the token to a file,
    a log, Git, or the console.

.DESCRIPTION
    collector/config.py reads its API token ONLY from the SORTVIEW_API_TOKEN
    environment variable -- never from collector_config.json, never from a
    command-line argument. A Machine-scope environment variable is visible
    to every process that starts AFTER it is set, on this machine,
    regardless of which account or logon type started it -- including the
    "SortView Collector" Scheduled Task running as SYSTEM. This is what
    lets the Collector's unattended, one-shot, every-15-minutes runs read
    it without any interactive login.

    Same mechanism as the continuous-agent deployment tooling's own
    set-sortview-api-token.ps1 (same env var name, same SecureString/
    Machine-scope approach) -- reimplemented here, rather than reused
    directly from the repository, so a standalone Collector release bundle
    never needs Git/repo access to set its own token. See
    collector/build_release.py's module docstring for why this script (not
    a fork of the repo path) is what actually ships.

    This script:
      1. Prompts for the token as a SecureString (never echoed to the
         console, never appears in PowerShell transcript/history as
         plaintext).
      2. Sets it as a Machine-scope environment variable via
         [Environment]::SetEnvironmentVariable(..., 'Machine').
      3. Immediately clears the plaintext copy from memory.
      4. Prints only a short confirmation (length + a truncated SHA-256
         hash prefix) so the operator can sanity-check the value was
         accepted -- never the token itself, and never anything an
         attacker could reverse into the token.

    Requires an elevated (Administrator) PowerShell session -- setting a
    Machine-scope environment variable always does.

    A newly-started process picks up a Machine env var change immediately.
    Since the Collector is a one-shot process (Task Scheduler starts a
    fresh python.exe every run, it never stays resident), its VERY NEXT
    scheduled or manual run already sees the new value -- there is no
    long-running service to restart, unlike the continuous agent.

.PARAMETER VariableName
    Defaults to SORTVIEW_API_TOKEN. Override only if a second, distinctly
    named Collector installation on this same machine needs its own token
    variable (not the normal single-branch case).

.EXAMPLE
    # Run from an elevated PowerShell prompt:
    .\set-api-token.ps1
    # (paste/type the token when prompted -- it will not be displayed)
#>

[CmdletBinding()]
param(
    [string]$VariableName = "SORTVIEW_API_TOKEN"
)

$ErrorActionPreference = "Stop"

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell session -- setting a Machine-scope environment variable requires it."
}

$secure = Read-Host -AsSecureString -Prompt "Paste the SortView Collector API token (input hidden)"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if ([string]::IsNullOrWhiteSpace($plainToken)) {
    throw "No token was entered -- nothing was set."
}

[Environment]::SetEnvironmentVariable($VariableName, $plainToken, "Machine")

# Confirmation only -- a hash PREFIX, never the token, never the full hash.
$sha256 = [Security.Cryptography.SHA256]::Create()
$hashBytes = $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($plainToken))
$hashPrefix = [BitConverter]::ToString($hashBytes).Replace("-", "").Substring(0, 8).ToLower()

$tokenLength = $plainToken.Length
$plainToken = $null
[GC]::Collect()

Write-Host ""
Write-Host "Set $VariableName as a Machine environment variable." -ForegroundColor Green
Write-Host "  length=$tokenLength  sha256_prefix=$hashPrefix..." -ForegroundColor DarkGray
Write-Host ""
Write-Host "This takes effect for NEWLY STARTED processes only -- the Collector's next" -ForegroundColor Yellow
Write-Host "scheduled or manual run (it is a one-shot process, not a long-running service)" -ForegroundColor Yellow
Write-Host "will see this value automatically."
