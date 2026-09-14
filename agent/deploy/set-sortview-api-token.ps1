<#
.SYNOPSIS
    Sets SORTVIEW_API_TOKEN as a MACHINE-level Windows environment variable,
    without ever writing the token to a file, a log, Git, or the console.

.DESCRIPTION
    The canonical SortView agent (agent/runtime/config.py) reads its API
    token ONLY from the SORTVIEW_API_TOKEN environment variable -- never
    from a config file, never from a command-line argument. A machine
    (System) environment variable is visible to every process that starts
    AFTER it is set, on this machine, regardless of which user account
    or logon type started it -- including a Scheduled Task run as SYSTEM
    or a dedicated service account "whether the user is logged on or
    not". This is what makes it usable by an unattended process without
    requiring an interactive login to type it in.

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

    A newly-started process picks up a Machine env var change
    immediately. An ALREADY-RUNNING process (including an already-running
    canonical agent, or an already-open PowerShell/cmd window) does NOT
    -- it must be restarted to see the new value. If you're rotating an
    existing production token, restart the canonical agent's Scheduled
    Task afterward (see unregister-sortview-task.ps1 / Task Scheduler)
    for the new value to take effect.

.PARAMETER VariableName
    Defaults to SORTVIEW_API_TOKEN. Override only if a second, distinctly
    named agent installation on this same machine needs its own token
    variable (not the normal case for a single-branch NBPL install).

.EXAMPLE
    # Run from an elevated PowerShell prompt:
    .\set-sortview-api-token.ps1
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

$secure = Read-Host -AsSecureString -Prompt "Paste the SortView agent API token (input hidden)"
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

# Confirmation only -- a hash PREFIX, never the token, never the full
# hash (which, combined with a database compromise of agent_tokens'
# token_hash column, would be a match, not just a sanity check).
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
Write-Host "This takes effect for NEWLY STARTED processes only." -ForegroundColor Yellow
Write-Host "Restart the canonical agent's Scheduled Task (or reboot) for it to see this value."
