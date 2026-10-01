<#
.SYNOPSIS
    Stores the SortView Collector's API token in the DPAPI-protected
    <DataRoot>\secrets\api_token.dpapi, by handing it to the Collector's own
    `api-token set` command on STDIN -- never on a command line, never in a
    plain file, a log, Git, or the console.

.DESCRIPTION
    The Collector reads its API token ONLY from api_token.dpapi (DPAPI machine
    scope, bound to the config's customer_id/branch_id, in the
    Administrators+SYSTEM-only secrets folder -- see
    collector/api_token_store.py). This script owns NO file format and no
    cryptography: it only obtains the token and pipes it to

        SortViewCollector.exe api-token set --config <ConfigPath>

    (or, for a source install, `python -m collector.api_token_store set ...`),
    which writes it atomically -- replacing any token already stored, which is
    how a token is rotated -- then verifies the folder's ACL.

    It neither sets nor reads the legacy Machine-scope SORTVIEW_API_TOKEN
    environment variable (readable by every local user), which the
    Collector ignores entirely. It never removes one either: cleaning up a
    leftover variable is a separate, explicit operator step
    (docs/deployment.md).

    This script:
      1. Prompts for the token as a SecureString (never echoed to the
         console, never in PowerShell transcript/history as plaintext) --
         or takes -Token, a SecureString, for automation.
      2. Converts it to text once, pipes it to the Collector's STDIN, and
         clears the plain copy immediately.
      3. Prints only the Collector's fixed confirmation line -- never the
         token, its length, or any hash of it.
      4. Throws if the Collector did not store it, so a caller can never
         mistake a failure for success.

    Requires an elevated (Administrator) PowerShell session: the secrets
    folder is Administrators + SYSTEM only.

    TENANT: normally taken from -ConfigPath. Guided setup stores the token
    BEFORE install.ps1 has written the config, so it passes -CustomerId,
    -BranchId and -DataRoot instead (the same three values install.ps1 then
    writes -- the file lands exactly where that config will look), together
    with -ExePath pointing at the bundle's own runtime.

    AUTOMATED USE (guided setup): collector/deploy/setup-collector.ps1
    (shipped as setup.ps1) calls this script in-process with -Token, the
    permanent token issued by one-time enrollment. -Token is a SecureString,
    not a string, so a plain-text token cannot be passed by accident, and it
    is never part of any process command line.

.PARAMETER InstallRoot
    The installed runtime (SortViewCollector.exe for a frozen install, or
    .venv\Scripts\python.exe for a source install).
.PARAMETER ConfigPath
    The installed collector_config.json (the tenant and the token path).
.PARAMETER ExePath
    Optional: a specific SortViewCollector.exe (guided setup: the bundle's own).
.PARAMETER CustomerId
    With -BranchId and -DataRoot: store the token before a config exists.
.PARAMETER BranchId
    See -CustomerId.
.PARAMETER DataRoot
    See -CustomerId.
.PARAMETER Token
    Optional, for automation only (see AUTOMATED USE). When omitted, the
    token is read from a hidden prompt.

.EXAMPLE
    # Run from an elevated PowerShell prompt (the token is typed/pasted, never shown):
    .\set-api-token.ps1
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\SortView\Collector",
    [string]$ConfigPath = "C:\ProgramData\SortViewCollector\config\collector_config.json",
    [string]$ExePath,
    [int]$CustomerId,
    [int]$BranchId,
    [string]$DataRoot,
    [SecureString]$Token
)

$ErrorActionPreference = "Stop"

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell session -- the secrets folder is Administrators + SYSTEM only."
}

# Where the tenant comes from: the installed config, or (guided setup, before install) all three explicit values.
$explicitTenant = $PSBoundParameters.ContainsKey("CustomerId") -or $PSBoundParameters.ContainsKey("BranchId") -or $PSBoundParameters.ContainsKey("DataRoot")
if ($explicitTenant) {
    if (-not ($PSBoundParameters.ContainsKey("CustomerId") -and $PSBoundParameters.ContainsKey("BranchId") -and $PSBoundParameters.ContainsKey("DataRoot"))) {
        throw "-CustomerId, -BranchId and -DataRoot must be given together."
    }
    $tenantArgs = @("--customer-id", [string]$CustomerId, "--branch-id", [string]$BranchId, "--data-root", $DataRoot)
} else {
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "The config file was not found at '$ConfigPath' -- nothing was stored."
    }
    $tenantArgs = @("--config", $ConfigPath)
}

# Which Collector runs `api-token set`: an explicit exe, the installed frozen runtime, or a source install's venv.
$frozenExe = if ($ExePath) { $ExePath } else { Join-Path $InstallRoot "SortViewCollector.exe" }
$venvPython = Join-Path $InstallRoot ".venv\Scripts\python.exe"
$useFrozen = Test-Path -LiteralPath $frozenExe -PathType Leaf
if (-not $useFrozen -and ($ExePath -or -not (Test-Path -LiteralPath $venvPython -PathType Leaf))) {
    throw "No SortView Collector runtime was found (looked for '$frozenExe'$(if (-not $ExePath) { " and '$venvPython'" })) -- nothing was stored."
}

$tokenFromCaller = $PSBoundParameters.ContainsKey("Token")
$secure = if ($tokenFromCaller) { $Token } else { Read-Host -AsSecureString -Prompt "Paste the SortView Collector API token (input hidden)" }
if ($null -eq $secure -or $secure.Length -eq 0) {
    throw "No token was entered -- nothing was stored."
}

$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

# The token travels ONLY on the child's STDIN. The child prints one fixed line (or a fixed failure code).
$global:LASTEXITCODE = $null
try {
    if ($useFrozen) {
        $plainToken | & $frozenExe api-token set @tenantArgs | Out-Host
    } else {
        # -m collector.api_token_store resolves the `collector` package via the process's working directory.
        Push-Location $InstallRoot
        try {
            $plainToken | & $venvPython -m collector.api_token_store set @tenantArgs | Out-Host
        } finally {
            Pop-Location
        }
    }
    $storeExitCode = $LASTEXITCODE
} finally {
    $plainToken = $null
    [GC]::Collect()
}

if ($storeExitCode -ne 0) {
    throw "The Collector did not store the API token (exit code $storeExitCode) -- see the failure code above."
}
Write-Host "Stored the API token in api_token.dpapi (DPAPI, Administrators + SYSTEM only; value not shown)." -ForegroundColor Green
